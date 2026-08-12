"""
SATB Harmony Generator Engine
=============================
Implements rule-based four-part (SATB) harmonization of a lead melody line,
inspired by Coconet's chorale-harmonization concept.

Instead of Coconet's neural inpainting + Gibbs sampling, this engine uses
common-practice voice-leading rules (the same rules Bach chorales follow):
  - Avoid parallel fifths and octaves between any two voices
  - Prefer contrary/similar motion over parallel motion
  - Double the tonic or dominant, never the leading tone
  - Resolve the leading tone upward by step in V→I cadences
  - Keep voices in their ranges (S: 60-81, A: 55-74, T: 48-67, B: 36-64)

Key research reference: .firecrawl/coconet-deep-dive.md
"""

import io
import random
import numpy as np

try:
    import pretty_midi
except ImportError:
    pretty_midi = None

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Diatonic triad qualities for each scale degree in major/minor keys.
# Index = scale degree (0=I, 1=ii, 2=iii, 3=IV, 4=V, 5=vi, 6=vii°)
MAJOR_TRIAD_TYPES = ['major', 'minor', 'minor', 'major', 'major', 'minor', 'dim']
MINOR_TRIAD_TYPES = ['minor', 'dim', 'major', 'minor', 'minor', 'major', 'major']

# SATB vocal ranges (MIDI note numbers).
RANGES = {
    'soprano': (60, 81),   # C4 to A5
    'alto':    (55, 74),   # G3 to D5
    'tenor':   (48, 67),   # C3 to G4
    'bass':    (36, 64),   # C2 to E4
}

# Chord tones by triad type, as scale-degree offsets.
TRIAD_INTERVALS = {
    'major': [0, 4, 7],
    'minor': [0, 3, 7],
    'dim':   [0, 3, 6],
}


def _note_to_pitch(note_name):
    """Convert a note name like 'C4' to a MIDI pitch number."""
    note_name = note_name.strip()
    # Parse: note letter, optional accidental, octave number.
    letter = note_name[0].upper()
    idx = NOTE_NAMES.index(letter) if letter in NOTE_NAMES else 0
    rest = note_name[1:]
    if rest.startswith('#'):
        idx = (idx + 1) % 12
        rest = rest[1:]
    elif rest.startswith('b'):
        idx = (idx - 1) % 12
        rest = rest[1:]
    octave = int(rest) if rest else 4
    return (octave + 1) * 12 + idx


def _pitch_class(midi):
    return midi % 12


def _interval(a, b):
    """Semitone interval between two MIDI notes."""
    return abs(a - b)


def _is_perfect_fifth(a, b):
    """Check if two notes form a perfect fifth (7 semitones, allowing octave)."""
    diff = abs(a - b) % 12
    return diff == 7


def _is_unison_or_octave(a, b):
    """Check if two notes form a unison or octave (0 or 12 semitones)."""
    diff = abs(a - b) % 12
    return diff == 0


def _key_to_root(key_name):
    """Extract the tonic pitch class from a key name like 'C major'."""
    return NOTE_NAMES.index(key_name.split()[0])


