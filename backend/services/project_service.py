"""Project save/load service for persisting multitrack sessions."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.paths import JOBS_DIR

PROJECTS_DIR = JOBS_DIR / "projects"

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_id(project_id: str) -> None:
    if not _SAFE_ID.match(project_id):
        raise ValueError(f"Invalid project ID: {project_id!r}")


def _ensure_dir() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def _project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def _meta_path(project_id: str) -> Path:
    return _project_dir(project_id) / "project.json"


def list_projects() -> list[dict]:
    """Return all saved projects sorted by most recent first."""
    _ensure_dir()
    projects = []
    for d in sorted(PROJECTS_DIR.iterdir(), reverse=True):
        meta = d / "project.json"
        if meta.exists():
            try:
                projects.append(json.loads(meta.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return projects


def save_project(
    project_id: str,
    name: str,
    state_json: str,
    clip_files: dict[str, Path],
) -> dict:
    """Save a project: metadata + state + clip audio buffers."""
    _validate_id(project_id)
    _ensure_dir()
    pdir = _project_dir(project_id)
    if pdir.exists():
        shutil.rmtree(pdir)
    pdir.mkdir(parents=True, exist_ok=True)

    # write clip buffers as WAV files
    clip_map: dict[str, str] = {}
    clips_dir = pdir / "clips"
    clips_dir.mkdir(exist_ok=True)
    for clip_id, src_path in clip_files.items():
        dst = clips_dir / f"{clip_id}.wav"
        shutil.copy2(src_path, dst)
        clip_map[clip_id] = f"clips/{clip_id}.wav"

    # parse state to extract clip buffer references
    state = json.loads(state_json)

    # build metadata
    now = datetime.now(timezone.utc).isoformat()
    track_count = len(state.get("tracks", []))
    clip_count = len(state.get("clips", []))
    meta = {
        "project_id": project_id,
        "name": name,
        "track_count": track_count,
        "clip_count": clip_count,
        "created_at": now,
        "updated_at": now,
    }

    # write metadata
    _meta_path(project_id).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # write state (without buffer objects — buffers are on disk as clips)
    state_path = pdir / "state.json"
    state_path.write_text(state_json, encoding="utf-8")

    return meta


def load_project(project_id: str) -> Optional[dict]:
    """Load project state + return metadata and clip file paths."""
    _validate_id(project_id)
    pdir = _project_dir(project_id)
    if not pdir.exists():
        return None

    meta_file = _meta_path(project_id)
    if not meta_file.exists():
        return None

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    state_file = pdir / "state.json"
    if not state_file.exists():
        return None

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # resolve clip paths
    clips_dir = pdir / "clips"
    clip_paths: dict[str, str] = {}
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            if f.suffix == ".wav":
                clip_paths[f.stem] = str(f)

    return {
        "meta": meta,
        "state": state,
        "clip_paths": clip_paths,
    }


def delete_project(project_id: str) -> bool:
    """Delete a project directory."""
    _validate_id(project_id)
    pdir = _project_dir(project_id)
    if not pdir.exists():
        return False
    shutil.rmtree(pdir)
    return True
