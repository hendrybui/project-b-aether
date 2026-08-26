"""Host-side unit tests for the transcribe plugin's pure internals.

Covers the note-event -> dict mapping (MIDI number -> pitch name/octave,
start/duration/amplitude rounding), the sort-by-start guarantee, the count,
and the two PluginError paths — all WITHOUT basic-pitch: the plugin's deferred
``from basic_pitch.inference import predict`` is satisfied by a fake module
planted in ``sys.modules``, so no torch/ONNX ever loads.

Run via the node test wrapper (``tests/plugin-units.test.mjs``) or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import builtins
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from plugins.base import PluginContext, PluginError
from plugins.transcribe_plugin import TranscribePlugin


def fake_basic_pitch(predict_impl) -> tuple[types.ModuleType, types.ModuleType]:
    """Two module objects mirroring basic_pitch / basic_pitch.inference."""
    mod = types.ModuleType("basic_pitch")
    inference = types.ModuleType("basic_pitch.inference")
    inference.predict = predict_impl
    mod.inference = inference
    return mod, inference


def run_with_events(events: list, *, input_path: str = "/tmp/song.wav") -> dict:
    plugin = TranscribePlugin.__new__(TranscribePlugin)
    ctx = PluginContext(params={"input_path": input_path}, work_dir=Path("/tmp"))
    mod, inference = fake_basic_pitch(lambda _p: (None, None, events))
    with mock.patch.dict(sys.modules, {"basic_pitch": mod, "basic_pitch.inference": inference}):
        return plugin.run(ctx)


class NoteMappingTest(unittest.TestCase):
    def test_midi_numbers_map_to_pitch_names_and_octaves(self) -> None:
        # C4 = middle C (60), A4 = 440 Hz (69), and the extremes of the MIDI
        # range (0 = C-1, 127 = G9) to pin the octave arithmetic.
        events = [
            (0.0, 1.0, 60, 1.0, []),
            (0.0, 1.0, 61, 1.0, []),
            (0.0, 1.0, 69, 1.0, []),
            (0.0, 1.0, 48, 1.0, []),
            (0.0, 1.0, 12, 1.0, []),
            (0.0, 1.0, 0, 1.0, []),
            (0.0, 1.0, 127, 1.0, []),
        ]
        result = run_with_events(events)
        pitches = [n["pitch"] for n in result["notes"]]
        self.assertEqual(pitches, ["C4", "C#4", "A4", "C3", "C0", "C-1", "G9"])

    def test_timings_and_amplitude_are_rounded_to_3(self) -> None:
        result = run_with_events([(1.23456, 3.5, 60, 0.98765, [])])
        note = result["notes"][0]
        self.assertEqual(note["start"], 1.235)
        self.assertEqual(note["duration"], round(3.5 - 1.23456, 3))  # 2.265
        self.assertEqual(note["amplitude"], 0.988)
        self.assertEqual(note["midi"], 60)
        self.assertEqual(result["count"], 1)

    def test_notes_are_sorted_by_start_time(self) -> None:
        result = run_with_events([
            (2.0, 2.5, 64, 0.5, []),
            (0.0, 0.5, 60, 0.5, []),
            (1.0, 1.5, 62, 0.5, []),
        ])
        starts = [n["start"] for n in result["notes"]]
        self.assertEqual(starts, [0.0, 1.0, 2.0])
        self.assertEqual([n["midi"] for n in result["notes"]], [60, 62, 64])
        self.assertEqual(result["count"], 3)

    def test_empty_note_list(self) -> None:
        self.assertEqual(run_with_events([]), {"notes": [], "count": 0})

    def test_bends_are_dropped(self) -> None:
        result = run_with_events([(0.0, 1.0, 60, 1.0, [(0, 0.0), (1, 0.25)])])
        self.assertNotIn("bends", result["notes"][0])
        self.assertEqual(set(result["notes"][0].keys()),
                         {"start", "duration", "midi", "pitch", "amplitude"})

    def test_numpy_values_are_coerced_to_plain_types(self) -> None:
        # basic-pitch returns numpy scalars; the manifest must get JSON-ready
        # python int/float (round() on np.float32 also emits a warning on some
        # numpy versions, which the float() wrap avoids).
        result = run_with_events([
            (np.float32(1.2345678), np.float32(2.5), np.int32(60), np.float32(0.8765432), []),
        ])
        note = result["notes"][0]
        self.assertIs(type(note["midi"]), int)
        self.assertIs(type(note["start"]), float)
        self.assertEqual(note["midi"], 60)
        self.assertEqual(note["start"], 1.235)
        self.assertEqual(note["amplitude"], 0.877)


class ErrorPathTest(unittest.TestCase):
    def test_missing_basic_pitch_raises_plugin_error(self) -> None:
        plugin = TranscribePlugin.__new__(TranscribePlugin)
        ctx = PluginContext(params={"input_path": "/tmp/song.wav"}, work_dir=Path("/tmp"))
        # The venv may or may not have basic_pitch installed; either way, a
        # genuinely missing package raises ImportError from the deferred
        # import, which must surface as PluginError (not a traceback).
        real_import = builtins.__import__

        def block_basic_pitch(name: str, *args, **kwargs):
            if name.startswith("basic_pitch"):
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", block_basic_pitch):
            with self.assertRaises(PluginError) as cm:
                plugin.run(ctx)
        self.assertIn("basic-pitch not available", str(cm.exception))

    def test_predict_failure_raises_plugin_error(self) -> None:
        plugin = TranscribePlugin.__new__(TranscribePlugin)
        ctx = PluginContext(params={"input_path": "/tmp/song.wav"}, work_dir=Path("/tmp"))

        def boom(_path: str):
            raise RuntimeError("inference exploded")

        mod, inference = fake_basic_pitch(boom)
        with mock.patch.dict(sys.modules, {"basic_pitch": mod, "basic_pitch.inference": inference}):
            with self.assertRaises(PluginError) as cm:
                plugin.run(ctx)
        self.assertIn("Transcription failed", str(cm.exception))

    def test_input_path_is_passed_to_predict(self) -> None:
        received: list[str] = []

        def capture(path: str):
            received.append(path)
            return None, None, []

        plugin = TranscribePlugin.__new__(TranscribePlugin)
        ctx = PluginContext(params={"input_path": "/tmp/real-name.wav"}, work_dir=Path("/tmp"))
        mod, inference = fake_basic_pitch(capture)
        with mock.patch.dict(sys.modules, {"basic_pitch": mod, "basic_pitch.inference": inference}):
            plugin.run(ctx)
        self.assertEqual(received, ["/tmp/real-name.wav"])


if __name__ == "__main__":
    unittest.main()
