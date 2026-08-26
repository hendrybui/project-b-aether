from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from domain.enums import JobStatus, SourceType


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateJobRequest(BaseModel):
    source_type: SourceType
    url: Optional[str] = None
    filename: Optional[str] = None
    stems: list[str] = Field(
        default_factory=lambda: ["vocals", "drums", "bass", "guitar", "piano", "other"]
    )


class JobSnapshot(BaseModel):
    job_id: str
    status: JobStatus
    progress: float = 0.0
    step: str = "created"
    message: str = "Job initialized"
    cancellable: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class AnalysisSummary(BaseModel):
    bpm: Optional[float] = None
    key: Optional[str] = None
    scale: Optional[str] = None
    confidence: Optional[float] = None
    lufs_integrated: Optional[float] = None
    peak_dbfs: Optional[float] = None
    duration_sec: Optional[float] = None
    stem_energy: Optional[dict[str, float]] = None


class ManifestSource(BaseModel):
    type: SourceType
    url: Optional[str] = None
    filename: Optional[str] = None


class ManifestResponse(BaseModel):
    job_id: str
    status: JobStatus
    source: ManifestSource
    selected_stems: list[str] = Field(default_factory=list)
    available_stems: list[str] = Field(default_factory=list)
    duration_sec: Optional[float] = None
    analysis: AnalysisSummary = Field(default_factory=AnalysisSummary)
    files: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class ToolReadiness(BaseModel):
    name: str
    available: bool
    path: Optional[str] = None
    detail: Optional[str] = None


class ContainerJobStats(BaseModel):
    """Measured timings from the most recent container-backed separation.

    wall_sec is the docker process wall time (launch to exit); compute_sec is
    the worker's chunk inference + stem-write time, which is the part that
    scales with track length (e.g. 20s of audio in ~3s); overhead_sec =
    wall - compute is the fixed per-job cost (python/torch/HIP startup, model
    load + warmup, teardown) that no optimization of the compute itself can
    remove; ready_sec is the worker-reported warmup portion of that overhead.
    realtime is the worker-reported compute ratio (compute / audio).
    """

    at: str
    image: str
    wall_sec: float
    ready_sec: float
    compute_sec: float
    overhead_sec: float
    audio_sec: float
    realtime: float  # worker-reported ratio (compute / audio)


class WarmPoolState(BaseModel):
    """Live state of the warm-pool container (one model load, jobs reused).

    up is whether the pool container is running right now; busy whether it is
    currently separating a job. jobs_served is the CUMULATIVE count of jobs
    the pool has completed — persisted across server restarts and container
    generations (stats.json in the pool dir), so it reads as history, not
    per-process state; ready_sec and started_at are the current generation's
    one-time startup (model load + HIP init + warmup forward).
    idle_timeout_sec is the configured idle-eviction window: the supervisor
    exits (releasing the GPU) after that long without a job, and eviction
    records why the last container generation ended ('idle' |
    'stale_heartbeat' | 'shutdown') until a new generation starts.
    first_seen_at/last_activity_at anchor the persisted history, and last_job
    mirrors ContainerJobStats for the most recent pool job, whose overhead is
    near-zero because the startup is not re-paid.
    """

    up: bool = False
    busy: bool = False
    jobs_served: int = 0
    ready_sec: Optional[float] = None
    started_at: Optional[str] = None
    idle_timeout_sec: Optional[float] = None
    first_seen_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    eviction: Optional[str] = None
    evicted_at: Optional[str] = None
    last_job: Optional[ContainerJobStats] = None


class SeparationBackend(BaseModel):
    """Which separation engine is active and whether it's healthy right now.

    backend is what the next job would use: 'rocm_container' when the
    configured docker image is available (GPU), 'cpu_worker' otherwise.
    container_available + detail explain why the container is or isn't used.
    warm_pool is the live state of the persistent warm-pool container when
    the container backend is in use.
    """

    backend: str  # 'rocm_container' | 'cpu_worker'
    device: str  # 'cuda' | 'cpu'
    image: Optional[str] = None
    container_available: bool = False
    detail: Optional[str] = None
    last_job: Optional[ContainerJobStats] = None
    warm_pool: Optional[WarmPoolState] = None


class DiagnosticsResponse(BaseModel):
    service: str = "splinter-x"
    ready: bool
    tools: list[ToolReadiness]
    plugins: list[str] = []  # registered processing capabilities (see plugins/registry.py)
    separation: Optional[SeparationBackend] = None
