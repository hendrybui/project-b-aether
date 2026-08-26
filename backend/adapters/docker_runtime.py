"""Optional ROCm container backend for the HTDemucs worker.

Host torch cannot drive Polaris-class GPUs (RX 580 / gfx803): upstream ROCm
dropped gfx803, so ``torch.cuda.is_available()`` is False and the Vulkan
backend ships only in the CPU wheel. This box does have a *container* with a
from-source ROCm 6.4 + PyTorch build that supports gfx803 (see
/mnt/Pandora/Workshop/GFX803_Rocm — image ``rocm64_gfx803_pytorch:2.4``,
verified working with ``HSA_OVERRIDE_GFX_VERSION=8.0.3``,
``ROC_ENABLE_PRE_VEGA=1``, ``HSA_ENABLE_SDMA=0``).

When ``AUDIOMASS_DEMUCS_DOCKER_IMAGE`` is set and the daemon + image are
available, the separation worker runs inside that container with the AMD
device nodes passed through — so long tracks get real GPU speed instead of
20+ CPU minutes. Whenever docker is missing, down, or lacks the image, the
plugin falls back to the existing local CPU worker; the container backend is
a strict improvement, never a requirement.

Mount strategy: the backend source and the jobs root are bind-mounted at
their *host paths* inside the container, so the worker receives identical
paths and needs no translation. The container is named
``audiomass-demucs-<job_id>`` so cancellation can ``docker kill`` it (killing
the ``docker run`` CLI alone would leave the container running).
"""

from __future__ import annotations

import grp
import os
import shutil
import subprocess
import time
from pathlib import Path

from utils.paths import BACKEND_DIR, JOBS_DIR

# gfx803 runtime overrides the ROCm project documents as required.
DEFAULT_ENV = {
    "HSA_OVERRIDE_GFX_VERSION": "8.0.3",
    "ROC_ENABLE_PRE_VEGA": "1",
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_ROCM_ARCH": "gfx803",
    # MIOpen kernel cache lives here (mounted from the host below). Without a
    # persisted cache every ephemeral container recompiles kernels for this
    # Polaris card — measured ~40s per job on top of the separation itself.
    # TORCH_HOME/HF_HOME are pinned to the mounted /opt/cache dirs as well:
    # the worker runs unprivileged (--user), and /root is 0700 root-owned so
    # the default ~/.cache paths would be unwritable.
    "MIOPEN_USER_DB_PATH": "/opt/cache/miopen",
    "TORCH_HOME": "/opt/cache/torch",
    "HF_HOME": "/opt/cache/huggingface",
}

CONTAINER_PREFIX = "audiomass-demucs-"
POOL_CONTAINER = "audiomass-demucs-pool"
_CACHE_TTL_SECONDS = 30


