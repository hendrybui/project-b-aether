"""Host-side unit tests for the waveform plugin's pure internals.

Covers ``_generate`` — the min/max bucketing math, mono down-mix, bucket
clamping, and the written JSON shape — plus ``run()``'s per-stem progress span,
failure-skip, and cancellation behavior. Uses REAL soundfile + numpy on tiny
generated WAVs; no GPU or server required.

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

import plugins.waveform_plugin as waveform_plugin
from plugins.base import CancelledError, PluginContext
from plugins.waveform_plugin import WaveformPlugin


def make_plugin() -> WaveformPlugin:
    return WaveformPlugin.__new__(WaveformPlugin)


def write_wav(directory: Path, name: str, data: np.ndarray, sr: int = 44100) -> Path:
    path = directory / name
    import soundfile as sf

    sf.write(path, data, sr)
    return path


class GenerateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # sf.write defaults to PCM16, so float amplitudes quantize by ~1/32768 —
    # tolerances reflect the encoding, not sloppy assertions.
    def assert_buckets(self, result: dict, expected: list[float]) -> None:
        np.testing.assert_allclose(result["min"], expected, atol=1e-3)
        np.testing.assert_allclose(result["max"], expected, atol=1e-3)

    def test_dc_mono_buckets_are_flat_at_the_amplitude(self) -> None:
        path = write_wav(self.dir, "dc.wav", np.full(8000, 0.5))
        result = make_plugin()._generate(str(path), str(self.dir / "dc.json"), buckets=8)
        self.assertEqual(result["sample_rate"], 44100)
        self.assertEqual(result["channels"], 1)
        self.assertEqual(result["buckets"], 8)
        self.assertAlmostEqual(result["duration_sec"], round(8000 / 44100, 3), places=6)
        self.assert_buckets(result, [0.5] * 8)

    def test_stereo_is_down_mixed_to_mono(self) -> None:
        # Left channel 0.0, right channel 1.0 -> mono 0.5 everywhere.
        data = np.column_stack([np.zeros(8000), np.ones(8000)])
        path = write_wav(self.dir, "stereo.wav", data)
        result = make_plugin()._generate(str(path), str(self.dir / "stereo.json"), buckets=4)
        self.assertEqual(result["channels"], 2)
        self.assert_buckets(result, [0.5] * 4)

    def test_sine_buckets_track_the_waveform(self) -> None:
        t = np.linspace(0, 1, 8000, endpoint=False)
        sine = np.sin(2 * np.pi * 4 * t)  # 4 cycles over 8000 samples
        path = write_wav(self.dir, "sine.wav", sine)
        result = make_plugin()._generate(str(path), str(self.dir / "sine.json"), buckets=8)
        # Each bucket spans half a cycle: maxima near +1, minima near -1.
        self.assertGreater(max(result["max"]), 0.9)
        self.assertLess(min(result["min"]), -0.9)
        for lo, hi in zip(result["min"], result["max"]):
            self.assertLessEqual(lo, hi, "min must never exceed max within a bucket")

    def test_buckets_clamped_to_sample_count(self) -> None:
        # More buckets than samples -> one bucket per sample, each min=max.
        data = np.array([-0.4, 0.1, 0.7, -0.2, 0.5])
        path = write_wav(self.dir, "tiny.wav", data)
        result = make_plugin()._generate(str(path), str(self.dir / "tiny.json"), buckets=2000)
        self.assertEqual(result["buckets"], 5)
        np.testing.assert_allclose(result["min"], data, atol=1e-3)
        np.testing.assert_allclose(result["max"], data, atol=1e-3)

    def test_silence_is_zero(self) -> None:
        path = write_wav(self.dir, "silence.wav", np.zeros(4000))
        result = make_plugin()._generate(str(path), str(self.dir / "silence.json"), buckets=4)
        self.assertEqual(result["min"], [0.0] * 4)
        self.assertEqual(result["max"], [0.0] * 4)

    def test_output_file_is_written_as_json(self) -> None:
        path = write_wav(self.dir, "dc.wav", np.full(100, 0.5))
        out = self.dir / "wf" / "stem.json"
        make_plugin()._generate(str(path), str(out), buckets=4)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["buckets"], 4)
        self.assertEqual(len(payload["min"]), 4)
        self.assertEqual(len(payload["max"]), 4)

    def test_missing_soundfile_raises(self) -> None:
        path = write_wav(self.dir, "dc.wav", np.full(100, 0.5))
        with mock.patch.object(waveform_plugin, "sf", None):
            with self.assertRaises(RuntimeError):
                make_plugin()._generate(str(path), str(self.dir / "x.json"))


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.good = write_wav(self.dir, "vocals.wav", np.full(2000, 0.5))
        self.other = write_wav(self.dir, "drums.wav", np.full(2000, -0.5))

    def run_plugin(self, stems: dict[str, Path], *, cancel_on_check: int | None = None):
        ticks: list[tuple[float, str, str]] = []
        calls = [0]

        def is_cancelled() -> bool:
            calls[0] += 1
            return cancel_on_check is not None and calls[0] > cancel_on_check

        ctx = PluginContext(
            params={"stems": {name: str(path) for name, path in stems.items()}},
            work_dir=self.dir,
            progress=lambda f, *, stage, message: ticks.append((f, stage, message)),
            is_cancelled=is_cancelled,
        )
        return make_plugin().run(ctx), ticks

    def test_progress_one_tick_per_stem(self) -> None:
        result, ticks = self.run_plugin({"vocals": self.good, "drums": self.other})
        # run() reports i/len BEFORE each stem: 0.0 then 0.5 (no terminal 1.0).
        self.assertEqual([f for f, _, _ in ticks], [0.0, 0.5])
        for _, stage, _ in ticks:
            self.assertEqual(stage, "generating_waveforms")
        self.assertEqual(set(result.keys()), {"vocals", "drums"})
        for name, out in result.items():
            self.assertTrue(Path(out).exists())

    def test_failing_stem_is_skipped(self) -> None:
        result, _ = self.run_plugin({
            "vocals": self.good,
            "drums": self.dir / "missing.wav",
        })
        self.assertEqual(set(result.keys()), {"vocals"})

    def test_cancel_between_stems_aborts(self) -> None:
        # Cancel on the 2nd check (before the second stem): the first stem's
        # waveform exists, the second is never generated.
        with self.assertRaises(CancelledError):
            self.run_plugin({"vocals": self.good, "drums": self.other}, cancel_on_check=1)
        self.assertTrue((self.dir / "waveforms" / "vocals.json").exists())
        self.assertFalse((self.dir / "waveforms" / "drums.json").exists())


if __name__ == "__main__":
    unittest.main()
