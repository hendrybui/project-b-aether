from __future__ import annotations

import subprocess
from threading import Lock


class CancellationService:
    """Tracks cancellation requests and active subprocesses for jobs."""

    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._processes: dict[str, set[subprocess.Popen[str]]] = {}
        self._lock = Lock()

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)
            processes = list(self._processes.get(job_id, set()))
        for process in processes:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def register_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.setdefault(job_id, set()).add(process)

    def unregister_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if job_id in self._processes:
                self._processes[job_id].discard(process)
                if not self._processes[job_id]:
                    self._processes.pop(job_id, None)

    def clear(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.discard(job_id)
            self._processes.pop(job_id, None)


cancellation_service = CancellationService()
