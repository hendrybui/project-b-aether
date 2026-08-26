from __future__ import annotations

import numpy as np

try:
    import pyloudnorm as pyln
except ImportError:
    pyln = None  # type: ignore[assignment]

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]


class LoudnessAdapter:
    """Measures integrated loudness (LUFS, ITU-R BS.1770) and true-peak level."""

    def analyze_loudness(self, audio_path: str) -> dict:
        if sf is None or pyln is None:
            return {'lufs_integrated': None, 'peak_dbfs': None}

        data, sr = sf.read(audio_path)

        # Ensure stereo for pyloudnorm
        if data.ndim == 1:
            data = np.column_stack([data, data])

        lufs = self._measure_lufs(data, sr)
        peak = self._measure_peak(data)

        return {
            'lufs_integrated': round(lufs, 1) if lufs is not None else None,
            'peak_dbfs': round(peak, 1) if peak is not None else None,
        }

    def _measure_lufs(self, data: np.ndarray, sr: int) -> float | None:
        try:
            meter = pyln.Meter(sr)
            return meter.integrated_loudness(data)
        except Exception:
            return None

    def _measure_peak(self, data: np.ndarray) -> float | None:
        try:
            peak = float(np.max(np.abs(data)))
            if peak <= 0:
                return -np.inf
            return float(20 * np.log10(peak))
        except Exception:
            return None
