"""Host-side tests for the pipeline's failure routing.

run() must convert any non-cancellation exception — a plugin crashing
mid-run, or a downstream step (ffmpeg mixdown) failing — into exactly one
mark_failed with the friendly message, run the partial-output cleanup, and
never start the next phase or call mark_done. These tests drive the REAL
PipelineService.run() with a recording job service, a stub job store, and a
configurable ffmpeg stub, under an isolated temp jobs dir. No GPU, docker,
or server required.

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
from plugins.registry import plugin_registry
from services.cancellation_service import cancellation_service
from services.pipeline_service import PipelineService

NEVER_PHASES = {JobStatus.analyzing, JobStatus.packaging}


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

    def mark_failed(self, job_id: str, message: str) -> None:
        self.calls.append(("mark_failed", message))

    def mark_done(self, job_id: str) -> None:
        self.calls.append(("mark_done", None))

    def job_updates(self) -> list[dict]:
        return [kwargs for kind, kwargs in self.calls if kind == "update_job"]


class StubFFmpeg:
    """No-op ffmpeg; the test can arm mix_wavs to fail."""

    def __init__(self) -> None:
        self.mix_error: Exception | None = None

    def transcode_to_wav(self, *_a, **_k) -> None:
        pass

    def mix_wavs(self, *_a, **_k) -> None:
        if self.mix_error is not None:
            raise self.mix_error

    def copy_audio(self, *_a, **_k) -> None:
        pass


class FailOnRunPlugin:
    """Fake htdemucs plugin: reports progress, then crashes (not a cancel)."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def run(self, ctx) -> dict:
        ctx.progress(0.5, stage="separating", message="Separating (2/4 chunks)")
        raise self.error


class CompletesPlugin:
    """Fake plugin that finishes separation successfully."""

    def run(self, ctx) -> dict:
        ctx.progress(1.0)
        return {}


class PipelineFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_dir = self.root / "job-1"
        # The real JobStore.ensure_job_dir lays out the job dir; the stub
        # returns it as-is, so create the layout the pipeline touches.
        (self.job_dir / "source").mkdir(parents=True)
        # Present before the failure so cleanup has something to purge/preserve.
        (self.job_dir / "analysis").mkdir()
        (self.job_dir / "stems").mkdir()

        self.job = RecordingJobService()
        self.job.manifest = types.SimpleNamespace(
            source=types.SimpleNamespace(
                type=SourceType.upload,
                url=None,
                filename=str(self.root / "upload.wav"),
            ),
            selected_stems=["vocals"],
        )
        import soundfile as sf

        sf.write(self.root / "upload.wav", np.zeros(4410), 44100)

        self.ffmpeg = StubFFmpeg()
        self.svc = PipelineService.__new__(PipelineService)
        self.svc.job_service = self.job
        self.svc.job_store = types.SimpleNamespace(ensure_job_dir=lambda _jid: self.job_dir)
        self.svc.ffmpeg = self.ffmpeg

        # Cancellation is not part of this suite: the probe stays False.
        self._cancelled_patch = mock.patch.object(
            cancellation_service, "is_cancelled", return_value=False,
        )
        self._cancelled_patch.start()
        self.addCleanup(self._cancelled_patch.stop)

    def run_pipeline(self, plugin) -> None:
        with mock.patch.object(plugin_registry, "require", return_value=plugin):
            self.svc.run("job-1")

    def assert_failed_cleanly(self, message_fragment: str) -> None:
        failed = [m for kind, m in self.job.calls if kind == "mark_failed"]
        self.assertEqual(len(failed), 1, f"mark_failed calls: {failed}")
        self.assertIn(message_fragment, failed[0])
        self.assertNotIn("mark_done", [k for k, _ in self.job.calls])
        log = (self.job_dir / "logs" / "pipeline.log").read_text(encoding="utf-8")
        self.assertIn("Pipeline error", log)
        # Cleanup contract: transient outputs purged, expensive stems kept.
        self.assertFalse((self.job_dir / "analysis").exists(),
                         "analysis must be cleaned up on failure")
        self.assertTrue((self.job_dir / "stems").exists(),
                        "stems must be preserved on failure")

    def test_plugin_failure_routes_to_mark_failed(self) -> None:
        self.run_pipeline(FailOnRunPlugin(RuntimeError("broken separator")))

        updates = self.job.job_updates()
        statuses = [u["status"] for u in updates]
        # The separating span ran (checkpoint + mid-run tick at 0.68)...
        self.assertIn(JobStatus.separating, statuses)
        self.assertIn(0.68, [u["progress"] for u in updates])
        # ...and nothing after it started; the failure is the terminal state.
        for phase in NEVER_PHASES:
            self.assertNotIn(phase, statuses, f"phase {phase} must not start after a failure")
        self.assertEqual(statuses[-1], JobStatus.separating)

        self.assert_failed_cleanly("Pipeline failed: broken separator")

    def test_missing_tool_error_gets_the_install_hint(self) -> None:
        # The friendly-message special case: 'Required command not found' is
        # augmented with install guidance so the user can act on it.
        self.run_pipeline(FailOnRunPlugin(RuntimeError("Required command not found: demucs")))
        self.assert_failed_cleanly("Install the missing tool and ensure it is on PATH.")

    def test_failure_in_postprocessing_blocks_analysis(self) -> None:
        # Separation succeeds; the ffmpeg mixdown (postprocessing) crashes.
        # The job must fail at postprocessing — analyzing/packaging never run.
        self.ffmpeg.mix_error = RuntimeError("mixdown exploded")
        self.run_pipeline(CompletesPlugin())

        updates = self.job.job_updates()
        statuses = [u["status"] for u in updates]
        self.assertIn(JobStatus.postprocessing, statuses)
        for phase in NEVER_PHASES:
            self.assertNotIn(phase, statuses, f"phase {phase} must not start after a failure")
        self.assertEqual(statuses[-1], JobStatus.postprocessing)

        self.assert_failed_cleanly("Pipeline failed: mixdown exploded")


if __name__ == "__main__":
    unittest.main()
