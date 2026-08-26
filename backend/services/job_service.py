from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from domain.enums import JobStatus, SourceType
from domain.models import AnalysisSummary, CreateJobRequest, JobSnapshot, ManifestResponse, ManifestSource
from services.cancellation_service import cancellation_service
from services.event_bus import event_bus
from storage.job_store import JobStore
from utils.validation import ValidationError, validate_create_job_request


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActiveJobConflictError(Exception):
    pass


class JobService:
    def __init__(self) -> None:
        self._jobs: dict[str, JobSnapshot] = {}
        self._manifests: dict[str, ManifestResponse] = {}
        self._active_job_id: str | None = None
        self._lock = Lock()
        self._store = JobStore()
        self._pipeline = None

    TERMINAL_STATUSES = {JobStatus.done, JobStatus.failed, JobStatus.cancelled}

    def attach_pipeline(self, pipeline: 'PipelineService') -> None:
        self._pipeline = pipeline

    def recover_interrupted_jobs(self) -> None:
        """Mark jobs persisted in a non-terminal state as failed.

        The pipeline runs in daemon threads; when the server process dies or is
        restarted mid-job (crash, OOM kill, manual restart), the on-disk
        snapshot keeps saying e.g. 'separating' forever. The UI then shows a
        job that can never finish and blocks its disk entry from being cleaned
        up. Called once at server startup; the message makes clear the work was
        interrupted rather than genuinely failed.
        """
        interrupted: list[str] = []
        with self._lock:
            for entry in self._store.jobs_root.iterdir():
                if not entry.is_dir():
                    continue
                snapshot = self._store.load_snapshot(entry.name)
                if snapshot is None or snapshot.status in self.TERMINAL_STATUSES:
                    continue
                manifest = self._store.load_manifest(entry.name)
                if manifest is None:
                    # Minimal stand-in so mark_failed can persist coherent state.
                    manifest = ManifestResponse(
                        job_id=entry.name,
                        status=snapshot.status,
                        source=ManifestSource(type=SourceType.upload),
                    )
                self._jobs[entry.name] = snapshot
                self._manifests[entry.name] = manifest
                interrupted.append(entry.name)
        for job_id in interrupted:
            self.mark_failed(job_id, 'Job interrupted by server restart')

    def create_job(self, payload: CreateJobRequest) -> JobSnapshot:
        validate_create_job_request(payload)
        with self._lock:
            if self._active_job_id and self._jobs[self._active_job_id].status not in {
                JobStatus.done,
                JobStatus.failed,
                JobStatus.cancelled,
            }:
                raise ActiveJobConflictError('Only one active job is allowed in V1')

            job_id = uuid4().hex[:12]
            now = utc_now_iso()
            snapshot = JobSnapshot(
                job_id=job_id,
                status=JobStatus.created,
                progress=0.0,
                step=JobStatus.created.value,
                message='Job initialized',
                cancellable=True,
                created_at=now,
                updated_at=now,
            )
            manifest = ManifestResponse(
                job_id=job_id,
                status=JobStatus.created,
                source=ManifestSource(type=payload.source_type, url=payload.url, filename=payload.filename),
                selected_stems=payload.stems,
                available_stems=[],
                created_at=now,
                updated_at=now,
            )
            self._jobs[job_id] = snapshot
            self._manifests[job_id] = manifest
            self._active_job_id = job_id
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_state')

        if self._pipeline is not None:
            self._pipeline.start(job_id)
        return deepcopy(snapshot)

    def get_job(self, job_id: str) -> JobSnapshot | None:
        snapshot = self._jobs.get(job_id) or self._store.load_snapshot(job_id)
        return deepcopy(snapshot) if snapshot else None

    def get_active_job(self) -> JobSnapshot | None:
        """The currently running job, if any (None when idle or after a restart
        has recovered stranded jobs). Used by the frontend to resume the
        progress modal after a page reload."""
        with self._lock:
            job_id = self._active_job_id
        return self.get_job(job_id) if job_id else None

    def get_manifest(self, job_id: str) -> ManifestResponse | None:
        manifest = self._manifests.get(job_id) or self._store.load_manifest(job_id)
        return deepcopy(manifest) if manifest else None

    def update_job(self, job_id: str, *, status: JobStatus, progress: float, step: str, message: str) -> None:
        with self._lock:
            snapshot = self._jobs[job_id]
            manifest = self._manifests[job_id]
            snapshot.status = status
            snapshot.progress = progress
            snapshot.step = step
            snapshot.message = message
            snapshot.updated_at = utc_now_iso()
            snapshot.cancellable = status not in {JobStatus.done, JobStatus.failed, JobStatus.cancelled}
            manifest.status = status
            manifest.updated_at = snapshot.updated_at
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_progress')

    def update_manifest_files(self, job_id: str, files: dict[str, str]) -> None:
        with self._lock:
            manifest = self._manifests[job_id]
            manifest.files.update(files)
            manifest.updated_at = utc_now_iso()
            self._persist(job_id)

    def update_analysis(self, job_id: str, analysis: dict) -> None:
        with self._lock:
            manifest = self._manifests[job_id]
            manifest.analysis = AnalysisSummary.model_validate(analysis)
            duration = analysis.get('duration_sec')
            if duration is not None:
                manifest.duration_sec = float(duration)
            manifest.updated_at = utc_now_iso()
            self._persist(job_id)

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            snapshot = self._jobs.get(job_id)
            if snapshot is None or snapshot.status in {JobStatus.done, JobStatus.failed, JobStatus.cancelled}:
                return False
            cancellation_service.request_cancel(job_id)
            snapshot.message = 'Cancellation requested'
            snapshot.updated_at = utc_now_iso()
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_progress')
            return True

    def mark_cancelled(self, job_id: str, message: str) -> None:
        with self._lock:
            snapshot = self._jobs[job_id]
            manifest = self._manifests[job_id]
            snapshot.status = JobStatus.cancelled
            snapshot.progress = min(snapshot.progress, 0.99)
            snapshot.step = JobStatus.cancelled.value
            snapshot.message = message
            snapshot.cancellable = False
            snapshot.updated_at = utc_now_iso()
            manifest.status = JobStatus.cancelled
            manifest.updated_at = snapshot.updated_at
            self._active_job_id = None
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_cancelled')

    def mark_failed(self, job_id: str, message: str) -> None:
        with self._lock:
            snapshot = self._jobs[job_id]
            manifest = self._manifests[job_id]
            snapshot.status = JobStatus.failed
            snapshot.step = JobStatus.failed.value
            snapshot.message = message
            snapshot.cancellable = False
            snapshot.updated_at = utc_now_iso()
            manifest.status = JobStatus.failed
            manifest.updated_at = snapshot.updated_at
            self._active_job_id = None
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_failed')

    def mark_done(self, job_id: str) -> None:
        with self._lock:
            snapshot = self._jobs[job_id]
            manifest = self._manifests[job_id]
            snapshot.status = JobStatus.done
            snapshot.progress = 1.0
            snapshot.step = JobStatus.done.value
            snapshot.message = 'Phase 1 pipeline completed'
            snapshot.cancellable = False
            snapshot.updated_at = utc_now_iso()
            manifest.status = JobStatus.done
            manifest.available_stems = sorted(set(list(manifest.selected_stems) + ['original', 'mix']))
            manifest.updated_at = snapshot.updated_at
            self._active_job_id = None
            self._persist(job_id)
            self._publish_snapshot(job_id, 'job_done')

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            snapshot = self._jobs.get(job_id) or self._store.load_snapshot(job_id)
            if snapshot is None:
                return False
            if snapshot.status not in {JobStatus.done, JobStatus.failed, JobStatus.cancelled}:
                return False
            self._jobs.pop(job_id, None)
            self._manifests.pop(job_id, None)
            if self._active_job_id == job_id:
                self._active_job_id = None
        return self._store.delete_job_dir(job_id)

    def _persist(self, job_id: str) -> None:
        self._store.save_snapshot(self._jobs[job_id])
        self._store.save_manifest(self._manifests[job_id])

    def _publish_snapshot(self, job_id: str, event_name: str) -> None:
        snapshot = self._jobs[job_id]
        event_bus.publish(job_id, event_name, snapshot.model_dump_json())


job_service = JobService()

from services.pipeline_service import PipelineService  # noqa: E402

job_service.attach_pipeline(PipelineService(job_service))
