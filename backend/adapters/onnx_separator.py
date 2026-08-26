"""ONNX-based stem separation adapter. Uses INT8 quantized HTDemucs for fast CPU inference.

3-5x faster than the Demucs CLI on CPU. No GPU required.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf

STEM_NAMES = ["drums", "bass", "other", "vocals", "guitar", "piano"]
# Segment length must match HTDemucs internal segment (39/5 seconds at 44100 Hz)
CHUNK_SAMPLES = int(44100 * 39 / 5)  # 343980
SAMPLE_RATE = 44100


def _prepare_model_for_inference(model):
    """Prepare HTDemucs model for ONNX-compatible inference."""
    model.eval()
    # Disable use_train_segment so model uses dynamic lengths
    model.use_train_segment = False


class OnnxSeparator:
    """Runs HTDemucs inference via ONNX Runtime instead of the Demucs CLI."""

    def __init__(self, model_path: str | Path | None = None):
        import onnxruntime as ort

        if model_path is None:
            models_dir = Path(__file__).parent / "models"
            # Prefer INT8 (smaller + faster), fall back to FP32
            model_path = models_dir / "htdemucs_6s_int8.onnx"
            if not model_path.exists():
                model_path = models_dir / "htdemucs_6s.onnx"

        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"ONNX model not found at {model_path}. "
                "Run: python adapters/onnx_adapter.py to export the model first."
            )

        self.model_path = model_path
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 0  # use all cores
        sess_opts.inter_op_num_threads = 0
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        print(f"[OnnxSeparator] Loaded {model_path.name} ({model_path.stat().st_size / 1e6:.1f} MB)")

    def separate(
        self,
        input_path: str,
        output_dir: str,
        *,
        progress_callback=None,
        total_chunks: int = 0,
    ) -> dict[str, str]:
        """Separate audio file into stems using ONNX inference.

        Args:
            input_path: Path to input WAV file (stereo, 44100 Hz).
            output_dir: Directory to write stem WAV files.
            progress_callback: Optional callable(chunks_done, total_chunks) for progress.
            total_chunks: Hint for total chunk count (for progress tracking).

        Returns:
            Dict mapping stem name -> output file path.
        """
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Load audio
        audio, sr = sf.read(input_path, dtype="float32")
        if audio.ndim == 1:
            audio = np.stack([audio, audio], axis=0)
        elif audio.ndim == 2:
            audio = audio.T  # (channels, samples)
        else:
            raise ValueError(f"Unexpected audio shape: {audio.shape}")

        if audio.shape[0] > 2:
            audio = audio[:2]
        elif audio.shape[0] == 1:
            audio = np.stack([audio[0], audio[0]])

        # Resample if needed (basic linear interpolation)
        if sr != SAMPLE_RATE:
            target_len = int(audio.shape[1] * SAMPLE_RATE / sr)
            indices = np.linspace(0, audio.shape[1] - 1, target_len)
            audio = np.array([
                np.interp(indices, np.arange(audio.shape[1]), audio[c])
                for c in range(audio.shape[0])
            ])
            sr = SAMPLE_RATE

        total_samples = audio.shape[1]
        n_chunks = max(1, (total_samples + CHUNK_SAMPLES - 1) // CHUNK_SAMPLES)

        if total_chunks:
            n_chunks = max(n_chunks, total_chunks)

        # Pad to chunk boundary
        pad_len = (n_chunks * CHUNK_SAMPLES) - total_samples
        if pad_len > 0:
            audio = np.pad(audio, ((0, 0), (0, pad_len)), mode="constant")

        # Collect stems
        n_stems = len(STEM_NAMES)
        all_stems = np.zeros((n_stems, 2, total_samples), dtype=np.float32)

        t0 = time.time()

        for i in range(n_chunks):
            start = i * CHUNK_SAMPLES
            end = start + CHUNK_SAMPLES
            chunk = audio[:, start:end]

            # ONNX inference: (1, 2, samples) -> (1, 6, 2, samples)
            inp = chunk[np.newaxis, :, :].astype(np.float32)
            out = self.session.run(None, {"mix": inp})[0]

            # out shape: (1, 6, 2, samples)
            stem_data = out[0]  # (6, 2, samples)

            # Trim to actual audio length
            actual_end = min(end, total_samples)
            actual_len = actual_end - start
            for s in range(n_stems):
                all_stems[s, :, start:actual_end] = stem_data[s, :, :actual_len]

            if progress_callback:
                progress_callback(i + 1, n_chunks)

            elapsed = time.time() - t0
            pct = (i + 1) / n_chunks * 100
            print(f"  Chunk {i+1}/{n_chunks} ({pct:.0f}%) - {elapsed:.1f}s elapsed")

        # Write stems
        stems: dict[str, str] = {}
        for s, name in enumerate(STEM_NAMES):
            out_path = out_dir / f"{name}.wav"
            # (2, samples) -> (samples, 2)
            stem_audio = all_stems[s].T
            sf.write(str(out_path), stem_audio, SAMPLE_RATE)
            stems[name] = str(out_path)

        total_time = time.time() - t0
        audio_duration = total_samples / SAMPLE_RATE
        print(f"[OnnxSeparator] Done in {total_time:.1f}s "
              f"(audio: {audio_duration:.1f}s, ratio: {total_time/audio_duration:.2f}x realtime)")

        return stems


def check_model_available() -> bool:
    """Check if an ONNX model is available for fast separation."""
    models_dir = Path(__file__).parent / "models"
    return (models_dir / "htdemucs_6s_int8.onnx").exists() or (models_dir / "htdemucs_6s.onnx").exists()
