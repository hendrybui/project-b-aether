#!/usr/bin/env python3
"""HTDemucs separation worker — runs the PyTorch separator in a child process.

Why a process instead of the server's pipeline thread? torch's OpenMP thread
pool is only safe to create in a process's main thread; in the server's
daemon threads it can crash the whole server, which is why the old code was
pinned to `torch.set_num_threads(1)`. A child process gets the full speed of
multi-threaded CPU inference (or a real GPU device) with zero crash risk to
the server.

Protocol with the parent (all via a JSONL progress file, one JSON object per
line, flushed after each write):

  {"log": "..."}            -> line appended to the job's pipeline.log
  {"done": n, "total": m}   -> separation progress update (n/m chunks)
  {"status": "done"}        -> success marker (written last)
  {"status": "error", "error": "..."} -> failure detail

All `audiomass.*` logging (the separator's per-chunk lines included) is
routed through a handler that emits {"log": ...} entries, so the parent
mirrors it into the job's pipeline.log live. The worker writes nothing to
stdout/stderr during the run (the parent reads them only after exit), so the
pipe can never fill up and deadlock a long separation. Exit code 0 = success,
1 = failure.

Usage: python demucs_worker.py <input_wav> <out_dir> <device> <threads> <progress_path> [parent_pid]
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path


def main() -> int:
    input_path, out_dir, device, threads, progress_path = sys.argv[1:6]
    parent_pid = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    # Main thread of this process — safe for OpenMP multi-threading.
    import torch
    torch.set_num_threads(max(1, int(threads)))

    # Make `adapters.*` importable when launched from anywhere.
    backend_dir = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend_dir))

    from adapters.optimized_demucs import OptimizedDemucs

    def emit(entry: dict) -> None:
        with open(progress_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()

    class JsonlLogHandler(logging.Handler):
        """Stream `audiomass.*` log lines to the parent as {"log": ...}."""

        def emit(self, record: logging.LogRecord) -> None:
            try:
                emit({"log": record.getMessage()})
            except Exception:
                pass

    # Route the separator's logger (audiomass.demucs) into the JSONL
    # protocol so its per-chunk lines reach pipeline.log in real time instead
    # of only being dumped when the parent reads stdout after exit.
    _handler = JsonlLogHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _audiomass_log = logging.getLogger("audiomass")
    _audiomass_log.addHandler(_handler)
    _audiomass_log.setLevel(logging.INFO)

    def parent_alive() -> bool:
        # Once the server process dies, this child is reparented (usually to
        # init/pid 1), so a changed getppid() means stop burning CPU.
        return parent_pid == 0 or os.getppid() == parent_pid

    def on_progress(done: int, total: int) -> None:
        if not parent_alive():
            raise SystemExit(1)
        emit({"done": done, "total": total})

    try:
        separator = OptimizedDemucs(device=device)
        separator.separate(
            input_path,
            out_dir,
            progress_callback=on_progress,
        )
        emit({"status": "done"})
        # Skip interpreter teardown: on the ROCm container path, HIP context
        # destruction on this Polaris card can take ~35s after the work is
        # already finished. All protocol output (progress JSONL, logs) is
        # flushed per-emit and stems are fully written before this marker,
        # so there is nothing left to flush or clean up. os._exit never
        # returns.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
        return 0  # pragma: no cover
    except SystemExit:
        # Server went away; exit quietly, no error marker needed.
        return 1
    except Exception as exc:  # noqa: BLE001
        try:
            emit({"status": "error", "error": str(exc)})
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
