"""Subprocess demucs runner for DJ Toolkit.

Runs demucs in its own OS process (the web server never imports torch — see
config.py) and writes stems with soundfile, avoiding torchaudio's torchcodec
dependency, which is not installed in the shared venv.

Usage: python demucs_run.py <input> <model_id> <out_dir>
Stems are written to <out_dir>/<model_id>/<trackname>/<stem>.wav — the same
layout the demucs CLI would produce.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    in_path = Path(sys.argv[1])
    model_id = sys.argv[2]
    out_dir = Path(sys.argv[3])

    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model(model_id)
    model.eval()

    data, sr = sf.read(str(in_path), dtype="float32", always_2d=True)  # (samples, channels)
    if sr != model.samplerate:
        import librosa
        data = librosa.resample(data.T, orig_sr=sr, target_sr=model.samplerate).T
        sr = model.samplerate

    wav = torch.from_numpy(data.T).float()  # (channels, samples)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)  # htdemucs models are stereo

    # Replicate the CLI's normalize / denormalize round-trip.
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / ref.std()
    sources = apply_model(
        model, wav[None], device="cpu", shifts=0, split=True,
        overlap=0.25, progress=True,
    )[0]
    sources = sources * ref.std() + ref.mean()

    out = out_dir / model_id / (in_path.stem or "track")
    out.mkdir(parents=True, exist_ok=True)
    for idx, name in enumerate(model.sources):
        clip = sources[idx].t().numpy()  # (samples, channels)
        sf.write(str(out / f"{name}.wav"), clip, sr)
    print(f"OK {out}")


if __name__ == "__main__":
    main()
