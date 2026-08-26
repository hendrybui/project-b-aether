"""Host-side tests for the pipeline's phase-to-progress aggregation.

Each plugin reports a phase-relative fraction in [0, 1]; the pipeline's
on_progress closures in PipelineService map that onto the job's aggregate
progress window (separating 0.55->0.80, analyzing 0.88->0.92, waveforms
0.92->0.95) and relay stage/message. These tests drive the REAL closures with
a recording job service and fake plugins reporting known fractions, then
assert the exact aggregate values, the default relay behavior, and that the
spans tile the fixed checkpoints with no gaps.

Run via the node test wrapper (``tests/plugin-units.test.mjs``) or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from domain.enums import JobStatus
from plugins.registry import plugin_registry
from services.pipeline_service import PipelineService


class RecordingJobService:
    """Records every update_job call exactly as the closures issue it."""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update_job(self, job_id: str, **kwargs) -> None:
        self.updates.append({"job_id": job_id, **kwargs})


class StagedProgressPlugin:
    """Fake plugin reporting known fractions, each with stage + message."""

    def __init__(self, fractions: list[float]) -> None:
        self.fractions = fractions

    def run(self, ctx) -> dict:
        for fraction in self.fractions:
            ctx.progress(fraction, stage=f"stage:{fraction}", message=f"msg:{fraction}")
        return {}


class BareProgressPlugin:
    """Fake plugin reporting fractions with NO stage/message (defaults path)."""

    def __init__(self, fractions: list[float]) -> None:
        self.fractions = fractions

    def run(self, ctx) -> dict:
        for fraction in self.fractions:
            ctx.progress(fraction)
        return {}


class PipelineProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.job = RecordingJobService()
        self.svc = PipelineService.__new__(PipelineService)
        self.svc.job_service = self.job
        self.log_path = self.dir / "logs" / "pipeline.log"

    def run_phase(self, method, fractions: list[float], plugin_cls=StagedProgressPlugin):
        """Run one phase's real mapping closure against a fake plugin."""
        self.job.updates.clear()  # each phase is a fresh job-progress sequence
        plugin = plugin_cls(fractions)
        with mock.patch.object(plugin_registry, "require", return_value=plugin):
            if method == self.svc._separate_stems:
                method("job-1", self.dir / "input.wav", ["vocals"], self.dir, self.log_path)
            elif method == self.svc._analyze_stems:
                method("job-1", str(self.dir / "input.wav"), {"vocals": "v.wav"}, self.dir, self.log_path)
            else:
                method("job-1", {"vocals": "v.wav"}, self.dir, self.log_path)
        # Copy: the next run_phase clears the shared list in place, and a
        # caller holding a reference would see it mutate under them.
        return list(self.job.updates)

    def test_separating_span_maps_onto_0_55_to_0_80(self) -> None:
        updates = self.run_phase(self.svc._separate_stems, [0.0, 0.4, 1.0])
        self.assertEqual([u["progress"] for u in updates], [0.55, 0.65, 0.8])
        for u in updates:
            self.assertEqual(u["status"], JobStatus.separating)
            self.assertEqual(u["job_id"], "job-1")
        # stage/message are relayed from the plugin's progress call.
        self.assertEqual(updates[1]["step"], "stage:0.4")
        self.assertEqual(updates[1]["message"], "msg:0.4")

    def test_analyze_span_maps_onto_0_88_to_0_92(self) -> None:
        updates = self.run_phase(self.svc._analyze_stems, [0.0, 0.5, 1.0])
        self.assertEqual([u["progress"] for u in updates], [0.88, 0.9, 0.92])
        for u in updates:
            self.assertEqual(u["status"], JobStatus.analyzing)
        self.assertEqual(updates[1]["step"], "stage:0.5")
        self.assertEqual(updates[1]["message"], "msg:0.5")

    def test_waveform_span_maps_onto_0_92_to_0_95(self) -> None:
        updates = self.run_phase(self.svc._generate_waveforms, [0.0, 0.3333, 1.0])
        self.assertEqual([u["progress"] for u in updates], [0.92, 0.93, 0.95])
        for u in updates:
            self.assertEqual(u["status"], JobStatus.analyzing)

    def test_defaults_apply_when_plugin_sends_no_stage_or_message(self) -> None:
        # The plugin-facing contract makes stage/message optional; the phase
        # value must be used as the step and a phase-specific message shown.
        sep = self.run_phase(self.svc._separate_stems, [0.5], BareProgressPlugin)[0]
        self.assertEqual(sep["step"], JobStatus.separating.value)
        self.assertEqual(sep["message"], "Separating selected stems")
        ana = self.run_phase(self.svc._analyze_stems, [0.5], BareProgressPlugin)[0]
        self.assertEqual(ana["step"], JobStatus.analyzing.value)
        self.assertEqual(ana["message"], "Analyzing audio (BPM, key, loudness)")
        wav = self.run_phase(self.svc._generate_waveforms, [0.5], BareProgressPlugin)[0]
        self.assertEqual(wav["step"], "generating_waveforms")
        self.assertEqual(wav["message"], "Generating waveform summaries")

    def test_spans_tile_the_checkpoints_without_gaps(self) -> None:
        # fraction 1.0 of one phase must land exactly where the next phase's
        # checkpoint (and fraction 0.0) picks up — the contract the job
        # progress bar depends on:
        #   separating ends 0.80 < postprocessing 0.82
        #   analyzing opens 0.88, closes 0.92
        #   waveforms opens exactly at 0.92, closes at 0.95 (packaging).
        sep = self.run_phase(self.svc._separate_stems, [1.0])[0]
        ana = self.run_phase(self.svc._analyze_stems, [0.0, 1.0])
        wav = self.run_phase(self.svc._generate_waveforms, [0.0, 1.0])
        self.assertEqual(sep["progress"], 0.8)
        self.assertEqual(ana[0]["progress"], 0.88)
        self.assertEqual(ana[-1]["progress"], 0.92)
        self.assertEqual(wav[0]["progress"], 0.92)
        self.assertEqual(wav[-1]["progress"], 0.95)


if __name__ == "__main__":
    unittest.main()
