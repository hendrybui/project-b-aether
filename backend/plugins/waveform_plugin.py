"""Waveform-summary capability, registered as the 'waveform' plugin.

Builds backend waveform summaries (min/max buckets) for each produced stem so
the editor can render peaks without decoding audio. Progress is reported one
step per stem through the context, and cancellation is checked between stems.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

from plugins.base import AudioPlugin, PluginContext

# Target number of data points — roughly 1 point per pixel at common widths.
DEFAULT_BUCKETS = 2000


class WaveformPlugin(AudioPlugin):
    name = "waveform"
    description = "Per-stem waveform peak summaries (min/max buckets)"

    def run(self, ctx: PluginContext) -> dict[str, str]:
        stem_paths = dict(ctx.params.get("stems") or {})
        wf_dir = ctx.work_dir / "waveforms"
        wf_dir.mkdir(parents=True, exist_ok=True)

        waveform_map: dict[str, str] = {}
        stem_names = list(stem_paths)
        for i, stem_name in enumerate(stem_names):
            ctx.check_cancelled()
            if ctx.progress:
                ctx.progress(
                    i / max(len(stem_names), 1),
                    stage="generating_waveforms",
                    message=f"Generating waveform summaries ({i + 1}/{len(stem_names)})",
                )
            out_path = str(wf_dir / f"{stem_name}.json")
            try:
                self._generate(stem_paths[stem_name], out_path)
                waveform_map[stem_name] = out_path
            except Exception:
                pass  # Skip stems that fail; the manifest just lacks their entry.

        return waveform_map

    def _generate(self, audio_path: str, output_path: str, *, buckets: int | None = None) -> dict:
        """Read a WAV file and write a JSON waveform summary.

        Output shape: { sample_rate, duration_sec, channels, buckets,
                        min: [...], max: [...] }
        """
        if sf is None:
            raise RuntimeError("soundfile is not installed")

        data, sr = sf.read(audio_path)
        n_samples, n_channels = data.shape if data.ndim > 1 else (len(data), 1)

        # Average to mono for the peak envelope.
        mono = np.mean(data, axis=1) if n_channels > 1 else data

        n_buckets = min(buckets or DEFAULT_BUCKETS, len(mono))
        bucket_size = len(mono) / n_buckets
        min_vals = np.empty(n_buckets)
        max_vals = np.empty(n_buckets)

        for i in range(n_buckets):
            start = int(i * bucket_size)
            end = min(int((i + 1) * bucket_size), len(mono))
            chunk = mono[start:end]
            if len(chunk) == 0:
                min_vals[i] = 0.0
                max_vals[i] = 0.0
            else:
                min_vals[i] = float(np.min(chunk))
                max_vals[i] = float(np.max(chunk))

        result = {
            "sample_rate": sr,
            "duration_sec": round(len(mono) / sr, 3),
            "channels": n_channels,
            "buckets": n_buckets,
            "min": min_vals.tolist(),
            "max": max_vals.tolist(),
        }

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json.dumps(result))
        return result


# Self-registration on import (see plugins/__init__.py).
from plugins.registry import plugin_registry  # noqa: E402

plugin_registry.register(WaveformPlugin())
