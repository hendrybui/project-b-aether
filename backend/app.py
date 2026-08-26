from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from api.jobs import router as jobs_router
from api.projects import router as projects_router
from api.streams import router as streams_router
from api.source import router as source_router
from services.job_service import job_service
from utils.paths import ROOT

# The AudioMass editor frontend still lives in the audiomass/ sub-project
# (only the backend was rebuilt). Serve it same-origin so the ?job= deep-link
# (src/auto-load.js) and relative /api calls keep working.
SRC_DIR = ROOT / "audiomass" / "src"


def create_app() -> FastAPI:
    app = FastAPI(title="AudioMass API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes first, then static files catch-all
    app.include_router(jobs_router, prefix="/api")
    app.include_router(projects_router, prefix="/api")
    app.include_router(streams_router, prefix="/api")
    app.include_router(source_router, prefix="/api")

    # Jobs left mid-flight by a previous server process can never finish;
    # mark them failed so they don't sit in a non-terminal state forever.
    job_service.recover_interrupted_jobs()

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "service": "audiomass-stems"}

    @app.get("/api/tools")
    async def tools() -> dict:
        return {
            "status": "ok",
            "notes": "Tool readiness is currently inferred by pipeline adapters at runtime.",
            "active_job": job_service.get_job(job_service._active_job_id).model_dump() if job_service._active_job_id else None,
        }

    # Serve AudioMass static files (must be last — catch-all)
    app.mount("/", StaticFiles(directory=str(SRC_DIR), html=True), name="static")

    return app


app = create_app()
