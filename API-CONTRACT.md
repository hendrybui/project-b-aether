# API-CONTRACT.md — the AudioMass backend REST contract

> **The new backend is LIVE in development** (2026-08-27): `backend/` at the
> repo root — FastAPI, moved + pruned from the old `audiomass/backend/`.
> Serving on **:5056** while the old backend holds :5055; all routes below
> verified against it end-to-end (CPU *and* ROCm warm-pool GPU paths,
> 61/61 unit tests). Cutover = flip the port. This doc stays the spec.
>
> **This is the spec the new backend must serve.** Written 2026-08-27 by
> enumerating every route the *current* stdlib server implements
> (`audiomass/src/audiomass-server.py`) and every call the three consumers
> actually make. The old Python backend is slated for scrap
> (see `audiomass/ROADMAP.md` pivot note); as long as the new backend serves
> these routes with these shapes, **no frontend changes are needed**.

## Consumers (verified call sites)

| Consumer | Calls | Source |
|---|---|---|
| **Stem Mixer** (mixing frontend) | `GET /api/jobs`, `GET /api/jobs/{id}/manifest`, `GET /api/jobs/{id}/stems/{name}?format=wav` | `mixer/mixer.js:1072-1110` (API base hardcoded to `:5055`, `mixer.js:36-37`) |
| **Aether** (synth, bounce handoff) | `POST /api/jobs/upload`, `GET /api/jobs/active` (2s poll), `POST /api/jobs/{id}/cancel`, `GET /api/diagnostics` (backend label) | `src/main.ts:802,1339,1415,1433` |
| **AudioMass editor frontend** (retained) | `GET /api/jobs/{id}/events` (SSE progress), `GET /api/jobs…` (auto-load via `?job=` URL param), `POST /api/tempo-segment?file=` (BPM scan), project save/load + clip WAVs, `POST /api/transcribe` | `audiomass/src/auto-load.js:78`, `engine.js:586-588`, `stems.js:11` |

## Routes

### GET
| Route | Purpose | Notes |
|---|---|---|
| `/api/health`, `/api/` | liveness | 200 JSON |
| `/api/diagnostics` | separation engine + warm-pool status | Aether reads it to label its AI backend indicator (`main.ts:1339`) — keep at least a `separation` object with the next-job engine name |
| `/api/jobs` | list all separation jobs | array of snapshots |
| `/api/jobs/active` | the in-flight job or `null` | **must route-match before** `/api/jobs/{id}` ("active" is a valid id pattern) |
| `/api/jobs/{id}` | one job snapshot | 404 unknown id |
| `/api/jobs/{id}/manifest` | job metadata: stems list, BPM, key | mixer renders from this |
| `/api/jobs/{id}/stems/{name}?format=wav` | stem audio bytes | `name` covers all stems incl. mix/original; `format=wav` requested by mixer |
| `/api/jobs/{id}/events` | **SSE** job progress | see protocol below |
| `/api/projects` | list saved editor projects | |
| `/api/projects/{id}` | load project state | |
| `/api/projects/{id}/clips/{clip_id}` | clip WAV bytes | |
| `/api/tempo-segment` | first-180s mono WAV for fast BPM scan | both GET (query `?file=`) and POST |

### POST
| Route | Purpose | Notes |
|---|---|---|
| `/api/jobs/upload` | multipart intake → creates separation job | Fields: `file` (the WAV blob — this exact name; Aether `main.ts:800` and stems.js both use it) + optional `stems` (JSON **string** form field, e.g. `["vocals","drums"]`). **409** when a job is already active — single-active-job semantics; Aether surfaces this. 422 on non-multipart |
| `/api/jobs/{id}/cancel` | request cancel → `{"status":"cancel_requested"}` | 404 if not cancellable |
| `/api/transcribe` | audio → JSON notes (basic-pitch) | notes only, **no MIDI file** (roadmap 6.1) |
| `/api/projects` | save project state | |
| `/api/tempo-segment` | same as GET, body form | |

### DELETE
| Route | Purpose |
|---|---|
| `/api/jobs/{id}` | delete job (404 unless terminal state) |
| `/api/projects/{id}` | delete project |

### Static
The old server also serves the AudioMass editor frontend itself from `/`
(`serve_static`, `audiomass-server.py:335`). After the rebuild the frontend can
be served by anything static — only keep it co-served if you want the
`?job=` deep-link (`auto-load.js`) to stay same-origin.

## Job lifecycle (state machine)

```
upload → queued → running → done
                       ├── error
                       └── cancelled   (via /cancel; lands in 'cancelled')
```

- **One active job at a time** — a second upload while running gets **409**.
- Job id charset: `[A-Za-z0-9_-]+` (route regexes).
- **SSE** `/api/jobs/{id}/events`: JSON progress markers; the pool protocol
  internally speaks JSONL `{"status": ...}` lines (warm pool) — the new
  backend may reimplement internals freely, only the SSE surface is contract.
- Stems per job: 6 model stems + mix + original (HTDemucs layout).

## Internal machinery — NOT part of the contract

The old backend's internals (warm-pool supervisor `request.json` atomic-rename
file protocol, heartbeat, docker container `audiomass-demucs-pool`, idle
eviction, `jobs/_incoming/` intake dir) may be replaced wholesale. Two pieces
are worth **reusing** rather than rebuilding:

1. **`rocm64_gfx803_demucs:2.4`** docker image + HTDemucs pipeline (works on
   the RX 580, model load ~40s, warm pool serves repeat jobs).
2. `scripts/check-demucs-gpu.sh` — back-to-back separation smoke test; adapt
   as the new backend's health check.

## Port & proxy

REST API lives on **:5055** (hardcoded in mixer.js; Aether default base).
Caddy routes `/mass*` and `/audiomass*` → `:5055` (with `strip_prefix`).
Keep 5055 at cutover or update: `mixer/mixer.js:36`, `src/main.ts`
(`DEFAULT_AM_BASE`), `/mnt/Pandora/caddy/Caddyfile`.
