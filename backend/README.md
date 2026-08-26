# AudioMass API — the rebuilt backend

FastAPI backend serving the REST contract in `../API-CONTRACT.md`.
Moved + pruned from the old `audiomass/backend/` (2026-08-27); the old
backend AND editor were deleted at cutover — `audiomass/` is gone.
Clients: Stem Mixer (:5058) and Aether (bounce upload).

## Run

Served by systemd user unit `mass-backend.service` on **:5055** (the
launcher starts/stops it). The unit sets
`AUDIOMASS_DEMUCS_DOCKER_IMAGE=rocm64_gfx803_demucs:2.4` for GPU
separation; without docker the pipeline falls back to the CPU worker.

```bash
systemctl --user start mass-backend    # or: ./run-aether-with-audiomass.sh start
# manual: AUDIOMASS_DEMUCS_DOCKER_IMAGE=rocm64_gfx803_demucs:2.4 \
#   backend/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5055 --app-dir backend
```

## Venv

`backend/.venv` — dedicated copy (everything installed: fastapi, uvicorn,
sse-starlette, librosa, torch, demucs, basic-pitch, flask for the DJ
Toolkit). Rebuild from `requirements.txt` if ever needed.

## Tests

```bash
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests -v
# 61 tests — plugin internals, pipeline progress/cancel/teardown. No GPU/docker needed.
```

## Paths (utils/paths.py)

- `JOBS_DIR` — env `AUDIOMASS_JOBS_DIR`, default `/mnt/Pandora/Music/Audiamass`
  (all existing separations intact — shared with the old backend).
- `/` returns a JSON pointer to the mixer (the old editor static mount is gone).
- `docker/Dockerfile.demucs-rocm` — recipe to rebuild the ROCm GPU image.
