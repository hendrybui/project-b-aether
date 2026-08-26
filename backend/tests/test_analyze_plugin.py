"""Host-side unit tests for the analyze plugin's pure internals.

Covers ``_get_duration`` / ``_compute_rms_energy`` (the soundfile/numpy
helpers) and the ``run()`` progress span — the exact fraction sequence the
pipeline's job progress derives from — plus cancellation between heavy steps.
Uses REAL soundfile + numpy on tiny generated WAVs (no GPU, no librosa needed:
the tempo/key and loudness adapters are stubbed so the span test measures the
plugin's structure, not librosa's behavior on a DC signal).

Run via the node test wrapper (``tests/plugin-units.test.mjs``) or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import plugins.analyze_plugin as analyze_plugin
from plugins.analyze_plugin import ANALYZING_STAGE, AnalyzePlugin
from plugins.base import CancelledError, PluginContext


class StubLibrosa:
    def analyze_tempo_and_key(self, audio_path: str) -> dict:
        return {"bpm": 120.0, "key": "C", "scale": "major", "confidence": 0.9}


class StubLoudness:
    def analyze_loudness(self, audio_path: str) -> dict:
        return {"lufs_integrated": -14.0, "peak_dbfs": -1.5}


def make_plugin() -> AnalyzePlugin:
    plugin = AnalyzePlugin.__new__(AnalyzePlugin)
    plugin.librosa_adapter = StubLibrosa()
    plugin.loudness_adapter = StubLoudness()
    return plugin


def write_wav(directory: Path, name: str, data: np.ndarray, sr: int = 44100) -> Path:
    path = directory / name
    import soundfile as sf

    sf.write(path, data, sr)
    return path


class RmsEnergyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # sf.write defaults to PCM16, so float amplitudes quantize by ~1/32768 —
    # tolerances reflect the encoding, not sloppy assertions.
    def assert_energy(self, actual: float | None, expected: float) -> None:
        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertAlmostEqual(actual, expected, places=4)

    def test_dc_signal_rms_is_its_amplitude(self) -> None:
        path = write_wav(self.dir, "dc.wav", np.full(1000, 0.5))
        self.assert_energy(AnalyzePlugin._compute_rms_energy(str(path)), 0.5)

    def test_negative_and_stereo_averages_squares(self) -> None:
        # Stereo: left constant -0.5, right constant 0.5 -> mean of squares is
        # (0.25 + 0.25)/2 = 0.25 -> RMS 0.5 (squaring must handle negatives).
        data = np.column_stack([np.full(500, -0.5), np.full(500, 0.5)])
        path = write_wav(self.dir, "stereo.wav", data)
        self.assert_energy(AnalyzePlugin._compute_rms_energy(str(path)), 0.5)

    def test_asymmetric_stereo_energy(self) -> None:
        # (0.3^2 + 0.7^2)/2 = 0.29 -> sqrt = 0.538516...
        data = np.column_stack([np.full(500, 0.3), np.full(500, 0.7)])
        path = write_wav(self.dir, "asym.wav", data)
        self.assert_energy(AnalyzePlugin._compute_rms_energy(str(path)), float(np.sqrt(0.29)))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(AnalyzePlugin._compute_rms_energy(str(self.dir / "nope.wav")))

    def test_soundfile_absent_returns_none(self) -> None:
        path = write_wav(self.dir, "dc.wav", np.full(100, 0.5))
        with mock.patch.object(analyze_plugin, "sf", None):
            self.assertIsNone(AnalyzePlugin._compute_rms_energy(str(path)))


class GetDurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_real_file_duration_rounded_to_3(self) -> None:
        # 22050 samples @ 44100 Hz = 0.5 s exactly.
        path = write_wav(self.dir, "half.wav", np.zeros(22050))
        self.assertEqual(AnalyzePlugin._get_duration(str(path)), 0.5)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(AnalyzePlugin._get_duration(str(self.dir / "nope.wav")))

    def test_soundfile_absent_returns_none(self) -> None:
        path = write_wav(self.dir, "half.wav", np.zeros(4410))
        with mock.patch.object(analyze_plugin, "sf", None):
            self.assertIsNone(AnalyzePlugin._get_duration(str(path)))


class RunProgressSpanTest(unittest.TestCase):
    """run() must report exactly one analyzing tick per phase and per stem."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tracks = {
            "vocals": write_wav(self.dir, "vocals.wav", np.full(2000, 0.5)),
            "drums": write_wav(self.dir, "drums.wav", np.full(2000, 0.5)),
        }

    def run_plugin(self, stems: dict[str, Path], *, cancel_at_report: int | None = None):
        ticks: list[tuple[float, str, str]] = []
        calls = [0]

        def is_cancelled() -> bool:
            calls[0] += 1
            return cancel_at_report is not None and calls[0] > cancel_at_report

        ctx = PluginContext(
            params={
                "input_path": str(next(iter(self.tracks.values()))),
                "stems": {name: str(path) for name, path in stems.items()},
            },
            work_dir=self.dir,
            progress=lambda f, *, stage, message: ticks.append((f, stage, message)),
            is_cancelled=is_cancelled,
        )
        return make_plugin().run(ctx), ticks

    def test_two_stems_cover_the_full_span(self) -> None:
        # total_steps = 3 phases + 2 stems = 5 -> fractions 0.2..1.0.
        results, ticks = self.run_plugin(self.tracks)
        self.assertEqual([round(f, 4) for f, _, _ in ticks], [0.2, 0.4, 0.6, 0.8, 1.0])
        for _, stage, _ in ticks:
            self.assertEqual(stage, ANALYZING_STAGE)
        # Per-stem messages name the stem being analyzed.
        self.assertTrue(any("vocals" in m for _, _, m in ticks))
        self.assertTrue(any("drums" in m for _, _, m in ticks))
        # The summary dict is complete and the manifest-facing JSON is written.
        self.assertEqual(results["bpm"], 120.0)
        self.assertEqual(results["key"], "C")
        self.assertEqual(results["lufs_integrated"], -14.0)
        self.assertEqual(results["duration_sec"], round(2000 / 44100, 3))
        self.assertAlmostEqual(results["stem_energy"]["vocals"], 0.5, places=4)
        self.assertAlmostEqual(results["stem_energy"]["drums"], 0.5, places=4)
        summary = json.loads((self.dir / "analysis" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["stem_energy"]["vocals"], results["stem_energy"]["vocals"])

    def test_no_stems_uses_the_minimum_span(self) -> None:
        # total_steps = 3 + max(0, 1) = 4 -> quarters 0.25..1.0.
        results, ticks = self.run_plugin({})
        self.assertEqual([round(f, 4) for f, _, _ in ticks], [0.25, 0.5, 0.75, 1.0])
        self.assertEqual(results["stem_energy"], {})

    def test_cancel_mid_span_aborts_without_summary(self) -> None:
        # Cancel lands on the first stem's check (4th is_cancelled call): the
        # run must raise CancelledError and must NOT write the summary.
        with self.assertRaises(CancelledError):
            self.run_plugin(self.tracks, cancel_at_report=3)
        self.assertFalse((self.dir / "analysis" / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
