import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { makeSineWav, pollUntil } from './helpers.mjs'

// Integration tests for the warm-pool supervisor (backend/adapters/
// demucs_pool_server.py) driving the real protocol the plugin uses, in CPU
// mode on the host (no docker needed):
//
//   scenario 1 (warm reuse):  two jobs served by ONE supervisor process with
//                             a single model load ("Warm pool ready" written
//                             exactly once) and correct stems per job.
//   scenario 2 (cancel):      cancel a mid-run job -> lands in 'cancelled',
//                             the supervisor stays alive, and the same warm
//                             pool serves the next job to completion.
//
// The protocol is the plugin's actual file contract: request.json (atomic
// rename) in, the worker's JSONL progress protocol ({"status": ...} markers)
// out, plus a heartbeat file the server (here: this test) must keep fresh.

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const POOL_SERVER = path.join(ROOT, 'audiomass', 'backend', 'adapters', 'demucs_pool_server.py')
const AM_PYTHON =
  process.env.AUDIOMASS_PYTHON ?? path.join(ROOT, 'audiomass', '.venv', 'bin', 'python')

const pools = [] // { proc, workDir } for every supervisor this run spawned
let heartbeatTimer = null

function startHeartbeat(poolDir) {
  // One live interval at a time: a second startPool (second test) must not
  // leave the first test's interval running — an uncleared interval keeps
  // the node event loop alive and hangs the whole test run after both
  // tests have already passed.
  if (heartbeatTimer) clearInterval(heartbeatTimer)
  const beat = () => {
    try { fs.writeFileSync(path.join(poolDir, 'heartbeat'), String(Date.now() / 1000)) } catch { /* gone */ }
  }
  beat()
  heartbeatTimer = setInterval(beat, 3000)
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
}

async function startPool(extraEnv = {}) {
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'am-pool-'))
  const poolDir = path.join(workDir, '_pool')
  fs.mkdirSync(poolDir, { recursive: true })
  startHeartbeat(poolDir)
  const poolProc = spawn(AM_PYTHON, [POOL_SERVER], {
    env: {
      ...process.env,
      AUDIOMASS_POOL_DIR: poolDir,
      AUDIOMASS_POOL_DEVICE: 'cpu',
      AUDIOMASS_POOL_THREADS: '2',
      ...extraEnv,
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  })
  poolProc.stderr.on('data', (d) => process.stderr.write(d))
  pools.push({ proc: poolProc, workDir })
  await pollUntil(
    () => (fs.existsSync(path.join(poolDir, 'ready')) ? true : null),
    150000,
    'pool ready (model loaded)',
  )
  return { poolDir, poolProc, workDir }
}

async function dispatchJob(workDir, poolDir, jobId, wav, outDir) {
  fs.mkdirSync(outDir, { recursive: true })
  const progress = path.join(workDir, `${jobId}-progress.jsonl`)
  const spec = { job_id: jobId, input: wav, out_dir: outDir, progress_path: progress }
  const tmp = path.join(poolDir, 'request.json.tmp')
  fs.writeFileSync(tmp, JSON.stringify(spec))
  fs.renameSync(tmp, path.join(poolDir, 'request.json')) // atomic, like the plugin
  return progress
}

function readJsonl(file) {
  try {
    return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l))
  } catch {
    return []
  }
}

function statusOf(file) {
  for (const e of readJsonl(file)) if (e.status) return e.status
  return null
}

after(() => {
  // Tear down EVERY supervisor this run spawned. The runner only exits once
  // each child's stdio pipe closes, so an un-killed supervisor hangs the
  // whole suite — with two tests each spawning their own pool, the module-
  // level single reference used to leave the first one alive forever.
  stopHeartbeat()
  for (const { proc, workDir } of pools) {
    try {
      fs.writeFileSync(path.join(workDir, '_pool', 'shutdown'), 'bye')
    } catch { /* already gone */ }
    proc.kill()
    try { fs.rmSync(workDir, { recursive: true, force: true }) } catch { /* gone */ }
  }
  pools.length = 0
})

