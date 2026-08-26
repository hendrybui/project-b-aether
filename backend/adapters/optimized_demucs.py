"""HTDemucs stem separator via PyTorch.

Device-aware: accepts "auto" or an explicit device (cuda / vulkan / cpu) and
smoke-tests each candidate with a real warmup forward pass, falling back to
the next candidate when the backend reports available but the model's ops
fail. On this box the CUDA wheel of torch ships without the Vulkan backend
and the RX 580 (Polaris) is unsupported by ROCm, so detection correctly
lands on CPU — the throughput win is multi-threaded inference, which is safe
here because this class now runs inside a dedicated worker process (its
OpenMP pool is created in the process main thread), not a server daemon
thread.

All progress is emitted through the stdlib `logging` module so it can be
captured into the per-job pipeline log instead of vanishing on stdout.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import torch
import soundfile as sf

log = logging.getLogger("audiomass.demucs")

STEM_NAMES = ["drums", "bass", "other", "vocals", "guitar", "piano"]
SAMPLE_RATE = 44100
# HTDemucs internal segment: 39/5 seconds at 44100 Hz
CHUNK_SAMPLES = int(44100 * 39 / 5)  # 343980


class OptimizedDemucs:
    """Runs HTDemucs. Constructed inside the dedicated worker process (one
    instance per job) so torch can use multiple CPU threads safely — its OpenMP
    pool is created in the process main thread, not a server daemon thread."""

    def __init__(
        self,
        model_name: str = "htdemucs_6s",
        device: str = "cpu",
        threads: int | None = None,
    ):
        from demucs.pretrained import get_model

        # The caller (worker process) chooses the thread count. When run from
        # the server's own daemon threads this MUST stay 1 — torch's OpenMP
        # pool created inside a non-main thread can crash the process — which
        # is exactly why the pipeline now runs separation in a child process.
        if threads is not None:
            torch.set_num_threads(max(1, int(threads)))

        log.info("Loading %s ...", model_name)
        t0 = time.time()
        bag = get_model(model_name)
        self.model = bag.models[0]
        self.model.eval()
        self.model.use_train_segment = False  # allow arbitrary-length input

        # Try the requested device (or every candidate for "auto"), using the
        # warmup forward as the smoke test: a backend can report available yet
        # still choke on the model's ops, so only a real forward decides.
        candidates = [device] if device != "auto" else ["cuda", "vulkan", "cpu"]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                if candidate == "cuda" and not torch.cuda.is_available():
                    raise RuntimeError("CUDA not available")
                if candidate == "vulkan":
                    vulkan = getattr(torch.backends, "vulkan", None)
                    if vulkan is None or not vulkan.is_available():
                        raise RuntimeError(
                            "Vulkan backend not compiled into this torch build"
                        )
                dev = torch.device(candidate)
                self.model = self.model.to(dev)
                log.info("Warming up on %s ...", candidate)
                dummy = torch.randn(1, 2, 44100, device=dev, dtype=torch.float32)
                with torch.no_grad():
                    _ = self.model(dummy)
                self.device = dev
                log.info("Ready in %.1fs (device=%s).", time.time() - t0, candidate)
                break
            except Exception as exc:
                last_error = exc
                log.warning("Device %s failed (%s); trying next.", candidate, exc)
                # Reset any partial parameter migration before the next candidate.
                self.model = self.model.to("cpu")
        else:
            raise RuntimeError(f"No usable device for HTDemucs: {last_error}")

    def separate(
        self,
        input_path: str,
        output_dir: str,
        *,
        progress_callback=None,
    ) -> dict[str, str]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Load audio
        audio, sr = sf.read(input_path, dtype="float32")
        if audio.ndim == 1:
            audio = np.stack([audio, audio], axis=0)
        elif audio.ndim == 2:
            audio = audio.T

        if audio.shape[0] > 2:
            audio = audio[:2]
        elif audio.shape[0] == 1:
            audio = np.stack([audio[0], audio[0]])

        # Resample if needed
        if sr != SAMPLE_RATE:
            target_len = int(audio.shape[1] * SAMPLE_RATE / sr)
            indices = np.linspace(0, audio.shape[1] - 1, target_len)
            audio = np.array([
                np.interp(indices, np.arange(audio.shape[1]), audio[c])
                for c in range(audio.shape[0])
            ])

        total_samples = audio.shape[1]
        n_chunks = max(1, (total_samples + CHUNK_SAMPLES - 1) // CHUNK_SAMPLES)

        # Pad
        pad_len = (n_chunks * CHUNK_SAMPLES) - total_samples
        if pad_len > 0:
            audio = np.pad(audio, ((0, 0), (0, pad_len)), mode="constant")

        # Stream each stem to its own WAV as chunks complete, instead of
        # accumulating the whole separated song in RAM. The full-stems array is
        # n_stems * 2ch * duration * 4B — ~2.5 GB for a 19-minute track — and
        # on this box that peak is what gets the server OOM-killed mid-job
        # ("separation stops working"). Peak memory drops to ~one chunk.
        n_stems = len(STEM_NAMES)
        writers: list[sf.SoundFile] = []
        try:
            for name in STEM_NAMES:
                writers.append(sf.SoundFile(
                    str(out_dir / f"{name}.wav"),
                    mode="w",
                    samplerate=SAMPLE_RATE,
                    channels=2,
                    subtype="PCM_16",
                ))
        except Exception:
            for writer in writers:
                writer.close()
            raise

        t0 = time.time()
        try:
            for i in range(n_chunks):
                start = i * CHUNK_SAMPLES
                end = start + CHUNK_SAMPLES
                chunk = torch.from_numpy(audio[:, start:end]).unsqueeze(0).to(self.device)

                with torch.no_grad():
                    out = self.model(chunk)

                stem_data = out[0].cpu().numpy()  # (6, 2, samples)
                actual_end = min(end, total_samples)
                chunk_len = actual_end - start
                for s in range(n_stems):
                    # Only the unpadded samples of the final chunk.
                    writers[s].write(stem_data[s, :, :chunk_len].T)

                if progress_callback:
                    progress_callback(i + 1, n_chunks)

                elapsed = time.time() - t0
                pct = (i + 1) / n_chunks * 100
                remaining = elapsed / (i + 1) * (n_chunks - i - 1) if i > 0 else 0
                log.info(
                    "Chunk %d/%d (%.0f%%) - %.1fs elapsed, ~%.0fs remaining",
                    i + 1, n_chunks, pct, elapsed, remaining,
                )
        finally:
            for writer in writers:
                writer.close()

        stems: dict[str, str] = {}
        for name in STEM_NAMES:
            stems[name] = str(out_dir / f"{name}.wav")

        total_time = time.time() - t0
        audio_duration = total_samples / SAMPLE_RATE
        log.info(
            "Done in %.1fs (audio: %.1fs, %.2fx realtime)",
            total_time, audio_duration, total_time / audio_duration,
        )

        return stems
