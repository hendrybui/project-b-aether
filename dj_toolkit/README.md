# DJ Toolkit

A small Flask web UI with three DJ tools, built on top of your existing
**AudioMass** backend (htdemucs_6s stem separation + librosa analysis):

1. **Stems & BPM/Key** — upload a track → 6 separated stems + BPM, musical
   key, and Camelot code for harmonic mixing.
2. **Vocal Remover** — one-click instrumental (vocals out) and isolated
   acapella, plus all 6 stems as a ZIP. (Same engine as Stems.)
3. **MP3 → MIDI** — convert audio to a downloadable `.mid` file. Runs locally
   with [basic-pitch](https://github.com/spotify/basic-pitch) (ONNX backend,
   no TensorFlow).

Stems and BPM/Key are **not re-implemented** here — the Flask app is a thin
client over AudioMass's HTTP API. Only MP3→MIDI runs inside this process.

## Prerequisites

- Python 3.11+ (tested on 3.12)
- `ffmpeg` on PATH (already required by AudioMass)
- The **AudioMass backend** running locally — it owns demucs + librosa

This app **shares** `/mnt/Pandora/Project-B/audiomass/.venv`. It does not
create its own virtualenv.

## Setup (first time)

```bash
cd /mnt/Pandora/Project-B/dj_toolkit

# 1. Install the Flask + HTTP deps and the MIDI post-processing libs.
#    (demucs, librosa, torch, numpy, soundfile are already in the audiomass venv.)
../audiomass/.venv/bin/pip install -r requirements.txt

# 2. Install basic-pitch WITHOUT its tensorflow pin (TF <2.15.1 has no
#    Python 3.12 wheel). The ONNX backend bundled in the wheel is used instead.
../audiomass/.venv/bin/pip install --no-deps basic-pitch
```

If `pip` complains about `tensorflow` conflicts afterward, that's expected and
harmless — basic-pitch's TF dependency is simply unsatisfied, and we never
import it.

## Running

You need **two** processes: AudioMass (the engine) and this Flask app (the UI).

```bash
# Terminal 1 — start AudioMass (if not already running)
cd /mnt/Pandora/Project-B/audiomass
AUDIOMASS_PORT=5055 .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5055 --app-dir backend

# Terminal 2 — start the DJ Toolkit UI
cd /mnt/Pandora/Project-B/dj_toolkit
../audiomass/.venv/bin/python app.py
# → open http://localhost:5001
```

Or with Flask's debug reloader:

```bash
../audiomass/.venv/bin/flask --app app run --debug --port 5001
```

The health badge in the navbar shows whether AudioMass and basic-pitch are
reachable. If it says "AudioMass ✗", start the backend (Terminal 1 above).

## Configuration

All settings live in `config.py` and can be overridden with environment
variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUDIOMASS_BASE_URL` | `http://localhost:5055` | AudioMass backend URL (validated: http/https, non-private host) |
| `DJ_TOOLKIT_PORT` | `5001` | Flask port |
| `DJ_TOOLKIT_HOST` | `0.0.0.0` | Flask bind address |
| `DJ_TOOLKIT_DEBUG` | `1` | Flask debug mode (`0` to disable) |
| `MIDI_ONSET_THRESHOLD` | `0.5` | basic-pitch onset sensitivity (lower = more notes) |
| `MIDI_FRAME_THRESHOLD` | `0.3` | basic-pitch frame sensitivity |

## How it works

```
Browser ──HTTP──▶ Flask (dj_toolkit) ──HTTP──▶ AudioMass (FastAPI)
                   │                              │
                   │  /stems, /midi pages         │  POST /api/jobs/upload
                   │  /api/analyze (proxy)        │  GET  /api/jobs/{id}
                   │  /api/job/<id> (poll)        │  GET  /api/jobs/{id}/manifest
                   │  /api/result/<id>            │  GET  /api/jobs/{id}/stems/{stem}
                   │  /api/stem/..., /instrumental│  POST /api/jobs/{id}/export/mix
                   │                              │  POST /api/jobs/{id}/export/batch
                   │  /api/to-midi (LOCAL)        │
                   └── basic-pitch (ONNX) ───────▶│  (no call — runs in-process)
```

- **BPM/Key/Camelot**: AudioMass returns `{key, scale}` in the manifest; the
  Camelot code is computed in Flask from the standard DJ wheel.
- **Vocal Remover**: uses AudioMass's `/export/mix` with the vocals stem muted
  to produce the instrumental. The isolated acapella is just the `vocals` stem.
- **MP3→MIDI**: runs entirely inside Flask via `basic_pitch.inference.predict()`
  with the ONNX backend. The model ships in the wheel — **no download**.

## Constraints & gotchas

- **One job at a time.** AudioMass allows only one active separation server-wide.
  A second upload while one is running returns HTTP 409, which the UI surfaces as
  "AudioMass is busy — retry in a moment."
- **Stem separation is slow on CPU** (~1–3 min for a 3-minute track). The UI
  shows live progress (stage + percentage) by polling every 1.5s.
- **Job status vocabulary.** AudioMass uses `done` (not `complete`) as the
  terminal-success state. The UI maps all 11 lifecycle states to friendly labels.
- **Files are ephemeral.** `tmp/` holds generated MIDI files and is auto-pruned
  (anything older than 1 hour is deleted on each request). AudioMass manages its
  own `jobs/` storage.
- **No Spotify / no YouTube.** Per scope: only file upload is supported.

## Project layout

```
dj_toolkit/
├── app.py                # Flask app: routes, AudioMass proxying, MIDI endpoint
├── audiomass_client.py   # Thin HTTP client over the AudioMass API contract
├── config.py             # Settings, Camelot wheel, job-lifecycle labels
├── midi_extractor.py     # basic-pitch ONNX wrapper (the only new processing)
├── requirements.txt
├── README.md
├── templates/            # base.html, index.html, stems.html, midi.html
├── static/css/style.css  # Dark DJ theme
├── static/js/app.js      # Drag-drop, polling, result rendering
└── tmp/                  # gitignored — MIDI output, auto-cleaned
```

## Troubleshooting

- **`MIDI ✗` in the badge / "No module named 'midi_extractor'"**: you skipped
  the `pip install --no-deps basic-pitch` step, or the post-processing deps
  (`pretty-midi`, `resampy`, `mir-eval`, `onnxruntime`) aren't installed.
- **`AudioMass ✗` in the badge**: the backend isn't running on
  `AUDIOMASS_BASE_URL`. Start it (see Running) or set the env var to its address.
- **Upload rejected (415)**: file extension not in
  `mp3, wav, flac, m4a, aac, ogg`.
- **413 Payload Too Large**: file exceeds the 200 MB Flask cap.
- **Stems page stuck at "separating"**: demucs is CPU-heavy; wait. If it never
  finishes, check the AudioMass logs — the model downloads on first ever use.
