// Thin wrapper running the AudioMass backend's host-side Python unit tests
// (audiomass/tests/test_htdemucs_plugin.py) inside the node test runner, so
// `npm test` covers the htdemucs plugin's pure logic — stats parsing, pool
// stats load/persist — without a GPU, docker, or a running server. The venv
// python and PYTHONPATH conventions mirror the other AudioMass scenarios
// (see audiomass-cancel.test.mjs).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const AM_PYTHON =
  process.env.AUDIOMASS_PYTHON ?? path.join(ROOT, 'audiomass', '.venv', 'bin', 'python')
const BACKEND_DIR = path.join(ROOT, 'audiomass', 'backend')
const TESTS_DIR = path.join(ROOT, 'audiomass', 'tests')

test('htdemucs plugin unit tests pass host-side (no GPU needed)', () => {
  if (!fs.existsSync(AM_PYTHON)) {
    throw new Error(`AudioMass venv python not found at ${AM_PYTHON} (install audiomass/.venv)`)
  }
  const run = spawnSync(
    AM_PYTHON,
    ['-m', 'unittest', 'discover', '-s', TESTS_DIR, '-p', 'test_*.py', '-v'],
    {
      cwd: BACKEND_DIR,
      env: { ...process.env, PYTHONPATH: BACKEND_DIR },
      encoding: 'utf8',
      timeout: 120000,
    },
  )
  const output = `${run.stdout ?? ''}${run.stderr ?? ''}`
  if (run.error) {
    throw new Error(`python -m unittest failed to run: ${run.error.message}\n${output}`)
  }
  if (run.status !== 0) {
    throw new Error(`python unit tests FAILED (exit ${run.status}):\n${output}`)
  }
  // unittest exits 0 even with zero discovered tests — the summary line must
  // show a real run so a silently-empty discovery fails loudly.
  const match = output.match(/Ran (\d+) tests?/)
  assert.ok(match, `no 'Ran N tests' summary in unittest output:\n${output}`)
  const ran = Number(match[1])
  assert.ok(ran >= 25, `expected >= 25 python unit tests, discovery ran ${ran}`)
  assert.ok(/OK\b/.test(output), `unittest summary should be OK:\n${output}`)
})
