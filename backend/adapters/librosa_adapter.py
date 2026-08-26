from __future__ import annotations

import math
import numpy as np

try:
    import librosa
except ImportError:
    librosa = None  # type: ignore[assignment]

# Krumhansl-Schmuckler key profiles (major / minor)
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


class LibrosaAdapter:
    """Analyzes tempo (BPM) and musical key/scale with confidence.

    Uses ``librosa.onset.onset_strength`` (proven adaptive normalization)
    with cross-correlation scoring, harmonic boosting, and a tempo-preference
    prior centered at 120 BPM to resolve octave ambiguity.  Returns a
    ``beat_offset`` so the frontend can phase-align the beat grid.
    """

    def analyze_tempo_and_key(self, audio_path: str) -> dict:
        if librosa is None:
            return {'bpm': None, 'key': None, 'scale': None, 'confidence': None}

        y, sr = librosa.load(audio_path, sr=22050, mono=True)

        bpm, confidence, offset = self._estimate_bpm(y, sr)
        key, scale, key_confidence = self._estimate_key(y, sr)

        return {
            'bpm': round(bpm, 1) if bpm else None,
            'key': key,
            'scale': scale,
            'confidence': round(key_confidence, 3) if key_confidence else None,
            'beat_offset': round(offset, 3) if offset else 0.0,
        }

    def _estimate_bpm(self, y: np.ndarray, sr: int) -> tuple[float | None, float, float]:
        """Estimate BPM using librosa onset-strength + cross-correlation.

        1. Onset strength envelope (librosa's adaptive normalization)
        2. Cross-correlation with harmonic boosting (lag×2, lag×3)
        3. Tempo-preference prior (log-Gaussian at 120 BPM)
        4. Phase detection for beat-grid alignment

        Returns (bpm, confidence, phase_offset_seconds).
        """
        try:
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            flux = onset_env.astype(np.float64)

            # Normalize to [0, 1]
            mx = flux.max()
            if mx > 0:
                flux /= mx

            rate = sr // 512  # hop length for onset_strength

            min_bpm, max_bpm = 60, 200
            min_lag = max(1, int(rate * 60 / max_bpm))
            max_lag = min(len(flux) - 1, int(rate * 60 / min_bpm))

            best_lag = 0
            best_score = 0.0
            raw_best = 0.0
            raw_second = 0.0

            for lag in range(min_lag, max_lag + 1):
                # Cross-correlation at this lag
                score = self._score_lag(flux, lag)
                # Harmonic boosting: also score half and third period
                if lag * 2 < len(flux):
                    score += self._score_lag(flux, lag * 2) * 0.35
                if lag * 3 < len(flux):
                    score += self._score_lag(flux, lag * 3) * 0.20

                # Track raw scores for confidence
                if score > raw_best:
                    raw_second = raw_best
                    raw_best = score
                elif score > raw_second:
                    raw_second = score

                # Tempo-preference prior: log-Gaussian centered at 120 BPM
                raw_bpm = 60 * rate / lag
                oct = math.log2(raw_bpm / 120) / 0.9
                score *= math.exp(-0.5 * oct * oct)

                if score > best_score:
                    best_score = score
                    best_lag = lag

            if not best_lag or not best_score:
                return None, 0.0, 0.0

            tempo = 60 * rate / best_lag

            # Phase detection: find the best starting offset for the beat grid
            period = best_lag
            phase_score = 0.0
            phase = 0
            phase_len = min(period, len(flux))
            for p in range(phase_len):
                ps = 0.0
                k = p
                while k < len(flux):
                    ps += flux[k]
                    k += period
                if ps > phase_score:
                    phase_score = ps
                    phase = p

            offset = phase / rate
            gap = (raw_best - raw_second) / raw_best if raw_best > 0 else 0.0
            confidence = max(0.0, min(100.0, gap * 100))

            return tempo, confidence, offset

        except Exception:
            return None, 0.0, 0.0

    @staticmethod
    def _score_lag(flux: np.ndarray, lag: int) -> float:
        """Cross-correlation score at a given lag."""
        if lag <= 0 or lag >= len(flux):
            return 0.0
        length = len(flux) - lag
        return float(np.sum(flux[lag:] * flux[:length])) / length

    def _estimate_key(self, y: np.ndarray, sr: int) -> tuple[str | None, str | None, float | None]:
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            chroma_avg = np.mean(chroma, axis=1)

            best_key: str | None = None
            best_scale: str | None = None
            best_corr: float = -1.0

            for shift in range(12):
                rotated = np.roll(chroma_avg, -shift)

                corr_major = float(np.corrcoef(rotated, MAJOR_PROFILE)[0, 1])
                if np.isnan(corr_major):
                    corr_major = 0.0
                if corr_major > best_corr:
                    best_corr = corr_major
                    best_key = NOTE_NAMES[shift]
                    best_scale = 'major'

                corr_minor = float(np.corrcoef(rotated, MINOR_PROFILE)[0, 1])
                if np.isnan(corr_minor):
                    corr_minor = 0.0
                if corr_minor > best_corr:
                    best_corr = corr_minor
                    best_key = NOTE_NAMES[shift]
                    best_scale = 'minor'

            return best_key, best_scale, best_corr
        except Exception:
            return None, None, None
