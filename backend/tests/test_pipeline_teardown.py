"""Host-side tests for the pipeline's finally-block teardown guarantees.

run()'s finally must ALWAYS run both teardown steps — ``_detach_log_handler``
and ``cancellation_service.clear(job_id)`` — even when the terminal-state
write itself (mark_cancelled / mark_failed) raises and that exception
propagates out of run(). A leaking teardown would accumulate FileHandlers on
the process-wide 'audiomass' logger (duplicated log lines) and leave the
cancellation registry holding a dead job id.

Run via the node test wrapper (``tests/plugin-units.test.mjs``) or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from domain.enums import SourceType
from plugins.base import CancelledError
from plugins.registry import plugin_registry
from services.cancellation_service import cancellation_service
from services.pipeline_service import PipelineService

AUDIOMASS_LOGGER = logging.getLogger("audiomass")


class RecordingJobService:
    def __init__(self) -> None:
        self.manifest = None

    def get_manifest(self, _job_id: str) -> object:
        return self.manifest

    def update_job(self, job_id: str, **kwargs) -> None:
        pass

    def update_manifest_files(self, job_id: str, files: dict) -> None:
        pass

    def mark_cancelled(self, job_id: str, message: str) -> None:
        raise RuntimeError("cancelled-persist exploded")

    def mark_failed(self, job_id: str, message: str) -> None:
        raise RuntimeError("failed-persist exploded")

    def mark_done(self, job_id: str) -> None:
        pass


class CancelPlugin:
    def run(self, ctx) -> dict:
        raise CancelledError()


class CrashPlugin:
    def run(self, ctx) -> dict:
        raise RuntimeError("separator exploded")


class PipelineTeardownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_dir = self.root / "job-1"
        (self.job_dir / "source").mkdir(parents=True)

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

        self.svc = PipelineService.__new__(PipelineService)
        self.svc.job_service = self.job
        self.svc.job_store = types.SimpleNamespace(ensure_job_dir=lambda _jid: self.job_dir)
        self.svc.ffmpeg = types.SimpleNamespace(
            transcode_to_wav=lambda *_a, **_k: None,
            mix_wavs=lambda *_a, **_k: None,
            copy_audio=lambda *_a, **_k: None,
        )
        self._cancelled_patch = mock.patch.object(
            cancellation_service, "is_cancelled", return_value=False,
        )
        self._cancelled_patch.start()
        self.addCleanup(self._cancelled_patch.stop)

    def run_pipeline(self, plugin) -> Exception:
        """Run the pipeline expecting the terminal-state write to raise."""
        with mock.patch.object(plugin_registry, "require", return_value=plugin):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.run("job-1")
        return cm.exception

    def assert_teardown_ran(self, propagated: Exception, reason: str) -> None:
        # The exception that escaped is the terminal-write failure, not a
        # teardown failure (which would have replaced it).
        self.assertIn(reason, str(propagated))
        # cancellation_service.clear ran exactly once with the job id.
        self._clear_mock.assert_called_once_with("job-1")
        # _detach_log_handler ran, and received the attached handler.
        self._detach_mock.assert_called_once()
        self.assertIsNotNone(self._detach_mock.call_args.args[0])
        # No handler leak on the process-wide logger: run() attached one
        # handler and must have removed it despite the exception.
        self.assertEqual(len(AUDIOMASS_LOGGER.handlers), self._handlers_before,
                         "pipeline handler leaked on the audiomass logger after teardown")

    def test_mark_cancelled_raising_still_runs_finally(self) -> None:
        self.start_teardown_spies()
        exc = self.run_pipeline(CancelPlugin())
        self.assert_teardown_ran(exc, "cancelled-persist exploded")

    def test_mark_failed_raising_still_runs_finally(self) -> None:
        self.start_teardown_spies()
        exc = self.run_pipeline(CrashPlugin())
        self.assert_teardown_ran(exc, "failed-persist exploded")

    def start_teardown_spies(self) -> None:
        """Patch the teardown targets; keep the mock objects (not the patches)."""
        clear_patch = mock.patch.object(cancellation_service, "clear")
        self._clear_mock = clear_patch.start()
        self.addCleanup(clear_patch.stop)
        detach_patch = mock.patch.object(
            self.svc, "_detach_log_handler", wraps=self.svc._detach_log_handler,
        )
        self._detach_mock = detach_patch.start()
        self.addCleanup(detach_patch.stop)
        self._handlers_before = len(AUDIOMASS_LOGGER.handlers)


if __name__ == "__main__":
    unittest.main()
