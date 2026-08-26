# AGENTS.md — Project-B (Aether + AudioMass)

Guidance for OpenCode agents working in this workspace.

**Aether** (repo root): AI-assisted web synth + step sequencer (Vite + TypeScript + Tone.js). Pairs with the **AudioMass API** (`backend/`, rebuilt FastAPI) and the **Stem Mixer** (`mixer/`, the DAW frontend) for a local "generate → separate → mix" music environment. `CLAUDE.md` documents the architecture in detail; `README.md` covers workflow/features. Don't duplicate those docs here.

## Backend rebuild (2026-08-27) — CUTOVER COMPLETE

The old AudioMass backend AND editor are **deleted** (`audiomass/` gone; safety archive at `backups/audiomass-archive-2026-08-27.tar.gz` — its own git history is inside). The API now has exactly two clients: the **Stem Mixer** (the DAW frontend) and **Aether** (bounce upload).

- **The backend is `backend/` at root** — FastAPI, moved + pruned, own venv (`backend/.venv`), served by systemd user unit `mass-backend.service` on **:5055** (launcher starts/stops it). Editor static mount removed; `/` returns a JSON pointer to the mixer.
- **Spec:** `API-CONTRACT.md` (repo root) — every route, consumer call-site, the job state machine.
- **Verified at cutover:** CPU + ROCm warm-pool GPU separation end-to-end on :5055 through `backend/.venv`, 61/61 unit tests (`PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests -v`).
- **Jobs data:** `AUDIOMASS_JOBS_DIR` (default `/mnt/Pandora/Music/Audiamass`) — shared, all existing separations intact. `backend/docker/` holds the `Dockerfile.demucs-rocm` to rebuild the GPU image.
- **DJ Toolkit** now uses `backend/.venv` (flask + basic-pitch live there).
- **Ownership split:** backend work in `backend/`; DAW-frontend work in `mixer/ROADMAP.md` (Phase 2 + 4.2/4.3 done 2026-08-27: clip drag, loop region, keybinds, undo/redo; next candidates: 3.2 separate-from-mixer, 4.4 theme, 5.x effects/automation). The contract is the interface.
- `mixer/` is a **separate git repo** nested in this one — its commits stay inside it.

## Commands

```bash
npm run dev        # Vite on :5173, served at "/" (the /aether/ base is added by the launcher)
npm run build      # tsc (noEmit) + vite build → dist/. This is the typecheck; there is no separate lint.
npm test           # node --test tests/*.test.mjs --test-concurrency=1
node --test tests/plugin-units.test.mjs    # single test file

./run-aether-with-audiomass.sh start       # also: stop, restart, status, build-aether, caddy-restart
```

- Run `npm run build` before reporting any change done.
- **Runtime verification belongs to the user.** The agent never runs the app or judges sound; state "user to verify in browser" for any audio/MIDI/sequencer claim.

## Tests (node:test, no framework)

- `smoke-audio.test.mjs` spawns its own Vite dev server on a free port and drives it via playwright-core + system Chrome (`AETHER_TEST_CHROME`, default `/usr/bin/google-chrome-stable`); set `AETHER_TEST_URL` to reuse an already-running server.
- The old `audiomass-*.test.mjs` warm-pool tests were deleted with the old backend (2026-08-27); Python-side tests live in `backend/tests` (61, no GPU/docker needed).
- Concurrency is pinned to 1 because tests spawn real servers.
- 2026-08-27: `tests/` is deleted in the uncommitted working tree but tracked in git — `git restore tests/` before `npm test`.

## Launcher / proxy

`./run-aether-with-audiomass.sh start` brings up: Aether :5173 (`--base /aether/`), AudioMass :5055, DJ Toolkit :5001, Music Tools :8091, Melody Suite :5002 (env `MELODY_SUITE_PORT`; the app defaults to 5000 — keep the Caddyfile `/melody` route in sync), Stem Mixer :5058 (systemd unit, NOT 5060 — `ERR_UNSAFE_PORT`), Open WebUI :3000, optional GPU llama.cpp + cloud-LLM seed, Caddy :80.

- Caddy's `/aether*` handle proxies **without** `strip_prefix` — Vite's `--base /aether/` handles the prefix, and stripping it causes a 302 loop. Use the trailing slash: `http://localhost/aether/`.
- The Open WebUI `handle` catch-all must stay LAST in the Caddyfile.
- Caddy is managed via pidfiles + config-hash in `/tmp`; needs one-time `setcap` for :80. Direct ports work without Caddy.
- Read the script header comments + `/mnt/Pandora/caddy/Caddyfile` before touching launcher/proxy.

