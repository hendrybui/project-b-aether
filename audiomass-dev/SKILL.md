---
name: audiomass-dev
description: Safely edit the AudioMass fork (audiomass/src) and its GPU separation backend — CRLF-safe editing, cache-busting, the plugin registry contract, the BPM/key scanner, and the ROCm warm-pool demucs pipeline.
---

# audiomass-dev

Operational knowledge for working on the AudioMass fork at `audiomass/` inside the Project-B workspace, plus its GPU demucs backend. This is the "gotchas" skill — the app is plain browser JS served directly from `src/`, and most mistakes here come from line endings, stale browser caches, or breaking the plugin contract.

## When to use

- Any edit to `audiomass/src/**` (frontend JS/CSS/HTML) or the AudioMass server.
- Work on htdemucs separation, the plugin registry, the BPM/key scanner, or the GPU warm pool.
- Debugging "my change doesn't show up" or "BPM is wrong" in AudioMass.

## Hard rules

1. **CRLF files — preserve `\r\n`.** The `audiomass/src/*.js` files use Windows line endings. LF-only anchors never match. Use a Python script that reads bytes and operates on `\r\n` (never rewrite the whole file with LF content or the diff explodes into line-ending noise). `str_replace`/`write_file` with LF content corrupts the diff — check `git diff --stat` afterward for inflated stats.
2. **Bump the cache-buster or your change won't load.** `audiomass/src/index.html` references files with `?v=` suffixes (e.g. `multitrack.js?v=mt118`, `tempo-estimator.js?v=mt5`, `engine.js?v=mt31`, `tempo-worker.js?v=mt4`). After editing a file, bump its version string or the browser serves stale code. This has bitten us repeatedly (missing key badge, missing features).
3. **Syntax gate is `node --check <file>.js`.** No linter, no test suite for the frontend. Run `node --check` on every edited JS file. Host-side unit tests live in `tests/` and run with `node --test`.
4. **Edits are live without restart.** AudioMass serves directly from `src/` (no bundle step). Edit → bump cache-buster → hard-reload the browser. The server (launcher-managed, :5055) does not need a restart.
5. **Runtime verification belongs to the user.** The agent can verify DOM/canvas/console in the preview tab, but the user hears the audio. Say "user to verify in browser" for anything sonic.
6. **Don't break the plugin contract.** Every processing plugin (htdemucs, analyze, waveform, transcribe) registers a named capability with a uniform job/progress contract. Pipeline phases: per-phase progress spans, `CancelledError` → `mark_cancelled`, non-cancel errors → `mark_failed` with friendly message, and the `finally` block must always run `cancellation_service.clear()` + log-handler detach.
7. **Single-key shortcuts fire on `keypress`, not `keydown`.** `KeyHandler.addSingleCallback` (keys.js) dispatches on the `keypress` event; combos via `addCallback` fire on `keydown`. All keybindings live in `engine.js` (Space=32, `` ` ``=96, Q=113, M=109 add-marker, L=108 loop, [ ]=219/221 markers). To simulate a key in the preview you must dispatch **both** keydown and keypress (with `keyCode` defined via `Object.defineProperty`, since `KeyboardEvent` ignores it).
8. **Start the server with an absolute path.** `audiomass/run.sh start` resolves its own dir with `cd "$(dirname "$0")"` internally, so a relative invocation (`./audiomass/run.sh`) breaks the inner `cd ./audiomass`. Use `/mnt/Pandora/Project-B/audiomass/run.sh start` (absolute), exactly like the launcher does. Detach with `setsid nohup env AUDIOMASS_PORT=5055 <abs>/run.sh start > log 2>&1 < /dev/null &` — plain nohup gets reaped when the command runner exits.

## File map

- `audiomass/src/tempo-estimator.js` — BPM + key detection (see BPM scanner section below)
- `audiomass/src/tempo-worker.js` — worker wrapper for the estimator
- `audiomass/src/engine.js` — wavesurfer wiring; auto-BPM-scan on song load (worker-first, capped to first 180s, job-id guarded)
- `audiomass/src/multitrack.js` — MultiTrack editor; owns beat-grid state + the main-view BPM/key badge
- `audiomass/src/main.css` — styles incl. compact sidebar
- `audiomass/src/index.html` — cache-buster version strings live here
- `audiomass/` server — uvicorn Python app (see `audiomass/CLAUDE.md`); /api/diagnostics exposes engine availability + warm-pool state

## BPM/key scanner (tempo-estimator.js)

- **Algorithm**: autocorrelation + log-Gaussian prior centered at 120 BPM (Ellis-style tempo induction) so octave-ambiguous tracks resolve to the perceived beat. The old interval-histogram method caused 167-BPM misreads and must stay demoted to an ultra-conservative fallback (flat curve AND interval confidence > 0.5). Confidence comes from the prior-independent peak gap (UI gate: > 15).
- **Key detection** (Krumhansl-Schmuckler): the FFT **must zero the imaginary buffer between frames** — forgetting this silently corrupts every frame after the first and the detector returns `null` (reproduce in Node with a synthetic C-major buffer).
- **Validate against known tempos**: synthetic tracks at 60/90/128/140 BPM with offbeat ticks + the real sample. Never trust a single run; detection can legitimately vary between plausible values on reload.

## GPU demucs warm pool

- Image `rocm64_gfx803_demucs:2.4` (RX 580 / gfx803 / ROCm) in docker. First job pays ~35s container+python+model startup; warm reuse drops subsequent jobs to seconds (e.g. 78s → 3.3s wall on a 12s track).
- `check-demucs-gpu.sh` — one-command smoke: starts docker if down, verifies image, runs two back-to-back jobs, asserts model loaded once, then idle-eviction (10s window) and eviction=idle in diagnostics.
- Pool semantics: warm reuse across jobs, cancellation kills the worker (verify the process is gone — a cancelled job must not burn CPU), idle eviction releases the GPU after N minutes, job counts + last-job stats persist across server restarts.
- Nightly regression: systemd timer runs the check; docker enabled at boot so the net actually runs.

## UI layout

- **Main editor** = viewing/playback + metadata (BPM/key badge over the waveform). No grid — the user explicitly removed it.
- **MultiTrack** = editing. Right-side channel strip is compact by default (57px); right-click expands to full (179px) view.
- Beat grid (MultiTrack) uses the toolbar BPM field; detection feeds the main-view badge only — do not let detection write into the multitrack BPM field/grid (decoupled by design).

## Workflow for a typical edit

1. `node --check` the target file before editing to confirm baseline.
2. Edit with CRLF-preserving tooling (Python script; see hard rules).
3. `node --check` again; bump cache-busters in `index.html`.
4. Verify in the preview tab (AudioMass at `http://localhost:5055/` — load sample, check DOM/canvas/console).
5. Report to the user; state anything sonic as "user to verify in browser".
6. On request, commit to the audiomass fork (`audiomass-aegis` remote) or the Aether repo (`project-b-aether`).
