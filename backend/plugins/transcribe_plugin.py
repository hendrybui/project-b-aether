"""Audio -> MIDI note transcription capability via basic-pitch.

Registered as the 'transcribe' plugin. Unlike the job-scoped separation
plugin, transcription is synchronous: the API endpoint supplies the input
file and gets notes back in one call. The uniform contract still applies —
params carry the input, run() returns a JSON-ready dict.
"""

from __future__ import annotations

from plugins.base import AudioPlugin, PluginContext, PluginError

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class TranscribePlugin(AudioPlugin):
    name = "transcribe"
    description = "Audio -> MIDI note transcription (basic-pitch)"

    def run(self, ctx: PluginContext) -> dict:
        input_path = str(ctx.params["input_path"])
        try:
            # Deferred import: torch/ONNX are heavy and only needed here.
            from basic_pitch.inference import predict
        except Exception as exc:
            raise PluginError(f"basic-pitch not available: {exc}") from exc

        try:
            _, _midi, note_events = predict(input_path)
        except Exception as exc:
            raise PluginError(f"Transcription failed: {exc}") from exc

        notes = []
        for start, end, midi_num, amplitude, _bends in note_events:
            name = NOTE_NAMES[((midi_num % 12) + 12) % 12]
            octave = midi_num // 12 - 1
            notes.append({
                "start": round(float(start), 3),
                "duration": round(float(end - start), 3),
                "midi": int(midi_num),
                "pitch": f"{name}{octave}",
                "amplitude": round(float(amplitude), 3),
            })
        # Sort by start time for predictable rendering.
        notes.sort(key=lambda n: n["start"])
        return {"notes": notes, "count": len(notes)}


# Self-registration on import (see plugins/__init__.py).
from plugins.registry import plugin_registry  # noqa: E402

plugin_registry.register(TranscribePlugin())
