from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from services.cancellation_service import cancellation_service


class ExternalToolError(RuntimeError):
    pass


def find_command_path(command: str) -> str | None:
    # First, check PATH (system-installed tools).
    found = shutil.which(command)
    if found:
        return found

    # Fallback: tools installed in the venv's `bin/` directory. When the
    # backend runs under a virtualenv, uvicorn doesn't put .venv/bin on
    # PATH, and sys.executable may resolve through a symlink to the
    # system Python (so its parent dir is /usr/bin, not the venv).
    # sys.prefix is the reliable signal: it always points at the venv
    # root while the venv is active.
    venv_bin = Path(sys.prefix) / 'bin'
    candidate = venv_bin / command
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)

    return None


def require_command(command: str) -> str:
    path = find_command_path(command)
    if not path:
        raise ExternalToolError(f"Required command not found: {command}")
    return path


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(message.rstrip() + '\n')


def run_command(command: Sequence[str], *, job_id: str, log_path: Path, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    append_log(log_path, '$ ' + ' '.join(command))
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    cancellation_service.register_process(job_id, process)
    try:
        stdout, _ = process.communicate()
    finally:
        cancellation_service.unregister_process(job_id, process)
    output = stdout or ''
    if output:
        append_log(log_path, output)
    if process.returncode != 0:
        raise ExternalToolError(f"Command failed ({process.returncode}): {' '.join(command)}")
    return subprocess.CompletedProcess(command, process.returncode, output, None)
