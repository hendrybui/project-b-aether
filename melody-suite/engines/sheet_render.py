"""
Sheet Music Rendering Engine
============================
Converts note sequences into MusicXML (for notation software like MuseScore)
and a text-based staff representation (for browser display without requiring
a JavaScript notation library like OSMD).

This powers the MP3-to-Sheet tool, the Analyzer's notation view, and the
harmony page's four-part MusicXML export.
"""

import xml.etree.ElementTree as ET
from xml.dom import minidom

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_TO_STEP = {'C': 'C', 'C#': 'C', 'D': 'D', 'D#': 'D', 'E': 'E', 'F': 'F',
                'F#': 'F', 'G': 'G', 'G#': 'G', 'A': 'A', 'A#': 'A', 'B': 'B'}
NOTE_ALTER = {'C': 0, 'C#': 1, 'D': 0, 'D#': 1, 'E': 0, 'F': 0,
              'F#': 1, 'G': 0, 'G#': 1, 'A': 0, 'A#': 1, 'B': 0}

# Step offsets within an octave (C=0, D=1, E=2, F=3, G=4, A=5, B=6).
STEP_INDEX = {'C': 0, 'D': 1, 'E': 2, 'F': 3, 'G': 4, 'A': 5, 'B': 6}

# Division: 480 ticks per quarter note (standard).
DIVISION = 480
MAX_BEATS = 4.0          # 4/4 time
EPS = 1e-6

# ── Key signature (fifths) ────────────────────────────────────────────────
# Maps a key name like 'C major' / 'A minor' / 'F# minor' to the MusicXML
# <fifths> value (sharps positive, flats negative). Based on the circle of
# fifths; minor keys use their relative major's signature.
_SHARP_ORDER = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
_FIFTHS_SHARP = {'C': 0, 'G': 1, 'D': 2, 'A': 3, 'E': 4, 'B': 5, 'F#': 6, 'C#': 7}
_FIFTHS_FLAT = {'F': -1, 'Bb': -2, 'Eb': -3, 'Ab': -4, 'Db': -5, 'Gb': -6, 'Cb': -7}
_SHARP_TO_FLAT = {'C#': 'Db', 'D#': 'Eb', 'F#': 'Gb', 'G#': 'Ab', 'A#': 'Bb'}
_FLAT_TO_SHARP = {v: k for k, v in _SHARP_TO_FLAT.items()}


def key_to_fifths(key_name):
    """Convert a key name like 'C major' or 'A minor' to a fifths value.

    Returns 0 (C major / A minor) for unknown or missing keys.
    """
    if not key_name:
        return 0
    parts = str(key_name).strip().lower().split()
    if not parts:
        return 0
    root = parts[0].capitalize()
    mode = parts[1] if len(parts) > 1 else 'major'
    root = _FLAT_TO_SHARP.get(root, root)
    if root not in _SHARP_ORDER:
        return 0
    if mode.startswith('min'):
        # Relative major of a minor tonic is 3 semitones up.
        root = _SHARP_ORDER[(_SHARP_ORDER.index(root) + 3) % 12]
    if root in _FIFTHS_SHARP:
        return _FIFTHS_SHARP[root]
    flat = _SHARP_TO_FLAT.get(root, root)
    return _FIFTHS_FLAT.get(flat, 0)


def _pitch_to_step_octave(midi):
    """Extract MusicXML step, alter, and octave from a MIDI pitch."""
    octave = int(midi / 12) - 1
    pc = int(midi) % 12
    name = NOTE_NAMES[pc]
    return NOTE_TO_STEP[name], NOTE_ALTER[name], octave


