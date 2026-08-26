# AGENTS.md — Project-B (Aether + AudioMass)

Guidance for OpenCode agents working in this workspace.

**Aether** (repo root): AI-assisted web synth + step sequencer (Vite + TypeScript + Tone.js). Pairs with **AudioMass** (Python, `audiomass/`) for a local "generate → edit" music environment. `CLAUDE.md` documents the architecture in detail; `README.md` covers workflow/features. Don't duplicate those docs here.

## Backend rebuild (2026-08-27) — pickup guide

The AudioMass **Python backend is scrapped**; the **editor frontend and mixer are kept**. A new backend serves the same REST contract.

- **Spec:** `API-CONTRACT.md` (repo root) — every route, consumer call-site, the job state machine, and what's internal/reusable (ROCm demucs image, smoke-test script).
- **Ownership split:** backend rebuild works from `API-CONTRACT.md` (no frontend changes needed); mixer-frontend work is `mixer/ROADMAP.md` phases 2.3/2.4/3.1. The two streams don't collide — the contract is the interface.
- **Cutover:** new backend takes `:5055` (or update the 3 hardcoded spots listed in the contract), Caddy `/mass` follows automatically. Then delete: `audiomass/src/audiomass-server.py`, `audiomass/backend/`, `tests/audiomass-*.test.mjs`, `scripts/check-demucs-gpu.sh`.
- **Reuse, don't rebuild:** the `rocm64_gfx803_demucs:2.4` image + warm-pool concept (GPU model load is ~40s; warm reuse matters on the RX 580).
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
- `audiomass-*.test.mjs` are warm-pool supervisor integration tests (CPU mode) using `audiomass/.venv/bin/python` (override with `AUDIOMASS_PYTHON`) — they exercise the **old** backend slated for scrap; expect them to go with it.
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
- `audiomass/` — **backend is being replaced** (decided 2026-08-27): the web editor frontend is retained, the Python backend (`src/audiomass-server.py`, `backend/adapters` warm pool, demucs pipeline) is slated for scrap — don't fix or extend it. Own CLAUDE.md, ROADMAP.md, run.sh, `.venv`; treat as a sub-project
- `mixer/` — new stem-mixer frontend for the rebuild (vanilla JS + Tone.js from CDN, **no build step**, own git repo); Phase 2 done, Phase 3.3/3.4 wired 2026-08-27. Talks to the AudioMass REST API on :5055 (`/api/jobs`, `/api/jobs/{id}/manifest`, `/stems/{name}`, `/source`) — that endpoint set is the **contract the new backend must serve**. Served on :5058 (NOT 5060 — browsers hard-block it with `ERR_UNSAFE_PORT`) by systemd user unit `mixer.service`, started/stopped by the launcher; Caddy route `/mixer`
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
- `audiomass/` → `audiomass/CLAUDE.md` and `audiomass/ROADMAP.md`
- Launcher/proxy → header comments of `run-aether-with-audiomass.sh` and `/mnt/Pandora/caddy/Caddyfile`

## Review workflow (when user asks "review the project")

1. Read-only first: `src/`, package.json, tsconfig.json, vite.config.ts.
2. `npm run build` for the current compile state.
3. Report: (a) what works, (b) broken/wonky, (c) ranked improvement suggestions — cite `file_path:line_number`.
4. Hand off runtime checks explicitly: "user to verify in browser".
