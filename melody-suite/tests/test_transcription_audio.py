"""
End-to-end transcription tests with real audio bytes.

The committed fixtures in tests/fixtures/ are actual 16-bit WAVs (the same
synthesis `/api/demo-audio` serves for the C-major one):

- demo_melody.wav    C major:      C4 E4 G4 C5 G4 E4 D4 C4 (fifths 0)
- e_minor_melody.wav E minor:      E3 G3 B3 E4 B3 G3 F#3 E3 (fifths 1)
- bb_major_melody.wav B-flat major: Bb3 D4 F4 Bb4 F4 D4 C4 Bb3 (fifths -2)

These tests push real bytes through the whole pipeline — upload parsing,
librosa pitch/beat/key detection, MIDI generation, and file serving —
instead of the mocked demo flow the jsdom tests exercise in the browser.
The minor and flat fixtures exist so key-signature rendering is exercised
against real audio in non-C keys in both directions: one sharp (E minor)
and two flats (B-flat major). The detector's own vocabulary is preserved
verbatim: B-flat audio is reported as 'A# major' with A#-spelled notes,
and 'A# major' maps to a two-flat signature (fifths -2).

The module skips cleanly when librosa isn't installed (mirroring the JS
wrapper's graceful skip), and tempo is asserted with a tolerance so a librosa
point release shifting beat tracking by a few BPM doesn't break the suite.
"""

import io
import pathlib
import re
import wave

import pytest

import app as appmod

pytest.importorskip('librosa')

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'
DEMO_FIXTURE = FIXTURES / 'demo_melody.wav'
MINOR_FIXTURE = FIXTURES / 'e_minor_melody.wav'
FLAT_FIXTURE = FIXTURES / 'bb_major_melody.wav'

EXPECTED_PITCHES = [60, 64, 67, 72, 67, 64, 62, 60]  # C4 E4 G4 C5 G4 E4 D4 C4
EXPECTED_MINOR_PITCHES = [52, 55, 59, 64, 59, 55, 54, 52]  # E3 G3 B3 E4 B3 G3 F#3 E3
EXPECTED_FLAT_PITCHES = [58, 62, 65, 70, 65, 62, 60, 58]  # Bb3 D4 F4 Bb4 F4 D4 C4 Bb3


@pytest.fixture
def client():
    return appmod.app.test_client()


@pytest.mark.parametrize('path', [DEMO_FIXTURE, MINOR_FIXTURE, FLAT_FIXTURE],
                         ids=['demo', 'e-minor', 'bb-major'])
def test_fixture_is_valid_wav(path):
    """Each committed binary is a real, undamaged WAV."""
    data = path.read_bytes()
    assert data[:4] == b'RIFF'
    with wave.open(str(path), 'rb') as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 22050
        assert w.getnframes() == 110250  # 5.0s


def test_demo_fixture_matches_demo_route(client):
    """Keep the C-major fixture in lockstep with /api/demo-audio.

    If the demo melody in app.py changes, this fails and the fixture must be
    regenerated: venv/bin/python tests/fixtures/generate_fixtures.py
    """
    r = client.get('/api/demo-audio')
    assert r.status_code == 200
    assert r.data == DEMO_FIXTURE.read_bytes()


def _transcribe(client, wav_bytes, filename='demo.wav'):
    """POST real audio bytes through the transcription endpoint."""
    r = client.post(
        '/api/transcribe',
        data={'audio': (io.BytesIO(wav_bytes), filename),
              'sensitivity': 'balanced'},
        content_type='multipart/form-data')
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _assert_melody(result, expected_pitches, expected_key):
    assert result['num_notes'] == len(expected_pitches)
    assert [n['pitch'] for n in result['notes']] == expected_pitches
    assert result['pitch_range'] == [min(expected_pitches), max(expected_pitches)]
    # 120 BPM source; librosa typically reports ~117. Tolerate tracking drift.
    assert 100 <= result['tempo'] <= 140
    assert result['key'] == expected_key
    assert result['duration'] == pytest.approx(5.0, abs=0.1)
    assert result['midi_url'].startswith('/output/trans_')


def _sheet_xml(client, result):
    """Render the transcribed notes+key through /api/sheet-music and return
    the generated MusicXML (the file the preview and download use)."""
    r = client.post('/api/sheet-music', json={
        'notes': result['notes'], 'tempo': result['tempo'],
        'key': result['key']})
    assert r.status_code == 200, r.get_json()
    return client.get(r.get_json()['xml_url']).get_data(as_text=True)


def _fifths_of(xml):
    return [int(f) for f in re.findall(r'<fifths>(-?\d+)</fifths>', xml)]


def test_demo_fixture_transcribes_end_to_end(client):
    result = _transcribe(client, DEMO_FIXTURE.read_bytes())
    _assert_melody(result, EXPECTED_PITCHES, 'C major')
    # C major → no accidentals in the rendered key signature.
    assert _fifths_of(_sheet_xml(client, result)) == [0]


def test_minor_fixture_transcribes_end_to_end(client):
    """Real audio in E minor: detected key renders one sharp (F#), not C major."""
    result = _transcribe(client, MINOR_FIXTURE.read_bytes(),
                         filename='e_minor.wav')
    _assert_melody(result, EXPECTED_MINOR_PITCHES, 'E minor')
    assert _fifths_of(_sheet_xml(client, result)) == [1]


def test_flat_fixture_transcribes_end_to_end(client):
    """Real audio in B-flat major: the detector's own vocabulary is kept
    verbatim — the key is reported as 'A# major' (its sharp naming) and the
    notes are spelled A#3, while the rendered signature still carries the
    two flats (-2) that 'A# major' maps to.
    """
    result = _transcribe(client, FLAT_FIXTURE.read_bytes(),
                         filename='bb_major.wav')
    _assert_melody(result, EXPECTED_FLAT_PITCHES, 'A# major')
    assert [n['pitch_name'] for n in result['notes']] == [
        'A#3', 'D4', 'F4', 'A#4', 'F4', 'D4', 'C4', 'A#3']
    xml = _sheet_xml(client, result)
    assert _fifths_of(xml) == [-2]
    # Notes keep the detector's sharp spelling (A# = step A, alter 1).
    assert '<alter>1</alter>' in xml
    assert '<alter>-1</alter>' not in xml


def test_demo_route_transcribes_end_to_end(client):
    """Server-side version of the browser demo flow, with real bytes."""
    r = client.get('/api/demo-audio')
    assert r.status_code == 200 and r.data[:4] == b'RIFF'
    _assert_melody(_transcribe(client, r.data), EXPECTED_PITCHES, 'C major')


def test_transcribe_midi_output_is_valid_smf(client):
    """The MIDI the endpoint generates is a real SMF and serves correctly."""
    result = _transcribe(client, DEMO_FIXTURE.read_bytes())
    r = client.get(result['midi_url'])
    assert r.status_code == 200
    assert r.data[:4] == b'MThd'  # SMF header magic
