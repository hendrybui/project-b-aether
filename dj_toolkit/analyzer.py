"""Audio analysis via librosa — BPM, musical key/scale, loudness, duration.

Runs in-process inside Flask. This is SAFE to do here (unlike demucs/PyTorch):
librosa's heavy lifting is in numba/numpy/scipy, which don't hit the
OpenMP × Python-daemon-thread crash that PyTorch does under web servers.

Key/scale detection uses the Krumhansl-Schmuckler key-profile method on a
chroma_cqt feature — the same approach as audiomass/librosa_adapter.py, so the
results match what the user is used to. Returns {key, scale, confidence}.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# Krumhansl-Schmuckler key profiles (major / minor). Standard reference values.
# Index 0 = C, 1 = C#, ... 11 = B.
MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def analyze(audio_path: Path | str) -> dict[str, Any]:
    """Analyze an audio file: BPM, key/scale/confidence, loudness, duration.

    Returns a dict with keys: bpm, key, scale, confidence, duration_sec,
    lufs_integrated (approx), peak_dbfs. Any field may be None if its
    computation fails; the function degrades gracefully rather than raising.
    """
    # Imported lazily so Flask boots fast and librosa (heavy) only loads when
    # analysis is actually requested.
    import librosa

    audio_path = Path(audio_path)
    result: dict[str, Any] = {
        "bpm": None,
        "key": None,
        "scale": None,
        "confidence": None,
        "duration_sec": None,
        "lufs_integrated": None,
        "peak_dbfs": None,
    }

    try:
        # mono mixdown at 22050 Hz is standard for BPM/key. Keep float32.
        y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    except Exception as e:  # noqa: BLE001
        log.error("librosa.load failed for %s: %s", audio_path, e)
        return result

    result["duration_sec"] = round(float(len(y) / sr), 2)

    # ── BPM ─────────────────────────────────────────────────────────────
    try:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo, _ = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])
        # Normalize to a plausible DJ range (60–200). beat_track can octave-
        # misjudge; this mirrors audiomass's normalization.
        while bpm > 200:
            bpm /= 2.0
        while bpm < 60 and bpm > 0:
            bpm *= 2.0
        result["bpm"] = round(bpm, 1)
    except Exception as e:  # noqa: BLE001
        log.warning("BPM detection failed: %s", e)

    # ── Key / scale ─────────────────────────────────────────────────────
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
        chroma_mean = chroma.mean(axis=1)  # (12,) average energy per pitch class
        key, scale, confidence = _detect_key(chroma_mean)
        result["key"] = key
        result["scale"] = scale
        result["confidence"] = round(confidence, 3)
    except Exception as e:  # noqa: BLE001
        log.warning("key detection failed: %s", e)

    # ── Loudness (RMS-based approximation of integrated LUFS + peak dBFS) ─
    try:
        result["peak_dbfs"] = round(float(20 * np.log10(np.max(np.abs(y)) + 1e-10)), 1)
        rms = librosa.feature.rms(y=y)[0]
        # rough LUFS-like value: RMS averaged → dB. Not true ITU-R BS.1770, but
        # a useful ballpark. pyloudnorm would give true LUFS but adds a dep.
        mean_rms = float(rms.mean())
        if mean_rms > 0:
            result["lufs_integrated"] = round(float(20 * np.log10(mean_rms)) - 0.691, 1)
    except Exception as e:  # noqa: BLE001
        log.warning("loudness analysis failed: %s", e)

    return result


def _detect_key(chroma_mean: np.ndarray) -> tuple[str, str, float]:
    """Krumhansl-Schmuckler: correlate chroma against 12 rotations of each
    profile, pick the best major/minor match. Returns (note, scale, conf)."""
    best_score = -np.inf
    best_key = 0
    best_scale = "major"

    # Try each of the 12 possible tonics for both modes.
    for shift in range(12):
        rotated = np.roll(chroma_mean, -shift)  # align tonic to index 0
        maj = float(np.corrcoef(rotated, MAJOR_PROFILE)[0, 1])
        minor = float(np.corrcoef(rotated, MINOR_PROFILE)[0, 1])
        if maj > best_score:
            best_score, best_key, best_scale = maj, shift, "major"
        if minor > best_score:
            best_score, best_key, best_scale = minor, shift, "minor"

    # Confidence = best correlation, clamped to [0, 1].
    conf = max(0.0, min(1.0, best_score))
    return NOTE_NAMES[best_key], best_scale, conf