test('warm pool: one loaded model serves two jobs back-to-back', async () => {
  const { poolDir, poolProc, workDir } = await startPool()
  const wavA = path.join(workDir, 'a.wav')
  const wavB = path.join(workDir, 'b.wav')
  makeSineWav(wavA, 8)
  makeSineWav(wavB, 8)
  const outA = path.join(workDir, 'out-a')
  const outB = path.join(workDir, 'out-b')

  const progressA = await dispatchJob(workDir, poolDir, 'job-a', wavA, outA)
  await pollUntil(() => (statusOf(progressA) === 'done' ? 'done' : null), 120000, 'job-a done')
  assert.ok(fs.existsSync(path.join(outA, 'vocals.wav')), 'job-a stems written')

  const progressB = await dispatchJob(workDir, poolDir, 'job-b', wavB, outB)
  await pollUntil(() => (statusOf(progressB) === 'done' ? 'done' : null), 120000, 'job-b done')
  assert.ok(fs.existsSync(path.join(outB, 'vocals.wav')), 'job-b stems written')

  // The point of the pool: the model was loaded exactly once for both jobs,
  // and the supervisor is still serving.
  const poolLog = fs.readFileSync(path.join(poolDir, 'pool.log'), 'utf8')
  assert.equal(
    (poolLog.match(/Warm pool ready/g) || []).length,
    1,
    `model should load exactly once, pool.log: ${poolLog.trim()}`,
  )
  assert.ok(fs.existsSync(path.join(poolDir, 'ready')), 'pool still ready after job-b')
  assert.equal(poolProc.exitCode, null, 'supervisor should still be running')
  for (const progress of [progressA, progressB]) {
    assert.ok(
      readJsonl(progress).some((e) => typeof e.log === 'string' && e.log.includes('Done in')),
      'job progress should carry the worker Done-in line',
    )
  }
})

test('warm pool: cancel lands cancelled and the pool stays warm for the next job', async () => {
  const { poolDir, poolProc, workDir } = await startPool()
  const long = path.join(workDir, 'long.wav')
  makeSineWav(long, 16)
  const outC = path.join(workDir, 'out-c')
  const progressC = await dispatchJob(workDir, poolDir, 'job-c', long, outC)

  // Wait until the supervisor is genuinely mid-run (first chunk reported),
  // then cancel via the marker file — exactly what the plugin does.
  await pollUntil(
    () => (readJsonl(progressC).some((e) => typeof e.done === 'number') ? true : null),
    90000,
    'first chunk progress',
  )
  fs.writeFileSync(path.join(poolDir, 'cancel_job-c'), 'cancel')
  await pollUntil(() => (statusOf(progressC) === 'cancelled' ? 'cancelled' : null),
    30000, 'cancelled marker')

  assert.equal(poolProc.exitCode, null, 'supervisor must survive the cancel')
  assert.ok(fs.existsSync(path.join(poolDir, 'ready')), 'pool ready after the cancel')

  // The same warm pool serves the next job to completion.
  const wavD = path.join(workDir, 'd.wav')
  makeSineWav(wavD, 8)
  const outD = path.join(workDir, 'out-d')
  const progressD = await dispatchJob(workDir, poolDir, 'job-d', wavD, outD)
  await pollUntil(() => (statusOf(progressD) === 'done' ? 'done' : null), 120000, 'job-d done')
  assert.ok(fs.existsSync(path.join(outD, 'vocals.wav')), 'job-d stems written')

  const poolLog = fs.readFileSync(path.join(poolDir, 'pool.log'), 'utf8')
  assert.equal(
    (poolLog.match(/Warm pool ready/g) || []).length,
    1,
    'cancel must not re-load the model (still exactly one ready)',
  )
})

test('warm pool: idle eviction releases the pool after the idle window', async () => {
  // A 2s idle window and no jobs: the supervisor must exit on its own
  // (releasing the GPU) instead of sitting warm forever, and record the
  // reason in the evicted marker the plugin surfaces in diagnostics.
  const { poolDir, poolProc } = await startPool({ AUDIOMASS_POOL_IDLE_TIMEOUT: '2' })
  await pollUntil(
    () => (poolProc.exitCode !== null ? true : null),
    30000,
    'supervisor exits on idle',
  )
  assert.equal(poolProc.exitCode, 0, 'idle eviction should be a clean exit')
  assert.equal(
    fs.readFileSync(path.join(poolDir, 'evicted'), 'utf8'),
    'idle',
    'evicted marker should record the idle reason',
  )
  assert.ok(
    !fs.existsSync(path.join(poolDir, 'ready')),
    'ready marker must be removed when the pool exits',
  )
  const poolLog = fs.readFileSync(path.join(poolDir, 'pool.log'), 'utf8')
  assert.match(poolLog, /idle eviction/, 'pool.log should explain the eviction')
})
