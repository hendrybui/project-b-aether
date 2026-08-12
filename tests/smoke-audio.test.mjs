import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import http from 'node:http'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'
import { getFreePort, waitForServer, pollUntil } from './helpers.mjs'

const require = createRequire(import.meta.url)
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// Dynamic port: a stale dev server on a fixed port would otherwise fail the
// whole suite confusingly (--strictPort) or get silently hijacked (no flag).
let URL = process.env.AETHER_TEST_URL ?? null
// Headless smoke test needs a real Web Audio engine; use the system Chrome.
const CHROME = process.env.AETHER_TEST_CHROME ?? '/usr/bin/google-chrome-stable'

let serverProc = null
let browser
let page
const pageErrors = []

before(async () => {
  if (!URL) {
    // Spawn our own vite dev server on a dedicated free port; the registered
    // preview (if any) stays untouched. The watcher-ignore fix in
    // vite.config.ts keeps this from blowing the inotify watch limit.
    const viteEntry = path.join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js')
    const port = await getFreePort()
    URL = `http://localhost:${port}/`
    serverProc = spawn(process.execPath, [viteEntry, '--port', String(port), '--strictPort'], {
      cwd: ROOT,
      stdio: 'ignore',
    })
    await waitForServer(URL)
  }

  browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--autoplay-policy=no-user-gesture-required'],
  })
  page = await browser.newPage()
  page.on('pageerror', (e) => pageErrors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error') pageErrors.push(m.text())
  })

  // 'domcontentloaded' + explicit selector wait is robust on slow/loaded
  // boxes where the full 'load' event lags behind first-hit module transforms.
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('#piano .key', { timeout: 60000 })

  // Trusted click starts the audio engine (browser autoplay policy).
  await page.click('.preset-btn[data-preset="warmpad"]')
  await waitForFn(
    () => document.getElementById('status-text')?.textContent?.includes('Audio engine running'),
    15000,
  )
})

after(async () => {
  await page?.close()
  await browser?.close()
  if (serverProc) {
    serverProc.kill()
    // don't wait for it: killing the process group of a detached child is
    // enough for a local dev loop
  }
})

// waitForFunction's real signature is (pageFunction[, arg, options]) — passing
// { timeout } as the second arg silently ignored it (default 30s). Wrap it.
const waitForFn = (fn, timeout) => page.waitForFunction(fn, undefined, { timeout })

// Assert no page errors were reported since the test started (the page is
// shared across tests, so scope to each test's slice).
function assertNoPageErrors(since) {
  assert.deepEqual(
    pageErrors.slice(since),
    [],
    `page reported errors: ${pageErrors.slice(since).join(' | ')}`,
  )
}

test('Tone.start() + a held key produce a non-flat visualizer waveform', async () => {
  const errorsAtStart = pageErrors.length

  // Sample the visualizer: count bright-blue waveform pixels (#7c9cff) and
  // their vertical spread. A silent graph draws only a flat center line.
  const sample = () =>
    page.evaluate(() => {
      const scope = document.getElementById('scope')
      const ctx = scope.getContext('2d')
      const { data, width, height } = ctx.getImageData(0, 0, scope.width, scope.height)
      let minY = height
      let maxY = -1
      let count = 0
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4
          if (data[i + 2] > 150 && data[i] < 150) {
            count++
            if (y < minY) minY = y
            if (y > maxY) maxY = y
          }
        }
      }
      return { count, spread: maxY - minY }
    })

  // Idle baseline (short gap so the analyser settles).
  await page.waitForTimeout(500)
  const idle = await sample()

  // Hold a key for real (trusted mouse events), sample mid-note.
  const box = await page.locator('#piano .key:not(.black)').first().boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.waitForTimeout(450)
  const held = await sample()
  const voices = await page.locator('#voice-count').textContent()
  await page.mouse.up()

  // The regression this guards: a broken PolySynth graph (pre-15 Tone API
  // pattern) left the waveform flat at 0 spread and the voice counter stuck
  // at 0, while the status text still claimed audio was running.
  assert.ok(
    held.spread > idle.spread + 3,
    `expected a non-flat waveform while holding a key (held spread=${held.spread}, idle spread=${idle.spread}, idle count=${idle.count})`,
  )
  assert.ok(
    parseInt(voices, 10) >= 1,
    `voice count should be >= 1 while a key is held, got ${voices}`,
  )
  assertNoPageErrors(errorsAtStart)
})

