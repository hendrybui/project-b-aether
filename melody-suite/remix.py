"""
Full remix pipeline — AudioMass (stems) + Melody Suite (analysis/remix) +
fluidsynth (render). Uses both running servers.

Usage:
  python remix.py my_song.mp3 --stems vocals,bass --transpose 0 --bars 4
  python remix.py my_song.mp3 --render-midi

The script will:
  1. Upload song.mp3 to AudioMass at :5055/api/jobs/upload  → get job_id
  2. Poll /api/jobs/{id} until status == "done"
  3. Download chosen stems to a local folder
  4. Run /api/bpm-key on each stem (Melody Suite :5000)
  5. Run /api/transcribe on the vocals stem
  6. Branch the melody with /api/melody/continue
  7. Harmonize with /api/harmony
  8. Optionally render the MIDI to WAV with fluidsynth
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import requests

AUDIOMASS = "http://localhost:5055"
MELODY = "http://localhost:5000"


# ── AudioMass: stem separation ────────────────────────────────────────────

def upload_for_stems(audio_path, stems=None):
    """Upload to AudioMass, ask for HTDemucs separation, return job_id."""
    if stems is None:
        stems = ["vocals", "drums", "bass", "other"]
    with open(audio_path, "rb") as f:
        r = requests.post(
            f"{AUDIOMASS}/api/jobs/upload",
            files={"file": (os.path.basename(audio_path), f)},
            data={"stems": json.dumps(stems)},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["job_id"]


def poll_job(job_id, timeout_s=600):
    """Block until the AudioMass job finishes. Demucs on a 4-min song is
    typically 60-180 s on CPU, longer on first run when it downloads."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        snap = requests.get(f"{AUDIOMASS}/api/jobs/{job_id}", timeout=10).json()
        status = snap.get("status", "?")
        progress = snap.get("progress", 0)
        print(f"  [{int(time.time()-t0):>3}s] {status} {progress}%", end="\r")
        if status == "done":
            print()
            return snap
        if status == "error" or status == "failed":
            raise RuntimeError(f"Job failed: {snap}")
        time.sleep(2)
    raise TimeoutError(f"Job {job_id} did not finish in {timeout_s}s")


def download_stem(job_id, stem_name, out_dir):
    out = Path(out_dir) / f"{stem_name}.wav"
    r = requests.get(f"{AUDIOMASS}/api/jobs/{job_id}/stems/{stem_name}",
                     stream=True, timeout=60)
    r.raise_for_status()
    out.write_bytes(r.content)
    return out


# ── Melody Suite: analysis + remix ────────────────────────────────────────

def bpm_and_key(audio_path):
    with open(audio_path, "rb") as f:
        r = requests.post(f"{MELODY}/api/bpm-key",
                          files={"audio": (os.path.basename(audio_path), f)},
                          timeout=120)
    r.raise_for_status()
    return r.json()


def transcribe(audio_path, sensitivity="balanced", instrument="balanced"):
    with open(audio_path, "rb") as f:
        r = requests.post(f"{MELODY}/api/transcribe",
                          files={"audio": (os.path.basename(audio_path), f)},
                          data={"sensitivity": sensitivity,
                                "instrument": instrument},
                          timeout=300)
    r.raise_for_status()
    return r.json()


def transpose(notes, n):
    return [{**x, "pitch": x["pitch"] + n} for x in notes]


def continue_melody(seed_notes, bars=4, temperature=0.9, prompt="continue"):
    r = requests.post(f"{MELODY}/api/melody/continue", json={
        "seed_melody": seed_notes,
        "bars": bars, "count": 3, "temperature": temperature,
        "prompt": prompt,
    }, timeout=60)
    r.raise_for_status()
    return r.json()["candidates"]


def harmonize(notes, key, style="balanced"):
    r = requests.post(f"{MELODY}/api/harmony", json={
        "notes": notes, "key": key, "style": style,
    }, timeout=60)
    r.raise_for_status()
    return r.json()


