"""
Audio Transcription Engine
==========================
Converts audio files into note sequences (MIDI pitches + timing) using
librosa's pitch tracking (pyin) and onset detection. This is the shared
engine behind the Analyzer, MP3-to-MIDI, and MP3-to-Sheet tools.

Outputs a list of notes: [{pitch, start, duration, pitch_name, confidence}]
"""

import numpy as np

try:
    import librosa
except ImportError:
    librosa = None

try:
    import pretty_midi
except ImportError:
    pretty_midi = None

from engines.bpm_key import detect_key

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def _midi_to_name(midi):
    """Convert MIDI pitch to note name like 'C4'."""
    octave = int(midi / 12) - 1
    return f"{NOTE_NAMES[int(midi) % 12]}{octave}"


def transcribe_audio(file_path, fmin=None, fmax=None, threshold=0.6,
                     min_note_dur=0.08, instrument_profile='balanced'):
    """
    Transcribe an audio file into a sequence of notes.

    Uses librosa.pyin for monophonic pitch tracking, then groups
    consecutive frames into discrete notes.

    Args:
        file_path: path to audio file (MP3/WAV/FLAC/OGG/M4A).
        fmin: minimum frequency to detect (Hz). 65 ≈ C2.
        fmax: maximum frequency to detect (Hz). 1000 ≈ B5.
        threshold: voicing threshold (0-1). Higher = stricter.
        min_note_dur: minimum note duration in seconds.
        instrument_profile: 'balanced', 'piano', 'guitar', 'vocal'.

    Returns:
        dict with notes list, tempo, key, and duration.
    """
    if librosa is None:
        raise ImportError("librosa is required")

    # Adjust frequency range based on instrument profile. Explicit fmin/fmax
    # (e.g. from the UI's custom pitch range) take precedence over the profile.
    profiles = {
        'balanced': (65, 1000),
        'piano':    (130, 2100),   # C3–C7
        'guitar':   (82, 1200),    # E2–D6
        'vocal':    (130, 700),    # C3–F5 (cut low rumble)
    }
    if fmin is None or fmax is None:
        fmin, fmax = profiles.get(instrument_profile, (65, 1000))

    y, sr = librosa.load(file_path, sr=22050, mono=True)
    duration = len(y) / sr

    # ── Pitch tracking with pYIN ──────────────────────────────────────
    # pYIN is a probabilistic version of YIN that outputs both a
    # fundamental frequency estimate and a voicing probability per frame.
    f0, voiced_flag, voiced_prob = librosa.pyin(
        y, fmin=fmin, fmax=fmax, sr=sr,
        fill_na=0.0,
    )

    # Convert frequencies to MIDI pitches.
    midi_pitches = np.zeros_like(f0)
    mask = f0 > 0
    midi_pitches[mask] = 69 + 12 * np.log2(f0[mask] / 440.0)
    midi_pitches = np.round(midi_pitches)

    # Frame timing.
    hop_length = 512
    frame_times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=hop_length)
    frame_dur = frame_times[1] - frame_times[0] if len(frame_times) > 1 else 0.023

    # ── Group consecutive voiced frames into notes ────────────────────
    notes = []
    current_pitch = None
    current_start = None
    current_frames = 0
    current_prob_sum = 0.0

    for i in range(len(midi_pitches)):
        pitch = midi_pitches[i] if voiced_flag[i] and voiced_prob[i] > threshold else 0

        if pitch > 0:
            if current_pitch is None:
                # Start new note.
                current_pitch = int(pitch)
                current_start = frame_times[i]
                current_frames = 1
                current_prob_sum = float(voiced_prob[i])
            elif abs(pitch - current_pitch) <= 1:
                # Continue same note (allow 1 semitone jitter).
                current_pitch = int(round((current_pitch * current_frames + pitch) / (current_frames + 1)))
                current_frames += 1
                current_prob_sum += float(voiced_prob[i])
            else:
                # Pitch changed → close previous note, start new one.
                note_dur = current_frames * frame_dur
                if note_dur >= min_note_dur:
                    notes.append(_make_note(current_pitch, current_start, note_dur, current_prob_sum / current_frames))
                current_pitch = int(pitch)
                current_start = frame_times[i]
                current_frames = 1
                current_prob_sum = float(voiced_prob[i])
        else:
            # Unvoiced frame → close note if active.
            if current_pitch is not None:
                note_dur = current_frames * frame_dur
                if note_dur >= min_note_dur:
                    notes.append(_make_note(current_pitch, current_start, note_dur, current_prob_sum / current_frames))
                current_pitch = None

    # Close final note.
    if current_pitch is not None:
        note_dur = current_frames * frame_dur
        if note_dur >= min_note_dur:
            notes.append(_make_note(current_pitch, current_start, note_dur, current_prob_sum / current_frames))

    # ── Tempo estimation ──────────────────────────────────────────────
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    tempo_val = float(tempo[0]) if hasattr(tempo, '__len__') else float(tempo)
    bpm = int(np.round(tempo_val))

    # ── Key detection (Krumhansl-Schmuckler, same method as BPM & Key) ──
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    key_result = detect_key(chroma.sum(axis=1))

    return {
        'notes': notes,
        'tempo': bpm,
        'duration': round(duration, 2),
        'num_notes': len(notes),
        'pitch_range': (
            min(n['pitch'] for n in notes) if notes else 60,
            max(n['pitch'] for n in notes) if notes else 60,
        ),
        'key': key_result['key'],
        'key_confidence': key_result['confidence'],
    }


def _make_note(pitch, start, duration, confidence):
    """Create a note dict."""
    return {
        'pitch': int(pitch),
        'pitch_name': _midi_to_name(int(pitch)),
        'start': round(float(start), 4),
        'duration': round(float(duration), 4),
        'confidence': round(float(confidence), 3),
    }


def notes_to_midi(notes, tempo=96, filename=None):
    """
    Convert a note list to a MIDI file.

    Args:
        notes: list of note dicts (pitch, start, duration).
        tempo: BPM for the output MIDI.
        filename: if provided, write to file. Otherwise return bytes.

    Returns:
        MIDI bytes (or None if filename given).
    """
    if pretty_midi is None:
        return None

    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    instrument = pretty_midi.Instrument(program=0)  # Acoustic Grand Piano

    for note in notes:
        midi_note = pretty_midi.Note(
            velocity=80,
            pitch=note['pitch'],
            start=note['start'],
            end=note['start'] + note['duration'],
        )
        instrument.notes.append(midi_note)

    pm.instruments.append(instrument)

    if filename:
        pm.write(filename)
        return None

    import io
    buf = io.BytesIO()
    pm.write(buf)
    return buf.getvalue()
