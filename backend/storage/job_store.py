from __future__ import annotations

import json
import shutil
from pathlib import Path

from domain.models import JobSnapshot, ManifestResponse
from utils.paths import JOBS_DIR


class JobStore:
    def __init__(self, jobs_root: Path | None = None) -> None:
        self.jobs_root = jobs_root or JOBS_DIR
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def ensure_job_dir(self, job_id: str) -> Path:
        job_dir = self.jobs_root / job_id
        for subdir in ('source', 'stems', 'analysis', 'waveforms', 'logs'):
            (job_dir / subdir).mkdir(parents=True, exist_ok=True)
        return job_dir

    def snapshot_path(self, job_id: str) -> Path:
        return self.ensure_job_dir(job_id) / 'snapshot.json'

    def manifest_path(self, job_id: str) -> Path:
        return self.ensure_job_dir(job_id) / 'manifest.json'

    def save_snapshot(self, snapshot: JobSnapshot) -> None:
        self.snapshot_path(snapshot.job_id).write_text(snapshot.model_dump_json(indent=2), encoding='utf-8')

    def save_manifest(self, manifest: ManifestResponse) -> None:
        self.manifest_path(manifest.job_id).write_text(manifest.model_dump_json(indent=2), encoding='utf-8')

    def load_snapshot(self, job_id: str) -> JobSnapshot | None:
        path = self.snapshot_path(job_id)
        if not path.exists():
            return None
        return JobSnapshot.model_validate(json.loads(path.read_text(encoding='utf-8')))

    def load_manifest(self, job_id: str) -> ManifestResponse | None:
        path = self.manifest_path(job_id)
        if not path.exists():
            return None
        return ManifestResponse.model_validate(json.loads(path.read_text(encoding='utf-8')))

    def delete_job_dir(self, job_id: str) -> bool:
        target = self.jobs_root / job_id
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True
