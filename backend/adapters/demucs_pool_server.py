#!/usr/bin/env python3
"""Warm-pool supervisor for the HTDemucs worker (GPU startup paid once).

The per-job cost of the plain worker (demucs_worker.py) is dominated by
process startup: python + torch import, HIP init, model load and the warmup
forward — measured ~35s on this box's gfx803 card. Every ephemeral container
pays it again. This supervisor keeps a fully-loaded separator alive inside
the container and serves jobs back-to-back, so the startup is paid once per
container generation instead of once per job.

Protocol (all under AUDIOMASS_POOL_DIR, which the server bind-mounts at its
host path):

  ready           written once the model is loaded; the server waits for it
  heartbeat       touched by the server every few seconds; when it goes stale
                  the server is gone, so the supervisor aborts the current
                  job and exits (the container is --rm'd). Also honoured
                  between jobs.
  shutdown        explicit stop marker (server graceful shutdown)
  request.json    server -> supervisor job spec, written atomically:
                  {"job_id", "input", "out_dir", "progress_path"}
  cancel_<job_id> server touches this to cancel the in-flight job; the
                  supervisor notices between chunks, writes a "cancelled"
                  status to the job's progress file, and keeps serving.

Per-job progress uses the exact same JSONL protocol as demucs_worker.py
({"log": ...}, {"done": n, "total": m}, {"status": "done"|"error"|"cancelled"}),
so the plugin's existing tailing code is reused unchanged.

Run (inside the container): python3 demucs_pool_server.py
Env:  AUDIOMASS_POOL_DIR (required), AUDIOMASS_POOL_DEVICE (default cuda),
      AUDIOMASS_POOL_THREADS (default 1), AUDIOMASS_POOL_HEARTBEAT_TIMEOUT (default 20s),
      AUDIOMASS_POOL_IDLE_TIMEOUT (default 600s; exit after this long with no jobs)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path


class _JobCancelled(Exception):
    """The current job was cancelled — the pool stays warm."""


class _PoolShutdown(Exception):
    """The server is gone or asked us to stop — exit the supervisor."""


def clear_stale_markers(pool_dir: Path) -> None:
    """Remove leftover generation markers from a previous pool container.

    ``ready``/``evicted`` are written by an earlier supervisor run; a stale
    ``shutdown`` marker is written by the server's graceful shutdown handler
    into the persistent pool dir and would otherwise abort this generation's
    model load before it starts, silently falling every later separation back
    to the local CPU worker. A marker from an earlier generation must never be
    mistaken for this one's state.
    """
    for name in ("ready", "evicted", "shutdown"):
        try:
            (pool_dir / name).unlink()
        except OSError:
            pass


def main() -> int:
    pool_dir = Path(os.environ.get("AUDIOMASS_POOL_DIR") or "")
    if not pool_dir.is_absolute():
        print("AUDIOMASS_POOL_DIR must be set to an absolute path", file=sys.stderr)
        return 1
    pool_dir.mkdir(parents=True, exist_ok=True)
    device = os.environ.get("AUDIOMASS_POOL_DEVICE", "cuda")
    threads = int(os.environ.get("AUDIOMASS_POOL_THREADS", "1"))
    heartbeat_timeout = float(os.environ.get("AUDIOMASS_POOL_HEARTBEAT_TIMEOUT", "20"))
    # Idle eviction: exit (and release the GPU) after this many seconds with
    # no job dispatched. Counted from the last job completion, so a long job
    # can't be cut short — only the idle time between jobs matters.
    idle_timeout = float(os.environ.get("AUDIOMASS_POOL_IDLE_TIMEOUT", "600"))
    pool_log = pool_dir / "pool.log"
    ready_path = pool_dir / "ready"
    heartbeat_path = pool_dir / "heartbeat"
    shutdown_path = pool_dir / "shutdown"
    evicted_path = pool_dir / "evicted"

    def log(line: str) -> None:
        try:
            with open(pool_log, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError:
            pass
        print(line, flush=True)

    def heartbeat_stale() -> bool:
        try:
            mtime = heartbeat_path.stat().st_mtime
        except OSError:
            return True
        return time.time() - mtime > heartbeat_timeout

    def mark_evicted(reason: str) -> None:
        """Record why this container generation ended (the plugin surfaces it
        in /api/diagnostics warm_pool.eviction until the next generation)."""
        try:
            evicted_path.write_text(reason, encoding="utf-8")
        except OSError:
            pass

    # The model is loaded in this process's main thread (torch OpenMP pool),
    # which is the same safety property the plain worker relies on.
    import torch
    torch.set_num_threads(max(1, threads))
    backend_dir = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend_dir))

    from adapters.optimized_demucs import OptimizedDemucs

    # Route `audiomass.*` logs to the current job's progress file (the JSONL
    # protocol), or to pool.log between jobs.
    current_progress: dict[str, str] = {"path": str(pool_log)}

    class PoolLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                with open(current_progress["path"], "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"log": record.getMessage()}) + "\n")
                    handle.flush()
            except Exception:
                pass

    _handler = PoolLogHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _audiomass_log = logging.getLogger("audiomass")
    _audiomass_log.addHandler(_handler)
    _audiomass_log.setLevel(logging.INFO)

    # Fresh markers: leftovers from a previous container generation (ready /
    # evicted / shutdown) must not be mistaken for this one's state — in
    # particular a stale `shutdown` marker must not abort this generation's
    # model load (see clear_stale_markers).
    clear_stale_markers(pool_dir)

    # The one-time model load (~35-45s) blocks, so run it on a thread and poll
    # the shutdown/stale markers meanwhile — a server shutdown during warmup
    # must not leave the container burning CPU until the load happens to end.
    boot_start = time.time()
    separator: "OptimizedDemucs | None" = None
    load_errors: list[BaseException] = []

    def _load() -> None:
        try:
            nonlocal separator
            separator = OptimizedDemucs(device=device)
        except BaseException as exc:  # noqa: BLE001
            load_errors.append(exc)

    loader = threading.Thread(target=_load, daemon=True)
    loader.start()
    while loader.is_alive():
        if shutdown_path.exists():
            mark_evicted("shutdown")
            log("Warm pool shutting down (shutdown marker during model load).")
            return 0
        if heartbeat_stale():
            mark_evicted("stale_heartbeat")
            log("Warm pool shutting down (stale heartbeat during model load).")
            return 0
        time.sleep(0.25)
    if load_errors:
        raise load_errors[0]
    assert separator is not None
    boot_secs = time.time() - boot_start
    log(f"Warm pool ready in {boot_secs:.1f}s (device={device}).")
    try:
        ready_path.write_text(f"{device} ready={boot_secs:.1f}s\n")
    except OSError:
        pass

    request_path = pool_dir / "request.json"
    last_job: str | None = None
    last_activity = time.time()
    try:
        while True:
            if shutdown_path.exists():
                mark_evicted("shutdown")
                log("Warm pool shutting down (shutdown marker).")
                break
            if heartbeat_stale():
                mark_evicted("stale_heartbeat")
                log("Warm pool shutting down (stale heartbeat).")
                break
            if time.time() - last_activity > idle_timeout:
                mark_evicted("idle")
                log(
                    f"Warm pool idle eviction: no jobs for "
                    f"{time.time() - last_activity:.0f}s (timeout {idle_timeout:.0f}s)."
                )
                break
            try:
                spec = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.25)
                continue
            job_id = spec.get("job_id")
            if not job_id or job_id == last_job:
                time.sleep(0.25)
                continue
            last_job = job_id
            try:
                request_path.unlink()
            except OSError:
                pass
            try:
                _run_job(separator, spec, pool_dir, current_progress, heartbeat_stale)
            except _PoolShutdown:
                break
            last_activity = time.time()
    finally:
        try:
            ready_path.unlink()
        except OSError:
            pass
    return 0


def _run_job(
    separator,
    spec: dict,
    pool_dir: Path,
    current_progress: dict[str, str],
    heartbeat_stale,
) -> None:
    """Run one separation through the shared separator; never returns on shutdown."""
    job_id = spec["job_id"]
    progress_path = Path(spec["progress_path"])
    cancel_path = pool_dir / f"cancel_{job_id}"
    current_progress["path"] = str(progress_path)
    try:
        progress_path.unlink()
    except OSError:
        pass
    try:
        cancel_path.unlink()
    except OSError:
        pass

    def emit(entry: dict) -> None:
        with open(progress_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()

    def check_stop() -> None:
        if cancel_path.exists():
            raise _JobCancelled()
        if (pool_dir / "shutdown").exists() or heartbeat_stale():
            raise _PoolShutdown()

    def on_progress(done: int, total: int) -> None:
        emit({"done": done, "total": total})
        check_stop()

    try:
        separator.separate(spec["input"], spec["out_dir"], progress_callback=on_progress)
        emit({"status": "done"})
    except _JobCancelled:
        # User cancel: mark the job, keep the pool warm.
        emit({"status": "cancelled"})
    except _PoolShutdown:
        emit({"status": "error", "error": "pool shutting down"})
        raise
    except Exception as exc:  # noqa: BLE001
        emit({"status": "error", "error": str(exc)})
    finally:
        try:
            cancel_path.unlink()
        except OSError:
            pass
        current_progress["path"] = str(pool_dir / "pool.log")


if __name__ == "__main__":
    sys.exit(main())