def _split_events(notes_beats):
    """
    Split beat-based notes into per-measure events with tie types.

    Args:
        notes_beats: list of (start_beats, dur_beats, pitch) tuples.

    Returns:
        dict {measure_idx: [[offset_beats, dur_beats, pitch, tie_type], ...]}
        where notes crossing a barline are split into tied segments.
    """
    measures = {}
    for start_beats, dur_beats, pitch in notes_beats:
        pos = start_beats
        remaining = dur_beats
        segments = []
        guard = 0
        while remaining > EPS and guard < 10000:
            in_measure = pos % MAX_BEATS
            if in_measure <= EPS or MAX_BEATS - in_measure <= EPS:
                # Snap float drift to the barline.
                pos = round(pos / MAX_BEATS) * MAX_BEATS
                in_measure = 0.0
            chunk = min(remaining, MAX_BEATS - in_measure)
            segments.append((round(pos, 6), round(chunk, 6)))
            pos += chunk
            remaining -= chunk
            guard += 1
        for i, (spos, schunk) in enumerate(segments):
            m_idx = int(spos // MAX_BEATS)
            offset = spos % MAX_BEATS
            if len(segments) > 1:
                tie = 'start' if i == 0 else (
                    'stop' if i == len(segments) - 1 else 'continue')
            else:
                tie = None
            measures.setdefault(m_idx, []).append([offset, schunk, pitch, tie])
    if not measures:
        # Empty input: still emit a single measure of silence.
        measures[0] = []
    return measures


def _write_note(measure, pitch, dur_ticks, type_beats, tie_type,
                staff=None, chord=False):
    """Emit one note (or rest, if pitch is None) element."""
    note_el = ET.SubElement(measure, 'note')
    if chord:
        # Subsequent notes of a chord: mark so notation software stacks them.
        ET.SubElement(note_el, 'chord')
    if pitch is None:
        ET.SubElement(note_el, 'rest')
    else:
        pitch_el = ET.SubElement(note_el, 'pitch')
        step, alter, octave = _pitch_to_step_octave(pitch)
        ET.SubElement(pitch_el, 'step').text = step
        if alter:
            ET.SubElement(pitch_el, 'alter').text = str(alter)
        ET.SubElement(pitch_el, 'octave').text = str(octave)
    ET.SubElement(note_el, 'duration').text = str(dur_ticks)
    if tie_type:
        ET.SubElement(note_el, 'tie', {'type': tie_type})
    ET.SubElement(note_el, 'type').text = _beats_to_type(type_beats)
    if staff is not None:
        ET.SubElement(note_el, 'staff').text = str(staff)
    if tie_type:
        notations = ET.SubElement(note_el, 'notations')
        ET.SubElement(notations, 'tied', {'type': tie_type})


def _fill_measure(measure, m_events, staff=None):
    """
    Render one measure's events in integer ticks: rests fill gaps,
    simultaneous notes stack as chords, and the trailing rest absorbs
    rounding drift so the measure sums to exactly 1920 ticks.
    """
    def ticks_of(beats):
        """Round beats to the nearest tick."""
        return int(beats * DIVISION + 0.5)

    m_events = sorted(m_events, key=lambda e: (e[0], 0 if e[2] is None else e[2]))
    cursor = 0  # ticks of time placed so far in this measure
    i = 0
    while i < len(m_events):
        offset = m_events[i][0]
        onset = int(offset * DIVISION)  # floor keeps the note in-measure
        if onset > cursor:
            _write_note(measure, None, onset - cursor,
                        (onset - cursor) / DIVISION, None, staff=staff)
        # Chord: every note sharing this offset stacks at the same spot.
        max_end = onset
        first_in_chord = True
        while i < len(m_events) and m_events[i][0] - offset <= EPS:
            dur_ticks = max(1, min(ticks_of(m_events[i][1]),
                                   DIVISION * MAX_BEATS - onset))
            _write_note(measure, m_events[i][2], dur_ticks, m_events[i][1],
                        m_events[i][3], staff=staff, chord=not first_in_chord)
            max_end = max(max_end, onset + dur_ticks)
            first_in_chord = False
            i += 1
        cursor = max_end
    measure_ticks = int(DIVISION * MAX_BEATS)
    if measure_ticks - cursor > 0:
        _write_note(measure, None, measure_ticks - cursor,
                    (measure_ticks - cursor) / DIVISION, None, staff=staff)


def _add_attributes(measure, tempo, staves=1, clefs=(('G', '2'),), fifths=0):
    """Write the measure-1 attributes: divisions, key, time, clef(s)."""
    attr = ET.SubElement(measure, 'attributes')
    ET.SubElement(attr, 'divisions').text = str(DIVISION)
    key = ET.SubElement(attr, 'key')
    ET.SubElement(key, 'fifths').text = str(fifths)
    time_el = ET.SubElement(attr, 'time')
    ET.SubElement(time_el, 'beats').text = '4'
    ET.SubElement(time_el, 'beat-type').text = '4'
    if staves > 1:
        ET.SubElement(attr, 'staves').text = str(staves)
    for number, (sign, line) in enumerate(clefs, start=1):
        clef = ET.SubElement(attr, 'clef')
        if staves > 1:
            clef.set('number', str(number))
        ET.SubElement(clef, 'sign').text = sign
        ET.SubElement(clef, 'line').text = line


def _add_metronome(measure, tempo):
    """Write a metronome direction marking the tempo."""
    dir_el = ET.SubElement(measure, 'direction', {'placement': 'above'})
    dir_type = ET.SubElement(dir_el, 'direction-type')
    ET.SubElement(dir_type, 'metronome')
    metronome = dir_type.find('metronome')
    ET.SubElement(metronome, 'beat-unit').text = 'quarter'
    ET.SubElement(metronome, 'per-minute').text = str(int(tempo))


def notes_to_musicxml(notes, title="Transcription", tempo=96, key=None):
    """
    Convert note list to a MusicXML string.

    Notes are placed on a 4/4 grid using their start/duration (in seconds).
    Notes that start at the same time are emitted as a chord. Gaps are
    filled with rests, notes that cross a barline are split into tied
    segments, and the final measure is padded — so every measure sums to
    exactly 4 beats.

    Args:
        notes: list of {pitch, start, duration} dicts (start/duration in
               seconds; if start is omitted the note is appended sequentially).
        title: score title.
        tempo: BPM.
        key: key name like 'C major' or 'A minor' for the key signature;
             defaults to C major (fifths=0).

    Returns:
        MusicXML string.
    """
    # Build the tree.
    root = ET.Element('score-partwise', {'version': '4.0'})

    # Work title.
    work = ET.SubElement(root, 'work')
    ET.SubElement(work, 'work-title').text = title

    # Part list.
    part_list = ET.SubElement(root, 'part-list')
    score_part = ET.SubElement(part_list, 'score-part', {'id': 'P1'})
    ET.SubElement(score_part, 'part-name').text = 'Melody'
    score_instr = ET.SubElement(score_part, 'score-instrument', {'id': 'P1-I1'})
    ET.SubElement(score_instr, 'instrument-name').text = 'Piano'

    # Part.
    part = ET.SubElement(root, 'part', {'id': 'P1'})

    seconds_per_beat = 60.0 / tempo

    # Normalize every note to absolute beats; notes without a start are
    # appended sequentially after the previous one.
    notes_beats = []
    run_cursor = 0.0
    for note_data in notes:
        pitch = note_data['pitch']
        dur_beats = note_data['duration'] / seconds_per_beat
        if 'start' in note_data:
            start_beats = note_data['start'] / seconds_per_beat
        else:
            start_beats = run_cursor
        notes_beats.append((start_beats, dur_beats, pitch))
        run_cursor = max(run_cursor, start_beats + dur_beats)

    measures = _split_events(notes_beats)
    for m_idx in sorted(measures):
        measure = ET.SubElement(part, 'measure', {'number': str(m_idx + 1)})
        if m_idx == 0:
            _add_attributes(measure, tempo, fifths=key_to_fifths(key))
            _add_metronome(measure, tempo)
        _fill_measure(measure, measures[m_idx])

    # Pretty-print.
    rough = ET.tostring(root, 'unicode')
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent='  ')


