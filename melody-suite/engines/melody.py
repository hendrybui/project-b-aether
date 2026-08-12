"""
Melody Generator Engine
=======================
Implements procedural melody generation using a Markov chain over
scale-degree transitions, inspired by MusicVAE (fresh ideas) and MusicRNN
(continuation) from Google Magenta.

Instead of neural networks, this engine uses:
  - A built-in transition matrix of scale-degree tendencies derived from
    common melodic patterns (nursery rhymes, folk, pop melodies)
  - Prompt parsing to select key, tempo, and melodic character
  - Temperature-scaled sampling for variety control

The workflow mirrors GadegetKit's: generate N candidates, compare them
side by side, continue from any one, curate the best.

Key research reference: .firecrawl/magenta-melody-gen-deep-dive.md
"""

import random
import numpy as np

try:
    import pretty_midi
except ImportError:
    pretty_midi = None

# ── Scale Definitions ───────────────────────────────────────────────────────

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

SCALES = {
    'major':     [0, 2, 4, 5, 7, 9, 11],
    'minor':     [0, 2, 3, 5, 7, 8, 10],
    'dorian':    [0, 2, 3, 5, 7, 9, 10],
    'mixolydian':[0, 2, 4, 5, 7, 9, 10],
    'pentatonic':[0, 2, 4, 7, 9],
    'blues':     [0, 3, 5, 6, 7, 10],
}

# ── Markov Transition Matrix ────────────────────────────────────────────────
# Scale-degree transition probabilities (0-indexed within an octave).
# These encode common melodic tendencies: stepwise motion > leaps,
# leaps tend to resolve inward, tonic and dominant are common resting points.
# Values are unnormalized weights; 0 = never, 5 = very likely.

BASE_TRANSITIONS = {
    # From tonic: likes to move stepwise up, or jump to dominant
    0: {0: 1, 1: 4, 2: 5, 3: 1, 4: 3, 5: 1, 6: 1, 7: 2},
    # From 2nd: resolves to tonic or moves to 3rd
    1: {0: 4, 1: 1, 2: 4, 3: 3, 4: 2, 5: 1, 6: 1, 7: 1},
    # From 3rd: resolves to tonic or moves stepwise
    2: {0: 3, 1: 3, 2: 1, 3: 2, 4: 4, 5: 2, 6: 1, 7: 1},
    # From 4th: leading tone to dominant (tension)
    3: {0: 1, 1: 1, 2: 2, 3: 1, 4: 5, 5: 3, 6: 2, 7: 1},
    # From dominant: powerful, moves to tonic or 6th
    4: {0: 5, 1: 2, 2: 3, 3: 2, 4: 1, 5: 4, 6: 3, 7: 2},
    # From 6th: moves to dominant or tonic
    5: {0: 3, 1: 1, 2: 2, 3: 2, 4: 5, 5: 1, 6: 2, 7: 2},
    # From 7th: leading tone, resolves UP to tonic
    6: {0: 5, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1},
}

# Rhythm patterns (in 16th-note steps). Each is one bar.
RHYTHM_PATTERNS = [
    [4, 4, 4, 4],          # quarter notes
    [2, 2, 4, 4, 2, 2],    # galloping eighth-quarter pattern
    [4, 2, 2, 4, 4],       # varied
    [8, 4, 4],             # half note then quarters
    [2, 2, 2, 2, 2, 2, 2, 2],  # all eighth notes
    [4, 4, 2, 2, 4],       # syncopated
    [16],                   # whole note
    [3, 1, 4, 4, 4],       # dotted quarter pickup
]

# ── Prompt Parsing ──────────────────────────────────────────────────────────

MOOD_KEYWORDS = {
    'happy':    {'scale': 'major',     'tempo': 120, 'leap_prob': 0.25, 'octave': 4},
    'bright':   {'scale': 'major',     'tempo': 130, 'leap_prob': 0.30, 'octave': 4},
    'sad':      {'scale': 'minor',     'tempo': 70,  'leap_prob': 0.10, 'octave': 3},
    'dark':     {'scale': 'minor',     'tempo': 80,  'leap_prob': 0.15, 'octave': 3},
    'calm':     {'scale': 'pentatonic','tempo': 72,  'leap_prob': 0.10, 'octave': 4},
    'tense':    {'scale': 'minor',     'tempo': 100, 'leap_prob': 0.20, 'octave': 4},
    'lofi':     {'scale': 'dorian',    'tempo': 78,  'leap_prob': 0.12, 'octave': 4},
    'lo-fi':    {'scale': 'dorian',    'tempo': 78,  'leap_prob': 0.12, 'octave': 4},
    'epic':     {'scale': 'minor',     'tempo': 90,  'leap_prob': 0.30, 'octave': 4},
    'playful':  {'scale': 'major',     'tempo': 140, 'leap_prob': 0.35, 'octave': 5},
    'mellow':   {'scale': 'mixolydian','tempo': 82,  'leap_prob': 0.12, 'octave': 4},
    'dreamy':   {'scale': 'pentatonic','tempo': 68,  'leap_prob': 0.08, 'octave': 4},
    'dark trap':{'scale': 'minor',     'tempo': 140, 'leap_prob': 0.15, 'octave': 3},
    'jazz':     {'scale': 'blues',     'tempo': 110, 'leap_prob': 0.20, 'octave': 4},
}