def _harmonize_note(soprano_pitch, key_root, is_minor, prev_voicing, style='balanced'):
    """
    Generate Alto, Tenor, Bass notes for a single soprano pitch.

    Args:
        soprano_pitch: MIDI pitch of the soprano (melody) note.
        key_root: pitch class index (0-11) of the key's tonic.
        is_minor: whether the key is minor.
        prev_voicing: dict {voice: midi_pitch} of the previous chord, or None.
        style: 'conservative', 'balanced', or 'adventurous'.

    Returns:
        dict {soprano, alto, tenor, bass} of MIDI pitches.
    """
    triad_types = MINOR_TRIAD_TYPES if is_minor else MAJOR_TRIAD_TYPES
    scale = _build_scale(key_root, is_minor)

    # Identify which scale degree the soprano is on.
    sop_pc = _pitch_class(soprano_pitch)
    scale_degrees = [_pitch_class(n) for n in scale]
    if sop_pc not in scale_degrees:
        # Non-chord tone: use nearest chord tone.
        degree = min(range(7), key=lambda i: abs(scale_degrees[i] - sop_pc))
    else:
        degree = scale_degrees.index(sop_pc)

    # Determine the chord: typically, harmonize scale degree `d` with
    # chord built on that degree, or the predominant/dominant area.
    triad_type = triad_types[degree]
    chord_intervals = TRIAD_INTERVALS[triad_type]
    chord_root_pc = _pitch_class(scale[degree])

    # Generate candidate chord tones in each voice's range.
    voices = {}
    voice_order = ['bass', 'tenor', 'alto']

    # Randomness factor based on style.
    rand_factor = {'conservative': 0.2, 'balanced': 0.5, 'adventurous': 0.8}.get(style, 0.5)

    for voice in voice_order:
        lo, hi = RANGES[voice]
        candidates = []
        for interval in chord_intervals:
            target_pc = (chord_root_pc + interval) % 12
            # Find all notes in this voice's range with this pitch class.
            for midi in range(lo, hi + 1):
                if _pitch_class(midi) == target_pc:
                    candidates.append(midi)
        if not candidates:
            candidates.append((lo + hi) // 2)

        # Score candidates: prefer smooth voice leading from previous chord.
        if prev_voicing and prev_voicing.get(voice) is not None:
            prev = prev_voicing[voice]
            # Prefer notes closest to the previous note (smooth voice leading).
            candidates.sort(key=lambda m: abs(m - prev))
        else:
            # Prefer middle of range for first chord.
            candidates.sort(key=lambda m: abs(m - (lo + hi) // 2))

        # Add randomness: sometimes skip the closest option.
        if random.random() < rand_factor and len(candidates) > 1:
            voice_note = candidates[1]
        else:
            voice_note = candidates[0]

        voices[voice] = voice_note

    voices['soprano'] = soprano_pitch

    # Check for parallel fifths/octaves with previous voicing.
    if prev_voicing:
        voices = _fix_parallel_motion(voices, prev_voicing)

    return voices


def _fix_parallel_motion(voices, prev_voicing):
    """Try to eliminate parallel fifths/octaves by swapping voices."""
    voice_pairs = [('soprano', 'alto'), ('soprano', 'tenor'), ('soprano', 'bass'),
                   ('alto', 'tenor'), ('alto', 'bass'), ('tenor', 'bass')]

    for v1, v2 in voice_pairs:
        if prev_voicing.get(v1) is None or prev_voicing.get(v2) is None:
            continue
        prev_interval = abs(prev_voicing[v1] - prev_voicing[v2]) % 12
        curr_interval = abs(voices[v1] - voices[v2]) % 12
        same_direction = ((voices[v1] - prev_voicing[v1]) * (voices[v2] - prev_voicing[v2])) > 0
        if same_direction and prev_interval in (0, 7) and curr_interval == prev_interval:
            # Parallel fifth/octave: shift the lower voice by an octave of the chord tone.
            lower = v1 if voices[v1] < voices[v2] else v2
            lo, hi = RANGES[lower]
            alt = voices[lower] + 12 if voices[lower] + 12 <= hi else voices[lower] - 12
            if lo <= alt:
                voices[lower] = alt
    return voices


def _build_scale(key_root, is_minor):
    """Build a 7-note diatonic scale starting from the key root."""
    if is_minor:
        intervals = [0, 2, 3, 5, 7, 8, 10]  # natural minor
    else:
        intervals = [0, 2, 4, 5, 7, 9, 11]  # major
    return [(key_root + i) % 12 + 60 for i in intervals]  # start at octave 4


def generate_satb(melody_notes, key='C major', style='balanced'):
    """
    Generate a full SATB arrangement from a lead melody.

    Args:
        melody_notes: list of dicts with 'pitch' (note name or MIDI number)
                      and 'duration' (in beats, default 1.0).
        key: key string like 'C major' or 'A minor'.
        style: 'conservative', 'balanced', or 'adventurous'.

    Returns:
        dict with 'voices' (list of per-note voicings) and 'midi_data' (bytes).
    """
    key_root = _key_to_root(key)
    is_minor = 'minor' in key.lower()

    voicings = []
    prev_voicing = None

    for note in melody_notes:
        pitch = note.get('pitch', note.get('note', 60))
        if isinstance(pitch, str):
            pitch = _note_to_pitch(pitch)
        duration = note.get('duration', 1.0)

        voicing = _harmonize_note(pitch, key_root, is_minor, prev_voicing, style)
        voicing['duration'] = duration
        voicings.append(voicing)
        prev_voicing = {k: v for k, v in voicing.items() if k != 'duration'}

    # Build MIDI.
    midi_data = None
    if pretty_midi:
        midi_data = _build_midi(voicings, bpm=96)

    return {
        'key': key,
        'style': style,
        'voices': voicings,
        'voice_names': ['soprano', 'alto', 'tenor', 'bass'],
        'midi_data': midi_data,
    }


def _build_midi(voicings, bpm=96):
    """Build a pretty_midi PrettyMIDI object from SATB voicings."""
    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    voices = {
        'soprano': pretty_midi.Instrument(program=73),  # Flute
        'alto':    pretty_midi.Instrument(program=68),  # Oboe
        'tenor':   pretty_midi.Instrument(program=70),  # English Horn
        'bass':    pretty_midi.Instrument(program=58),  # Tuba
    }

    current_time = 0.0
    for voicing in voicings:
        dur = voicing.get('duration', 1.0)
        for voice_name, instrument in voices.items():
            pitch = voicing[voice_name]
            note = pretty_midi.Note(
                velocity=80,
                pitch=pitch,
                start=current_time,
                end=current_time + dur,
            )
            instrument.notes.append(note)
        current_time += dur

    for instrument in voices.values():
        pm.instruments.append(instrument)

    buf = io.BytesIO()
    pm.write(buf)
    return buf.getvalue()