# ── Multi-part (SATB) export ───────────────────────────────────────────────

# (voice key, part name, MIDI program, staff number)
SATB_VOICES = [
    ('soprano', 'Soprano', 73, 1),   # Flute
    ('alto',    'Alto',    68, 1),   # Oboe
    ('tenor',   'Tenor',   70, 2),   # English Horn
    ('bass',    'Bass',    58, 2),   # Tuba
]
# In the four-part layout each part spans two staves (treble + bass); S/A
# sing on staff 1, T/B on staff 2, so the four parts sit on two staff lines.
SATB_CLEFS = (('G', '2'), ('F', '4'))

# Layout names accepted by harmony_to_musicxml.
HARMONY_LAYOUTS = ('parts4', 'grand')


def _add_score_part(part_list, pid, name, program):
    """Add a score-part (with instrument + MIDI program) to the part-list."""
    score_part = ET.SubElement(part_list, 'score-part', {'id': pid})
    ET.SubElement(score_part, 'part-name').text = name
    score_instr = ET.SubElement(score_part, 'score-instrument', {'id': f'{pid}-I1'})
    ET.SubElement(score_instr, 'instrument-name').text = name
    midi_instr = ET.SubElement(score_part, 'midi-instrument', {'id': f'{pid}-I1'})
    ET.SubElement(midi_instr, 'midi-program').text = str(program)


def _render_part(root, pid, clefs, staff, notes_beats, tempo, fifths=0):
    """Render one part from beat-based notes with full measure discipline."""
    part = ET.SubElement(root, 'part', {'id': pid})
    measures = _split_events(notes_beats)
    for m_idx in sorted(measures):
        measure = ET.SubElement(part, 'measure', {'number': str(m_idx + 1)})
        if m_idx == 0:
            _add_attributes(measure, tempo, staves=len(clefs), clefs=clefs,
                            fifths=fifths)
            _add_metronome(measure, tempo)
        _fill_measure(measure, measures[m_idx], staff=staff)


def _pretty(root):
    """Serialize a score tree to a pretty-printed MusicXML string."""
    rough = ET.tostring(root, 'unicode')
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent='  ')


