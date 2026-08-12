import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Smoke tests for the AudioMass stem-separation bridge, driving the exact
// HTTP contract stems.js uses (/api/jobs/upload, the SSE /events stream,
// /api/jobs/{id}/cancel, /api/jobs/{id}, /api/jobs/{id}/manifest):
//
//   scenario 1 (cancel):   upload -> let HTDemucs get genuinely mid-run ->
//                          cancel -> job lands in 'cancelled' and the demucs
//                          worker process is actually gone (no CPU leak).
//   scenario 2 (success):  upload a short file -> run to completion -> job
//                          lands in 'done' with all 6 stems + mix + original
//                          in the manifest and on disk.
//   scenario 3 (recovery): upload -> mid-run -> hard-kill the server (crash
//                          simulation, manifest dropped to cover the
//                          snapshot-without-manifest case) -> restart on the
//                          same jobs dir -> the stranded job is recovered to
//                          'failed' and is deletable.
//
// The server is spawned on its own port with an isolated AUDIOMASS_JOBS_DIR,
// so the user's running instance (usually :5055) and its real job library are
// never touched. The active-job guard is in-memory per process, so a second
// instance cannot conflict with the first.
import {
  drainSse, findChildren, getFreePort, makeSineWav, pollUntil, waitForServer,
} from './helpers.mjs'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const AM_SERVER = path.join(ROOT, 'audiomass', 'src', 'audiomass-server.py')
const AM_PYTHON =
  process.env.AUDIOMASS_PYTHON ?? path.join(ROOT, 'audiomass', '.venv', 'bin', 'python')
const STEM_NAMES = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other']
// 20s = 3 HTDemucs chunks (~7.8s each) — plenty of cancel window.
const CANCEL_WAV_SECONDS = 20
// 12s = 2 chunks — short enough to finish quickly, long enough to exercise
// the multi-chunk padded path to completion.
const COMPLETE_WAV_SECONDS = 12

let serverProc = null
let jobsDir = null
let baseUrl = ''
const serverOutput = []

function dumpServerOutput() {
  const tail = serverOutput.join('').split('\n').filter(Boolean).slice(-25).join('\n')
  console.error(`--- audiomass server output (tail) ---\n${tail}\n---------------------------------------`)
}

