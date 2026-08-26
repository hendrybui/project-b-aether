"""Endpoint to serve the original uploaded source audio for a job.

This lets the Aether editor (and any other frontend) auto-load the
bounced WAV without manual drag-and-drop — the browser opens
/mass/?job=JOB_ID, the editor fetches /api/jobs/JOB_ID/source,
and decodes it into the multitrack waveform view.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from services.job_service import job_service

router = APIRouter(tags=['jobs'])


@router.get('/jobs/{job_id}/source')
async def get_source_audio(job_id: str) -> FileResponse:
    """Return the original uploaded/recorded source audio for a job.

    The file path is stored in the manifest's ``source.filename`` field
    (set at upload time by ``/api/jobs/upload``).  Returns the raw file
    with the correct MIME type so the browser can decode it directly.
    """
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Job not found')

    src = manifest.source
    if not src.filename:
        raise HTTPException(status_code=404, detail='No source file recorded for this job')

    p = Path(src.filename)
    if not p.exists():
        raise HTTPException(status_code=404, detail='Source file not found on disk')

    # Guess MIME from extension
    suffix = p.suffix.lower()
    mime_map = {
        '.wav': 'audio/wav',
        '.mp3': 'audio/mpeg',
        '.flac': 'audio/flac',
        '.ogg': 'audio/ogg',
    }
    media_type = mime_map.get(suffix, 'application/octet-stream')

    return FileResponse(
        p,
        media_type=media_type,
        filename=p.name,
    )
