// DAW-feature smoke test for the Stem Mixer (mixer/): clip drag with beat
// snap, undo/redo, track selection keybinds, loop region, help panel.
// Spawns its own static server on a free port (mixer/ has no build step)
// and drives real Chrome via playwright-core. Requires the AudioMass API on
// :5055 with at least one completed separation job — skips otherwise.
import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { getFreePort, waitForServer } from './helpers.mjs'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const MIXER_DIR = path.join(ROOT, 'mixer')
const CHROME = process.env.AETHER_TEST_CHROME ?? '/usr/bin/google-chrome-stable'
const API = 'http://localhost:5055'

let serverProc = null
let browser = null
let page = null
let base = null
let skipped = false

before(async () => {
  // Live backend is a prerequisite (jobs to load)
  let jobs = []
  try {
    const res = await fetch(`${API}/api/jobs`, { signal: AbortSignal.timeout(3000) })
    if (res.ok) {
      const all = await res.json()
      jobs = all.filter((j) => j.status === 'done' && j.stems && j.stems.length && j.duration_sec > 0)
    }
  } catch { /* backend down */ }
  if (!jobs.length) { skipped = true; return }

  const port = await getFreePort()
  base = `http://localhost:${port}`
  serverProc = spawn('python3', ['-m', 'http.server', String(port), '--bind', '127.0.0.1', '--directory', MIXER_DIR], { stdio: 'ignore' })
  await waitForServer(base)

  browser = await chromium.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'],
  })
  page = await browser.newPage()
  page.on('pageerror', (e) => { throw new Error('page error: ' + e.message) })

  // Shortest done job = fastest fetch/decode (real jobs can be 8 x 45 MB)
  jobs.sort((a, b) => a.duration_sec - b.duration_sec)
  await page.goto(`${base}/?job=${jobs[0].job_id}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('#job-overlay', { state: 'attached' })
  await page.waitForFunction(() => {
    const ov = document.querySelector('#job-overlay')
    if (!ov) return true // overlay gone (skip pressed) — treat as ready
    for (const b of ov.querySelectorAll('button')) {
      if (b.textContent === 'Load stems' && !b.disabled) return true
    }
    return false
  }, { timeout: 30000 })
  await page.click('#job-overlay >> button:text-is("Load stems")')
  await page.waitForFunction(() => window.__mixer && window.__mixer.stems.length > 0, { timeout: 90000 })
})

after(async () => {
  if (browser) await browser.close()
  if (serverProc) serverProc.kill()
})

test('mixer DAW features (drag/snap/undo/keys/loop/help)', { skip: skipped ? 'no live backend or done jobs' : false }, async () => {
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

  // Transport
  await page.click('#btn-play')
  await page.waitForTimeout(400)
  assert.equal(await page.evaluate(() => window.__mixer.isPlaying), true)
  await page.click('#btn-stop')
  assert.equal(await page.evaluate(() => window.__mixer.isPlaying), false)

  // Clip drag with beat snap
  const lane = page.locator('.lane-row canvas').first()
  const box = await lane.boundingBox()
  const before = await page.evaluate(() => window.__mixer.stems[0].offset)
  await page.mouse.move(box.x + 40, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + 200, box.y + box.height / 2, { steps: 8 })
  await page.mouse.up()
  const after = await page.evaluate(() => window.__mixer.stems[0].offset)
  assert.ok(after > before, `clip drag moved offset ${before} -> ${after}`)
  const g = await page.evaluate(() => ({ beat: 60 / window.__mixer.bpm, off: window.__mixer.beatOffset }))
  const beats = (after - g.off) / g.beat
  assert.ok(Math.abs(beats - Math.round(beats)) < 0.01, `offset ${after} snapped to beat grid`)

  // Undo / redo
  await page.keyboard.press('Control+z')
  await page.waitForTimeout(150)
  assert.equal(await page.evaluate(() => window.__mixer.stems[0].offset), before, 'undo restores offset')
  await page.keyboard.press('Control+Shift+z')
  await page.waitForTimeout(150)
  assert.equal(await page.evaluate(() => window.__mixer.stems[0].offset), after, 'redo reapplies offset')

  // Selection + mute keybind (+ its undo)
  const selBefore = await page.evaluate(() => window.__mixer.selectedStem.name)
  await page.keyboard.press('Tab')
  const selAfter = await page.evaluate(() => window.__mixer.selectedStem.name)
  assert.notEqual(selBefore, selAfter, 'Tab cycles selection')
  await page.keyboard.press('m')
  assert.equal(await page.evaluate(() => window.__mixer.selectedStem.mute), true, 'M mutes selected')
  await page.keyboard.press('Control+z')
  assert.equal(await page.evaluate(() => window.__mixer.selectedStem.mute), false, 'undo restores mute')

  // Loop region via ruler drag
  const rbox = await page.locator('#ruler-canvas').boundingBox()
  await page.mouse.move(rbox.x + 50, rbox.y + rbox.height / 2)
  await page.mouse.down()
  await page.mouse.move(rbox.x + 250, rbox.y + rbox.height / 2, { steps: 6 })
  await page.mouse.up()
  const region = await page.evaluate(() => ({ s: window.__mixer.loopStart, e: window.__mixer.loopEnd }))
  assert.ok(region.e > region.s, `loop region set ${region.s}..${region.e}`)

  // Playback with the region set
  await page.click('#btn-play')
  await page.waitForTimeout(500)
  assert.equal(await page.evaluate(() => window.__mixer.isPlaying), true)
  await page.click('#btn-stop')

  // Help panel
  await page.keyboard.press('?')
  assert.equal(await page.evaluate(() => document.getElementById('help-panel').style.display), 'flex')
  await page.keyboard.press('Escape')
  assert.notEqual(await page.evaluate(() => document.getElementById('help-panel').style.display), 'flex')

  assert.deepEqual(errors, [], 'no console errors')
})
