"""HTDemucs stem-separation capability, registered as the 'htdemucs' plugin.

Encapsulates the full separation path behind the uniform plugin contract:
device detection, the PyTorch worker child process (with its JSONL
progress/log protocol), the demucs CLI fallback, and placeholder substitution
for missing stems. The pipeline only supplies a PluginContext (progress span,
cancellation probe, log sink) and receives a stem file map back.

The worker spawn keeps torch out of the server's import/thread model: the
PyTorch separator (and its OpenMP pool) lives entirely inside the child
process, which is what makes multi-threaded CPU inference safe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from adapters.demucs_adapter import DemucsAdapter
from adapters.docker_runtime import DockerRuntime
from adapters.ffmpeg_adapter import FFmpegAdapter
from adapters.process_utils import ExternalToolError
from plugins.base import AudioPlugin, CancelledError, PluginContext
from services.cancellation_service import cancellation_service
from services.device_service import DeviceService
from utils.paths import JOBS_DIR

# Entry point for the PyTorch separator child process.
WORKER_PATH = Path(__file__).resolve().parents[1] / "adapters" / "demucs_worker.py"

SEPARATING_STAGE = "separating"


class HTDemucsPlugin(AudioPlugin):
    name = "htdemucs"
    description = "AI stem separation into vocals / drums / bass / guitar / piano / other"

    def __init__(self) -> None:
        self.demucs = DemucsAdapter()
        self.ffmpeg = FFmpegAdapter()
        self.device_service = DeviceService()
        self.docker = DockerRuntime()
        self.container_runs = 0
        self.last_container_stats: dict | None = None
        # Warm-pool live state (see WarmPoolState in domain/models.py): the
        # persistent container generation, its one-time startup, and the jobs
        # it has served — surfaced in /api/diagnostics for the bridge. The
        # cumulative served count and last-job stats persist across server
        # restarts (stats.json in the pool dir), so diagnostics keeps history
        # instead of resetting per process; ready_sec/started_at describe the
        # current container generation only.
        self.pool_busy = False
        self.pool_jobs_served = 0
        self.pool_ready_sec: float | None = None
        self.pool_started_at: str | None = None
        self.pool_last_stats: dict | None = None
        self.pool_last_activity_at: str | None = None
        self.pool_first_seen_at: str | None = None
        self._load_pool_stats()
        self._pool_heartbeat_thread: threading.Thread | None = None

    def run(self, ctx: PluginContext) -> dict[str, str]:
        """Separate `params['input_path']` into `params['stems']`, returning
        {stem_name: wav_path}. Raises CancelledError when cancelled."""
        input_path = str(ctx.params["input_path"])
        selected_stems = list(ctx.params.get("stems") or [])
        final_dir = ctx.work_dir / "stems"
        raw_dir = ctx.work_dir / "stems_raw"
        progress_path = ctx.work_dir / "logs" / "demucs_progress.jsonl"
        final_dir.mkdir(parents=True, exist_ok=True)

        # Prefer the ROCm container (real GPU for gfx803) when configured and
        # available; otherwise the local CPU worker. The device string is the
        # worker's target, so the container path always says cuda.
        container_ok = False
        container_reason = ""
        if self.docker.enabled:
            container_ok, container_reason = self.docker.available()
        if container_ok:
            device, threads = "cuda", 1
            # The host CLI fallback has no CUDA; it must always run on CPU.
            fallback_device = "cpu"
            ctx.log(f"Separating stems with HTDemucs via ROCm container: {container_reason}")
        else:
            detected = self.device_service.detect()
            device = detected.get("device", "cpu")
            fallback_device = device
            threads = detected.get("threads", 1)
            ctx.log(f"Separating stems with HTDemucs (PyTorch, device={device}, threads={threads})")
            if self.docker.enabled:
                ctx.log(f"Container backend unavailable: {container_reason}")

        # NOTE: CancelledError must propagate — it is NOT a separator failure.
        try:
            self._separate_via_worker(ctx, input_path, final_dir, device, threads, progress_path, container=container_ok)
        except CancelledError:
            raise
        except Exception as exc:
            if ctx.is_cancelled():
                # A cancel requested while the worker path failed must not
                # start a fallback that ignores the cancellation for many
                # minutes — surface the cancellation immediately.
                raise CancelledError() from exc
            ctx.log(f"PyTorch separator failed, falling back to Demucs CLI: {exc}")
            raw_stems: dict[str, str] = {}
            try:
                raw_stems = self.demucs.separate(
                    input_path, str(raw_dir),
                    job_id=ctx.job_id, log_path=ctx.log_path, device=fallback_device,
                )
            except ExternalToolError as fallback_exc:
                ctx.log(f"Demucs unavailable or failed, using placeholder fallback: {fallback_exc}")
                raw_stems = {}
            return self._finalize(ctx, input_path, final_dir, selected_stems, raw_stems)

        return self._finalize(ctx, input_path, final_dir, selected_stems, {})

    def _finalize(
        self,
        ctx: PluginContext,
        input_path: str,
        final_dir: Path,
        selected_stems: list[str],
        raw_stems: dict[str, str],
    ) -> dict[str, str]:
        outputs: dict[str, str] = {}
        for stem in selected_stems:
            target = final_dir / f"{stem}.wav"
            if stem in raw_stems:
                self.ffmpeg.copy_audio(raw_stems[stem], str(target))
            elif not target.exists():
                # Should not happen (the separator writes all 6 stems), but an
                # empty path would poison the manifest and crash the ffmpeg
                # mixdown later. Degrade to the original instead.
                ctx.log(f"Missing stem {stem} after separation, substituting original")
                self.ffmpeg.copy_audio(input_path, str(target))
            outputs[stem] = str(target)
        return outputs

    def _separate_via_worker(
        self,
        ctx: PluginContext,
        input_path: str,
        final_dir: Path,
        device: str,
        threads: int,
        progress_path: Path,
        container: bool = False,
    ) -> None:
        """Run the PyTorch separator in a child process, tailing its progress.

        The worker writes JSONL progress/log lines to `progress_path`; this
        method waits on the process and mirrors those lines into the context's
        log and progress. Cancellation terminates the worker process (via
        CancellationService), which is what actually interrupts a separation
        that can otherwise run for many minutes.
        """
        if container and ctx.job_id:
            # Warm pool: the model stays loaded across jobs (see
            # demucs_pool_server.py); dispatch the job, tail the same JSONL
            # protocol. Startup is paid once per container generation.
            self._separate_via_pool(ctx, input_path, final_dir, progress_path)
            return
        if progress_path.exists():
            try:
                progress_path.unlink()
            except OSError:
                pass
        command = [
            sys.executable, str(WORKER_PATH),
            str(input_path), str(final_dir), device, str(threads), str(progress_path),
            str(os.getpid()),  # worker exits if this server dies mid-job
        ]
        wall_start = time.monotonic()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cancellation_service.register_process(ctx.job_id, process)
        seen = 0
        try:
            while True:
                try:
                    process.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if ctx.is_cancelled():
                        self._terminate_process(process, ctx.job_id)
                        raise CancelledError()
                    seen = self._tail_worker_progress(ctx, progress_path, seen)
            seen = self._tail_worker_progress(ctx, progress_path, seen)
            if ctx.is_cancelled():
                # Cancel raced with the worker exiting: request_cancel() may
                # have SIGTERMed the worker between our 1s poll checks, so the
                # wait() above returned because the process DIED, not via
                # timeout — and the non-zero exit must not be mistaken for a
                # separator failure (that would start the long CLI fallback
                # with no cancel check inside it).
                raise CancelledError()
            output = process.stdout.read() if process.stdout else ""
            if output.strip():
                ctx.log(output)
            if process.returncode != 0:
                raise ExternalToolError(f"HTDemucs worker failed (exit {process.returncode})")
            if container:
                # Success in container mode: record the measured fixed
                # overhead for /api/diagnostics (see runtime_info).
                self._record_container_stats(progress_path, time.monotonic() - wall_start)
        except Exception:
            if process.poll() is None:
                self._terminate_process(process, ctx.job_id)
            raise
        finally:
            cancellation_service.unregister_process(ctx.job_id, process)

    def _separate_via_pool(
        self,
        ctx: PluginContext,
        input_path: str,
        final_dir: Path,
        progress_path: Path,
    ) -> None:
        """Dispatch one separation to the warm-pool container and tail it.

        The pool supervisor (adapters/demucs_pool_server.py) keeps a loaded
        separator alive and serves jobs from a request file; this method
        writes the spec, then waits on the same JSONL protocol the local
        worker uses. Cancel touches a marker file the supervisor notices
        between chunks (GPU chunks are sub-second) — the pool itself stays
        warm. If the pool can't start or dies mid-job, ExternalToolError is
        raised so the caller's CLI fallback runs (same semantics as a dead
        per-job container).
        """
        pool_dir = JOBS_DIR / "_pool"
        if not self._ensure_pool(ctx, pool_dir):
            raise ExternalToolError("warm pool unavailable — falling back")
        cancel_path = pool_dir / f"cancel_{ctx.job_id}"
        try:
            cancel_path.unlink()
        except OSError:
            pass
        if progress_path.exists():
            try:
                progress_path.unlink()
            except OSError:
                pass
        request_path = pool_dir / "request.json"
        spec = {
            "job_id": ctx.job_id,
            "input": str(input_path),
            "out_dir": str(final_dir),
            "progress_path": str(progress_path),
        }
        tmp = request_path.with_name("request.json.tmp")
        try:
            tmp.write_text(json.dumps(spec), encoding="utf-8")
            os.replace(tmp, request_path)
        except OSError as exc:
            raise ExternalToolError(f"cannot dispatch to warm pool: {exc}") from exc

        wall_start = time.monotonic()
        seen = 0
        dead_checks = 0
        terminal = None
        self.pool_busy = True
        try:
            while True:
                seen = self._tail_worker_progress(ctx, progress_path, seen)
                terminal = self._read_terminal_status(progress_path)
                if terminal in ("done", "cancelled"):
                    break
                if terminal == "error":
                    raise ExternalToolError("warm pool worker reported failure")
                if ctx.is_cancelled():
                    try:
                        cancel_path.touch()
                    except OSError:
                        pass
                # Throttled docker probe (~2s): the container vanishing
                # without a terminal marker means the pool crashed mid-job.
                dead_checks += 1
                if dead_checks % 8 == 0 and not self.docker.pool_alive():
                    raise ExternalToolError("warm pool container died during separation")
                time.sleep(0.25)
        finally:
            self.pool_busy = False
            try:
                cancel_path.unlink()
            except OSError:
                pass
        stats = self._parse_worker_stats(progress_path, time.monotonic() - wall_start)
        if stats:
            self.pool_jobs_served += 1
            self.pool_last_stats = stats
            self.pool_last_activity_at = stats["at"]
            if self.pool_first_seen_at is None:
                self.pool_first_seen_at = stats["at"]
            self._persist_pool_stats()
        if terminal == "cancelled" or ctx.is_cancelled():
            # Cancel won (or raced the supervisor's own cancel marker).
            raise CancelledError()

    def _ensure_pool(self, ctx: PluginContext, pool_dir: Path) -> bool:
        """Reuse the running pool, or start one and wait until it is ready."""
        pool_dir.mkdir(parents=True, exist_ok=True)
        self._start_pool_heartbeat(pool_dir)
        if self.docker.pool_alive():
            return True
        ctx.log("Starting warm pool container (first job pays the model load)...")
        self.docker.kill_pool()
        # Let a dying container fully release the name before we reuse it.
        for _ in range(20):
            if not self.docker.pool_alive():
                break
            time.sleep(0.25)
        try:
            start = subprocess.run(
                self.docker.pool_command(str(pool_dir)),
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            ctx.log(f"Warm pool start failed: {exc}")
            return False
        if start.returncode != 0:
            ctx.log(f"Warm pool start failed: {(start.stderr or start.stdout).strip()}")
            return False
        ready_path = pool_dir / "ready"
        try:
            ready_path.unlink()  # freshness: ignore a previous generation's marker
        except OSError:
            pass
        deadline = time.monotonic() + 150  # model load + HIP init + warmup
        while time.monotonic() < deadline:
            if ready_path.exists():
                text = ready_path.read_text(encoding="utf-8").strip()
                ctx.log(f"Warm pool ready: {text}")
                # Record the one-time startup this container generation paid
                # (the ready file is '<device> ready=<secs>s') for diagnostics.
                match = re.search(r"ready=([\d.]+)s", text)
                if match:
                    self.pool_ready_sec = float(match.group(1))
                    self.pool_started_at = datetime.now(timezone.utc).isoformat()
                # Fresh generation: clear the previous one's eviction marker
                # so warm_pool.eviction reflects THIS container's lifecycle.
                try:
                    (pool_dir / "evicted").unlink()
                except OSError:
                    pass
                return True
            if not self.docker.pool_alive():
                ctx.log("Warm pool container exited during startup")
                return False
            time.sleep(0.5)
        ctx.log("Warm pool did not become ready in time")
        return False

    def _start_pool_heartbeat(self, pool_dir: Path) -> None:
        """Daemon thread keeping the pool's heartbeat fresh while this server lives.

        The supervisor treats a stale heartbeat as a dead server and shuts
        itself down (the container is --rm'd), so this thread is also the
        watchdog: it dies with the server process, and the pool cleans up.
        """
        if self._pool_heartbeat_thread is not None and self._pool_heartbeat_thread.is_alive():
            return

        def beat() -> None:
            while True:
                try:
                    pool_dir.mkdir(parents=True, exist_ok=True)
                    (pool_dir / "heartbeat").write_text(str(time.time()), encoding="utf-8")
                except OSError:
                    pass
                time.sleep(5)

        thread = threading.Thread(target=beat, name="audiomass-pool-heartbeat", daemon=True)
        thread.start()
        self._pool_heartbeat_thread = thread

    @staticmethod
    def _read_terminal_status(progress_path: Path) -> str | None:
        """Last 'status' marker in the worker's progress file, if any."""
        try:
            lines = progress_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        terminal = None
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            status = entry.get("status")
            if status in ("done", "error", "cancelled"):
                terminal = status
        return terminal

    def _parse_worker_stats(self, progress_path: Path, wall_sec: float) -> dict | None:
        """Parse the worker's JSONL for Ready/Done timings into a stats dict.

        The worker's 'Done in' is measured from separate()'s own clock, so it
        is exactly the chunk inference + stem-write time — the part that
        scales with track length. 'Ready in' (model load + HIP init + warmup
        forward, from __init__'s clock) and everything else (python/torch
        startup, teardown) is the fixed per-job overhead, which is what
        wall_sec - compute_sec captures. Returns None when the job produced no
        'Done in' marker (e.g. a cancelled or failed job) — nothing to report.
        """
        ready_sec = done_sec = audio_sec = realtime = None
        try:
            lines = progress_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            text = entry.get("log", "")
            match = re.search(r"Ready in ([\d.]+)s", text)
            if match:
                ready_sec = float(match.group(1))
            match = re.search(r"Done in ([\d.]+)s \(audio: ([\d.]+)s, ([\d.]+)x realtime\)", text)
            if match:
                done_sec, audio_sec, realtime = (
                    float(match.group(1)), float(match.group(2)), float(match.group(3)),
                )
        if done_sec is None:
            return None
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "image": self.docker.image,
            "wall_sec": round(wall_sec, 1),
            "ready_sec": round(ready_sec or 0.0, 1),
            "compute_sec": round(max(0.0, done_sec), 1),
            "overhead_sec": round(wall_sec - done_sec, 1),
            "audio_sec": round(audio_sec or 0.0, 1),
            "realtime": round(realtime or 0.0, 3),
        }

    def _record_container_stats(self, progress_path: Path, wall_sec: float) -> None:
        """Store per-job stats for the plain per-job container path."""
        stats = self._parse_worker_stats(progress_path, wall_sec)
        if stats is None:
            return
        self.container_runs += 1
        self.last_container_stats = stats

    def _load_pool_stats(self) -> None:
        """Restore persisted warm-pool history from the pool dir.

        stats.json lives under JOBS_DIR (bind-mounted at its host path, so it
        survives server restarts and container generations). Restoring on
        boot means a restarted server keeps reporting how many jobs the pool
        has served overall and the last job's measured stats, instead of
        forgetting everything.
        """
        try:
            data = json.loads((JOBS_DIR / "_pool" / "stats.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        # The file is trusted-ish (we wrote it), but boot must survive a
        # hand-edited or foreign file with the wrong shapes — a crash here
        # would take down the whole server on startup.
        try:
            self.pool_jobs_served = int(data.get("jobs_served_total", 0) or 0)
        except (TypeError, ValueError):
            self.pool_jobs_served = 0
        last_job = data.get("last_job")
        self.pool_last_stats = last_job if isinstance(last_job, dict) else None
        self.pool_first_seen_at = data.get("first_seen_at")
        self.pool_last_activity_at = data.get("last_activity_at")

    def _persist_pool_stats(self) -> None:
        """Write the cumulative pool history after a successful pool job.

        Best-effort and atomic (tmp + rename): if the write fails the
        in-memory counters still work for this process; jobs are serialized
        by the server (one active job at a time), so there is no write race.
        """
        try:
            pool_dir = JOBS_DIR / "_pool"
            pool_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "jobs_served_total": self.pool_jobs_served,
                "last_job": self.pool_last_stats,
                "first_seen_at": self.pool_first_seen_at,
                "last_activity_at": self.pool_last_activity_at,
            }
            tmp = pool_dir / "stats.json.tmp"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, pool_dir / "stats.json")
        except OSError:
            pass  # best-effort persistence; in-memory state still works

    def _pool_state(self, container_ok: bool) -> dict:
        """Live warm-pool state for /api/diagnostics (see WarmPoolState).

        up probes the container only when the container backend is actually in
        use (a cpu_worker setup has no pool by definition); the rest is state
        tracked by this plugin across jobs. eviction is read live from the
        supervisor's marker file: the reason the last container generation
        exited ('idle' / 'stale_heartbeat' / 'shutdown'), until a new
        generation clears it on startup. Probe cost is one docker inspect per
        diagnostics call — the bridge calls it at startup and after each
        terminal job, so this stays off any hot path.
        """
        eviction = None
        evicted_at = None
        try:
            marker = JOBS_DIR / "_pool" / "evicted"
            if marker.exists():
                eviction = marker.read_text(encoding="utf-8").strip() or None
                evicted_at = datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            pass
        return {
            "up": self.docker.pool_alive() if container_ok else False,
            "busy": self.pool_busy,
            "jobs_served": self.pool_jobs_served,
            "ready_sec": self.pool_ready_sec,
            "started_at": self.pool_started_at,
            # The configured idle eviction window (seconds); None without the
            # container backend, where no pool exists to evict.
            "idle_timeout_sec": float(os.environ.get("AUDIOMASS_POOL_IDLE_TIMEOUT", "600")) if container_ok else None,
            # Persisted history: first-ever pool job, last completion time,
            # and the most recent job's stats — survive server restarts.
            "first_seen_at": self.pool_first_seen_at,
            "last_activity_at": self.pool_last_activity_at,
            "eviction": eviction,
            "evicted_at": evicted_at,
            "last_job": self.pool_last_stats,
        }

    def runtime_info(self) -> dict:
        """Which separation engine the next job uses, for /api/diagnostics."""
        container_ok, reason = False, ""
        if self.docker.enabled:
            container_ok, reason = self.docker.available()
        if container_ok:
            return {
                "backend": "rocm_container",
                "device": "cuda",
                "image": self.docker.image,
                "container_available": True,
                "detail": reason,
                "last_job": self.last_container_stats,
                "warm_pool": self._pool_state(container_ok),
            }
        detected = self.device_service.detect()
        return {
            "backend": "cpu_worker",
            "device": detected.get("device", "cpu"),
            "image": self.docker.image if self.docker.enabled else None,
            "container_available": False,
            "detail": reason or "local CPU worker",
            "last_job": self.last_container_stats,
            "warm_pool": self._pool_state(container_ok),
        }

    def _tail_worker_progress(self, ctx: PluginContext, progress_path: Path, seen: int) -> int:
        """Mirror new worker log lines and progress updates into the context."""
        if not progress_path.exists():
            return seen
        try:
            lines = progress_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return seen
        for line in lines[seen:]:
            try:
                entry = json.loads(line)
            except ValueError:
                # The worker may be mid-write of the final line (no trailing
                # newline yet). Do NOT consume it — retry on the next poll;
                # once the process has exited every line is complete.
                break
            if "log" in entry:
                ctx.log(entry["log"])
            elif "done" in entry and "total" in entry:
                self._report_progress(ctx, entry["done"], entry["total"])
            elif entry.get("status") == "error":
                # Mirror the worker's failure detail so it survives even when
                # the worker's exit code is all the parent eventually sees.
                ctx.log(f"HTDemucs worker error: {entry.get('error')}")
            seen += 1
        return seen

    def _report_progress(self, ctx: PluginContext, done: int, total: int) -> None:
        """Report chunk progress — also a cancellation checkpoint, so a cancel
        that raced in between the worker-wait polls aborts here."""
        ctx.check_cancelled()
        if ctx.progress:
            ctx.progress(
                done / max(total, 1),
                stage=SEPARATING_STAGE,
                message=f"Separating stems ({done}/{total} chunks)",
            )

    def _terminate_process(self, process: subprocess.Popen, job_id: str | None = None) -> None:
        if job_id:
            # Killing the `docker run` CLI does NOT stop the container — the
            # worker would keep burning the GPU until the job finishes. Kill
            # it by name first, then reap the CLI.
            self.docker.kill_container(job_id)
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


# Self-registration on import (see plugins/__init__.py).
from plugins.registry import plugin_registry  # noqa: E402

plugin_registry.register(HTDemucsPlugin())
