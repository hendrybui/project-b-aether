"""
Tests for notes_to_musicxml: measure packing, rests for gaps, and tied
notes split across barlines. Every measure must sum to the 4/4 time
signature (4 beats = 1920 ticks at division 480).
"""

import xml.etree.ElementTree as ET

import pytest

from engines.sheet_render import notes_to_musicxml

BEAT = 60.0 / 96.0          # seconds per beat at 96 BPM
MEASURE_TICKS = 4 * 480     # 4 beats × 480 ticks


def _root(xml_str):
    return ET.fromstring(xml_str)


def _measures(root):
    return root.find('part').findall('measure')


def _ticks(measure):
    """Total time span of a measure (chord-aware).

    Notes carrying a <chord/> element are simultaneous with the previous
    note and don't advance the time cursor; the span is the right invariant
    for a measure's time signature, unlike a naive duration sum.
    """
    cursor = 0
    for n in measure.findall('note'):
        if n.find('chord') is not None:
            continue  # shares the previous note's onset
        cursor += int(n.findtext('duration'))
    return cursor


def _rests(measure):
    return measure.findall('note/rest')


def _notes_with_ties(measure):
    out = []
    for n in measure.findall('note'):
        tie = n.find('tie')
        out.append((n.findtext('pitch/step'), tie.get('type') if tie is not None else None))
    return out


def _note(sec_start, sec_dur, pitch=60):
    return {'pitch': pitch, 'start': sec_start, 'duration': sec_dur}


def test_every_measure_sums_to_time_signature():
    # Quarter-note melody with a gap and a barline-crossing note.
    notes = [
        _note(0 * BEAT, BEAT, 60),
        _note(1 * BEAT, BEAT, 62),
        _note(3 * BEAT, 2.5 * BEAT, 64),   # starts at beat 3, spans the barline
        _note(6 * BEAT, BEAT, 67),         # gap after the tied note
    ]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert measures, 'expected at least one measure'
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS, f'measure {m.get("number")} not full'


def test_contiguous_notes_single_measure():
    notes = [_note(i * BEAT, BEAT) for i in range(4)]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 1
    assert _ticks(measures[0]) == MEASURE_TICKS
    assert len(_rests(measures[0])) == 0


def test_gap_filled_with_rest():
    # One beat of sound, then two beats of silence, then one beat.
    notes = [_note(0, BEAT), _note(3 * BEAT, BEAT)]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 1
    # 1 (note) + 2 (gap rest) + 1 (note) = 4 beats, no trailing pad needed.
    assert _ticks(measures[0]) == MEASURE_TICKS
    assert len(_rests(measures[0])) == 1


def test_note_crossing_barline_split_with_ties():
    # 2 beats of lead-in, then a 2.5-beat note crossing the barline.
    notes = [
        _note(0, 2 * BEAT, 60),
        _note(2 * BEAT, 2.5 * BEAT, 64),
    ]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    # Measure 1: lead-in note (C) + first tied segment, pitch 64 = E4 (tie start).
    assert ('E', 'start') in _notes_with_ties(measures[0])
    # Measure 2: tied continuation (tie stop) + trailing rest padding.
    assert ('E', 'stop') in _notes_with_ties(measures[1])
    assert _ticks(measures[1]) == MEASURE_TICKS


def test_long_note_split_across_multiple_measures():
    # A 6-beat note spans two barlines: segments 4 + 2, tied start/stop.
    notes = [_note(0, 6 * BEAT, 60)]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    m1, m2 = _notes_with_ties(measures[0]), _notes_with_ties(measures[1])
    assert ('C', 'start') in m1
    assert ('C', 'stop') in m2
    for m in _measures(root):
        assert _ticks(m) == MEASURE_TICKS


def test_rest_spanning_barline_split():
    # One beat of sound, then a gap that crosses into the next measure.
    notes = [
        _note(0, BEAT, 60),
        _note(6 * BEAT, BEAT, 64),  # 5 beats of silence after beat 1
    ]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    # Measure 1: note + 3-beat rest. Measure 2: 2-beat rest + note + pad.
    assert len(_rests(measures[0])) >= 1
    assert len(_rests(measures[1])) >= 1
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS


def test_final_measure_padded_with_rest():
    notes = [_note(0, BEAT, 60)]  # single beat of music
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 1
    assert _ticks(measures[0]) == MEASURE_TICKS
    assert len(_rests(measures[0])) == 1


def test_notes_without_start_placed_sequentially():
    # Backward compatibility: no 'start' → notes append back to back.
    notes = [{'pitch': 60, 'duration': BEAT} for _ in range(6)]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS


# ── harmony_to_musicxml (four-part SATB) ─────────────────────────────────

from engines.sheet_render import harmony_to_musicxml


def _voicings(n=4, dur=1.0):
    # Same shape as generate_satb output: MIDI pitches + duration in beats.
    return [
        {'soprano': 60 + i * 2, 'alto': 55 + i * 2, 'tenor': 48 + i * 2,
         'bass': 36 + i * 2, 'duration': dur}
        for i in range(n)
    ]


def _parts(root):
    return {p.get('id'): p for p in root.findall('part')}


def _part_ticks(part):
    """Per-measure time span for one part's sequential voice."""
    out = []
    for m in part.findall('measure'):
        cursor = 0
        for n in m.findall('note'):
            if n.find('chord') is not None:
                continue
            cursor += int(n.findtext('duration'))
        out.append(cursor)
    return out


def test_harmony_xml_has_four_parts():
    root = _root(harmony_to_musicxml(_voicings()))
    parts = _parts(root)
    assert list(parts) == ['P1', 'P2', 'P3', 'P4']
    names = [p.findtext('part-name') for p in root.find('part-list').findall('score-part')]
    assert names == ['Soprano', 'Alto', 'Tenor', 'Bass']
    # Two braced part groups (S+A, T+B).
    groups = root.find('part-list').findall('part-group')
    assert [(g.get('type'), g.get('number')) for g in groups] == [
        ('start', '1'), ('stop', '1'), ('start', '2'), ('stop', '2')]


def test_harmony_xml_voices_on_correct_staffs():
    voicings = _voicings()
    root = _root(harmony_to_musicxml(voicings))
    parts = _parts(root)
    for pid, key, staff in [('P1', 'soprano', '1'), ('P2', 'alto', '1'),
                            ('P3', 'tenor', '2'), ('P4', 'bass', '2')]:
        notes = parts[pid].findall('measure/note')
        note_staffs = [n.findtext('staff') for n in notes if n.find('rest') is None]
        assert note_staffs and all(s == staff for s in note_staffs), f'{pid} wrong staff'
        steps = [n.findtext('pitch/step') for n in notes if n.find('rest') is None]
        assert len(steps) == len(voicings)


def test_harmony_xml_every_part_measure_sums():
    # Varying durations and a barline-crossing whole note in every voice.
    root = _root(harmony_to_musicxml(_voicings(n=6, dur=1.5)))
    for pid, part in _parts(root).items():
        for m_idx, ticks in enumerate(_part_ticks(part)):
            assert ticks == MEASURE_TICKS, f'{pid} measure {m_idx + 1}: {ticks}'


def test_harmony_xml_two_staves_and_clefs():
    root = _root(harmony_to_musicxml(_voicings()))
    attrs = root.find('part[@id="P1"]/measure/attributes')
    assert attrs.findtext('staves') == '2'
    clefs = attrs.findall('clef')
    assert [(c.get('number'), c.findtext('sign'), c.findtext('line')) for c in clefs] == \
        [('1', 'G', '2'), ('2', 'F', '4')]


def test_harmony_xml_barline_crossing_tied_in_every_voice():
    # 1.5-beat voicing repeated 5x: the note starting at beat 3 spans the
    # barline (3->4 in m1, 4->4.5 in m2) and is tied in every part.
    root = _root(harmony_to_musicxml(_voicings(n=5, dur=1.5)))
    for pid, part in _parts(root).items():
        m1 = part.findall('measure')[0]
        m2 = part.findall('measure')[1]
        ties1 = [t.get('type') for n in m1.findall('note') for t in n.findall('tie')]
        ties2 = [t.get('type') for n in m2.findall('note') for t in n.findall('tie')]
        assert ties1.count('start') == 1, f'{pid} measure 1 missing tie start'
        assert ties2.count('stop') == 1, f'{pid} measure 2 missing tie stop'


