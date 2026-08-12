"""Stems worker for DJ Toolkit — demucs separation + instrumental build.

This is the module ``app._run_stems_job`` imports. It runs demucs as a CLI
subprocess (separate process), so the classic OpenMP x daemon-thread crash
can never take the Flask web server down — see config.py for the rationale.

Contract (used by app.py):
    separate(in_path, out_dir, model_id, progress) -> dict[str, Path]
    build_instrumental(stems, out_path) -> Path | None
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import config

log = logging.getLogger("dj_toolkit.stems_worker")


def separate(
    in_path: Path,
    out_dir: Path,
    model_id: str,
    progress: Callable[[float, str], None],
) -> dict[str, Path]:
    """Run demucs (via our soundfile-based runner) and collect every stem.

    Stems land in the demucs CLI layout ``out_dir/<model>/<track>/<stem>.wav``.
    Returns ``{stem_name: Path}`` for each ``*.wav`` found there.
    """
    out_dir = Path(out_dir)
    in_path = Path(in_path)

    progress(0.0, "Starting demucs…")
    # Use demucs_run.py (same venv, own OS process) rather than the demucs
    # CLI: torchaudio.save requires torchcodec, which isn't installed in the
    # shared venv. The runner writes stems with soundfile instead.
    venv_py = str(Path(config.DEMUCS_BIN).resolve().parent / "python")
    runner = Path(__file__).resolve().parent / "demucs_run.py"
    cmd = [venv_py, str(runner), str(in_path), model_id, str(out_dir)]
    log.info("running: %s", " ".join(cmd))
    proc = subprocess_run(cmd)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"demucs failed (exit {proc.returncode}): {detail}")

    model_root = out_dir / model_id
    if not model_root.is_dir():
        raise RuntimeError(f"demucs produced no output under {model_root}")

    stems: dict[str, Path] = {}
    for track_dir in model_root.iterdir():
        if not track_dir.is_dir():
            continue
        for wav in sorted(track_dir.glob("*.wav")):
            stems[wav.stem] = wav

    if not stems:
        raise RuntimeError("demucs produced no stems")
    progress(0.95, "Separation done")
    log.info("stems for %s: %s", model_id, ", ".join(sorted(stems)))
    return stems


def build_instrumental(
    stems: dict[str, Path], out_path: Path
) -> Path | None:
    """Mix every stem except vocals into one instrumental WAV.

    Returns the output path, or None if there is no vocals stem to remove
    (so app.py reports ``has_instrumental=False`` instead of failing).
    """
    vocal = stems.get("vocals")
    if vocal is None:
        return None
    others = [p for name, p in stems.items() if name != "vocals"]
    if not others:
        return None

    import numpy as np
    import soundfile as sf

    mixed = None
    sr = None
    for p in others:
        data, sr = sf.read(p, dtype="float32")
        mixed = data if mixed is None else mixed + data
    if mixed is None or sr is None:
        return None

    # Peak-normalize so summing stems can't clip the instrumental.
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.95:
        mixed = mixed * (0.95 / peak)
    sf.write(str(out_path), mixed, sr)
    log.info("instrumental written: %s", out_path)
    return out_path


def subprocess_run(cmd: list[str]):
    """Thin wrapper so the heavy import stays out of module import time."""
    import subprocess
    return subprocess.run(
        cmd,
        env=config.DEMUCS_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
