import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from domain.enums import SourceType
from domain.models import CreateJobRequest, DiagnosticsResponse, JobSnapshot, ManifestResponse
from services.job_service import ActiveJobConflictError, job_service
from services.tooling_service import tooling_service
from utils.paths import JOBS_DIR
from utils.validation import ValidationError, validate_upload_filename

router = APIRouter(tags=['jobs'])


class MixSettings(BaseModel):
    """Settings for custom mix export"""
    master_gain: float = 1.0
    format: str = 'wav'  # wav, mp3, flac, ogg
    stems: dict[str, dict]  # stem_name -> {gain, muted, soloed}


@router.get('/diagnostics', response_model=DiagnosticsResponse)
async def diagnostics() -> DiagnosticsResponse:
    return tooling_service.diagnostics()


@router.post('/jobs', response_model=JobSnapshot, status_code=status.HTTP_201_CREATED)
async def create_job(payload: CreateJobRequest) -> JobSnapshot:
    try:
        return job_service.create_job(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post('/jobs/upload', response_model=JobSnapshot, status_code=status.HTTP_201_CREATED)
async def create_upload_job(file: UploadFile = File(...), stems: str = Form('["vocals","drums","bass","guitar","piano","other"]')) -> JobSnapshot:
    filename = file.filename or 'upload.wav'
    if not validate_upload_filename(filename):
        raise HTTPException(status_code=422, detail='Unsupported upload file type')

    try:
        stem_list = json.loads(stems)
        if not isinstance(stem_list, list):
            raise ValueError('stems must be a JSON array')
    except Exception as exc:
        raise HTTPException(status_code=422, detail='Invalid stems payload') from exc

    upload_dir = JOBS_DIR / '_incoming'
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    target = upload_dir / safe_name
    data = await file.read()
    target.write_bytes(data)

    payload = CreateJobRequest(source_type=SourceType.upload, filename=str(target), stems=stem_list)
    try:
        return job_service.create_job(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get('/jobs/active', response_model=JobSnapshot)
async def get_active_job() -> JobSnapshot | JSONResponse:
    """The in-flight job, if any. Must be declared before '/jobs/{job_id}'
    so 'active' isn't captured as a job id. Lets the frontend resume the
    progress modal after a page reload."""
    snapshot = job_service.get_active_job()
    if snapshot is None:
        # 200, not 404: "no active job" is the normal idle state, and the
        # Aether bridge polls this every 2s — a 404 would log a console
        # error on every poll.
        return JSONResponse(content={'active': False})
    return snapshot


@router.get('/jobs')
async def list_jobs() -> list[dict]:
    """List completed separation jobs from disk (reads manifests)."""
    jobs = []
    if not JOBS_DIR.exists():
        return jobs
    for entry in sorted(JOBS_DIR.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith('_'):
            continue
        manifest_path = entry / 'manifest.json'
        if not manifest_path.exists():
            continue
        try:
            import json
            m = json.loads(manifest_path.read_text(encoding='utf-8'))
            # Only include jobs that have stems (completed separations)
            stems_dir = entry / 'stems'
            if not stems_dir.exists():
                continue
            stem_files = [f.stem for f in stems_dir.iterdir() if f.suffix == '.wav']
            if not stem_files:
                continue
            source_name = ''
            if m.get('source', {}).get('filename'):
                source_name = Path(m['source']['filename']).name
            jobs.append({
                'job_id': m.get('job_id', entry.name),
                'status': m.get('status', 'done'),
                'source_name': source_name,
                'stems': sorted(stem_files),
                'duration_sec': m.get('duration_sec') or m.get('analysis', {}).get('duration_sec'),
                'bpm': m.get('analysis', {}).get('bpm'),
                'created_at': m.get('created_at', ''),
            })
        except Exception:
            continue
    return jobs


@router.get('/jobs/{job_id}', response_model=JobSnapshot)
async def get_job(job_id: str) -> JobSnapshot:
    snapshot = job_service.get_job(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail='Job not found')
    return snapshot


@router.get('/jobs/{job_id}/manifest', response_model=ManifestResponse)
async def get_manifest(job_id: str) -> ManifestResponse:
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Manifest not found')
    return manifest


@router.get('/jobs/{job_id}/stems/{stem_name}')
async def get_stem_audio(job_id: str, stem_name: str, format: str = 'wav') -> FileResponse:
    """Get stem audio in specified format (wav, mp3, flac, ogg)"""
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Job not found')

    # Validate format
    valid_formats = ['wav', 'mp3', 'flac', 'ogg']
    if format not in valid_formats:
        raise HTTPException(status_code=422, detail=f'Invalid format. Must be one of: {valid_formats}')

    # Check direct stem file mapping first, then files dict
    audio_path = manifest.files.get(stem_name)
    if audio_path is None:
        raise HTTPException(status_code=404, detail=f'Stem {stem_name} not found')
    p = Path(audio_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail='Audio file not found on disk')

    # If WAV requested, return directly
    if format == 'wav':
        return FileResponse(p, media_type='audio/wav', filename=f'{stem_name}.wav')

    # For other formats, convert with ffmpeg
    format_config = {
        'mp3': {
            'suffix': '.mp3',
            'media_type': 'audio/mpeg',
            'filename': f'{stem_name}.mp3',
            'codec_args': ['-codec:a', 'libmp3lame', '-b:a', '192k']
        },
        'flac': {
            'suffix': '.flac',
            'media_type': 'audio/flac',
            'filename': f'{stem_name}.flac',
            'codec_args': ['-codec:a', 'flac']
        },
        'ogg': {
            'suffix': '.ogg',
            'media_type': 'audio/ogg',
            'filename': f'{stem_name}.ogg',
            'codec_args': ['-codec:a', 'libvorbis', '-b:a', '192k']
        }
    }

    config = format_config[format]

    with tempfile.NamedTemporaryFile(suffix=config['suffix'], delete=False) as tmp:
        tmp_path = tmp.name

    cmd = [
        'ffmpeg', '-y',
        '-i', str(p),
        *config['codec_args'],
        tmp_path
    ]

    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return FileResponse(
            tmp_path,
            media_type=config['media_type'],
            filename=config['filename'],
            background=None
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f'Failed to convert audio: {e.stderr.decode() if e.stderr else str(e)}'
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f'Conversion failed: {str(e)}'
        )


@router.post('/jobs/{job_id}/export/mix')
async def export_custom_mix(job_id: str, settings: MixSettings) -> FileResponse:
    """Export custom mix with gain/mute/solo settings applied"""
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Job not found')

    # Validate format
    valid_formats = ['wav', 'mp3', 'flac', 'ogg']
    if settings.format not in valid_formats:
        raise HTTPException(status_code=422, detail=f'Invalid format. Must be one of: {valid_formats}')

    # Get available stems from manifest
    stem_names = manifest.available_stems if manifest.available_stems else manifest.selected_stems

    # Build ffmpeg filter complex for mixing
    inputs = []
    filters = []

    # Check if any stem is soloed
    has_solo = any(s.get('soloed', False) for s in settings.stems.values())

    for i, stem_name in enumerate(stem_names):
        audio_path = manifest.files.get(stem_name)
        if not audio_path:
            continue

        p = Path(audio_path)
        if not p.exists():
            continue

        # Input file
        inputs.extend(['-i', str(p)])

        stem_settings = settings.stems.get(stem_name, {})
        gain = stem_settings.get('gain', 1.0)
        muted = stem_settings.get('muted', False)
        soloed = stem_settings.get('soloed', False)

        # Calculate effective gain
        effective_gain = gain * settings.master_gain

        # Apply mute/solo logic
        if muted:
            effective_gain = 0
        elif has_solo and not soloed:
            effective_gain = 0

        # Build filter for this stem
        filter_parts = []
        if effective_gain != 1.0:
            filter_parts.append(f'volume={effective_gain}')

        if filter_parts:
            filters.append(f'[{i}:0]{",".join(filter_parts)}[stem{i}]')
        else:
            filters.append(f'[{i}:0]acopy[stem{i}]')

    # Mix all stems together
    if len(stem_names) == 1:
        mix_filter = f'[stem0]acopy[out]'
    else:
        stem_inputs = ''.join(f'[stem{i}]' for i in range(len(stem_names)))
        mix_filter = f'{stem_inputs}amix=inputs={len(stem_names)}:duration=longest[out]'

    filters.append(mix_filter)

    # Build ffmpeg command
    filter_complex = ';'.join(filters)

    # Format-specific settings
    format_config = {
        'wav': {
            'suffix': '.wav',
            'media_type': 'audio/wav',
            'filename': 'mix.wav',
            'codec_args': []
        },
        'mp3': {
            'suffix': '.mp3',
            'media_type': 'audio/mpeg',
            'filename': 'mix.mp3',
            'codec_args': ['-codec:a', 'libmp3lame', '-b:a', '192k']
        },
        'flac': {
            'suffix': '.flac',
            'media_type': 'audio/flac',
            'filename': 'mix.flac',
            'codec_args': ['-codec:a', 'flac']
        },
        'ogg': {
            'suffix': '.ogg',
            'media_type': 'audio/ogg',
            'filename': 'mix.ogg',
            'codec_args': ['-codec:a', 'libvorbis', '-b:a', '192k']
        }
    }

    config = format_config[settings.format]

    with tempfile.NamedTemporaryFile(suffix=config['suffix'], delete=False) as tmp:
        tmp_path = tmp.name

    cmd = [
        'ffmpeg', '-y',
        *inputs,
        '-filter_complex', filter_complex,
        '-map', '[out]',
        *config['codec_args'],
        tmp_path
    ]

    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return FileResponse(
            tmp_path,
            media_type=config['media_type'],
            filename=config['filename'],
            background=None
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f'Failed to mix audio: {e.stderr.decode() if e.stderr else str(e)}'
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f'Export failed: {str(e)}'
        )


@router.post('/jobs/{job_id}/export/batch')
async def export_batch_stems(job_id: str, format: str = 'wav') -> FileResponse:
    """Export all stems as a ZIP archive"""
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Job not found')

    # Validate format
    valid_formats = ['wav', 'mp3', 'flac', 'ogg']
    if format not in valid_formats:
        raise HTTPException(status_code=422, detail=f'Invalid format. Must be one of: {valid_formats}')

    # Get available stems
    stem_names = manifest.available_stems if manifest.available_stems else manifest.selected_stems

    # Format configuration
    format_config = {
        'wav': {'suffix': '.wav', 'codec_args': []},
        'mp3': {'suffix': '.mp3', 'codec_args': ['-codec:a', 'libmp3lame', '-b:a', '192k']},
        'flac': {'suffix': '.flac', 'codec_args': ['-codec:a', 'flac']},
        'ogg': {'suffix': '.ogg', 'codec_args': ['-codec:a', 'libvorbis', '-b:a', '192k']}
    }

    config = format_config[format]

    # Create temporary ZIP file (don't delete automatically)
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp_zip:
        zip_path = tmp_zip.name

    # Create temporary directory for conversions
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for stem_name in stem_names:
                audio_path = manifest.files.get(stem_name)
                if not audio_path:
                    continue

                p = Path(audio_path)
                if not p.exists():
                    continue

                # If WAV requested, add directly to ZIP
                if format == 'wav':
                    zip_file.write(p, f'{stem_name}.wav')
                else:
                    # Convert to other format first
                    converted_path = tmp_path / f'{stem_name}{config["suffix"]}'
                    cmd = [
                        'ffmpeg', '-y',
                        '-i', str(p),
                        *config['codec_args'],
                        str(converted_path)
                    ]

                    try:
                        subprocess.run(cmd, capture_output=True, check=True)
                        zip_file.write(converted_path, f'{stem_name}{config["suffix"]}')
                    except subprocess.CalledProcessError as e:
                        # Skip this stem if conversion fails
                        continue

    # Return ZIP file
    return FileResponse(
        zip_path,
        media_type='application/zip',
        filename=f'stems_{job_id[:8]}.zip',
        background=None
    )


@router.get('/jobs/{job_id}/waveforms/{stem_name}')
async def get_waveform(job_id: str, stem_name: str) -> FileResponse:
    manifest = job_service.get_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='Job not found')
    wf_key = f'waveform_{stem_name}'
    wf_path = manifest.files.get(wf_key)
    if wf_path is None:
        raise HTTPException(status_code=404, detail=f'Waveform for {stem_name} not found')
    p = Path(wf_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail='Waveform file not found on disk')
    return FileResponse(p, media_type='application/json', filename=f'{stem_name}.json')


@router.post('/jobs/{job_id}/cancel')
async def cancel_job(job_id: str) -> dict:
    ok = job_service.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail='Job not found or not cancellable')
    return {'job_id': job_id, 'status': 'cancel_requested'}


@router.delete('/jobs/{job_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str) -> None:
    ok = job_service.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail='Job not found or not in terminal state')
