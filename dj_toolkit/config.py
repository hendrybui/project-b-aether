"""Configuration for the DJ Toolkit Flask app.

SELF-CONTAINED — does NOT depend on the AudioMass FastAPI server.
Stem separation runs as a demucs CLI subprocess (the user's proven workflow);
BPM/key analysis runs librosa in-process (safe: no OpenMP/daemon-thread issue
like PyTorch has). MP3→MIDI runs basic-pitch in-process.

Why subprocess for demucs (not in-process PyTorch):
  Running htdemucs via the PyTorch API inside a multi-threaded web server
  triggers the classic OpenMP × daemon-thread crash
  ("terminate called without active exception"). The user confirmed this is
  why FastAPI didn't work for them. The demucs CLI is a separate process,
  so the crash can't take the web server down. We also set
  OMP_NUM_THREADS=1 in the subprocess env as belt-and-suspenders.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Flask app ────────────────────────────────────────────────────────────
HOST = os.environ.get("DJ_TOOLKIT_HOST", "0.0.0.0")
PORT = int(os.environ.get("DJ_TOOLKIT_PORT", "5001"))
DEBUG = os.environ.get("DJ_TOOLKIT_DEBUG", "1") == "1"
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200 MB upload cap

# ── File handling ────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Allowed upload extensions (demucs + librosa both handle all of these).
ALLOWED_EXTENSIONS = frozenset({"mp3", "wav", "flac", "m4a", "aac", "ogg"})
TMP_MAX_AGE_SECONDS = 60 * 60  # auto-prune tmp/ files older than 1h

# ── demucs CLI ───────────────────────────────────────────────────────────
# The demucs executable. We resolve via the venv that has demucs installed.
# Override with DEMUCS_BIN if needed.
DEMUCS_BIN = os.environ.get(
    "DEMUCS_BIN",
    str(BASE_DIR.parent / "audiomass" / ".venv" / "bin" / "demucs"),
)
DEMUCS_DEVICE = os.environ.get("DEMUCS_DEVICE", "cpu")  # cpu | cuda
# Belt-and-suspenders against the OpenMP × daemon-thread crash.
DEMUCS_ENV = {**os.environ, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}

# Model presets selectable per upload. Maps preset id → (model name, stems).
# Stems match what each demucs model actually emits (verified against CLI).
DEMUCS_MODELS = {
    "htdemucs": {
        "label": "htdemucs (4 stems · fast)",
        "name": "htdemucs",
        "stems": ["drums", "bass", "vocals", "other"],
    },
    "htdemucs_ft": {
        "label": "htdemucs_ft (4 stems · fine-tuned, better quality)",
        "name": "htdemucs_ft",
        "stems": ["drums", "bass", "vocals", "other"],
    },
    "htdemucs_6s": {
        "label": "htdemucs_6s (6 stems · adds guitar + piano)",
        "name": "htdemucs_6s",
        "stems": ["drums", "bass", "other", "vocals", "guitar", "piano"],
    },
}
DEFAULT_MODEL = "htdemucs_ft"  # the fine-tuned 4-stem, matches user's disk folders

# ── Audio stem name → pretty label / icon (for the UI) ──────────────────
STEM_LABELS = {
    "vocals": ("Vocals (acapella)", "mic"),
    "drums": ("Drums", "music-note-beamed"),
    "bass": ("Bass", "graph-down"),
    "other": ("Other", "soundwave"),
    "guitar": ("Guitar", "music-note"),
    "piano": ("Piano", "music-note-list"),
}

# ── MP3 → MIDI (basic-pitch) ─────────────────────────────────────────────
MIDI_ONSET_THRESHOLD = float(os.environ.get("MIDI_ONSET_THRESHOLD", "0.5"))
MIDI_FRAME_THRESHOLD = float(os.environ.get("MIDI_FRAME_THRESHOLD", "0.3"))
MIDI_MIN_NOTE_LENGTH = int(os.environ.get("MIDI_MIN_NOTE_LENGTH", "127"))  # ms

# ── Camelot wheel mapping ────────────────────────────────────────────────
# librosa gives us {key, scale}; we compute the Camelot DJ code.
CAMELOT_WHEEL = {
    ("C", "major"): "8B", ("C#", "major"): "3B", ("D", "major"): "10B",
    ("D#", "major"): "5B", ("E", "major"): "12B", ("F", "major"): "7B",
    ("F#", "major"): "2B", ("G", "major"): "9B", ("G#", "major"): "4B",
    ("A", "major"): "11B", ("A#", "major"): "6B", ("B", "major"): "1B",
    ("C", "minor"): "5A", ("C#", "minor"): "12A", ("D", "minor"): "7A",
    ("D#", "minor"): "2A", ("E", "minor"): "9A", ("F", "minor"): "4A",
    ("F#", "minor"): "11A", ("G", "minor"): "6A", ("G#", "minor"): "1A",
    ("A", "minor"): "8A", ("A#", "minor"): "3A", ("B", "minor"): "10A",
}
ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def camelot_for(key: str | None, scale: str | None) -> str | None:
    if not key or not scale:
        return None
    note = ENHARMONIC.get(key, key)
    scale_norm = scale.lower()
    if scale_norm not in ("major", "minor"):
        return None
    return CAMELOT_WHEEL.get((note, scale_norm))


# ── Job lifecycle (in-process) ───────────────────────────────────────────
# Our own pipeline stages, surfaced to the UI.
STAGE_LABELS = {
    "queued": "Queued",
    "analyzing": "Detecting BPM & key",          # librosa, fast
    "separating": "Separating stems (demucs)",    # the long one
    "packaging": "Packaging downloads",
    "done": "Complete",
    "failed": "Failed",
}
TERMINAL_STATES = frozenset({"done", "failed"})