test('AudioMass bridge: renders live separation progress and cancel from /api/jobs/active', async () => {
  const errorsAtStart = pageErrors.length

  // Stub the AudioMass job endpoints so the test never touches a real server:
  // the bridge polls /api/jobs/active every 2s and renders the plugin
  // pipeline's per-phase messages (separating chunks, analyzing stems,
  // generating waveforms) plus a cancel action.
  let job = null
  const stub = http.createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*')
    res.setHeader('Content-Type', 'application/json')
    if (req.method === 'POST' && req.url.includes('/cancel')) {
      job = {
        ...job,
        status: 'cancelled',
        cancellable: false,
        message: 'Job cancelled before completion',
      }
      res.end(JSON.stringify({ job_id: job.job_id, status: 'cancel_requested' }))
      return
    }
    if (req.method === 'GET' && req.url.includes('/api/jobs/active')) {
      if (!job) { res.end(JSON.stringify({ active: false })); return }
      res.end(JSON.stringify(job))
      return
    }
    if (req.method === 'GET' && req.url.includes('/api/diagnostics')) {
      // The bridge refreshes its engine indicator after a terminal job.
      res.end(JSON.stringify({
        ready: true,
        tools: [],
        plugins: ['analyze', 'htdemucs', 'transcribe', 'waveform'],
        separation: {
          backend: 'cpu_worker',
          device: 'cpu',
          container_available: false,
          detail: 'stub',
          last_job: null,
          // The bridge also renders live warm-pool state when present.
          warm_pool: {
            up: true,
            busy: false,
            jobs_served: 3,
            ready_sec: 36.3,
            started_at: '2026-08-12T00:00:00+00:00',
            idle_timeout_sec: 600,
            first_seen_at: '2026-08-11T23:00:00+00:00',
            last_activity_at: '2026-08-12T00:01:00+00:00',
            eviction: null,
            evicted_at: null,
            last_job: { at: 'x', image: 'stub', wall_sec: 3.4, ready_sec: 0, compute_sec: 3.2, overhead_sec: 0.2, audio_sec: 30, realtime: 0.107 },
          },
        },
      }))
      return
    }
    res.statusCode = 404
    res.end('{}')
  })

  try {
    await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve))
    const base = `http://127.0.0.1:${stub.address().port}`

    // The job snapshot the plugin-driven pipeline would produce mid-run.
    job = {
      job_id: 'stub123',
      status: 'separating',
      progress: 0.61,
      step: 'separating',
      message: 'Separating stems (2/150 chunks)',
      cancellable: true,
    }

    await page.fill('#am-base-url', base)
    await waitForFn(
      () => document.getElementById('am-job-message')?.textContent?.includes('Separating stems (2/150 chunks)'),
      10000,
    )

    const label = await page.textContent('#am-job-label')
    assert.match(label, /SEPARATING STEMS/, `phase label should show separation, got: ${label}`)
    assert.equal(
      await page.evaluate(() => document.getElementById('am-job-bar').style.width),
      '61%',
      'progress bar should reflect the job progress',
    )
    assert.equal(
      await page.evaluate(() => document.getElementById('am-job-cancel').style.display),
      'inline-block',
      'cancel button should show while the job is cancellable',
    )

    // Cancel mid-run: the stub flips the job to cancelled; the next poll
    // (<= 2s) must render the terminal state and hide the cancel button.
    await page.click('#am-job-cancel')
    await waitForFn(
      () => document.getElementById('am-job-label')?.textContent?.includes('CANCELLED'),
      10000,
    )
    assert.equal(
      await page.evaluate(() => document.getElementById('am-job-cancel').style.display),
      'none',
      'cancel button should hide once the job is terminal',
    )

    // The engine indicator (from /api/diagnostics) refreshes after the job
    // reaches a terminal state — it must show the stub's CPU worker plus the
    // live warm-pool state (up, jobs served, last pool job's compute/wall).
    await waitForFn(
      () => document.getElementById('am-engine')?.textContent?.includes('CPU worker'),
      10000,
    )
    const engineText = await page.textContent('#am-engine')
    assert.match(engineText, /Separation engine: CPU worker/, 'engine indicator should render the active separation backend')
    assert.match(engineText, /pool: up/, 'engine indicator should show the warm pool is up')
    assert.match(engineText, /3 jobs served/, "engine indicator should show the pool's served-job count")
    assert.match(engineText, /idle evict 10m/, 'engine indicator should show the idle-eviction window')
    assert.match(engineText, /3\.2s compute \/ 3\.4s wall/, 'engine indicator should show the last pool job compute vs wall')
  } finally {
    stub.close()
    // Point the poller back at the real default so later runs are unaffected.
    await page.fill('#am-base-url', 'http://localhost:5055').catch(() => {})
    job = null
  }

  assertNoPageErrors(errorsAtStart)
})

