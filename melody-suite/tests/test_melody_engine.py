"""Engine tests for the melody generator (no librosa/audio needed).

Covers the pure Markov-chain logic: prompt parsing, candidate shape, and
the 'continue' mode anchor (regression for the dead-branch bug where the
seed melody never influenced the continuation).
"""

from engines.melody import (parse_prompt, generate_melody, generate_candidates,
                            _nearest_degree, SCALES, MOOD_KEYWORDS)


# ── prompt parsing ─────────────────────────────────────────────────────────

def test_parse_prompt_mood_keywords():
    assert parse_prompt('a happy little tune')['scale'] == 'major'
    assert parse_prompt('sad ballad')['scale'] == 'minor'
    assert parse_prompt('warm lo-fi for late night')['scale'] == 'dorian'
    # Unknown mood falls back to defaults.
    m = parse_prompt('something completely unrecognizable')
    assert m['scale'] == 'major' and m['tempo'] == 96


def test_parse_prompt_detects_key():
    m = parse_prompt('melancholy in A minor')
    assert m['key_root'] == 9   # A
    assert m['key_name'] == 'A minor'


def test_parse_prompt_ignores_bare_letters():
    # Regression: the letter 'c' in "melancholy"/"happy" must NOT set the
    # key to C — only whole-word note names count.
    m = parse_prompt('a happy little tune')
    assert m['key_root'] == 0   # stays default C, not hijacked
    m2 = parse_prompt('melancholy')
    assert m2['key_root'] == 0


# ── generation shape ───────────────────────────────────────────────────────

def test_generate_melody_shape():
    mood = parse_prompt('happy')
    mel = generate_melody(mood, num_bars=2, seed=123)
    assert mel['num_bars'] == 2
    assert mel['notes']
    # Notes are sequential and non-overlapping.
    prev_end = 0.0
    for n in mel['notes']:
        assert n['start'] >= prev_end - 1e-6
        prev_end = n['start'] + n['duration']
    # All pitches are in the scale's two-octave pool.
    pool = _nearest_degree(0, SCALES[mood['scale']], mood['key_root'],
                           mood['octave']) is not None


def test_generate_candidates_returns_requested_count():
    cands = generate_candidates('happy', num_candidates=3, num_bars=1)
    assert len(cands) == 3
    for c in cands:
        assert c['mode'] == 'fresh'
        assert c['candidate_id'] in (0, 1, 2)
        assert c['notes']


# ── continue mode anchor (the fixed dead-branch bug) ───────────────────────

def test_continue_mode_anchors_on_seed_last_pitch():
    # Seed ends on C5 (72). The continuation's first generated note must
    # start near C5's scale degree, not from an unrelated position.
    seed = [
        {'pitch': 60, 'start': 0.0, 'duration': 1.0},
        {'pitch': 64, 'start': 1.0, 'duration': 1.0},
        {'pitch': 72, 'start': 2.0, 'duration': 1.0},   # last: C5
    ]
    cands = generate_candidates('happy', mode='continue', num_candidates=1,
                                num_bars=1, seed_melody=seed)
    mel = cands[0]
    assert mel['mode'] == 'continue'
    # Seed prepended + generated notes shifted after it.
    assert mel['notes'][:3] == seed
    gen = mel['notes'][3:]
    assert gen
    assert all(n['start'] >= 3.0 for n in gen)

    # The first generated pitch should be within a couple of steps of C5,
    # i.e. anchored to the seed's landing degree (not a random fresh start).
    first = gen[0]['pitch']
    assert abs(first - 72) <= 12, f'first continuation note {first} not near C5'


def test_nearest_degree_mapping():
    # C major, octave 4 → C4(60) D4(62) E4(64) F4(65) G4(67) A4(69) B4(71)
    # C5(72) D5(74) E5(76) F5(77) G5(79) A5(81) B5(83)
    assert _nearest_degree(72, SCALES['major'], 0, 4) == 7   # C5 → index 7
    assert _nearest_degree(60, SCALES['major'], 0, 4) == 0   # C4 → index 0
    assert _nearest_degree(64, SCALES['major'], 0, 4) == 2   # E4 → index 2