## Repo layout (mixed root — don't "clean up" without asking)

- `src/` — Aether: `audio/` (`engine.ts` synth, `drumKit.ts` drums, `paramMap.ts` 0-1 → real-unit maps), `sequencer/stepSequencer.ts` (Transport-driven clock + note-storing pattern), `ai/` (`ollama.ts` LLM bridge, `patchGenerator.ts`, `melodyGenerator.ts`, `sequenceGenerators.ts`), `ui/` (`knob.ts`, `keyboard.ts`), `main.ts` (wires everything)
- `backend/` — the AudioMass API (rebuilt FastAPI; see "Backend rebuild" section above). Own venv, tests, docker image recipe.
- `mixer/` — the Stem Mixer / DAW frontend (see above).
- `melody-suite/`, `dj_toolkit/`, `music-tools/` — launcher-managed sub-projects; don't fold them into the Aether tree
- `scripts/` — launcher helpers (`start-llama-gpu.sh`, `seed-llm-config.sh`, `check-*.sh`); `check-demucs-gpu.sh` smoke-tests the **old** demucs warm pool (nightly timer deleted 2026-08-26 — manual runs only)
- `exports/`, `samples/` — shared Aether↔AudioMass handoff folders, auto-created by the launcher; bounced WAVs (`aether-*-to-audiomass.wav`) go to `exports/`
- `splinter-x/`, `PROJECT_X-Splinter/`, `backups/`, `Project-B/` — archival; leave alone unless asked

## Hard rules / gotchas

1. **Lazy audio graph**: no Tone nodes in constructors — build everything in `ensureGraph()` after the first user gesture (browser autoplay policy).
2. **Adding a synth parameter**: `SynthParams` + `defaultParams` in `src/audio/engine.ts`, mapping in `applyParams()`, knob in `index.html` (`data-knob="name"` canvas inside a `data-param` wrapper) wired via `attachKnobs()` in `main.ts`.
3. **LLM is a 3-tier fallback chain and never throws**: cloud (OpenAI-compatible, e.g. 9router; persisted in localStorage `aether-llm-cloud`) → GPU llama.cpp (`scripts/start-llama-gpu.sh`) → Ollama (`llama3.1:8b`). All AI features must degrade gracefully to the local keyword/scale generators when every tier fails. The launcher seeds the cloud config into git-ignored `public/llm-seed.json` (auto-applied on load, removed on stop) — never commit it or copy keys into code/docs.
4. **Vite watcher ignores** `audiomass/`, venvs, sibling projects, `dist/`, `exports/`, `samples/`, `node_modules/.vite` (`vite.config.ts`) because the mixed root exceeds the inotify watch limit (ENOSPC crashes the dev server). Don't remove entries from this list.
5. **AudioMass has no MIDI round-trip**: its basic-pitch transcription returns JSON notes only. Aether's `exportMidi()` is export-only. Don't promise MIDI import in AudioMass (roadmap Phase 6.1).
6. **tsconfig enforces `erasableSyntaxOnly`** (no enums, namespaces, or parameter properties), `noUnusedLocals`/`noUnusedParameters`, and `verbatimModuleSyntax` (type-only imports) — all gated by `npm run build`.

### Bugs that drove the 2026-08 engine rebuild (don't reintroduce)

1. **Sequencer note leak**: `setTimeout`-based noteOff left held notes on stop/clear → sequencer stores pitches per step and calls `releaseAll()` on stop.
2. **Transport drift / tempo tear**: `Tone.Loop` with a fixed interval is detached from the global clock → use `Tone.getTransport().scheduleRepeat(cb, "16n")` + `Transport.bpm.value`.
3. **Stuck notes on manual retrigger** → `PolySynth(MonoSynth)` voice tracking + `releaseAll()`.
4. **Arbitrary pitch runs**: pitches derived from `(step*3)%scale` at trigger time → patterns store **actual midi notes**, scale-quantized on generate, editable per step.

## Before editing sensitive areas

- Audio engine / scheduling → `CLAUDE.md` "Architecture" + "Key Patterns" and `src/audio/paramMap.ts`
- Backend / separation pipeline → `API-CONTRACT.md` and `backend/README.md`
- Launcher/proxy → header comments of `run-aether-with-audiomass.sh` and `/mnt/Pandora/caddy/Caddyfile`

## Review workflow (when user asks "review the project")

1. Read-only first: `src/`, package.json, tsconfig.json, vite.config.ts.
2. `npm run build` for the current compile state.
3. Report: (a) what works, (b) broken/wonky, (c) ranked improvement suggestions — cite `file_path:line_number`.
4. Hand off runtime checks explicitly: "user to verify in browser".
