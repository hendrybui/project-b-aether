"""MP3 → MIDI conversion via basic-pitch (ONNX backend, no TensorFlow).

basic-pitch normally pins tensorflow<2.15.1 (unavailable on Python 3.12).
We install it --no-deps + onnxruntime instead, which makes ONNX the default
backend (`_default_model_type = FilenameSuffix.onnx`). The bundled ONNX model
ships inside the wheel (saved_models/icassp_2022/nmp.onnx), so there is NO
first-run download — conversion works offline.

predict() returns (model_output, PrettyMIDI, note_events). We write the
PrettyMIDI object to a .mid file and return lightweight stats.
"""
from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

# Silence basic-pitch's verbose backend-absence warnings (TF/CoreML/TFLite)
# and librosa's deprecation noise — they're expected in our ONNX-only setup.
warnings.filterwarnings("ignore")
logging.getLogger("basic_pitch").setLevel(logging.ERROR)

import config  # noqa: E402  (after warning filters)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def to_midi(
    audio_path: Path | str,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length: int = 127,
) -> tuple[Path, dict[str, Any]]:
    """Convert an audio file to MIDI.

    Args:
        audio_path: input audio (mp3/wav/flac/m4a/aac/ogg).
        onset_threshold: note-start sensitivity (lower = more notes).
        frame_threshold: note-continuation sensitivity (lower = more notes).
        min_note_length: minimum note length in milliseconds.

    Returns:
        (midi_path, stats) where stats = {note_count, duration_sec,
        onset_threshold, frame_threshold}.

    Raises on any conversion failure; the caller handles cleanup of the
    input file and surfaces the error to the UI.
    """
    # Imported lazily so the Flask app starts fast and /api/health can report
    # MIDI availability without forcing the heavy import at boot.
    from basic_pitch.inference import predict  # type: ignore

    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)

    # minimum_frequency / maximum_frequency left as defaults (full range).
    # midi_tempo defaults to 120 BPM in basic-pitch.
    _model_output, midi_data, note_events = predict(
        str(audio_path),
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
        minimum_note_length=float(min_note_length),
    )

    if not note_events:
        # Still write an empty (valid) MIDI so the caller gets a file, but
        # surface that nothing was detected.
        out_path = config.TMP_DIR / (audio_path.stem + ".mid")
        midi_data.write(str(out_path))
        return out_path, {
            "note_count": 0,
            "duration_sec": 0.0,
            "onset_threshold": onset_threshold,
            "frame_threshold": frame_threshold,
            "note": "No notes detected — try lowering the thresholds.",
        }

    # Write the .mid to our tmp/ dir.
    out_path = config.TMP_DIR / (audio_path.stem + ".mid")
    midi_data.write(str(out_path))

    # Derive lightweight stats from note_events: (start, end, midi_pitch, amp, bends)
    last_end = max((e[1] for e in note_events), default=0.0)
    pitches = [int(e[2]) for e in note_events]
    low = min(pitches) if pitches else None
    high = max(pitches) if pitches else None

    return out_path, {
        "note_count": len(note_events),
        "duration_sec": round(last_end, 2),
        "pitch_low": _note_name(low),
        "pitch_high": _note_name(high),
        "onset_threshold": onset_threshold,
        "frame_threshold": frame_threshold,
    }


def _note_name(midi_pitch: int | None) -> str | None:
    if midi_pitch is None:
        return None
    octave = (midi_pitch // 12) - 1
    return f"{NOTE_NAMES[midi_pitch % 12]}{octave}"