def test_harmony_xml_metronome_matches_tempo():
    root = _root(harmony_to_musicxml(_voicings(), tempo=112))
    for pid in ('P1', 'P2', 'P3', 'P4'):
        per_minute = root.find(f'part[@id="{pid}"]/measure/direction/direction-type/metronome/per-minute')
        assert per_minute is not None and per_minute.text == '112'


def test_harmony_xml_grand_layout_two_parts():
    root = _root(harmony_to_musicxml(_voicings(), layout='grand'))
    parts = _parts(root)
    assert list(parts) == ['P1', 'P2']
    names = [p.findtext('part-name') for p in root.find('part-list').findall('score-part')]
    assert names == ['Soprano & Alto', 'Tenor & Bass']
    # One braced group, treble clef on P1, bass clef on P2.
    groups = root.find('part-list').findall('part-group')
    assert [(g.get('type'), g.get('number')) for g in groups] == [('start', '1'), ('stop', '1')]
    assert root.find('part[@id="P1"]/measure/attributes/clef/sign').text == 'G'
    assert root.find('part[@id="P2"]/measure/attributes/clef/sign').text == 'F'


def test_harmony_xml_grand_layout_chords_and_unisons():
    # S and A differ on the first beat (chord), are a unison on the second.
    voicings = [
        {'soprano': 60, 'alto': 64, 'tenor': 55, 'bass': 48, 'duration': 1.0},
        {'soprano': 62, 'alto': 62, 'tenor': 57, 'bass': 50, 'duration': 1.0},
    ]
    root = _root(harmony_to_musicxml(voicings, layout='grand'))
    p1 = root.find('part[@id="P1"]')
    p1_notes = [n for n in p1.iter('note') if n.find('rest') is None]
    # Beat 1 chord: 2 notes, second one chord-marked. Beat 2 unison: 1 note.
    assert len(p1_notes) == 3
    assert p1_notes[1].find('chord') is not None
    assert p1_notes[2].find('chord') is None
    p2 = root.find('part[@id="P2"]')
    assert len([n for n in p2.iter('note') if n.find('rest') is None]) == 4


def test_harmony_xml_grand_layout_measures_sum():
    root = _root(harmony_to_musicxml(_voicings(n=6, dur=1.5), layout='grand'))
    for pid, part in _parts(root).items():
        for m_idx, ticks in enumerate(_part_ticks(part)):
            assert ticks == MEASURE_TICKS, f'{pid} measure {m_idx + 1}: {ticks}'


def test_harmony_xml_unknown_layout_raises():
    with pytest.raises(ValueError):
        harmony_to_musicxml(_voicings(), layout='jazz')


def test_harmony_xml_midi_programs_schema_valid():
    # The MusicXML 'midi-128' type is 1..128 (1-based): 0 is rejected by the
    # official schema. The grand layout used to emit 0 for its two parts.
    for layout in ('parts4', 'grand'):
        root = _root(harmony_to_musicxml(_voicings(), layout=layout))
        programs = [int(p.findtext('midi-instrument/midi-program'))
                    for p in root.find('part-list').findall('score-part')]
        assert programs, f'{layout}: no midi-programs'
        assert all(1 <= pr <= 128 for pr in programs), \
            f'{layout}: midi-programs out of 1..128 range: {programs}'


# ── Key signatures ────────────────────────────────────────────────────────

from engines.sheet_render import key_to_fifths


def test_key_to_fifths_major_and_minor():
    # Major keys along the circle of fifths.
    assert key_to_fifths('C major') == 0
    assert key_to_fifths('G major') == 1
    assert key_to_fifths('D major') == 2
    assert key_to_fifths('A major') == 3
    assert key_to_fifths('E major') == 4
    assert key_to_fifths('B major') == 5
    assert key_to_fifths('F# major') == 6
    assert key_to_fifths('C# major') == 7
    assert key_to_fifths('F major') == -1
    # Minor keys share their relative major's signature.
    assert key_to_fifths('A minor') == 0
    assert key_to_fifths('E minor') == 1
    assert key_to_fifths('D minor') == -1
    assert key_to_fifths('G minor') == -2
    assert key_to_fifths('C minor') == -3
    assert key_to_fifths('F# minor') == 3
    assert key_to_fifths('C# minor') == 4
    assert key_to_fifths('A# minor') == 7
    assert key_to_fifths('A# major') == -2   # = Bb major
    assert key_to_fifths('G# major') == -4   # = Ab major
    # Case-insensitive, garbage-tolerant.
    assert key_to_fifths('e minor') == 1
    assert key_to_fifths('C MAJOR') == 0
    assert key_to_fifths('') == 0
    assert key_to_fifths(None) == 0
    assert key_to_fifths('not a key') == 0