DEFAULT_MOOD = {'scale': 'major', 'tempo': 96, 'leap_prob': 0.18, 'octave': 4}


def parse_prompt(prompt):
    """
    Parse a natural-language mood prompt into musical parameters.

    Args:
        prompt: text like "warm lo-fi intro for late-night study"

    Returns:
        dict with scale, tempo, leap_prob, octave, key_root.
    """
    prompt_lower = prompt.lower()

    for keyword, config in MOOD_KEYWORDS.items():
        if keyword in prompt_lower:
            mood = dict(config)  # copy
            break
    else:
        mood = dict(DEFAULT_MOOD)

    # Try to extract a specific key from the prompt. Only whole words count,
    # and a bare note-name word only counts when it's clearly musical:
    # followed by 'major'/'minor' ("in D minor") or preceded by 'in'
    # ("a song in F"). Otherwise the article "a" or letters inside words
    # like "melancholy"/"happy" would hijack the key.
    key_root = 0  # C by default
    words = prompt_lower.split()
    name_to_idx = {name.lower(): i for i, name in enumerate(NOTE_NAMES)}
    for i, w in enumerate(words):
        if w not in name_to_idx:
            continue
        followed_by_mode = (i + 1 < len(words) and words[i + 1] in ('major', 'minor'))
        preceded_by_in = (i > 0 and words[i - 1] == 'in')
        if followed_by_mode or preceded_by_in:
            key_root = name_to_idx[w]
            break

    # An explicit 'major'/'minor' word overrides the mood's default scale
    # (e.g. "melancholy in A minor" should stay minor, not default major).
    if 'minor' in words:
        mood['scale'] = 'minor'
    elif 'major' in words:
        mood['scale'] = 'major'

    mood['key_root'] = key_root
    mood['key_name'] = f"{NOTE_NAMES[key_root]} {mood['scale']}"
    return mood


# ── Markov Chain Melody Generation ──────────────────────────────────────────

def _weighted_choice(weights_dict, temperature=1.0, exclude=None):
    """
    Choose a key from a weights dict using temperature-scaled sampling.

    Args:
        weights_dict: {value: weight} mapping.
        temperature: float; <1 = more predictable, >1 = more random.
        exclude: value to exclude from selection.
    """
    if exclude is not None:
        weights_dict = {k: v for k, v in weights_dict.items() if k != exclude}

    values = list(weights_dict.keys())
    raw_weights = np.array(list(weights_dict.values()), dtype=float)

    # Apply temperature: divide log-weights by temperature.
    # Low temp → sharpens toward highest weights. High temp → flattens.
    if temperature != 1.0 and temperature > 0:
        log_weights = np.log(raw_weights + 1e-8)
        scaled = log_weights / temperature
        # Softmax-style normalization.
        scaled = scaled - scaled.max()
        raw_weights = np.exp(scaled)

    raw_weights /= raw_weights.sum()
    choice_idx = np.random.choice(len(values), p=raw_weights)
    return values[choice_idx]


def generate_melody(mood, num_bars=4, temperature=1.0, seed=None, leap_prob=None,
                    start_degree=0):
    """
    Generate a single melody using the Markov chain.

    Args:
        mood: dict from parse_prompt().
        num_bars: number of bars to generate.
        temperature: sampling temperature.
        seed: optional random seed for reproducibility.
        leap_prob: override leap probability from mood.
        start_degree: scale-degree index to begin on (0 = tonic); used by
            'continue' mode so the continuation lands on the seed's last note.

    Returns:
        dict with notes (list of {pitch, start, duration}), tempo, key.
    """
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)

    scale_name = mood['scale']
    scale = SCALES[scale_name]
    key_root = mood['key_root']
    base_octave = mood['octave']
    lp = leap_prob if leap_prob is not None else mood['leap_prob']
    tempo = mood['tempo']

    # Compute actual MIDI pitches for 2 octaves of the scale.
    notes_in_range = []
    for octave in [base_octave, base_octave + 1]:
        for degree_offset in scale:
            midi = (octave + 1) * 12 + key_root + degree_offset
            notes_in_range.append(midi)

    # Start on the tonic (or the continuation's landing degree).
    current_degree = max(0, min(int(start_degree), len(notes_in_range) - 1))

    melody_notes = []
    current_step = 0  # in 16th-note steps

    for bar in range(num_bars):
        rhythm = random.choice(RHYTHM_PATTERNS)

        for duration in rhythm:
            # Decide: step vs leap.
            if random.random() < lp:
                # Leap: jump 2+ scale degrees.
                leap_size = random.choice([2, 2, 3, 3, 4, 5])
                leap_dir = random.choice([-1, 1])
                new_degree = current_degree + (leap_dir * leap_size)
            else:
                # Stepwise: use Markov transition.
                transitions = BASE_TRANSITIONS.get(current_degree, BASE_TRANSITIONS[0])
                new_degree = _weighted_choice(transitions, temperature)

            # Wrap around within a 2-octave range.
            num_degrees = len(notes_in_range)
            clamped_degree = max(0, min(new_degree, num_degrees - 1))
            current_degree = clamped_degree

            pitch = notes_in_range[clamped_degree]

            # Convert 16th-note steps to beats (4 steps per beat in 4/4).
            start_beat = current_step / 4.0
            duration_beats = duration / 4.0

            melody_notes.append({
                'pitch': pitch,
                'pitch_name': _midi_to_name(pitch),
                'start': round(start_beat, 3),
                'duration': round(duration_beats, 3),
            })

            current_step += duration

    return {
        'notes': melody_notes,
        'tempo': tempo,
        'key': mood['key_name'],
        'scale': scale_name,
        'num_bars': num_bars,
    }