def render_midi_to_wav(midi_url, wav_path, soundfont=None):
    """Render a Melody Suite MIDI file to a WAV AudioMass can ingest."""
    midi_local = Path("output") / Path(midi_url).name
    if not midi_local.is_file():
        midi_bytes = requests.get(MELODY + midi_url, timeout=30).content
        midi_local.write_bytes(midi_bytes)
    if soundfont is None:
        # Prefer full GM coverage (the harmony engine uses orchestral programs).
        for guess in ("/usr/share/sounds/sf2/default-GM.sf2",
                      "/usr/share/sounds/sf2/TimGM6mb.sf2",
                      "/usr/share/sounds/sf2/FluidR3_GS.sf2"):
            if os.path.isfile(guess):
                soundfont = guess
                break
    if not soundfont or not os.path.isfile(soundfont):
        raise RuntimeError("fluidsynth or .sf2 soundfont missing.")
    subprocess.run(["fluidsynth", "-ni", soundfont, str(midi_local),
                    "-F", wav_path, "-r", "44100", "-g", "0.8"], check=True)
    return wav_path


# ── AudioMass: close the loop ─────────────────────────────────────────────

def _safe_id(name):
    """AudioMass sanitizes clip ids with [^A-Za-z0-9_-] → '_'."""
    import re
    base = os.path.basename(name)
    if base.lower().endswith(".wav"):
        base = base[:-4]
    return re.sub(r"[^A-Za-z0-9_-]", "_", base) or "clip"


