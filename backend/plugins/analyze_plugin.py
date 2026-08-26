"""Audio analysis capability, registered as the 'analyze' plugin.

Runs BPM/key/scale (librosa), loudness (LUFS/peak), duration and per-stem RMS
energy over the canonical input and the produced stems, returning the dict the
job manifest consumes. Progress is reported step-by-step through the context's
progress callable (tempo/key, loudness, duration, then one step per stem), and
cancellation is checked before each heavy step — analysis of a long track is
linear in duration, so without these checks a Cancel would be a no-op for
minutes.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

from adapters.librosa_adapter import LibrosaAdapter
from adapters.loudness_adapter import LoudnessAdapter
from plugins.base import AudioPlugin, PluginContext

ANALYZING_STAGE = "analyzing"


class AnalyzePlugin(AudioPlugin):
    name = "analyze"
    description = "BPM, key, loudness and per-stem energy analysis (librosa)"

    def __init__(self) -> None:
        self.librosa_adapter = LibrosaAdapter()
        self.loudness_adapter = LoudnessAdapter()

    def run(self, ctx: PluginContext) -> dict:
        input_wav = str(ctx.params["input_path"])
        stem_paths = dict(ctx.params.get("stems") or {})

        results: dict = {
            "bpm": None,
            "key": None,
            "scale": None,
            "confidence": None,
            "lufs_integrated": None,
            "peak_dbfs": None,
            "duration_sec": None,
            "stem_energy": {},
        }

        total_steps = 3 + max(len(stem_paths), 1)
        done = 0

        def report(message: str) -> None:
            nonlocal done
            done += 1
            if ctx.progress:
                ctx.progress(done / total_steps, stage=ANALYZING_STAGE, message=message)

        # Tempo + key from canonical input (heaviest step — librosa on the full
        # track; cancellable so a long analysis isn't a cancel no-op).
        ctx.check_cancelled()
        report("Analyzing tempo and key...")
        tk = self.librosa_adapter.analyze_tempo_and_key(input_wav)
        results["bpm"] = tk["bpm"]
        results["key"] = tk["key"]
        results["scale"] = tk["scale"]
        results["confidence"] = tk["confidence"]

        ctx.check_cancelled()
        report("Measuring loudness...")
        ld = self.loudness_adapter.analyze_loudness(input_wav)
        results["lufs_integrated"] = ld["lufs_integrated"]
        results["peak_dbfs"] = ld["peak_dbfs"]

        ctx.check_cancelled()
        report("Reading duration...")
        results["duration_sec"] = self._get_duration(input_wav)

        # Per-stem RMS energy — each read is linear in stem length, so check
        # cancellation between stems.
        stem_names = list(stem_paths)
        for i, stem_name in enumerate(stem_names):
            ctx.check_cancelled()
            report(f"Analyzing stem {i + 1}/{len(stem_names)} ({stem_name})...")
            energy = self._compute_rms_energy(stem_paths[stem_name])
            if energy is not None:
                results["stem_energy"][stem_name] = round(energy, 4)

        # total_steps reserves a slot for the stem loop via
        # max(len(stem_paths), 1); with no stems that slot is never filled, so
        # the span would stall below 1.0 and the job could never report 100%.
        if not stem_names:
            report("Analysis complete")

        # Write summary to disk (same layout the pipeline's cleanup expects).
        analysis_dir = ctx.work_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.json").write_text(json.dumps(results, indent=2))

        return results

    @staticmethod
    def _get_duration(audio_path: str) -> float | None:
        try:
            if sf is not None:
                info = sf.info(audio_path)
                return round(info.duration, 3)
        except Exception:
            pass
        return None

    @staticmethod
    def _compute_rms_energy(audio_path: str) -> float | None:
        try:
            if sf is None:
                return None
            data, _ = sf.read(audio_path)
            return float(np.sqrt(np.mean(data ** 2)))
        except Exception:
            return None


# Self-registration on import (see plugins/__init__.py).
from plugins.registry import plugin_registry  # noqa: E402

plugin_registry.register(AnalyzePlugin())