def _harmony_xml(voicings, title, tempo, layout, key=None):
    """Build the score tree for the requested layout."""
    root = ET.Element('score-partwise', {'version': '4.0'})
    work = ET.SubElement(root, 'work')
    ET.SubElement(work, 'work-title').text = title
    part_list = ET.SubElement(root, 'part-list')
    fifths = key_to_fifths(key)

    def brace(num):
        grp = ET.SubElement(part_list, 'part-group', {'type': 'start', 'number': str(num)})
        ET.SubElement(grp, 'group-symbol').text = 'brace'

    if layout == 'grand':
        # Piano-style reduction: two parts, one per hand. S+A are chorded on
        # the treble staff, T+B on the bass staff; unisons are collapsed so a
        # pianist plays each pitch once.
        brace(1)
        _add_score_part(part_list, 'P1', 'Soprano & Alto', 1)
        _add_score_part(part_list, 'P2', 'Tenor & Bass', 1)
        ET.SubElement(part_list, 'part-group', {'type': 'stop', 'number': '1'})
        for pid, clef, (key1, key2) in (
                ('P1', (('G', '2'),), ('soprano', 'alto')),
                ('P2', (('F', '4'),), ('tenor', 'bass'))):
            notes_beats = []
            seen = set()
            start = 0.0
            for voicing in voicings:
                dur = voicing['duration']
                for key in (key1, key2):
                    pitch = voicing[key]
                    if (round(start, 6), pitch) not in seen:
                        seen.add((round(start, 6), pitch))
                        notes_beats.append((start, dur, pitch))
                start += dur
            _render_part(root, pid, clef, 1, notes_beats, tempo, fifths=fifths)
        return root

    # parts4 (default): one part per voice; S/A share the treble staff and
    # T/B the bass staff, braced in pairs.
    brace(1)
    part_ids = []
    for i, (key, name, program, staff) in enumerate(SATB_VOICES, start=1):
        pid = f'P{i}'
        part_ids.append((pid, key, staff))
        _add_score_part(part_list, pid, name, program)
        if i == 2:
            ET.SubElement(part_list, 'part-group', {'type': 'stop', 'number': '1'})
            brace(2)
    ET.SubElement(part_list, 'part-group', {'type': 'stop', 'number': '2'})
    for pid, key, staff in part_ids:
        notes_beats = []
        start = 0.0
        for voicing in voicings:
            dur = voicing['duration']
            notes_beats.append((start, dur, voicing[key]))
            start += dur
        _render_part(root, pid, SATB_CLEFS, staff, notes_beats, tempo,
                     fifths=fifths)
    return root


def harmony_to_musicxml(voicings, title="SATB Arrangement", tempo=96,
                        layout='parts4', key=None):
    """
    Convert a SATB voicing sequence to a multi-part MusicXML string.

    Two layouts are available:

    - ``parts4`` (default): four separate parts — Soprano and Alto share the
      treble staff, Tenor and Bass the bass staff — braced in pairs. Each
      part is an independent voice.
    - ``grand``: a piano-style grand-staff reduction in two parts: Soprano &
      Alto chorded on the treble staff, Tenor & Bass on the bass staff, with
      unisons collapsed.

    Every part gets rests, barline ties, and measures that sum to exactly
    the time signature.

    Args:
        voicings: list of {'soprano', 'alto', 'tenor', 'bass' (MIDI pitches),
                  'duration' (beats)} dicts, as produced by generate_satb.
        title: score title.
        tempo: BPM (the metronome marking; durations are already in beats).
        layout: 'parts4' or 'grand'.
        key: key name like 'C major' or 'A minor' for the key signature.

    Returns:
        MusicXML string.
    """
    if layout not in HARMONY_LAYOUTS:
        raise ValueError(f'Unknown layout: {layout!r}')
    return _pretty(_harmony_xml(voicings, title, tempo, layout, key=key))


def _beats_to_type(beats):
    """Convert duration in beats to a MusicXML note type string."""
    if beats >= 4:
        return 'whole'
    if beats >= 2:
        return 'half'
    if beats >= 1:
        return 'quarter'
    if beats >= 0.5:
        return 'eighth'
    if beats >= 0.25:
        return '16th'
    return '32nd'


def notes_to_staff_text(notes, max_per_line=8):
    """
    Render notes as a simple text staff for browser display.

    Args:
        notes: list of note dicts.
        max_per_line: notes per line.

    Returns:
        List of strings, each a line of the staff.
    """
    if not notes:
        return ['(no notes detected)']

    lines = []
    for i in range(0, len(notes), max_per_line):
        chunk = notes[i:i + max_per_line]
        line = ' │ '.join(
            f"{n['pitch_name']:>4} ({n['duration']:.2f}s)"
            for n in chunk
        )
        lines.append(f"  {line}")

    return lines
