// Shared helpers for the Aether + AudioMass smoke-test suites. Each test file
// keeps its own spawn/teardown specifics (one page vs. one server), but the
// process/port/SSE plumbing is common.
import { spawnSync } from 'node:child_process'
import net from 'node:net'

export function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.once('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

export async function waitForServer(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url)
      if (res.ok) return
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(`server never answered at ${url}`)
}

export async function pollUntil(fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    last = await fn()
    if (last) return last
    await new Promise((r) => setTimeout(r, 500))
  }
  throw new Error(`timed out after ${timeoutMs}ms waiting for ${label}; last: ${JSON.stringify(last)}`)
}

// Drain an SSE response in the background, pushing {event, data} frames into
// the given array. The AudioMass server sends a heartbeat every 15s, so a read
// that produces nothing for 20s means the stream ended or stalled.
export function drainSse(res, events) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  ;(async () => {
    while (true) {
      let result
      try {
        result = await Promise.race([
          reader.read(),
          new Promise((_, reject) => setTimeout(() => reject(new Error('sse read stalled')), 20000)),
        ])
      } catch {
        return
      }
      if (result.done) return
      buf += decoder.decode(result.value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const evt = { event: 'message', data: '' }
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) evt.event = line.slice(6).trim()
          else if (line.startsWith('data:')) evt.data += line.slice(5).trim()
        }
        events.push(evt)
      }
    }
  })()
}

// Child processes of `ppid` whose command line contains `nameFragment`.
// Scoping by PPID means the user's real AudioMass instance (:5055) and its
// workers are never mistaken for a test server's children.
export function findChildren(ppid, nameFragment) {
  const out = spawnSync('ps', ['-eo', 'pid=,ppid=,args='], { encoding: 'utf8' })
  const matches = []
  for (const line of (out.stdout || '').split('\n')) {
    const m = line.match(/^\s*(\d+)\s+(\d+)\s+(.+)$/)
    if (m && m[2] === String(ppid) && m[3].includes(nameFragment)) {
      matches.push(m[1])
    }
  }
  return matches
}

// Generate a short real stereo 44.1kHz sine WAV with ffmpeg so uploads go
// through the same validation path as a user's file.
export function makeSineWav(filePath, seconds) {
  const gen = spawnSync('ffmpeg', [
    '-y', '-f', 'lavfi', '-i', `sine=frequency=220:duration=${seconds}`,
    '-ar', '44100', '-ac', '2', filePath,
  ], { stdio: 'ignore' })
  if (gen.status !== 0) {
    throw new Error(`failed to generate test WAV ${filePath}: ${gen.stderr?.toString() || gen.error}`)
  }
}
