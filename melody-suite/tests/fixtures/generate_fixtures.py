"""
Regenerate tests/fixtures/*.wav deterministically.

The fixtures are short monophonic melodies (one beat each at 120 BPM, plus a
1s silence tail) synthesized as 16-bit mono WAVs:

- demo_melody.wav    C major:     C4 E4 G4 C5 G4 E4 D4 C4
- e_minor_melody.wav E minor:     E3 G3 B3 E4 B3 G3 F#3 E3
- bb_major_melody.wav B-flat major: Bb3 D4 F4 Bb4 F4 D4 C4 Bb3

The synthesis uses no randomness, so the files are byte-for-byte
reproducible — run this script again after changing a melody and commit the
updated .wav alongside it.

The major fixture mirrors the synthesis behind the app's `/api/demo-audio`
route (app._demo_wav_bytes); the minor and flat fixtures exist so
key-signature rendering is exercised against real audio in non-C keys (E
minor has one sharp, B-flat major has two flats).

Run from the repo root:

    venv/bin/python tests/fixtures/generate_fixtures.py
"""

import io
import pathlib
import wave

import numpy as np

# C4 E4 G4 C5 G4 E4 D4 C4 — the same demo melody the UI plays.
DEMO_MELODY = [60, 64, 67, 72, 67, 64, 62, 60]
# E3 G3 B3 E4 B3 G3 F#3 E3 — E natural minor (one sharp: F#).
MINOR_MELODY = [52, 55, 59, 64, 59, 55, 54, 52]
# Bb3 D4 F4 Bb4 F4 D4 C4 Bb3 — B-flat major (two flats: Bb, Eb).
FLAT_MELODY = [58, 62, 65, 70, 65, 62, 60, 58]


def synthesize_wav(melody, sr=22050, bpm=120):
    """Synthesize a melody as 16-bit mono WAV bytes."""
    beat = 60.0 / bpm
    total = int(sr * beat * len(melody)) + sr  # +1s silence tail
    audio = np.zeros(total)
    t = 0.0
    for midi in melody:
        f = 440.0 * 2 ** ((midi - 69) / 12.0)
        n = int(sr * beat)
        tt = np.arange(n) / sr
        # Short attack + exponential decay keeps onsets clear for beat tracking.
        env = np.minimum(tt / 0.01, 1.0) * np.exp(-tt * 3.0)
        tone = (np.sin(2 * np.pi * f * tt)
                + 0.3 * np.sin(2 * np.pi * 2 * f * tt)
                + 0.1 * np.sin(2 * np.pi * 3 * f * tt)) * env
        start = int(t * sr)
        audio[start:start + n] += 0.5 * tone
        t += beat
    peak = float(np.max(np.abs(audio))) or 1.0
    pcm = (audio / peak * 0.7 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


FIXTURES = {
    'demo_melody.wav': DEMO_MELODY,
    'e_minor_melody.wav': MINOR_MELODY,
    'bb_major_melody.wav': FLAT_MELODY,
}


def main():
    base = pathlib.Path(__file__).parent
    for name, melody in FIXTURES.items():
        out = base / name
        data = synthesize_wav(melody)
        out.write_bytes(data)
        print(f'wrote {out} ({len(data)} bytes)')


if __name__ == '__main__':
    main()