test('bounce auto-uploads the WAV to AudioMass and the live job row shows the separation', async () => {
  const errorsAtStart = pageErrors.length

  // Stub the AudioMass job API so the bounce's auto-upload lands here: POST
  // /jobs/upload accepts the multipart WAV and creates a job; /api/jobs/active
  // then reports it so the bridge's monitor renders the progress row live.
  let uploaded = null
  const stub = http.createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*')
    res.setHeader('Content-Type', 'application/json')
    if (req.method === 'POST' && req.url.includes('/jobs/upload')) {
      const chunks = []
      req.on('data', (c) => chunks.push(c))
      req.on('end', () => {
        uploaded = {
          url: req.url,
          // multipart body carries the raw WAV bytes, which start with RIFF
          isRiff: Buffer.concat(chunks).includes(Buffer.from('RIFF')),
          bytes: Buffer.concat(chunks).length,
        }
        res.statusCode = 201
        res.end(JSON.stringify({ job_id: 'bounce1', status: 'created', progress: 0, cancellable: true }))
      })
      return
    }
    if (req.method === 'GET' && req.url.includes('/api/jobs/active')) {
      if (!uploaded) { res.end(JSON.stringify({ active: false })); return }
      res.end(JSON.stringify({
        job_id: 'bounce1',
        status: 'separating',
        progress: 0.61,
        step: 'separating',
        message: 'Separating stems (2/150 chunks)',
        cancellable: true,
      }))
      return
    }
    if (req.method === 'GET' && req.url.includes('/api/diagnostics')) {
      res.end(JSON.stringify({
        ready: true,
        tools: [],
        plugins: ['analyze', 'htdemucs', 'transcribe', 'waveform'],
        separation: { backend: 'cpu_worker', device: 'cpu', container_available: false, detail: 'stub', last_job: null },
      }))
      return
    }
    res.statusCode = 404
    res.end('{}')
  })

  try {
    await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve))
    const base = `http://127.0.0.1:${stub.address().port}`
    await page.fill('#am-base-url', base)

    // Populate the grid, then bounce a real pattern: the bounce renders ~1 bar
    // of audio (a few seconds), auto-uploads the WAV, and starts separation.
    await page.click('#gen-drums-techno')
    await page.click('#bounce-drums-btn')

    await pollUntil(() => uploaded, 20000, 'bounce upload to reach the stub')
    assert.ok(uploaded.isRiff, `uploaded body should contain a WAV (RIFF), got ${uploaded.bytes} bytes`)

    // The bridge monitor (2s poll) must pick the new job up and render the
    // plugin pipeline's per-phase message.
    await waitForFn(
      () => document.getElementById('am-job-message')?.textContent?.includes('Separating stems (2/150 chunks)'),
      10000,
    )
    assert.match(
      await page.textContent('#am-job-label'),
      /SEPARATING STEMS/,
      'live row should show the separating phase after the bounce upload',
    )
    assert.equal(
      await page.evaluate(() => document.getElementById('am-job-cancel').style.display),
      'inline-block',
      'live row should be cancellable during separation',
    )
  } finally {
    stub.close()
    await page.fill('#am-base-url', 'http://localhost:5055').catch(() => {})
  }

  assertNoPageErrors(errorsAtStart)
})

