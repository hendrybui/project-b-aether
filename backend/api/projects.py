"""API routes for project save/load."""

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from services import project_service

router = APIRouter(tags=["projects"])


@router.get("/projects")
async def list_projects():
    """List all saved projects."""
    return project_service.list_projects()


@router.post("/projects")
async def save_project(
    name: str = Form("Untitled Project"),
    state: str = Form("{}"),
    clips: list[UploadFile] = [],
):
    """Save a multitrack project with state + clip audio files."""
    project_id = uuid.uuid4().hex[:12]

    # save uploaded clip files to temp locations
    clip_files: dict[str, Path] = {}
    tmp_dir = tempfile.mkdtemp(prefix="am_proj_")
    try:
        for clip_upload in clips:
            clip_id = clip_upload.filename or "clip"
            # strip .wav extension if present — clip_id is the key
            if clip_id.endswith(".wav"):
                clip_id = clip_id[:-4]
            dst = Path(tmp_dir) / clip_upload.filename
            content = await clip_upload.read()
            dst.write_bytes(content)
            clip_files[clip_id] = dst

        meta = project_service.save_project(
            project_id=project_id,
            name=name,
            state_json=state,
            clip_files=clip_files,
        )
        return JSONResponse(content=meta, status_code=201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}")
async def load_project(project_id: str):
    """Load a project — returns state JSON and clip audio file paths."""
    data = project_service.load_project(project_id)
    if not data:
        raise HTTPException(status_code=404, detail="Project not found")
    # Return state + clip list; clips are fetched individually
    return {
        "meta": data["meta"],
        "state": data["state"],
        "clips": list(data["clip_paths"].keys()),
    }


@router.get("/projects/{project_id}/clips/{clip_id}")
async def get_clip(project_id: str, clip_id: str):
    """Serve a single clip audio file."""
    data = project_service.load_project(project_id)
    if not data:
        raise HTTPException(status_code=404, detail="Project not found")
    path = data["clip_paths"].get(clip_id)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path, media_type="audio/wav", filename=f"{clip_id}.wav")


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a saved project."""
    ok = project_service.delete_project(project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted", "project_id": project_id}
