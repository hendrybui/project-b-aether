"""Host-side unit tests for the htdemucs plugin's pure internals.

Covers the pieces that previously only ran inside the real pipeline (or on the
GPU): ``_parse_worker_stats`` (the Ready/Done JSONL timing parser that feeds
per-job diagnostics), ``_read_terminal_status`` (the last-status marker the
pool path waits on), and ``_load_pool_stats`` / ``_persist_pool_stats`` (the
cumulative history that must survive server restarts).

No GPU, docker, or running server is needed: the plugin is constructed via
``__new__`` (skipping ``__init__``'s adapter wiring) with only the attributes
the methods under test touch, and the module-level ``JOBS_DIR`` is patched to
a temp dir so persistence tests never read or write the real library.

Run from the repo root via the node test wrapper (``tests/plugin-units.test.mjs``),
or directly::

    cd audiomass/backend && PYTHONPATH=. ../.venv/bin/python -m unittest discover -s ../tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import plugins.htdemucs_plugin as htdemucs_plugin
from plugins.htdemucs_plugin import HTDemucsPlugin

IMAGE = "rocm64_gfx803_demucs:2.4"

# The exact formats optimized_demucs.py emits (%.1fs, %.2fx realtime), which
# the plugin's regexes must keep matching.
READY_LINE = '{"log": "Ready in 24.6s (device=cuda)."}'
DONE_LINE = '{"log": "Done in 3.2s (audio: 30.0s, 0.11x realtime)"}'


def make_plugin(**overrides) -> HTDemucsPlugin:
    """A plugin with only the attributes the pure methods touch.

    ``__new__`` skips ``__init__`` entirely, so no adapters, torch, or docker
    are constructed; tests set the handful of fields each method reads.
    """
    plugin = HTDemucsPlugin.__new__(HTDemucsPlugin)
    plugin.docker = types.SimpleNamespace(image=IMAGE)
    defaults = {
        "pool_busy": False,
        "pool_jobs_served": 0,
        "pool_ready_sec": None,
        "pool_started_at": None,
        "pool_last_stats": None,
        "pool_last_activity_at": None,
        "pool_first_seen_at": None,
    }
    for key, value in {**defaults, **overrides}.items():
        setattr(plugin, key, value)
    return plugin


def write_progress(lines: list[str]) -> Path:
    """Write JSONL fixture lines to a temp file (auto-removed)."""
    fd, name = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    path = Path(name)
    return path


class ParseWorkerStatsTest(unittest.TestCase):
    """_parse_worker_stats: JSONL Ready/Done timings -> the stats dict."""

    def test_full_stats_parses_every_field(self) -> None:
        path = write_progress([
            '{"log": "Loading separator..."}',
            READY_LINE,
            '{"done": 1, "total": 4}',
            '{"done": 4, "total": 4}',
            DONE_LINE,
            '{"status": "done"}',
        ])
        self.addCleanup(os.unlink, path)
        stats = make_plugin()._parse_worker_stats(path, wall_sec=5.14)
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats["image"], IMAGE)
        self.assertEqual(stats["wall_sec"], 5.1)          # round(5.14, 1)
        self.assertEqual(stats["ready_sec"], 24.6)
        self.assertEqual(stats["compute_sec"], 3.2)
        self.assertEqual(stats["overhead_sec"], 1.9)      # wall 5.14 - compute 3.2
        self.assertEqual(stats["audio_sec"], 30.0)
        self.assertEqual(stats["realtime"], 0.11)
        # 'at' is a UTC ISO timestamp that survives round-tripping to JSON.
        parsed = datetime.fromisoformat(stats["at"])
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_no_done_marker_returns_none(self) -> None:
        # A cancelled/failed job never reaches the 'Done in' line — nothing to
        # report, so the caller must see None (not zeros).
        path = write_progress([READY_LINE, '{"status": "cancelled"}'])
        self.addCleanup(os.unlink, path)
        self.assertIsNone(make_plugin()._parse_worker_stats(path, wall_sec=60.0))

    def test_missing_progress_file_returns_none(self) -> None:
        self.assertIsNone(make_plugin()._parse_worker_stats(Path("/nonexistent/progress.jsonl"), 1.0))

    def test_missing_ready_defaults_zero(self) -> None:
        path = write_progress([DONE_LINE])
        self.addCleanup(os.unlink, path)
        stats = make_plugin()._parse_worker_stats(path, wall_sec=4.0)
        assert stats is not None
        self.assertEqual(stats["ready_sec"], 0.0)

    def test_garbage_and_progress_lines_are_ignored(self) -> None:
        # Non-JSON noise (e.g. a line the worker was mid-write of), entries
        # without log text, and partial-write tails must not break the parse.
        path = write_progress([
            'this is not json {',
            '{"foo": 1, "bar": 2}',
            READY_LINE,
            '{"done": 2, "total": 4}',
            DONE_LINE,
            '{"log": "Done in 3.2s (audio: 30',  # partial-write tail
        ])
        self.addCleanup(os.unlink, path)
        stats = make_plugin()._parse_worker_stats(path, wall_sec=3.0)
        assert stats is not None
        self.assertEqual(stats["ready_sec"], 24.6)
        self.assertEqual(stats["compute_sec"], 3.2)

    def test_done_line_without_audio_suffix_is_not_a_done_marker(self) -> None:
        # The regex requires the full '(audio: Xs, Yx realtime)' suffix the
        # worker always writes; a bare 'Done in' is not our marker.
        path = write_progress(['{"log": "Done in 3.2s"}'])
        self.addCleanup(os.unlink, path)
        self.assertIsNone(make_plugin()._parse_worker_stats(path, wall_sec=3.0))


class ReadTerminalStatusTest(unittest.TestCase):
    """_read_terminal_status: the LAST done/error/cancelled marker wins."""

    def test_last_terminal_marker_wins(self) -> None:
        path = write_progress([
            '{"status": "error"}',
            '{"done": 4, "total": 4}',
            '{"status": "done"}',
        ])
        self.addCleanup(os.unlink, path)
        self.assertEqual(HTDemucsPlugin._read_terminal_status(path), "done")

    def test_non_terminal_statuses_are_ignored(self) -> None:
        path = write_progress([
            '{"status": "starting"}',
            '{"done": 1, "total": 4}',
        ])
        self.addCleanup(os.unlink, path)
        self.assertIsNone(HTDemucsPlugin._read_terminal_status(path))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(HTDemucsPlugin._read_terminal_status(Path("/nonexistent/progress.jsonl")))

    def test_malformed_lines_are_skipped(self) -> None:
        path = write_progress([
            "not json",
            '{"status": "cancelled"}',
            '{"status": "don',
        ])
        self.addCleanup(os.unlink, path)
        self.assertEqual(HTDemucsPlugin._read_terminal_status(path), "cancelled")


class PoolStatsPersistenceTest(unittest.TestCase):
    """_load_pool_stats / _persist_pool_stats: history across restarts."""

    LAST_JOB = {
        "wall_sec": 1.8,
        "ready_sec": 24.6,
        "compute_sec": 1.5,
        "overhead_sec": 0.3,
        "audio_sec": 8.0,
        "realtime": 0.19,
        "image": IMAGE,
        "at": "2026-08-11T10:01:00+00:00",
    }

    def test_persist_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp)
            with mock.patch.object(htdemucs_plugin, "JOBS_DIR", jobs):
                writer = make_plugin(
                    pool_jobs_served=2,
                    pool_last_stats=self.LAST_JOB,
                    pool_first_seen_at="2026-08-11T10:00:00+00:00",
                    pool_last_activity_at="2026-08-11T10:01:00+00:00",
                )
                writer._persist_pool_stats()

                stats_file = jobs / "_pool" / "stats.json"
                self.assertTrue(stats_file.exists(), "stats.json should be written to the pool dir")
                payload = json.loads(stats_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["jobs_served_total"], 2)
                self.assertEqual(payload["last_job"], self.LAST_JOB)
                self.assertEqual(payload["first_seen_at"], "2026-08-11T10:00:00+00:00")

                # A fresh instance (as after a server restart) restores every
                # field from disk.
                reader = make_plugin()
                reader._load_pool_stats()
                self.assertEqual(reader.pool_jobs_served, 2)
                self.assertEqual(reader.pool_last_stats, self.LAST_JOB)
                self.assertEqual(reader.pool_first_seen_at, "2026-08-11T10:00:00+00:00")
                self.assertEqual(reader.pool_last_activity_at, "2026-08-11T10:01:00+00:00")

    def test_persist_is_atomic(self) -> None:
        # tmp + rename: no .tmp leftover, so a crash mid-write can never tear
        # the committed file.
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp)
            with mock.patch.object(htdemucs_plugin, "JOBS_DIR", jobs):
                writer = make_plugin(pool_jobs_served=1, pool_last_stats=self.LAST_JOB)
                writer._persist_pool_stats()
                self.assertFalse((jobs / "_pool" / "stats.json.tmp").exists())
                json.loads((jobs / "_pool" / "stats.json").read_text(encoding="utf-8"))

    def test_load_missing_file_keeps_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(htdemucs_plugin, "JOBS_DIR", Path(tmp)):
                reader = make_plugin()
                reader._load_pool_stats()
                self.assertEqual(reader.pool_jobs_served, 0)
                self.assertIsNone(reader.pool_last_stats)
                self.assertIsNone(reader.pool_first_seen_at)
                self.assertIsNone(reader.pool_last_activity_at)

    def test_load_corrupt_json_keeps_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "_pool"
            pool.mkdir()
            (pool / "stats.json").write_text("not json {{", encoding="utf-8")
            with mock.patch.object(htdemucs_plugin, "JOBS_DIR", Path(tmp)):
                reader = make_plugin()
                reader._load_pool_stats()  # must not raise
                self.assertEqual(reader.pool_jobs_served, 0)

    def test_load_bad_types_keeps_defaults(self) -> None:
        # Valid JSON with the wrong shapes (a hand-edited or foreign file):
        # boot must survive it rather than crash on int('oops').
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "_pool"
            pool.mkdir()
            (pool / "stats.json").write_text(
                json.dumps({"jobs_served_total": "oops", "last_job": "nope"}), encoding="utf-8",
            )
            with mock.patch.object(htdemucs_plugin, "JOBS_DIR", Path(tmp)):
                reader = make_plugin()
                reader._load_pool_stats()  # must not raise
                self.assertEqual(reader.pool_jobs_served, 0)
                self.assertIsNone(reader.pool_last_stats)


if __name__ == "__main__":
    unittest.main()