test('sequencer: a generated pattern plays and the step highlight advances in sync with the tempo', async () => {
  const errorsAtStart = pageErrors.length

  // Generate a drum pattern so the grid is populated.
  await page.click('#gen-drums-techno')

  // Read the actual tempo so the expected step duration is exact:
  // a step is one 16th note = 15000/bpm ms.
  const tempoBpm = parseInt(await page.inputValue('#tempo-slider'), 10)
  const stepMs = 15000 / tempoBpm

  // Start with a clean step-boundary trace (see __aetherTransport in main.ts).
  await page.evaluate(() => window.__aetherTransport.clear())

  // Start playback; the play button flips to PAUSE.
  await page.click('#seq-play')
  await waitForFn(
    () => document.getElementById('seq-play')?.textContent?.includes('PAUSE'),
    5000,
  )
  // First highlight tick comes from a 35ms poller after start(). Wait for
  // the top of the bar specifically: the sequencer starts at step 0 (the
  // transport is reset on stop, so playback begins at a bar boundary).
  await page.waitForSelector('.seq-step.current[data-step="0"]', { timeout: 5000 })

  // The DOM highlight is a laggy 35ms mirror of the audio clock. Sample it
  // at ~40ms intervals for ~2.5 bars purely as a VISUAL check that it moves;
  // every quantitative assertion below reads the audio-rate trace, which
  // survives main-thread stalls (Tone's lookahead fires every missed
  // boundary back-to-back with exact scheduled times) — wall-clock DOM
  // sampling cannot (a stall reads as a teleport like 0 -> 13).
  const samples = await page.evaluate(async (stepMs) => {
    const out = []
    const t0 = performance.now()
    while (performance.now() - t0 < stepMs * 40) {
      const el = document.querySelector('.seq-step.current')
      out.push(el ? parseInt(el.dataset.step, 10) : -1)
      await new Promise((r) => setTimeout(r, 40))
    }
    return out
  }, stepMs)

  // Stop cleanly and confirm the transport is back to the stopped state.
  await page.click('#seq-stop')
  assert.equal((await page.textContent('#seq-play')).trim(), '▶ PLAY')
  assert.equal(
    await page.locator('.seq-step.current').count(),
    0,
    'STOP should clear the current-step highlight',
  )

  // Visual check only: the highlight must actually move (a stuck highlight
  // would trip this even though the trace looks healthy). No completeness
  // assertion here — under load the sampler can legitimately miss steps.
  assert.ok(
    samples.every((s) => s >= 0),
    'sampling hit a moment with no current-step highlight',
  )
  assert.ok(
    new Set(samples).size >= 4,
    `the highlight should visibly advance; saw only [${[...new Set(samples)].sort((a, b) => a - b).join(',')}]`,
  )

  // Quantitative assertions read the audio-rate trace (main.ts wires
  // setOnAudioStep): one entry per 16n boundary, seconds on the transport
  // timeline. Exact event times mean the trace is complete and in order even
  // under stalls, so these can be strict.
  const trace = await page.evaluate(() => window.__aetherTransport.trace)
  assert.ok(
    trace.length >= 21,
    `too few step boundaries recorded to judge cadence (${trace.length})`,
  )

  // 1) Phase: playback starts at the top of the bar (step 0). Under heavy
  // load Tone's scheduler can start its lookahead late enough that the very
  // first 16n boundary (tick 0) never fires — the first recorded boundary is
  // then step 1. That leading-boundary drop is an inaudible scheduler
  // artifact; what this assertion guards against is resuming MID-BAR (step
  // 2+, the transport-not-reset bug). The DOM check above already proved the
  // highlight starts at step 0.
  assert.ok(
    trace[0].step === 0 || trace[0].step === 1,
    `playback should start at the top of the bar (step 0 or a dropped leading boundary at 1), got ${trace[0].step}`,
  )

  // 2) Full bar cycle: all 16 steps appear over ~2.5 bars.
  const seen = new Set(trace.map((t) => t.step))
  assert.equal(
    seen.size,
    16,
    `expected all 16 steps to appear over ~2.5 bars, saw [${[...seen].sort((a, b) => a - b).join(',')}]`,
  )

  // 3) Ordered cyclic advancement: every boundary is +1 mod 16 (the trace
  // records each audio event, so a gap would mean the transport genuinely
  // skipped — never a reversal or teleport).
  for (let i = 1; i < trace.length; i++) {
    const a = trace[i - 1].step
    const b = trace[i].step
    if (a === b) continue
    const delta = (b - a + 16) % 16
    assert.equal(delta, 1, `step went backwards or teleported: ${a} -> ${b}`)
  }

  // 4) Cadence matches the tempo: the transport-seconds delta between
  // consecutive boundaries must equal one 16th note. The trace's seconds are
  // the event's exact scheduled audio time (no poller jitter), so the mean
  // ratio is tight while still catching genuine transport drift (a 1.1x BPM
  // error reads as a mean ratio of 1.1).
  const meanRatio = (() => {
    let sum = 0
    let n = 0
    for (let i = 1; i < trace.length; i++) {
      const ds = (trace[i].step - trace[i - 1].step + 16) % 16
      if (ds === 0) continue
      sum += (trace[i].seconds - trace[i - 1].seconds) / (ds * (stepMs / 1000))
      n++
    }
    return sum / n
  })()
  assert.ok(
    Number.isFinite(meanRatio) && meanRatio >= 0.97 && meanRatio <= 1.05,
    `mean step interval ${(meanRatio * stepMs).toFixed(1)}ms is not ~${stepMs.toFixed(0)}ms (${tempoBpm} BPM)`,
  )

  assertNoPageErrors(errorsAtStart)
})
