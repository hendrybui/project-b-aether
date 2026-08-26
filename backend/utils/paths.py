import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
# Overridable so a test instance can isolate its jobs from the real library
# (same spirit as AUDIOMASS_PORT). Without this, a smoke-test server would
# write into the user's actual jobs root and its startup recovery would mark
# the user's in-flight jobs as failed.
JOBS_DIR = Path(os.environ.get("AUDIOMASS_JOBS_DIR", "/mnt/Pandora/Music/Audiamass"))
DOCS_DIR = ROOT / "docs"