def _probe_wav_seconds(wav_path):
    """Read WAV header to get duration for the multitrack clip placement."""
    import wave
    with wave.open(str(wav_path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate) if rate else 0.0


def upload_to_audiomass_project(wav_paths, project_name, bpm=None, key=None):
    """Create a new AudioMass multitrack project containing one clip per
    WAV, all placed on track 0 starting at t=0. Returns the new project_id.

    Endpoint contract (from audiomass-server.py + project_handler.py):
      POST /api/projects  multipart with:
        - name   (text)
        - state  (JSON: {tracks, clips, ...} — no .buffer fields)
        - clips  (one file part per clip, named <clip_id>.wav)
    """
    clips_meta = []
    files = []
    for p in wav_paths:
        p = Path(p)
        if not p.is_file():
            raise FileNotFoundError(p)
        cid = _safe_id(p.stem)
        duration = _probe_wav_seconds(p)
        clips_meta.append({
            "id": cid,
            "track": 0,
            "start": 0.0,
            "in": 0.0,
            "out": duration,
            "fi": 0.0,
            "fo": 0.0,
            "name": p.stem,
        })
        files.append(("clips", (f"{cid}.wav", p.read_bytes(), "audio/wav")))

    state = {
        "bpm": bpm or 120,
        "tracks": [{
            "id": "track_0",
            "name": "Remix",
            "mute": False, "solo": False, "vol": 0.8,
            "pan": 0.0, "h": 1, "rec": False,
        }],
        "clips": clips_meta,
        "mt_markers": [],
        "mt_loops": [],
    }

    data = {
        "name": project_name,
        "state": json.dumps(state),
    }
    r = requests.post(
        f"{AUDIOMASS}/api/projects",
        data=data,
        files=files,
        timeout=60,
    )
    r.raise_for_status()
    info = r.json()
    return info["project_id"], info


# ── Orchestrator ──────────────────────────────────────────────────────────

def remix(audio_path, stems=("vocals",), transpose_n=0, num_bars=4,
          render_midi=False, soundfont=None, work_dir="stems",
          push_to_audiomass=False, project_name=None,
          also_push_stems=False):
    work_dir = Path(work_dir)
    work_dir.mkdir(exist_ok=True)

    print(f"→ uploading {audio_path} to AudioMass for HTDemucs separation")
    job_id = upload_for_stems(audio_path, stems=list(stems))
    print(f"  job: {job_id}")

    print("→ waiting for stems (HTDemucs)…")
    poll_job(job_id)

    print(f"→ downloading stems: {', '.join(stems)}")
    stem_paths = {s: download_stem(job_id, s, work_dir) for s in stems}
    for s, p in stem_paths.items():
        print(f"  {s}: {p}")

    print("→ analyzing each stem with Melody Suite /api/bpm-key")
    analyses = {}
    for s, p in stem_paths.items():
        a = bpm_and_key(str(p))
        analyses[s] = a
        print(f"  {s}: {a['bpm']} BPM, {a['key']} ({a['camelot']})")

    # Vocals are the right input for /api/transcribe (pyin is monophonic-tuned).
    if "vocals" in stem_paths:
        print("→ transcribing vocals stem (best for pyin)")
        t = transcribe(str(stem_paths["vocals"]))
        notes = t["notes"]
        print(f"  {len(notes)} notes @ {t['tempo']} BPM")
    else:
        # Fall back to whatever stem was requested.
        first = next(iter(stem_paths.values()))
        print(f"→ transcribing {first.name} (no vocals stem available)")
        t = transcribe(str(first))
        notes = t["notes"]

    if transpose_n:
        notes = transpose(notes, transpose_n)
        print(f"  transposed +{transpose_n} semitones")

    # Use the *vocals* key if we have it, else the first stem.
    key = analyses.get("vocals", next(iter(analyses.values())))["key"]

    print("→ generating 3 continuation candidates")
    cands = continue_melody(notes, bars=num_bars)
    winner = cands[0]
    print(f"  picked candidate #0 (score: {winner.get('score', 'n/a')})")

    print(f"→ harmonizing in {key}")
    harm = harmonize(winner["notes"], key=key)
    print(f"  MIDI: {harm.get('midi_url')}")
    print(f"  XML:  {harm.get('xml_url')}")

    result = {
        "job_id": job_id,
        "stems": {s: str(p) for s, p in stem_paths.items()},
        "analyses": analyses,
        "winner_notes": winner["notes"],
        "harmony_url": harm.get("midi_url"),
        "xml_url": harm.get("xml_url"),
    }

    if render_midi and harm.get("midi_url"):
        wav = Path(audio_path).stem + "_remix.wav"
        render_midi_to_wav(harm["midi_url"], wav, soundfont=soundfont)
        result["wav"] = str(wav)
        print(f"  rendered: {wav}")

        if push_to_audiomass:
            clips_to_push = [wav]
            if also_push_stems:
                clips_to_push = [str(p) for p in stem_paths.values()] + clips_to_push
            proj_name = project_name or (Path(audio_path).stem + " remix")
            proj_id, info = upload_to_audiomass_project(
                clips_to_push, proj_name,
                bpm=analyses.get("vocals",
                                 next(iter(analyses.values())))["bpm"],
                key=key,
            )
            result["audiomass_project_id"] = proj_id
            print(f"  ↪ AudioMass project '{proj_name}' created (id={proj_id})")
            print(f"    open: http://localhost:5055/?multitrack=1&load={proj_id}")

    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("audio", help="path to a song file")
    p.add_argument("--stems", default="vocals",
                   help="comma-separated stem names: vocals,drums,bass,other")
    p.add_argument("--transpose", type=int, default=0)
    p.add_argument("--bars", type=int, default=4)
    p.add_argument("--render-midi", action="store_true",
                   help="render harmony MIDI → WAV with fluidsynth")
    p.add_argument("--soundfont", default=None)
    p.add_argument("--work-dir", default="stems")
    p.add_argument("--push", action="store_true",
                   help="after rendering, upload the result as a new "
                        "AudioMass multitrack project (closes the loop)")
    p.add_argument("--push-stems-too", action="store_true",
                   help="include the downloaded stems as additional tracks "
                        "in the new AudioMass project")
    p.add_argument("--project-name", default=None,
                   help="name for the new AudioMass project "
                        "(default: <song> remix)")
    args = p.parse_args()

    out = remix(args.audio, stems=tuple(s.strip() for s in args.stems.split(",")),
                transpose_n=args.transpose, num_bars=args.bars,
                render_midi=args.render_midi, soundfont=args.soundfont,
                work_dir=args.work_dir,
                push_to_audiomass=args.push,
                project_name=args.project_name,
                also_push_stems=args.push_stems_too)

    summary = {k: v for k, v in out.items() if k != "winner_notes"}
    print("\n" + json.dumps(summary, indent=2))
