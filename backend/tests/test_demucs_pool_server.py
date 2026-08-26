"""Host-side unit tests for the warm-pool supervisor's stale-marker cleanup.

Regression tests for the bug where a stale ``shutdown`` marker (written by the
server's graceful shutdown handler into the persistent pool dir) aborted the
next container generation's model load at boot, silently falling every later
separation back to the local CPU worker. The fix: the supervisor clears
leftover ready/evicted/shutdown markers on every boot via
``clear_stale_markers``.

Run via::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adapters.demucs_pool_server import clear_stale_markers


class ClearStaleMarkersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool_dir = Path(self.tmp.name)

    def _mark(self, name: str, content: str = "x") -> Path:
        path = self.pool_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_removes_all_three_stale_markers(self) -> None:
        # The exact leftovers a real pool dir accumulates: a ready marker from
        # the previous generation, an eviction reason, and the shutdown marker
        # the server's graceful handler wrote (the regression case).
        self._mark("ready", "cuda ready=39.6s")
        self._mark("evicted", "idle")
        self._mark("shutdown", "server graceful shutdown")

        clear_stale_markers(self.pool_dir)

        for name in ("ready", "evicted", "shutdown"):
            self.assertFalse((self.pool_dir / name).exists(), f"{name} not cleared")

    def test_leaves_live_protocol_files_untouched(self) -> None:
        # heartbeat / request.json are live protocol files, not generation
        # markers — the cleanup must never be widened to remove them.
        self._mark("heartbeat", "1.0")
        self._mark("request.json", '{"job_id": "abc"}')
        self._mark("shutdown", "server graceful shutdown")

        clear_stale_markers(self.pool_dir)

        self.assertTrue((self.pool_dir / "heartbeat").exists())
        self.assertTrue((self.pool_dir / "request.json").exists())
        self.assertFalse((self.pool_dir / "shutdown").exists())

    def test_clean_pool_dir_is_a_noop(self) -> None:
        clear_stale_markers(self.pool_dir)  # must not raise
        self.assertEqual(list(self.pool_dir.iterdir()), [])

    def test_rogue_directory_named_shutdown_is_tolerated(self) -> None:
        # A directory squatting on the marker name must not crash the boot —
        # the unlink OSError is swallowed exactly like every other marker path.
        (self.pool_dir / "shutdown").mkdir()
        clear_stale_markers(self.pool_dir)  # must not raise
        self.assertTrue((self.pool_dir / "shutdown").is_dir())


if __name__ == "__main__":
    unittest.main()
