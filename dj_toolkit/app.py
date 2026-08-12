"""DJ Toolkit — self-contained Flask app.

Three DJ tools, no external server required:
  /        — index
  /stems   — upload → model-selectable stem separation (demucs CLI subprocess)
             + BPM/Key/Camelot (librosa, in-process) + downloads
  /midi    — upload → MIDI file (basic-pitch, in-process)

Why subprocess for demucs: the in-process PyTorch path crashes under web
servers (OpenMP × daemon-thread). See config.py docstring. The demucs CLI runs
in its own process; if it dies, only that job fails — Flask stays up.

No FastAPI / no AudioMass dependency. No Celery — a ThreadPoolExecutor with
NON-daemon workers handles jobs so they don't get killed mid-run and so the
OS reaps the demucs subprocess cleanly.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename

import config

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dj_toolkit")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH


# ── Job store & runner ───────────────────────────────────────────────────
# ThreadPoolExecutor with daemon=False is deliberate: daemon threads get
# killed abruptly on interpreter exit, which can orphan the demucs subprocess
# mid-write. Non-daemon workers let a job finish (or fail cleanly) even if
# Flask is shutting down. max_workers=1 serializes separations — demucs is
# CPU-heavy and running two in parallel just thrashes.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="djjob")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job(kind: str, meta: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[jid] = {
            "job_id": jid,
            "kind": kind,
            "status": "queued",
            "progress": 0.0,
            "stage": "queued",
            "message": "Queued",
            "result": None,
            "error": None,
            "created_at": time.time(),
            "meta": meta,
        }
    return jid


def _update(jid: str, **fields) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(fields)


def _set_progress(jid: str, fraction: float, stage: str) -> None:
    _update(jid, progress=max(0.0, min(1.0, fraction)), stage=stage)


# ── Helpers ──────────────────────────────────────────────────────────────
def _allowed(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in config.ALLOWED_EXTENSIONS
    )


def _prune_tmp() -> None:
    cutoff = time.time() - config.TMP_MAX_AGE_SECONDS
    for p in config.TMP_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


@app.before_request
def _before_request() -> None:
    _prune_tmp()


def _error(message: str, code: int):
    return jsonify({"error": message}), code


def _job_dir(jid: str) -> Path:
    d = config.TMP_DIR / jid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Stems + BPM/Key worker (runs in the pool) ───────────────────────────
def _run_stems_job(jid: str, in_path: Path, model_id: str) -> None:
    """The actual separation + analysis pipeline for one job."""
    job_dir = _job_dir(jid)
    try:
        # 1. Analyze first (fast, ~1-5s) — gives the user BPM/key quickly.
        _update(jid, status="analyzing", stage="analyzing", message="Detecting BPM & key…")
        import analyzer
        analysis = analyzer.analyze(in_path)
        _update(jid, progress=0.15)

        # 2. Separate (slow, 1-3 min) via the demucs CLI subprocess.
        _update(
            jid, status="separating", stage="separating",
            message=f"Separating with {model_id}…",
        )
        import stems_worker
        stems = stems_worker.separate(
            in_path,
            out_dir=job_dir / "separated",
            model_id=model_id,
            progress=lambda f, s: _set_progress(jid, 0.15 + f * 0.75, "separating"),
        )

        # 3. Build an instrumental (vocals removed) for the DJ workflow.
        _update(jid, status="packaging", stage="packaging", progress=0.93,
                message="Building instrumental…")
        instrumental = stems_worker.build_instrumental(
            stems, job_dir / "instrumental.wav"
        )

        # 4. Done — assemble result.
        result = {
            "analysis": analysis,
            "camelot": config.camelot_for(analysis.get("key"), analysis.get("scale")),
            "model_id": model_id,
            "model_label": config.DEMUCS_MODELS[model_id]["label"],
            "stems": sorted(stems.keys()),
            "has_instrumental": instrumental is not None,
        }
        _update(
            jid, status="done", stage="done", progress=1.0,
            message="Complete", result=result,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("stems job %s failed", jid)
        _update(jid, status="failed", stage="failed", error=str(e),
                message=f"Failed: {e}")


# ── Pages ────────────────────────────────────────────────────────────────
@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/stems")
def stems_page() -> str:
    return render_template(
        "stems.html",
        models=config.DEMUCS_MODELS,
        default_model=config.DEFAULT_MODEL,
    )


@app.route("/midi")
def midi_page() -> str:
    return render_template(
        "midi.html",
        onset=config.MIDI_ONSET_THRESHOLD,
        frame=config.MIDI_FRAME_THRESHOLD,
    )


# ── API: health ──────────────────────────────────────────────────────────
@app.get("/api/health")
def api_health():
    """Self-check: demucs binary present, librosa importable, basic-pitch."""
    status = {"demucs": "unknown", "librosa": "unknown", "midi": "unknown", "ffmpeg": "unknown"}

    # demucs binary
    if Path(config.DEMUCS_BIN).is_file():
        status["demucs"] = "ok"
    else:
        status["demucs"] = f"missing: {config.DEMUCS_BIN}"

    # ffmpeg
    import shutil
    status["ffmpeg"] = "ok" if shutil.which("ffmpeg") else "missing"

    # librosa (analysis)
    try:
        import analyzer  # noqa: F401
        status["librosa"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["librosa"] = f"unavailable: {e}"

    # basic-pitch (midi)
    try:
        import midi_extractor  # noqa: F401
        status["midi"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["midi"] = f"unavailable: {e}"

    overall = "ok" if all(v == "ok" for v in status.values()) else "degraded"
    return jsonify({"status": overall, "components": status})


# ── API: stems + BPM/Key (in-process) ───────────────────────────────────
@app.post("/api/analyze")
def api_analyze():
    """Accept an upload + model selection, queue a separation job."""
    if "file" not in request.files:
        return _error("no file field in request", 400)
    f = request.files["file"]
    if not f or not f.filename:
        return _error("empty filename", 400)
    if not _allowed(f.filename):
        return _error(
            f"unsupported file type. allowed: {sorted(config.ALLOWED_EXTENSIONS)}",
            415,
        )

    model_id = (request.form.get("model") or config.DEFAULT_MODEL).strip()
    if model_id not in config.DEMUCS_MODELS:
        return _error(f"unknown model {model_id!r}", 400)

    safe = secure_filename(f.filename) or "upload"
    jid = _new_job("stems", {"model": model_id, "filename": safe})
    job_dir = _job_dir(jid)
    in_path = job_dir / f"in_{safe}"
    f.save(in_path)

    _executor.submit(_run_stems_job, jid, in_path, model_id)
    return jsonify({"job_id": jid, "status": "queued"}), 201


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    """Poll one job's progress + result."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return _error("job not found", 404)
        snap = dict(job)

    status = snap["status"]
    return jsonify({
        "job_id": job_id,
        "status": status,
        "done": status in config.TERMINAL_STATES,
        "failed": status == "failed",
        "progress": round(snap["progress"] * 100, 1),
        "stage": config.STAGE_LABELS.get(status, status),
        "message": snap.get("message", ""),
        "error": snap.get("error"),
    })


