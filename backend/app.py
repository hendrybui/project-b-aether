from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.jobs import router as jobs_router
from api.projects import router as projects_router
from api.streams import router as streams_router
from api.source import router as source_router
from services.job_service import job_service


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

    # The Stem Mixer is a STANDALONE DAW served by its own static server
    # (:5055, mixer.service). This backend is reference/API only and holds
    # no port claim anymore — it must not be running while the mixer owns :5055.
    @app.get("/")
    async def root() -> dict:
        return {
            "service": "audiomass-api (reference)",
            "status": "ok",
            "clients": {"mixer": "http://localhost:5055", "aether": "http://localhost/aether/"},
        }

    return app


app = create_app()
