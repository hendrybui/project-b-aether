"""
Runs the jsdom unit tests for static/score_preview.js via Node's test runner.

The JS tests live in tests/js/score_preview.test.js (run `npm test` there
directly). This wrapper makes them part of `pytest` so the whole suite runs
in CI with one command. It skips cleanly when node or jsdom isn't installed:

    cd tests/js && npm install
"""

import pathlib
import shutil
import subprocess

import pytest

JS_DIR = pathlib.Path(__file__).parent / 'js'
TEST_FILE = JS_DIR / 'score_preview.test.js'


def test_score_preview_js():
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed; JS preview tests skipped')
    if not (JS_DIR / 'node_modules' / 'jsdom').exists():
        pytest.skip(
            'jsdom not installed for JS preview tests; '
            'run: cd tests/js && npm install')
    result = subprocess.run(
        [node, '--test', str(TEST_FILE)],
        cwd=JS_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f'score_preview.js JS tests failed (exit {result.returncode})\n'
        f'{result.stdout}\n{result.stderr}'
    )