def test_notes_to_musicxml_honors_key_signature():
    notes = [_note(0, BEAT, 60), _note(1 * BEAT, BEAT, 62)]
    root = _root(notes_to_musicxml(notes, tempo=96, key='G major'))
    fifths = root.find('part/measure/attributes/key/fifths')
    assert fifths is not None and fifths.text == '1'
    # Default stays C major.
    root = _root(notes_to_musicxml(notes, tempo=96))
    assert root.find('part/measure/attributes/key/fifths').text == '0'


def test_harmony_xml_honors_key_signature():
    root = _root(harmony_to_musicxml(_voicings(), layout='parts4', key='D minor'))
    for pid in ('P1', 'P2', 'P3', 'P4'):
        fifths = root.find(f'part[@id="{pid}"]/measure/attributes/key/fifths')
        assert fifths is not None and fifths.text == '-1', f'{pid} wrong signature'
    root = _root(harmony_to_musicxml(_voicings(), layout='grand', key='F# minor'))
    for pid in ('P1', 'P2'):
        fifths = root.find(f'part[@id="{pid}"]/measure/attributes/key/fifths')
        assert fifths is not None and fifths.text == '3', f'{pid} wrong signature'
def test_small_notes_do_not_crash():
    notes = [_note(i * 0.2 * BEAT, 0.2 * BEAT) for i in range(10)]
    root = _root(notes_to_musicxml(notes, tempo=96))
    assert _measures(root)


def test_simultaneous_notes_stack_as_chords():
    # Four voices sounding together on every beat (the harmony export shape).
    notes = []
    for i in range(8):
        for p in (60, 64, 67, 72):
            notes.append(_note(i * BEAT, BEAT, p))
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS
        chords = m.findall('note/chord')
        # 4 beats × 3 chord markers (first note of each chord is unmarked).
        assert len(chords) == 12


def test_measures_sum_with_fractional_durations():
    # Real transcription timing is rarely tick-exact: durations like 0.9042
    # beats lose ticks to rounding, and the trailing rest must absorb the
    # drift so every measure still sums to exactly 1920.
    notes = [
        _note(0, 0.9042 * BEAT, 60),
        _note(0.9938 * BEAT, 0.9042 * BEAT, 64),
        _note(1.8980 * BEAT, 0.9042 * BEAT, 67),
        _note(2.9022 * BEAT, 0.9042 * BEAT, 72),
        _note(3.8064 * BEAT, 0.9042 * BEAT, 67),
        _note(4.8106 * BEAT, 0.9042 * BEAT, 64),
        _note(5.7148 * BEAT, 0.9042 * BEAT, 62),
        _note(6.7190 * BEAT, 0.9042 * BEAT, 60),
    ]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS, f'measure {m.get("number")} not full'


def test_chord_crossing_barline_each_note_tied():
    # A chord whose notes span the barline: every voice splits with ties.
    notes = [
        _note(0, 2 * BEAT, 60),
        _note(2 * BEAT, 2.5 * BEAT, 64),
        _note(2 * BEAT, 2.5 * BEAT, 67),  # second voice of the same chord
    ]
    root = _root(notes_to_musicxml(notes, tempo=96))
    measures = _measures(root)
    assert len(measures) == 2
    m1_ties = [t for _, t in _notes_with_ties(measures[0])]
    m2_ties = [t for _, t in _notes_with_ties(measures[1])]
    # Both chord voices emit start ties in measure 1, stop ties in measure 2.
    assert m1_ties.count('start') == 2
    assert m2_ties.count('stop') == 2
    for m in measures:
        assert _ticks(m) == MEASURE_TICKS