def _midi_to_name(midi):
    """Convert a MIDI pitch to a note name like 'C4'."""
    octave = (midi // 12) - 1
    pc = midi % 12
    return f"{NOTE_NAMES[pc]}{octave}"


def _nearest_degree(pitch, scale, key_root, base_octave):
    """Map a seed MIDI pitch to the nearest scale-degree index in the
    2-octave note pool, so a continuation starts on the seed's landing note."""
    notes_in_range = [
        (octave + 1) * 12 + key_root + degree
        for octave in (base_octave, base_octave + 1)
        for degree in scale
    ]
    return min(range(len(notes_in_range)), key=lambda i: abs(notes_in_range[i] - pitch))


def generate_candidates(prompt, mode='fresh', num_candidates=3, temperature=1.0, num_bars=4, seed_melody=None):
    """
    Generate N candidate melodies (GadegetKit's "3-candidate grid").

    Args:
        prompt: mood prompt string (for fresh mode).
        mode: 'fresh' (like MusicVAE sampling) or 'continue' (like MusicRNN).
        num_candidates: how many melodies to generate.
        temperature: sampling temperature.
        num_bars: length of each melody.
        seed_melody: list of note dicts (required for 'continue' mode).

    Returns:
        list of melody dicts (see generate_melody).
    """
    mood = parse_prompt(prompt)

    # In 'continue' mode, the seed's final pitch selects the starting scale
    # degree, so the generated phrase continues from where the seed left off.
    start_degree = 0
    if mode == 'continue' and seed_melody:
        last = seed_melody[-1]
        try:
            last_pitch = float(last.get('pitch', last.get('note', 60)))
            start_degree = _nearest_degree(last_pitch, SCALES[mood['scale']],
                                           mood['key_root'], mood['octave'])
        except (TypeError, ValueError, AttributeError):
            start_degree = 0

    candidates = []
    for i in range(num_candidates):
        seed = random.randint(0, 2**31)

        if mode == 'continue' and seed_melody:
            # Continue from a seed melody: anchor the start on the seed's
            # last pitch, then generate onward.
            mel = generate_melody(mood, num_bars=num_bars, temperature=temperature,
                                  seed=seed, start_degree=start_degree)
            # Generated notes start at 0; shift them to begin right after the
            # seed's last note so the seed and continuation don't overlap.
            offset = max(
                (float(n.get('start', 0.0)) + float(n.get('duration', 1.0))
                 for n in seed_melody),
                default=0.0,
            )
            for n in mel['notes']:
                n['start'] = round(n['start'] + offset, 3)
            # Prepend the seed melody.
            mel['notes'] = seed_melody + mel['notes']
            mel['mode'] = 'continue'
        else:
            mel = generate_melody(mood, num_bars=num_bars, temperature=temperature,
                                  seed=seed)
            mel['mode'] = 'fresh'

        mel['candidate_id'] = i
        candidates.append(mel)

    return candidates


def melody_to_midi(melody, filename=None):
    """
    Convert a melody dict to MIDI bytes (or write to file).

    Args:
        melody: dict from generate_melody/generate_candidates.
        filename: if provided, write to this path. Otherwise return bytes.

    Returns:
        MIDI bytes (or None if filename given).
    """
    if pretty_midi is None:
        return None

    # Generator output is in beats (start/duration); pretty_midi uses seconds.
    tempo = melody.get('tempo', 96)
    seconds_per_beat = 60.0 / tempo
    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    instrument = pretty_midi.Instrument(program=73)  # Flute

    for note in melody['notes']:
        start = note['start'] * seconds_per_beat
        midi_note = pretty_midi.Note(
            velocity=75,
            pitch=note['pitch'],
            start=start,
            end=start + note['duration'] * seconds_per_beat,
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