// Boot a fresh server on a free port against the (shared, isolated) jobs dir.
// Called once in before(), and again by the recovery scenario after it kills
// the first instance.
async function spawnServer() {
  const port = await getFreePort()
  baseUrl = `http://127.0.0.1:${port}`
  serverProc = spawn(AM_PYTHON, [AM_SERVER], {
    env: {
      ...process.env,
      AUDIOMASS_PORT: String(port),
      AUDIOMASS_JOBS_DIR: jobsDir,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  serverProc.stdout.on('data', (d) => serverOutput.push(d))
  serverProc.stderr.on('data', (d) => serverOutput.push(d))
  await waitForServer(`${baseUrl}/api/health`, 90000)
}

async function getJob(jobId) {
  return (await fetch(`${baseUrl}/api/jobs/${jobId}`)).json()
}

async function uploadWav(filename) {
  const wavBytes = fs.readFileSync(path.join(jobsDir, filename))
  const form = new FormData()
  form.append('file', new Blob([wavBytes], { type: 'audio/wav' }), filename)
  // NOTE: the assert message is evaluated eagerly, so reading upRes.text()
  // there would consume the body before .json() below.
  const res = await fetch(`${baseUrl}/api/jobs/upload`, { method: 'POST', body: form })
  assert.equal(res.status, 201, 'upload should return 201')
  const created = await res.json()
  assert.ok(created.job_id, 'upload response should include a job_id')
  return created
}

before(async () => {
  if (!fs.existsSync(AM_PYTHON)) {
    throw new Error(`AudioMass venv python not found at ${AM_PYTHON} (install audiomass/.venv)`)
  }
  const ff = spawn('ffmpeg', ['-version'], { stdio: 'ignore' })
  await new Promise((r) => ff.on('exit', r))
  if (ff.exitCode !== 0) {
    throw new Error('ffmpeg not found on PATH — required by the AudioMass server')
  }

  jobsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audiomass-smoke-'))

  // Short real WAVs (sine, 44.1kHz stereo). Generated with ffmpeg so uploads
  // go through the same validation path as a user's file.
  makeSineWav(path.join(jobsDir, 'smoke-cancel.wav'), CANCEL_WAV_SECONDS)
  makeSineWav(path.join(jobsDir, 'smoke-complete.wav'), COMPLETE_WAV_SECONDS)

  await spawnServer()
})

after(() => {
  if (serverProc) serverProc.kill()
  if (jobsDir) fs.rmSync(jobsDir, { recursive: true, force: true })
})

test('AudioMass bridge: upload -> separate -> cancel lands the job in cancelled state', async () => {
  const sseEvents = []
  let jobId = null
  try {
    // 1) Upload — the same multipart POST stems.js issues. The pipeline
    //    starts in a daemon thread before create_job returns, so the response
    //    status may already have advanced past 'created' on a fast machine;
    //    the stable assertions are the job id and that it is not yet
    //    cancellable-free (i.e. non-terminal).
    const created = await uploadWav('smoke-cancel.wav')
    jobId = created.job_id
    assert.ok(
      ['created', 'validating_input', 'ingesting_source', 'transcoding'].includes(created.status),
      `unexpected early job status: ${created.status}`,
    )
    assert.equal(created.cancellable, true)

    // 2) Open the SSE progress stream — what stems.js's streamProgress() does.
    const sseRes = await fetch(`${baseUrl}/api/jobs/${jobId}/events`)
    assert.equal(sseRes.status, 200)
    drainSse(sseRes, sseEvents)

    // 3) Wait until separation is genuinely mid-run: at least one ~7.8s chunk
    //    has completed (progress = 0.55 + done/total * 0.25 -> >= 0.60).
    const midRun = await pollUntil(async () => {
      const j = await getJob(jobId)
      return j.status === 'separating' && j.progress >= 0.6 ? j : null
    }, 120000, 'separation to be mid-run (separating, progress >= 0.60)')
    assert.equal(midRun.status, 'separating')
    assert.ok(midRun.progress >= 0.6, `job should be mid-separation, got progress ${midRun.progress}`)
    assert.equal(midRun.cancellable, true, 'a running job must still be cancellable')

    // The demucs worker child process must actually be alive right now — this
    // both proves the identification logic works and makes the post-cancel
    // "gone" assertion meaningful.
    const workersBefore = findChildren(serverProc.pid, 'demucs_worker.py')
    assert.ok(workersBefore.length >= 1,
      `expected the demucs worker to be running mid-separation (server pid ${serverProc.pid}), found: ${workersBefore.join(', ') || 'none'}`)

    // The frontend's reload-resume endpoint reports the in-flight job.
    const activeRes = await fetch(`${baseUrl}/api/jobs/active`)
    assert.equal(activeRes.status, 200, 'GET /api/jobs/active should return the running job')
    assert.equal((await activeRes.json()).job_id, jobId)

    // 4) Cancel mid-run.
    const cancelRes = await fetch(`${baseUrl}/api/jobs/${jobId}/cancel`, { method: 'POST' })
    assert.equal(cancelRes.status, 200, `cancel should return 200, got ${cancelRes.status}`)
    assert.equal((await cancelRes.json()).status, 'cancel_requested')

    // 5) The job must land in the 'cancelled' terminal state — the regression
    //    this guards: cancellation used to be a no-op during the chunk loop,
    //    so a 20-minute separation could not be interrupted at all.
    const terminal = await pollUntil(async () => {
      const j = await getJob(jobId)
      return j.status === 'cancelled' ? j : null
    }, 30000, 'job to reach cancelled')
    assert.equal(terminal.status, 'cancelled')
    assert.equal(terminal.cancellable, false)
    assert.ok(/cancell/i.test(terminal.message), `message should mention cancellation, got: ${terminal.message}`)

    // No active job once the job is terminal: 200 with active:false — the
    // idle state is not an error (the Aether bridge polls this every 2s).
    const activeAfter = await fetch(`${baseUrl}/api/jobs/active`)
    assert.equal(activeAfter.status, 200, 'idle /api/jobs/active should be 200')
    assert.deepEqual(await activeAfter.json(), { active: false })

    // The regression this guards: a cancelled job must not keep its worker
    // alive burning CPU. mark_cancelled only runs after the pipeline
    // terminated AND reaped the worker, so it must be gone once the job shows
    // 'cancelled' — poll briefly for the reaping to settle.
    await pollUntil(() => {
      const w = findChildren(serverProc.pid, 'demucs_worker.py')
      return w.length === 0 ? w : null
    }, 5000, 'the demucs worker process to be gone after cancellation')
    assert.deepEqual(findChildren(serverProc.pid, 'demucs_worker.py'), [],
      'demucs worker still running after the job was cancelled')

    // 6) The SSE stream — the transport the AudioMass UI depends on — must
    //    have delivered the terminal job_cancelled event.
    await pollUntil(() => (sseEvents.some((e) => e.event === 'job_cancelled') ? sseEvents : null),
      15000, 'SSE job_cancelled event')
    assert.ok(
      sseEvents.some((e) => e.event === 'job_cancelled'),
      `SSE never delivered job_cancelled; got events: ${sseEvents.map((e) => e.event).join(', ')}`,
    )

    // 7) Terminal-state hygiene: double-cancel is rejected, DELETE cleans up.
    const again = await fetch(`${baseUrl}/api/jobs/${jobId}/cancel`, { method: 'POST' })
    assert.equal(again.status, 404)
    const del = await fetch(`${baseUrl}/api/jobs/${jobId}`, { method: 'DELETE' })
    assert.equal(del.status, 204)
  } catch (err) {
    dumpServerOutput()
    throw err
  }
})

test('AudioMass bridge: a short separation runs to completion with all 6 stems in the manifest', async () => {
  const sseEvents2 = []
  let jobId2 = null
  try {
    // Upload a fresh, short file (the cancel scenario's job was deleted).
    const created = await uploadWav('smoke-complete.wav')
    jobId2 = created.job_id

    const sseRes = await fetch(`${baseUrl}/api/jobs/${jobId2}/events`)
    assert.equal(sseRes.status, 200)
    drainSse(sseRes, sseEvents2)

    // Wait for the full pipeline: separating -> postprocessing -> analyzing
    // -> packaging -> done. Fail fast if the job errors instead of timing out.
    const seen = new Set()
    const terminal = await pollUntil(async () => {
      const j = await getJob(jobId2)
      if (j.status === 'failed') {
        throw new Error(`job failed during run: ${j.message}`)
      }
      if (j.status) seen.add(j.status)
      return j.status === 'done' ? j : null
    }, 240000, 'separation to complete (done)')

    assert.equal(terminal.status, 'done')
    assert.equal(terminal.progress, 1.0)
    assert.equal(terminal.cancellable, false)

    // The job genuinely executed the real pipeline, not a shortcut: the
    // long-lived phases (chunked separation, then analysis over the produced
    // stems) must both have been observed. Short phases like 'postprocessing'
    // (a ~1s ffmpeg mixdown) can fall between 500ms polls, so they aren't
    // required.
    for (const phase of ['separating', 'analyzing']) {
      assert.ok(seen.has(phase), `job never passed through phase '${phase}' (saw: ${[...seen].join(', ')})`)
    }

    // The SSE stream delivered the completion event stems.js finalizes on.
    await pollUntil(() => (sseEvents2.some((e) => e.event === 'job_done') ? sseEvents2 : null),
      15000, 'SSE job_done event')
    assert.ok(
      sseEvents2.some((e) => e.event === 'job_done'),
      `SSE never delivered job_done; got events: ${sseEvents2.map((e) => e.event).join(', ')}`,
    )

    // Manifest: all 6 stems + mix + original, present on disk.
    const manifest = await (await fetch(`${baseUrl}/api/jobs/${jobId2}/manifest`)).json()
    assert.equal(manifest.status, 'done')
    for (const name of STEM_NAMES) {
      assert.ok(manifest.available_stems.includes(name),
        `available_stems missing ${name}: ${manifest.available_stems}`)
      assert.ok(manifest.files[name], `manifest.files missing stem ${name}`)
      assert.ok(fs.existsSync(manifest.files[name]),
        `stem file ${name} missing on disk: ${manifest.files[name]}`)
    }
    for (const extra of ['mix', 'original']) {
      assert.ok(manifest.available_stems.includes(extra),
        `available_stems missing ${extra}: ${manifest.available_stems}`)
      assert.ok(manifest.files[extra] && fs.existsSync(manifest.files[extra]),
        `missing ${extra} in manifest.files`)
    }

    // Waveform summaries generated for the produced stems (the 'waveform'
    // plugin step of the pipeline).
    assert.ok(manifest.files.waveform_vocals && fs.existsSync(manifest.files.waveform_vocals),
      'waveform summary for vocals should exist on disk')

    // Analysis populated with the input's duration and per-stem energy.
    assert.ok(manifest.analysis, 'manifest should include analysis')
    assert.ok(
      Math.abs(manifest.analysis.duration_sec - COMPLETE_WAV_SECONDS) < 0.5,
      `analysis duration_sec ${manifest.analysis.duration_sec} should be ~${COMPLETE_WAV_SECONDS}`,
    )
    assert.ok(Object.keys(manifest.analysis.stem_energy || {}).length >= STEM_NAMES.length,
      `stem_energy should cover the stems, got: ${Object.keys(manifest.analysis.stem_energy || {}).join(', ')}`)

    // Stems are actually downloadable through the API.
    const stemRes = await fetch(`${baseUrl}/api/jobs/${jobId2}/stems/vocals?format=wav`)
    assert.equal(stemRes.status, 200)
    assert.match(stemRes.headers.get('content-type') || '', /audio\/wav/)
    const bytes = await stemRes.arrayBuffer()
    assert.ok(bytes.byteLength > 44, 'stem WAV should have a real body')

    // /api/diagnostics reports which separation engine the job used (the
    // Aether bridge reads this to show ROCm container vs CPU worker). This
    // suite's server runs without AUDIOMASS_DEMUCS_DOCKER_IMAGE, so it must
    // report the local CPU worker.
    const diag = await (await fetch(`${baseUrl}/api/diagnostics`)).json()
    assert.equal(diag.separation?.backend, 'cpu_worker',
      `diagnostics should report the active separation engine, got: ${diag.separation?.backend}`)
    assert.equal(diag.separation?.device, 'cpu')
    assert.equal(diag.separation?.container_available, false)
    // The warm-pool block is always present with a stable shape: no docker
    // image is configured in this suite, so the pool is up:false / 0 served.
    assert.ok(diag.separation?.warm_pool, 'separation should carry a warm_pool block')
    assert.equal(diag.separation.warm_pool.up, false,
      `pool must not report up without the container backend, got: ${diag.separation.warm_pool.up}`)
    assert.equal(diag.separation.warm_pool.jobs_served, 0)
    assert.equal(diag.separation.warm_pool.busy, false)
    // No container backend -> no idle-eviction config, no eviction state,
    // and no persisted pool history.
    assert.equal(diag.separation.warm_pool.idle_timeout_sec, null)
    assert.equal(diag.separation.warm_pool.eviction, null)
    assert.equal(diag.separation.warm_pool.evicted_at, null)
    assert.equal(diag.separation.warm_pool.first_seen_at, null)
    assert.equal(diag.separation.warm_pool.last_activity_at, null)
    assert.ok(diag.plugins.includes('htdemucs'),
      `plugins should list htdemucs, got: ${diag.plugins}`)

    // Cleanup: a done job is deletable.
    const del = await fetch(`${baseUrl}/api/jobs/${jobId2}`, { method: 'DELETE' })
    assert.equal(del.status, 204)
  } catch (err) {
    dumpServerOutput()
    throw err
  }
})

test('AudioMass bridge: a job stranded by a server crash is recovered to failed on restart', async () => {
  // This scenario must run last: it kills the server, then boots a fresh one.
  let jobId3 = null
  try {
    // Get a separation genuinely mid-run with its worker spawned.
    const created = await uploadWav('smoke-cancel.wav')
    jobId3 = created.job_id
    await pollUntil(async () => {
      const j = await getJob(jobId3)
      return j.status === 'separating' && findChildren(serverProc.pid, 'demucs_worker.py').length >= 1 ? j : null
    }, 120000, 'separation mid-run with the demucs worker spawned')

    // Simulate a crash: SIGKILL (no chance for any cleanup) and drop the
    // manifest, leaving the snapshot-only layout that recovery must tolerate
    // (a crash between the two persistence writes).
    serverProc.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 300))
    fs.rmSync(path.join(jobsDir, jobId3, 'manifest.json'))

    // Restart on the same jobs dir. If recovery crashes at boot (e.g. the
    // manifest-less path referencing an unimported SourceType), the server
    // never answers and waitForServer fails here.
    await spawnServer()

    // The stranded job must be recovered to a terminal, deletable state.
    const recovered = await pollUntil(async () => {
      const j = await getJob(jobId3)
      return j.status === 'failed' ? j : null
    }, 15000, 'stranded job to be recovered to failed')
    assert.match(recovered.message, /interrupted|restart/i,
      `recovery message should mention the interruption, got: ${recovered.message}`)
    assert.equal(recovered.cancellable, false)

    // And the recovered job is deletable like any other terminal job.
    const del = await fetch(`${baseUrl}/api/jobs/${jobId3}`, { method: 'DELETE' })
    assert.equal(del.status, 204)
  } catch (err) {
    dumpServerOutput()
    throw err
  }
})