class DockerRuntime:
    """Builds and probes the ``docker run`` invocation for separation.

    Enabled only when ``AUDIOMASS_DEMUCS_DOCKER_IMAGE`` is set (so a stock
    install never depends on docker). ``available()`` checks the daemon and
    the image once per short TTL; everything else is a pure command builder.
    """

    def __init__(self) -> None:
        self.image = os.environ.get("AUDIOMASS_DEMUCS_DOCKER_IMAGE", "").strip()
        self.devices = [
            d.strip()
            for d in os.environ.get("AUDIOMASS_DEMUCS_DOCKER_DEVICES", "/dev/kfd,/dev/dri").split(",")
            if d.strip()
        ]
        self.extra_env: dict[str, str] = {}
        for pair in os.environ.get("AUDIOMASS_DEMUCS_DOCKER_ENV", "").split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.extra_env[k] = v
        self._availability: tuple[float, bool, str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.image)

    def available(self) -> tuple[bool, str]:
        """Is the configured image runnable right now? (cached per TTL)."""
        if not self.enabled:
            return False, "AUDIOMASS_DEMUCS_DOCKER_IMAGE not set — using local CPU worker"
        now = time.monotonic()
        if self._availability and now - self._availability[0] < _CACHE_TTL_SECONDS:
            return self._availability[1], self._availability[2]

        if shutil.which("docker") is None:
            result = (False, "docker CLI not found — using local CPU worker")
        elif self._daemon_up() is False:
            result = (False, "docker daemon not reachable — using local CPU worker")
        elif not self._image_present():
            result = (False, f"image {self.image} not found — using local CPU worker")
        else:
            result = (True, f"running separator in container {self.image} (ROCm GPU)")
        self._availability = (now, result[0], result[1])
        return result

    @staticmethod
    def _daemon_up() -> bool:
        try:
            probe = subprocess.run(
                ["docker", "info"],
                capture_output=True, text=True, timeout=3,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def _image_present(self) -> bool:
        try:
            probe = subprocess.run(
                ["docker", "image", "inspect", self.image],
                capture_output=True, text=True, timeout=5,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def container_name(self, job_id: str) -> str:
        safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
        return f"{CONTAINER_PREFIX}{safe}"

    def _base_flags(self, container_name: str) -> list[str]:
        """Shared ``docker run`` flags: devices, groups, user, env, mounts.

        Numeric host GIDs: the image's /etc/group may map video/render to
        different IDs than the host (e.g. this box: render=992 vs the image's
        109), and the device nodes keep the *host* ownership — a name-based
        --group-add would grant the wrong GID and GPU init would fail with
        "CUDA not available".

        Run as the host user so stems/logs written into the (bind-mounted)
        jobs dir are owned by the user, not root. Cache homes are pinned via
        TORCH_HOME/HF_HOME/MIOPEN_USER_DB_PATH to the mounted /opt/cache
        dirs, so --user never falls back to per-container defaults (which
        would either hit the unwritable root-owned /root or the image's
        /home/ubuntu home).
        """
        cmd = ["docker", "run", "--rm", "--name", container_name]
        for device in self.devices:
            cmd += ["--device", device]
        for group_name in ("video", "render"):
            try:
                gid = grp.getgrnam(group_name).gr_gid
            except KeyError:
                gid = group_name  # let docker resolve by name as a fallback
            cmd += ["--group-add", str(gid)]
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for key, value in {**DEFAULT_ENV, **self.extra_env}.items():
            cmd += ["-e", f"{key}={value}"]
        # Source and job data at host paths inside the container.
        cmd += ["-v", f"{BACKEND_DIR}:{BACKEND_DIR}:ro"]
        cmd += ["-v", f"{JOBS_DIR}:{JOBS_DIR}"]
        # Reuse any host-cached model weights so a fresh container doesn't
        # re-download htdemucs_6s (~80MB) on every job. demucs >= 4.1 pulls
        # weights through HuggingFace Hub, plus a torch.hub checkpoint; the
        # dirs are mounted at the paths TORCH_HOME/HF_HOME point at, and
        # created on the host so the unprivileged container user can write
        # into them (first-run downloads included).
        cache_root = Path.home() / ".cache"
        for sub, target in (("torch", "/opt/cache/torch"), ("huggingface", "/opt/cache/huggingface")):
            host_dir = cache_root / sub
            host_dir.mkdir(parents=True, exist_ok=True)
            cmd += ["-v", f"{host_dir}:{target}"]
        # Persist the MIOpen kernel cache (see DEFAULT_ENV) — without it every
        # job pays ~40s of kernel recompilation.
        miopen_dir = cache_root / "miopen"
        miopen_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{miopen_dir}:/opt/cache/miopen"]
        # demucs >= 4.1 pings HF Hub on every model load even when the
        # weights are cached; once the cache has the model, go offline so
        # each job skips that network round trip. First run (empty cache)
        # still downloads normally.
        hf_model_dir = cache_root / "huggingface" / "hub" / "models--adefossez--HTDemucs-6s"
        if hf_model_dir.is_dir():
            cmd += ["-e", "HF_HUB_OFFLINE=1"]
        return cmd

    def worker_command(self, job_id: str, worker_args: list[str]) -> list[str]:
        """``docker run`` command running demucs_worker.py with identical paths.

        worker_args are the same host paths the local worker would get
        ([input, out_dir, device, threads, progress_path, parent_pid]) —
        valid because BACKEND_DIR and JOBS_DIR are mounted at their host
        paths. parent_pid is passed as 0: inside the container the worker's
        parent is the container's PID 1, not the server, so the ppid watchdog
        cannot apply; the container is killed by name on cancel instead.
        """
        cmd = self._base_flags(self.container_name(job_id))
        cmd += [self.image, "python3", str(BACKEND_DIR / "adapters" / "demucs_worker.py")]
        cmd += [str(arg) for arg in worker_args]
        return cmd

    def pool_command(self, pool_dir: str) -> list[str]:
        """``docker run -d`` starting the warm pool (returns once it is up).

        The supervisor (demucs_pool_server.py) loads the model once and serves
        jobs back-to-back from ``pool_dir``; see its docstring for the
        file-based protocol. The container is detached so no CLI process
        needs tracking — the plugin polls ``pool_alive()`` instead.
        """
        cmd = self._base_flags(POOL_CONTAINER)
        cmd += ["-d"]
        cmd += ["-e", f"AUDIOMASS_POOL_DIR={pool_dir}"]
        cmd += ["-e", "AUDIOMASS_POOL_DEVICE=cuda"]
        cmd += ["-e", "AUDIOMASS_POOL_THREADS=1"]
        cmd += ["-e", "AUDIOMASS_POOL_HEARTBEAT_TIMEOUT=20"]
        # Idle eviction: the GPU is released after this many seconds without a
        # job (the supervisor exits and the --rm container is removed).
        cmd += ["-e", f"AUDIOMASS_POOL_IDLE_TIMEOUT={os.environ.get('AUDIOMASS_POOL_IDLE_TIMEOUT', '600')}"]
        cmd += [self.image, "python3", str(BACKEND_DIR / "adapters" / "demucs_pool_server.py")]
        return cmd

    def pool_alive(self) -> bool:
        """Is the warm pool container running right now?"""
        try:
            probe = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", POOL_CONTAINER],
                capture_output=True, text=True, timeout=5,
            )
            return probe.returncode == 0 and probe.stdout.strip() == "true"
        except Exception:
            return False

    def kill_pool(self) -> None:
        """Force-stop the warm pool (restart / shutdown cleanup)."""
        try:
            subprocess.run(["docker", "kill", POOL_CONTAINER], capture_output=True, timeout=10)
        except Exception:
            pass

    def kill_container(self, job_id: str) -> None:
        """Force-stop the job's container (cancellation / failure cleanup)."""
        name = self.container_name(job_id)
        try:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
        except Exception:
            pass
