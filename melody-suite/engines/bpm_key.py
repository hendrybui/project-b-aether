"""
BPM & Key Finder Engine
=======================
Implements tempo detection (via librosa onset-based beat tracking) and
musical key detection (via the Krumhansl-Schmuckler key-finding algorithm).

This is the Python equivalent of GadegetKit's browser-based BPM & Key Finder,
which uses bpm-detective (Joe Sullivan's peak-counting algorithm) + K-S
pitch-class profiling. Here we use librosa's more robust implementations
of the same DSP concepts.

Key research reference: .firecrawl/bpm-detective-deep-dive.md
"""

import numpy as np

try:
    import librosa
except ImportError:
    librosa = None


# ── Krumhansl-Kessler Key Profiles ──────────────────────────────────────────
# These 12-dimensional vectors encode the perceived stability of each pitch
# class relative to a tonic, derived from probe-tone experiments.
# Source: Krumhansl & Kessler (1982), as documented in the deep-dive research.

# Index order: C  C# D  D# E  F  F# G  G# A  A# B
MAJOR_PROFILE = np.array([
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88
])

MINOR_PROFILE = np.array([
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17
])

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Camelot Wheel mapping for DJ harmonic mixing.
# Major keys → 'B' codes, minor keys → 'A' codes.
CAMELOT_MAJOR = {
    'C': '8B', 'G': '9B', 'D': '10B', 'A': '11B', 'E': '12B', 'B': '1B',
    'F#': '2B', 'C#': '3B', 'G#': '4B', 'D#': '5B', 'A#': '6B', 'F': '7B',
}
CAMELOT_MINOR = {
    'A': '8A', 'E': '9A', 'B': '10A', 'F#': '11A', 'C#': '12A', 'G#': '1A',
    'D#': '2A', 'A#': '3A', 'F': '4A', 'C': '5A', 'G': '6A', 'D': '7A',
}


def _pearson_correlation(a, b):
    """Pearson correlation coefficient between two vectors."""
    a_mean = a - np.mean(a)
    b_mean = b - np.mean(b)
    denom = np.sqrt(np.sum(a_mean ** 2) * np.sum(b_mean ** 2))
    if denom == 0:
        return 0.0
    return np.sum(a_mean * b_mean) / denom


def _rotate(arr, n):
    """Circularly rotate a 12-element array so index n becomes the tonic.

    np.roll(arr, n) maps template[k] = arr[(k - n) % 12], i.e. the profile's
    index 0 (tonic) lands on pitch class n — exactly what K-S needs.
    """
    return np.roll(arr, n)


def detect_key(chroma):
    """
    Run the Krumhansl-Schmuckler algorithm on a chroma vector.

    Args:
        chroma: 12-element array of pitch-class energy/duration totals.

    Returns:
        dict with keys: key, mode, correlation, confidence, camelot
    """
    best_r = -2.0
    second_r = -2.0
    best_key = 'C'
    best_mode = 'major'

    # Test all 24 keys (12 major + 12 minor).
    for tonic in range(12):
        # Major candidate: rotate the major profile so this tonic is the root.
        major_template = _rotate(MAJOR_PROFILE, tonic)
        r = _pearson_correlation(chroma, major_template)
        if r > best_r:
            second_r = best_r
            best_r = r
            best_key = NOTE_NAMES[tonic]
            best_mode = 'major'
        elif r > second_r:
            second_r = r

        # Minor candidate.
        minor_template = _rotate(MINOR_PROFILE, tonic)
        r = _pearson_correlation(chroma, minor_template)
        if r > best_r:
            second_r = best_r
            best_r = r
            best_key = NOTE_NAMES[tonic]
            best_mode = 'minor'
        elif r > second_r:
            second_r = r

    # Confidence: how much the best key dominates the runner-up.
    confidence = 0.0
    if best_r > 0:
        confidence = round(((best_r - second_r) / best_r) * 100, 1)

    camelot = (CAMELOT_MAJOR if best_mode == 'major' else CAMELOT_MINOR).get(best_key, '?')

    key_name = f"{best_key} {best_mode}"

    return {
        'key': key_name,
        'root': best_key,
        'mode': best_mode,
        'correlation': round(best_r, 4),
        'confidence': max(confidence, 0.0),
        'camelot': camelot,
    }


def analyze_audio(file_path):
    """
    Full BPM + Key analysis of an audio file.

    Args:
        file_path: path to an audio file (MP3, WAV, FLAC, OGG, M4A).

    Returns:
        dict with bpm, key info, confidence, and beat timestamps.
    """
    if librosa is None:
        raise ImportError("librosa is required for audio analysis. Install with: pip install librosa")

    # Load audio at 22050 Hz mono (standard for librosa analysis).
    y, sr = librosa.load(file_path, sr=22050, mono=True)
    duration = len(y) / sr

    # ── Tempo (BPM) detection ──────────────────────────────────────────────
    # librosa.beat.beat_track uses onset-strength envelope + dynamic programming
    # to find the most likely beat period. This is more robust than the
    # raw-peak-counting approach of bpm-detective but conceptually identical:
    # find periodic energy spikes.
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    # librosa >= 0.10 returns tempo as an array; extract scalar.
    tempo_val = float(tempo[0]) if hasattr(tempo, '__len__') else float(tempo)
    bpm = int(np.round(tempo_val))

    # Get beat timestamps for waveform/tempo-map display.
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    beat_timestamps = [round(float(t), 3) for t in beat_times[:128]]  # cap for JSON

    # BPM confidence heuristic: strength of the onset envelope periodicity.
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    if len(onset_env) > 1 and onset_env.std() > 0:
        bpm_confidence = min(95.0, round(60 + 35 * (onset_env.std() / (onset_env.mean() + 1e-8))))
    else:
        bpm_confidence = 50.0

    # ── Key detection via K-S algorithm ────────────────────────────────────
    # Extract chroma (12-bin pitch-class energy) using the constant-Q transform.
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    # Sum across all time frames to get total energy per pitch class.
    chroma_vector = chroma.sum(axis=1)

    key_result = detect_key(chroma_vector)

    return {
        'bpm': int(bpm),
        'bpm_confidence': bpm_confidence,
        'duration': round(duration, 2),
        'beats': beat_timestamps,
        'key': key_result['key'],
        'root': key_result['root'],
        'mode': key_result['mode'],
        'key_correlation': key_result['correlation'],
        'key_confidence': key_result['confidence'],
        'camelot': key_result['camelot'],
    }