@app.get("/api/result/<job_id>")
def api_result(job_id: str):
    """Return the analysis + stem list for a completed job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return _error("job not found", 404)
        if job["status"] != "done":
            return _error(f"job is {job['status']}, not done", 409)
        result = dict(job["result"] or {})

    a = result.get("analysis") or {}
    return jsonify({
        "job_id": job_id,
        "model_id": result.get("model_id"),
        "model_label": result.get("model_label"),
        "bpm": a.get("bpm"),
        "key": a.get("key"),
        "scale": a.get("scale"),
        "camelot": result.get("camelot"),
        "confidence": a.get("confidence"),
        "lufs_integrated": a.get("lufs_integrated"),
        "peak_dbfs": a.get("peak_dbfs"),
        "duration_sec": a.get("duration_sec"),
        "stems": result.get("stems", []),
        "has_instrumental": result.get("has_instrumental", False),
    })


@app.get("/api/stem/<job_id>/<stem>")
def api_stem_download(job_id: str, stem: str):
    """Download one separated stem by name. ?format=wav|mp3 (default wav)."""
    fmt = request.args.get("format", "wav").lower()
    if fmt not in ("wav", "mp3"):
        return _error("format must be wav or mp3", 400)
    if not stem.replace("_", "").isalnum():  # whitelist: stem names are alphanumeric
        return _error("bad stem name", 400)

    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return _error("job not ready", 404)

    job_dir = _job_dir(job_id)
    # Stems live under separated/<model>/<trackname>/<stem>.wav
    matches = list((job_dir / "separated").rglob(f"{stem}.wav"))
    if not matches:
        return _error(f"stem {stem!r} not found", 404)
    src = matches[0]

    if fmt == "mp3":
        mp3_path = job_dir / f"{stem}.mp3"
        if not mp3_path.is_file():  # transcode once, cache
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-b:a", "192k", str(mp3_path)],
                capture_output=True, check=True,
            )
        return send_file(mp3_path, as_attachment=True, download_name=f"{stem}.mp3")

    return send_file(src, as_attachment=True, download_name=f"{stem}.wav")


@app.get("/api/instrumental/<job_id>")
def api_instrumental_download(job_id: str):
    """Download the pre-built instrumental (vocals removed)."""
    fmt = request.args.get("format", "wav").lower()
    if fmt not in ("wav", "mp3"):
        return _error("format must be wav or mp3", 400)

    job_dir = _job_dir(job_id)
    wav_path = job_dir / "instrumental.wav"
    if not wav_path.is_file():
        return _error("instrumental not available for this job", 404)

    if fmt == "mp3":
        mp3_path = job_dir / "instrumental.mp3"
        if not mp3_path.is_file():
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(wav_path), "-b:a", "192k", str(mp3_path)],
                capture_output=True, check=True,
            )
        return send_file(mp3_path, as_attachment=True, download_name="instrumental.mp3")
    return send_file(wav_path, as_attachment=True, download_name="instrumental.wav")


@app.get("/api/stems-zip/<job_id>")
def api_stems_zip(job_id: str):
    """Bundle all stems (+ instrumental) into a ZIP for download."""
    import io
    import zipfile

    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return _error("job not ready", 404)

    job_dir = _job_dir(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # all stems
        for wav in (job_dir / "separated").rglob("*.wav"):
            zf.write(wav, wav.name)
        # instrumental if present
        inst = job_dir / "instrumental.wav"
        if inst.is_file():
            zf.write(inst, "instrumental.wav")
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"stems_{job_id[:8]}.zip",
        mimetype="application/zip",
    )


# ── API: MP3 → MIDI (local basic-pitch) ────────────────────────────────
@app.post("/api/to-midi")
def api_to_midi():
    """Convert an uploaded audio file to MIDI using basic-pitch (local)."""
    if "file" not in request.files:
        return _error("no file field in request", 400)
    f = request.files["file"]
    if not f or not f.filename:
        return _error("empty filename", 400)
    if not _allowed(f.filename):
        return _error(
            f"unsupported file type. allowed: {sorted(config.ALLOWED_EXTENSIONS)}",
            415,
        )

    safe = secure_filename(f.filename) or "upload"
    jid = uuid.uuid4().hex[:12]
    in_path = _job_dir(jid) / f"in_{safe}"
    f.save(in_path)

    try:
        import midi_extractor
    except Exception as e:  # noqa: BLE001
        in_path.unlink(missing_ok=True)
        return _error(f"basic-pitch not available: {e}", 501)

    onset = _float_arg("onset", config.MIDI_ONSET_THRESHOLD)
    frame = _float_arg("frame", config.MIDI_FRAME_THRESHOLD)

    try:
        mid_path, stats = midi_extractor.to_midi(
            in_path,
            onset_threshold=onset,
            frame_threshold=frame,
            min_note_length=config.MIDI_MIN_NOTE_LENGTH,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("MIDI extraction failed")
        in_path.unlink(missing_ok=True)
        return _error(f"MIDI extraction failed: {e}", 500)
    finally:
        in_path.unlink(missing_ok=True)

    if request.args.get("stats") == "1":
        return jsonify({"stats": stats, "download_url": f"/api/midi/{mid_path.name}"})

    return send_file(
        mid_path,
        as_attachment=True,
        download_name=Path(safe).stem + ".mid",
    )


@app.get("/api/midi/<name>")
def api_midi_download(name: str):
    safe = secure_filename(name)
    path = config.TMP_DIR / safe
    if not path.is_file():
        return _error("midi file not found (it may have been cleaned up)", 404)
    return send_file(path, as_attachment=True, download_name=safe)


def _float_arg(name: str, default: float) -> float:
    raw = request.form.get(name) or request.args.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ── Error handlers ───────────────────────────────────────────────────────
@app.errorhandler(413)
def _too_large(_e):
    return _error("file too large (max 200 MB)", 413)


if __name__ == "__main__":
    log.info(
        "DJ Toolkit on %s:%s (demucs: %s)",
        config.HOST, config.PORT, config.DEMUCS_BIN,
    )
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
