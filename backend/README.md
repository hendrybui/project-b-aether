# AudioMass API — rebuilt backend

FastAPI backend serving the REST contract in `../API-CONTRACT.md`.
Moved + pruned from `audiomass/backend/` on 2026-08-27 (the old backend
is slated for scrap; this is its replacement).

## Run (dev, :5056 — old backend keeps :5055 until cutover)

```bash
# CPU mode (demucs on host)
AUDIOMASS_PORT=5056 ../audiomass/.venv/bin/python -m uvicorn app:app \
  --host 0.0.0.0 --port 5056 --app-dir backend

# GPU mode (ROCm warm pool in docker — RX 580)
AUDIOMASS_PORT=5056 AUDIOMASS_DEMUCS_DOCKER_IMAGE=rocm64_gfx803_demucs:2.4 \
  ../audiomass/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5056 --app-dir backend
```

systemd template: `../systemd/mass-backend.service`.

## Venv

Reuses `../audiomass/.venv` for now (everything needed is installed:
fastapi, uvicorn, sse-starlette, librosa, torch, demucs, basic-pitch).
At cutover, bootstrap a dedicated venv from `requirements.txt` and drop
the audiomass one with the rest of the old backend.

## Tests

```bash
PYTHONPATH=backend ../audiomass/.venv/bin/python -m unittest discover -s backend/tests -v
# 61 tests — plugin internals, pipeline progress/cancel/teardown. No GPU/docker needed.
```

## Paths (utils/paths.py)

- `JOBS_DIR` — env `AUDIOMASS_JOBS_DIR`, default `/mnt/Pandora/Music/Audiamass`
  (shared with the old backend → existing jobs stay visible).
- Static frontend — `audiomass/src/` (served same-origin at `/` for the
  `?job=` deep-link; edit `SRC_DIR` in `app.py` if the frontend moves).
