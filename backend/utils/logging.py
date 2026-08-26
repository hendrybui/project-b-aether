from __future__ import annotations

import logging
from pathlib import Path

from utils.paths import JOBS_DIR

_LOGGING_INITIALIZED = False


def configure_logging() -> None:
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
    _LOGGING_INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def get_job_log_path(job_id: str) -> Path:
    path = JOBS_DIR / job_id / "logs" / "pipeline.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
