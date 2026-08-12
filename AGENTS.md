# AGENTS.md — Project-B (Aether + AudioMass)

Guidance for ZCode agents working in this workspace.

**Status: Aether is mid-rebuild** as of 2026-08-09. Old `engine.ts`/`drumKit.ts`/`stepSequencer.ts` had fundamental timing/voice-leak bugs and is being replaced with `Tone.PolySynth(MonoSynth)`-based voices + `Tone.Transport.scheduleRepeat`. Don't extend the old manual-oscillator design — finish the rebuild first. See **Hard rules / gotchas** for the specific bugs being engineered out.

## What this repo is

**Aether**: an AI-assisted web synthesizer + step sequencer (Vite + TypeScript + Tone.js), living at the repo root. It pairs with **AudioMass** (a Python/uvicorn multitrack waveform editor) in `audiomass/` to form a local "generate → edit" music environment.

Read `CLAUDE.md` first — it documents the architecture in detail. `README.md` covers the workflow and feature status. Don't duplicate those docs here; the notes below are the operational essentials.

## Commands

```bash
npm run dev        # Vite dev server on :5173
npm run build      # tsc typecheck + vite build → dist/ (this is the typecheck; no separate lint/test)
npm run preview    # serve dist/ locally

./run-aether-with-audiomass.sh start   # Aether (:5173, base /aether/) + AudioMass (:5055) + Caddy proxy (:80)
./run-aether-with-audiomass.sh status  # also: stop, restart, build-aether, caddy-restart
```

**No test suite, no linter.** `npm run build` (tsc) is the only automated gate. Confirm a build before reporting done.

**The agent does not run the app or do perceptual review.** The user verifies runtime behavior (sound, MIDI, sequencer timing, AudioMass bounce) by launching via the script or `npm run dev`. When suggesting changes, the agent must state: "user to verify in browser".

## Repo layout (root is mixed — don't "clean up" without asking)

- `src/` — Aether app:
  - `audio/` — `engine.ts` (synth), `drumKit.ts` (drums), `paramMap.ts` (0-1 → real units), `recorder.ts` (Tone.Recorder + WAV encoder) if added
  - `sequencer/` — `stepSequencer.ts` (Transport-driven clock + note-storing pattern)
  - `ai/` — `ollama.ts` (LLM bridge), `patchGenerator.ts` (keyword→patch), `melodyGenerator.ts` (one-shot phrases), `sequenceGenerators.ts` (writes notes into the sequencer)
  - `ui/` — `knob.ts`, `keyboard.ts`
  - `main.ts` — wires everything
- `audiomass/` — separate Python app with its own CLAUDE.md, run.sh, venv; treat as a sub-project
- `exports/`, `samples/` — shared handoff folders between the two apps (auto-created by launcher)
- `dist/`, `public/`, `index.html`, `vite.config.ts`, `tsconfig.json` — Aether build
- `dj_toolkit/`, `music-tools/`, `splinter-x/`, `PROJECT_X-Splinter/`, `Project-B/`, `backups/` — sibling experiments/archival; leave alone unless asked
- `*.png`, `Splinter-x.md` — design references/docs

## Hard rules / gotchas

### Bugs that drove the rebuild (don't reintroduce)
1. **Sequencer synth-track note leak**: `setTimeout`-based `noteOff` left held notes when the pattern was stopped/cleared. Fix: sequencer stores pitches per step + calls `releaseAll()` on stop.
2. **Transport drift / tempo tear**: `Tone.Loop` with a fixed interval is detached from the global clock; tempo changes stretched the current step. Fix: `Tone.Transport.scheduleRepeat("16n")` + `Transport.bpm.value`.
3. **Stuck notes on manual retrigger**: same MIDI note retriggered while releasing — old oscillators kept ringing. Fix: voice tracking via `PolySynth` + `releaseAll()`.
4. **Sequencer synth played arbitrary runs**: velocities stored but pitches derived from `(step*3)%scale` at trigger time. Fix: pattern stores **actual midi notes** (scale-quantized on generate), editable per-step.

### Hard rules (still load-bearing)
1. **Lazy audio graph remains.** No Tone nodes in constructors — built in `ensureGraph()` after first user gesture (browser autoplay policy).
2. **Adding a synth parameter** still requires: `SynthParams` interface + `defaultParams` in `src/audio/engine.ts`, mapping in `applyParams()`, and a knob in `main.ts`/`index.html` (`data-knob="name"`). But during the rebuild, prefer extending the existing mapped field set rather than adding new fields.
3. **Ollama is optional and local.** Calls go direct to `http://localhost:11434/api/chat` (model `llama3.1:8b`). All AI features must degrade gracefully to the local keyword/scale-based generators when Ollama is down.
4. **Dev server runs with `--base /aether/`** so the Caddy proxy path works; direct access is `:5173`. Bounced WAVs are written for `exports/`.
5. **AudioMass has no MIDI import/export** — basic-pitch MIDI data is currently discarded (`audiomass/src/audiomass-server.py:~535`); it's roadmap Phase 6.1. Don't promise MIDI round-trip.
6. **Vite watch ignores `audiomass/`** and `node_modules/.vite` (see `vite.config.ts`) — Python changes don't restart Aether.
7. The launcher manages Caddy via pidfiles/config-hash in `/tmp` and needs one-time `setcap` for port 80; direct ports work fine without Caddy.
8. **Runtime verification belongs to the user**, not the agent. Don't claim "tested in browser" — only the user can hear whether it sounds right.

## Before editing sensitive areas

- Audio engine / scheduling → read `CLAUDE.md` "Architecture" + "Key Patterns" and `paramMap.ts` (the new mapping helpers)
- `audiomass/` work → read `audiomass/CLAUDE.md` and `audiomass/ROADMAP.md`
- Launcher/proxy changes → read the header comments of `run-aether-with-audiomass.sh` and `/mnt/Pandora/caddy/Caddyfile`

## Review/improvement workflow (when user asks "review the project")

1. **Read-only review first.** Inspect `src/`, package.json, tsconfig.json, vite.config.ts. Don't run things.
2. **Run `npm run build`** to see current compile state.
3. **Report findings** as: (a) what works, (b) what's broken / wonky, (c) ranked improvement suggestions. Cite files with `file_path:line_number`.
4. **Hand off runtime check** explicitly: "user to verify in browser" for any audio/MIDI/sequencer claim.

## Workflow / status snapshot (2026-08-09)

- **Aether rebuild in progress**: switched the synth/drums/sequencer from hand-rolled oscillator graph + `Tone.Loop` + `setTimeout` to Tone-instrument-based voices + `Tone.Transport.scheduleRepeat`. Old files (`engine.ts`, `drumKit.ts`, `stepSequencer.ts`) are being rewritten in place.
- New helpers: `src/audio/paramMap.ts` centralizes 0-1 → real-unit conversions (cutoff, time, lfo rate, delay time, detune clamp, waveform index).
- `melodyGenerator.ts` + `patchGenerator.ts` retained for the AI/pattern polish pass; generators now write **stored midi notes** into the sequencer instead of one-shot `setTimeout`s.
- AudioMass bridge UI/logic, Ollama integration, WAV bounce, MIDI export, preset bar, visualizer: kept (still work as before; verify after rebuild).
- The user runs the app and gives the perceptual/sound review; the agent gives a code-level review.
