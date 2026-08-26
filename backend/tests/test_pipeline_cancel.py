"""Host-side tests for the pipeline's cancellation routing.

run() must convert ANY CancelledError — raised by a plugin mid-run, or by
_check_cancel at a phase boundary — into a single mark_cancelled, run the
partial-output cleanup, and NEVER start the next phase or call mark_done.
These tests drive the REAL PipelineService.run() with a recording job
service, a stub job_store, a no-op ffmpeg, and a fake htdemucs plugin, all
under an isolated temp jobs dir. No GPU, docker, or server required.

Run via the node test wrapper (``tests/plugin-units.test.mjs``) or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from domain.enums import JobStatus, SourceType
from plugins.base import CancelledError
from plugins.registry import plugin_registry
from services.cancellation_service import cancellation_service
from services.pipeline_service import PipelineService

# Phases run() may never reach when a cancel lands before them.
NEVER_PHASES = {JobStatus.postprocessing, JobStatus.analyzing, JobStatus.packaging}


class RecordingJobService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.manifest = None

    def get_manifest(self, _job_id: str) -> object:
        return self.manifest

    def update_job(self, job_id: str, **kwargs) -> None:
        self.calls.append(("update_job", kwargs))

    def update_manifest_files(self, job_id: str, files: dict) -> None:
        self.calls.append(("manifest_files", files))

    def update_analysis(self, job_id: str, analysis: dict) -> None:
        self.calls.append(("analysis", analysis))

    def mark_cancelled(self, job_id: str, message: str) -> None:
        self.calls.append(("mark_cancelled", message))

    def mark_done(self, job_id: str) -> None:
        self.calls.append(("mark_done", None))

    def job_updates(self) -> list[dict]:
        return [kwargs for kind, kwargs in self.calls if kind == "update_job"]


class CancelOnRunPlugin:
    """Fake htdemucs plugin: reports some progress, then raises CancelledError."""

    def run(self, ctx) -> dict:
        ctx.progress(0.5, stage="separating", message="Separating (2/4 chunks)")
        raise CancelledError()


class CompleteThenSignalPlugin:
    """Fake plugin that completes, then flips a flag the next checkpoint sees."""

    def __init__(self, flag: dict) -> None:
        self.flag = flag

    def run(self, ctx) -> dict:
        ctx.progress(1.0)
        self.flag["cancelled"] = True
        return {}


class PipelineCancelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_dir = self.root / "job-1"
        # The real JobStore.ensure_job_dir lays out the job dir; the stub
        # returns it as-is, so create the layout the pipeline touches.
        (self.job_dir / "source").mkdir(parents=True)

        self.flag: dict = {"cancelled": False}
        self.job = RecordingJobService()
        self.job.manifest = types.SimpleNamespace(
            source=types.SimpleNamespace(
                type=SourceType.upload,
                url=None,
                filename=str(self.root / "upload.wav"),
            ),
            selected_stems=["vocals"],
        )
        # A real WAV so the upload copy path works end to end.
        import soundfile as sf

        sf.write(self.root / "upload.wav", np.zeros(4410), 44100)

        self.svc = PipelineService.__new__(PipelineService)
        self.svc.job_service = self.job
        self.svc.job_store = types.SimpleNamespace(ensure_job_dir=lambda _jid: self.job_dir)
        self.svc.ffmpeg = types.SimpleNamespace(
            transcode_to_wav=lambda *_a, **_k: None,
            mix_wavs=lambda *_a, **_k: None,
            copy_audio=lambda *_a, **_k: None,
        )

        # The cancel probe: False at the phase checkpoints until a test flips it.
        self._cancelled_patch = mock.patch.object(
            cancellation_service, "is_cancelled", side_effect=lambda _jid: self.flag["cancelled"],
        )
        self._cancelled_patch.start()
        self.addCleanup(self._cancelled_patch.stop)

    def run_pipeline(self, plugin) -> None:
        with mock.patch.object(plugin_registry, "require", return_value=plugin):
            self.svc.run("job-1")

    def assert_cancelled_cleanly(self, message_fragment: str) -> None:
        """Shared terminal-state assertions for every cancel scenario."""
        # Exactly one mark_cancelled, and never mark_done.
        cancelled = [m for kind, m in self.job.calls if kind == "mark_cancelled"]
        self.assertEqual(len(cancelled), 1, f"mark_cancelled calls: {cancelled}")
        self.assertIn(message_fragment, cancelled[0])
        self.assertNotIn("mark_done", [k for k, _ in self.job.calls])
        # The pipeline log records the cancellation.
        log = (self.job_dir / "logs" / "pipeline.log").read_text(encoding="utf-8")
        self.assertIn("Job cancelled", log)

    def test_cancel_mid_plugin_routes_to_mark_cancelled(self) -> None:
        # Stems dir exists (separation completed before the cancel landed) —
        # cleanup must preserve it, while transient outputs are purged.
        stems = self.job_dir / "stems"
        stems.mkdir()
        analysis = self.job_dir / "analysis"
        analysis.mkdir()

        self.run_pipeline(CancelOnRunPlugin())

        updates = self.job.job_updates()
        statuses = [u["status"] for u in updates]
        # The separating span ran (checkpoint + the plugin's mid-run tick)...
        self.assertIn(JobStatus.separating, statuses)
        self.assertIn(0.68, [u["progress"] for u in updates], "plugin tick 0.5 must map to 0.68")
        # ...and nothing after it started.
        for phase in NEVER_PHASES:
            self.assertNotIn(phase, statuses, f"phase {phase} must not start after a cancel")
        # The last recorded status is the separating tick, not a later phase.
        self.assertEqual(statuses[-1], JobStatus.separating)

        self.assert_cancelled_cleanly("Job cancelled before completion")
        # Cleanup: transient dirs purged, the expensive stems dir preserved.
        self.assertFalse(analysis.exists(), "analysis must be cleaned up on cancel")
        self.assertTrue(stems.exists(), "stems must be preserved on cancel")

    def test_cancel_at_phase_boundary_blocks_the_next_phase(self) -> None:
        # Separation completes normally; the cancel lands at the checkpoint
        # BEFORE postprocessing. Postprocessing must never start.
        self.run_pipeline(CompleteThenSignalPlugin(self.flag))

        updates = self.job.job_updates()
        statuses = [u["status"] for u in updates]
        self.assertIn(JobStatus.separating, statuses)
        for phase in NEVER_PHASES:
            self.assertNotIn(phase, statuses, f"phase {phase} must not start after a boundary cancel")
        # The last update is the separating completion tick (0.55 + 0.25).
        self.assertEqual(statuses[-1], JobStatus.separating)
        self.assertEqual(updates[-1]["progress"], 0.8)

        self.assert_cancelled_cleanly("Job cancelled before completion")


if __name__ == "__main__":
    unittest.main()
