"""
API validation tests for the Melody Suite endpoints.

Covers:
- melody generate/continue input clamping (count, bars, temperature)
- transcribe sensitivity/instrument validation
- custom pitch range → fmin/fmax forwarding
- upload size limit (413 with a JSON body)
- the _clamp helper

Engine functions are monkeypatched in most tests so the *endpoint contract*
is asserted precisely (clamps, defaults, error codes) without slow or
nondeterministic librosa/pretty_midi work.
"""

import io

import pytest

import app as appmod


@pytest.fixture
def client():
    return appmod.app.test_client()


# ── _clamp helper ──────────────────────────────────────────────────────────

def test_clamp_helper():
    assert appmod._clamp(0, 3, 1, 8, 'count') == 1
    assert appmod._clamp(999, 3, 1, 8, 'count') == 8
    assert appmod._clamp(None, 3, 1, 8, 'count') == 3
    assert appmod._clamp('4', 3, 1, 8, 'count') == 4
    assert appmod._clamp(-5, 4, 1, 32, 'bars') == 1
    assert appmod._clamp(5.0, 1.0, 0.1, 2.0, 'temperature', converter=float) == 2.0
    with pytest.raises(ValueError):
        appmod._clamp('abc', 3, 1, 8, 'count')


# ── melody generate/continue clamping ──────────────────────────────────────

@pytest.fixture
def fake_melody_engine(monkeypatch):
    """Replace the melody engine with a recorder so clamps are assertable."""
    captured = {}

    def fake_generate_candidates(prompt, mode='fresh', num_candidates=3,
                                 temperature=1.0, num_bars=4, seed_melody=None):
        captured.update(num_candidates=num_candidates, num_bars=num_bars,
                        temperature=temperature, seed_melody=seed_melody)
        return [
            {'notes': [{'pitch': 60, 'start': 0.0, 'duration': 1.0}],
             'tempo': 96, 'key': 'C major', 'mode': mode}
            for _ in range(num_candidates)
        ]

    monkeypatch.setattr(appmod, 'generate_candidates', fake_generate_candidates)
    monkeypatch.setattr(appmod, 'melody_to_midi', lambda *a, **k: None)
    monkeypatch.setattr(appmod, 'output_path', lambda *a, **k: '/tmp/fake.mid')
    monkeypatch.setattr(appmod, 'output_url', lambda *a, **k: '/output/fake.mid')
    return captured


def test_melody_count_clamped_to_min(fake_melody_engine, client):
    r = client.post('/api/melody/generate', json={'count': 0})
    assert r.status_code == 200
    assert len(r.get_json()['candidates']) == 1


def test_melody_count_clamped_to_max(fake_melody_engine, client):
    r = client.post('/api/melody/generate', json={'count': 999})
    assert r.status_code == 200
    assert len(r.get_json()['candidates']) == 8


def test_melody_bars_and_temperature_clamped(fake_melody_engine, client):
    r = client.post('/api/melody/generate',
                    json={'count': 1, 'bars': 200, 'temperature': 5.0})
    assert r.status_code == 200
    assert fake_melody_engine['num_bars'] == 32
    assert fake_melody_engine['temperature'] == 2.0


def test_melody_defaults(fake_melody_engine, client):
    r = client.post('/api/melody/generate', json={})
    assert r.status_code == 200
    assert fake_melody_engine['num_candidates'] == 3
    assert fake_melody_engine['num_bars'] == 4
    assert fake_melody_engine['temperature'] == 1.0


def test_melody_invalid_count_rejected(fake_melody_engine, client):
    r = client.post('/api/melody/generate', json={'count': 'abc'})
    assert r.status_code == 400
    assert 'Invalid count' in r.get_json()['error']


def test_melody_invalid_temperature_rejected(fake_melody_engine, client):
    r = client.post('/api/melody/generate', json={'temperature': 'hot'})
    assert r.status_code == 400
    assert 'Invalid temperature' in r.get_json()['error']


def test_melody_continue_requires_seed(fake_melody_engine, client):
    r = client.post('/api/melody/continue', json={})
    assert r.status_code == 400
    assert 'seed required' in r.get_json()['error']


def test_melody_continue_clamps_count(fake_melody_engine, client):
    seed = [{'pitch': 60, 'start': 0.0, 'duration': 1.0}]
    r = client.post('/api/melody/continue',
                    json={'seed_melody': seed, 'count': 0})
    assert r.status_code == 200
    assert len(r.get_json()['candidates']) == 1


def test_melody_generate_end_to_end(client):
    """Real engine path: default generate returns 3 candidates with MIDI."""
    r = client.post('/api/melody/generate', json={})
    assert r.status_code == 200
    cands = r.get_json()['candidates']
    assert len(cands) == 3
    assert all(c.get('midi_url', '').startswith('/output/melody_') for c in cands)
    assert all(c['notes'] for c in cands)


# ── transcribe validation + custom pitch range ─────────────────────────────

def _wav(name='test.wav', data=b'fake wav bytes'):
    # Fresh BytesIO per request — multipart encoding consumes/closes the stream.
    return (io.BytesIO(data), name)


@pytest.fixture
def fake_transcriber(monkeypatch):
    """Replace transcription with a recorder; endpoint logic is the target."""
    captured = {}

    def fake_transcribe_audio(file_path, fmin=None, fmax=None, threshold=0.6,
                              min_note_dur=0.08, instrument_profile='balanced'):
        captured.update(fmin=fmin, fmax=fmax, threshold=threshold,
                        instrument_profile=instrument_profile)
        return {'notes': [], 'tempo': 96, 'duration': 1.0, 'num_notes': 0,
                'pitch_range': (60, 60)}

    monkeypatch.setattr(appmod, 'transcribe_audio', fake_transcribe_audio)
    monkeypatch.setattr(appmod, 'notes_to_midi', lambda *a, **k: None)
    monkeypatch.setattr(appmod, 'output_path', lambda *a, **k: '/tmp/fake.mid')
    monkeypatch.setattr(appmod, 'output_url', lambda *a, **k: '/output/fake.mid')
    return captured


def test_transcribe_invalid_sensitivity(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'sensitivity': 'banana'},
                    content_type='multipart/form-data')
    assert r.status_code == 400
    assert 'Invalid sensitivity' in r.get_json()['error']


def test_transcribe_sensitivity_out_of_range(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'sensitivity': '2.5'},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def test_transcribe_unknown_instrument(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'instrument': 'weird'},
                    content_type='multipart/form-data')
    assert r.status_code == 400
    assert 'instrument' in r.get_json()['error'].lower()


def test_transcribe_sensitivity_mapping(fake_transcriber, client):
    # 'strict' maps to a 0.8 voicing threshold.
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'sensitivity': 'strict'},
                    content_type='multipart/form-data')
    assert r.status_code == 200
    assert fake_transcriber['threshold'] == 0.8


def test_transcribe_custom_pitch_range_forwarded(fake_transcriber, client):
    # C5 (MIDI 72) → 523.25 Hz, C6 (MIDI 84) → 1046.50 Hz.
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'lowest': '72', 'highest': '84'},
                    content_type='multipart/form-data')
    assert r.status_code == 200
    assert fake_transcriber['fmin'] == pytest.approx(523.251, rel=1e-3)
    assert fake_transcriber['fmax'] == pytest.approx(1046.502, rel=1e-3)


def test_transcribe_without_custom_range_leaves_engine_default(fake_transcriber, client):
    # No lowest/highest → endpoint passes None so the engine uses the profile.
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'instrument': 'vocal'},
                    content_type='multipart/form-data')
    assert r.status_code == 200
    assert fake_transcriber['fmin'] is None
    assert fake_transcriber['fmax'] is None


def test_transcribe_custom_range_rejects_swapped(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'lowest': '90', 'highest': '60'},
                    content_type='multipart/form-data')
    assert r.status_code == 400
    assert 'below highest' in r.get_json()['error']


def test_transcribe_custom_range_rejects_non_midi(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'lowest': '200', 'highest': '84'},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def test_transcribe_custom_range_rejects_garbage(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'lowest': 'abc', 'highest': '84'},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def test_transcribe_custom_range_requires_both(fake_transcriber, client):
    r = client.post('/api/transcribe',
                    data={'audio': _wav(), 'lowest': '48'},
                    content_type='multipart/form-data')
    assert r.status_code == 400


# ── harmony validation ────────────────────────────────────────────────────

def test_harmony_valid(client):
    r = client.post('/api/harmony', json={
        'notes': [{'pitch': 'C4', 'duration': 1}, {'pitch': 'E4', 'duration': 1}],
        'key': 'C major', 'style': 'balanced'})
    assert r.status_code == 200
    d = r.get_json()
    assert d['voice_names'] == ['soprano', 'alto', 'tenor', 'bass']
    assert d['voices'][0]['soprano'] == 60  # 'C4' normalized to MIDI int


def test_harmony_invalid_key(client):
    r = client.post('/api/harmony', json={'notes': [{'pitch': 'C4'}], 'key': 'X major'})
    assert r.status_code == 400
    assert 'Invalid key' in r.get_json()['error']


def test_harmony_invalid_style(client):
    r = client.post('/api/harmony', json={'notes': [{'pitch': 'C4'}], 'style': 'metal'})
    assert r.status_code == 400
    assert 'style' in r.get_json()['error'].lower()


def test_harmony_invalid_pitch(client):
    r = client.post('/api/harmony', json={'notes': [{'pitch': 'H4'}]})
    assert r.status_code == 400
    assert 'Invalid pitch' in r.get_json()['error']


def test_harmony_pitch_out_of_range(client):
    r = client.post('/api/harmony', json={'notes': [{'pitch': 300}]})
    assert r.status_code == 400


def test_harmony_notes_must_be_list(client):
    r = client.post('/api/harmony', json={'notes': 'C4, E4'})
    assert r.status_code == 400


def test_harmony_duration_clamped(client):
    r = client.post('/api/harmony', json={'notes': [{'pitch': 'C4', 'duration': 1000}]})
    assert r.status_code == 200
    assert r.get_json()['voices'][0]['duration'] == 32.0


def test_harmony_notes_count_clamped(client):
    notes = [{'pitch': 60, 'duration': 1}] * 600
    r = client.post('/api/harmony', json={'notes': notes})
    assert r.status_code == 200
    assert len(r.get_json()['voices']) == 512


def test_harmony_empty_uses_default_melody(client):
    r = client.post('/api/harmony', json={'notes': []})
    assert r.status_code == 200
    assert len(r.get_json()['voices']) == 8  # built-in demo melody


# ── harmony MusicXML layout rendering ───────────────────────────────────────

def _fetch(client, url):
    """Fetch a generated output file through the test client."""
    r = client.get(url)
    assert r.status_code == 200
    return r.get_data(as_text=True)


VOICES_SAMPLE = [
    {'soprano': 60, 'alto': 64, 'tenor': 55, 'bass': 48, 'duration': 1.0},
    {'soprano': 62, 'alto': 67, 'tenor': 57, 'bass': 50, 'duration': 1.0},
]


def test_harmony_xml_renders_default_parts4(client):
    r = client.post('/api/harmony/xml', json={'voices': VOICES_SAMPLE})
    assert r.status_code == 200
    xml = _fetch(client, r.get_json()['xml_url'])
    assert xml.count('<part id="P1">') == 1 and xml.count('<part id="P4">') == 1


def test_harmony_xml_grand_layout(client):
    r = client.post('/api/harmony/xml', json={'voices': VOICES_SAMPLE, 'layout': 'grand'})
    assert r.status_code == 200
    xml = _fetch(client, r.get_json()['xml_url'])
    assert xml.count('<part id="P1">') == 1 and xml.count('<part id="P2">') == 1
    assert '<part id="P3">' not in xml


def test_harmony_xml_bad_layout(client):
    r = client.post('/api/harmony/xml', json={'voices': VOICES_SAMPLE, 'layout': 'jazz'})
    assert r.status_code == 400
    assert 'layout' in r.get_json()['error'].lower()


def test_harmony_xml_missing_voices(client):
    r = client.post('/api/harmony/xml', json={'layout': 'grand'})
    assert r.status_code == 400
    assert 'voices' in r.get_json()['error'].lower()


def test_harmony_xml_malformed_voice(client):
    r = client.post('/api/harmony/xml', json={'voices': [{'soprano': 60}]})
    assert r.status_code == 400


def test_harmony_xml_honors_key(client):
    r = client.post('/api/harmony/xml',
                    json={'voices': VOICES_SAMPLE, 'key': 'G major'})
    assert r.status_code == 200
    xml = _fetch(client, r.get_json()['xml_url'])
    assert '<fifths>1</fifths>' in xml
    # Default stays C major.
    r = client.post('/api/harmony/xml', json={'voices': VOICES_SAMPLE})
    xml = _fetch(client, r.get_json()['xml_url'])
    assert '<fifths>0</fifths>' in xml


# ── sheet-music (transcription export) key signature ───────────────────────

def test_sheet_music_honors_key(client):
    notes = [{'pitch': 60, 'start': 0.0, 'duration': 0.5},
             {'pitch': 64, 'start': 0.5, 'duration': 0.5}]
    r = client.post('/api/sheet-music', json={'notes': notes, 'key': 'E minor'})
    assert r.status_code == 200
    xml = _fetch(client, r.get_json()['xml_url'])
    assert '<fifths>1</fifths>' in xml
    # No key → C major.
    r = client.post('/api/sheet-music', json={'notes': notes})
    xml = _fetch(client, r.get_json()['xml_url'])
    assert '<fifths>0</fifths>' in xml


def test_sheet_music_rejects_invalid_notes(client):
    """Malformed note payloads return 400, not a raw 500 (regression guard
    for the notes validation added to the endpoint)."""
    bad = [
        {'notes': [{'duration': 1.0}]},                  # missing pitch
        {'notes': [{'pitch': 'ZZ9', 'duration': 1.0}]},  # invalid pitch name
        {'notes': [{'pitch': 999, 'duration': 1.0}]},    # MIDI out of range
        {'notes': [{'pitch': 60, 'duration': 'x'}]},     # non-numeric duration
        {'notes': [{'pitch': 60, 'duration': 1.0}], 'tempo': 'abc'},  # bad tempo
        {'notes': 'not-a-list'},                         # wrong top-level type
    ]
    for payload in bad:
        r = client.post('/api/sheet-music', json=payload)
        assert r.status_code == 400, f'{payload} → {r.status_code}'


def test_sheet_music_clamps_tempo(client):
    """Out-of-range tempo is clamped instead of crashing the renderer."""
    notes = [{'pitch': 60, 'start': 0.0, 'duration': 0.5}]
    for tempo in (0, 1, 9999):
        r = client.post('/api/sheet-music',
                        json={'notes': notes, 'tempo': tempo})
        assert r.status_code == 200, f'tempo={tempo} → {r.status_code}'


# ── editor-export validation ───────────────────────────────────────────────


# ── editor-export validation ───────────────────────────────────────────────

def test_editor_export_valid(client):
    r = client.post('/api/editor-export', json={
        'notes': [{'pitch': 60, 'start': 0.0, 'duration': 0.5}], 'tempo': 120})
    assert r.status_code == 200
    assert r.get_json()['midi_url'].startswith('/output/editor_')


def test_editor_export_requires_notes(client):
    r = client.post('/api/editor-export', json={})
    assert r.status_code == 400


def test_editor_export_empty_notes_rejected(client):
    r = client.post('/api/editor-export', json={'notes': []})
    assert r.status_code == 400


def test_editor_export_invalid_tempo(client):
    r = client.post('/api/editor-export',
                    json={'notes': [{'pitch': 60}], 'tempo': 'abc'})
    assert r.status_code == 400
    assert 'Invalid tempo' in r.get_json()['error']


def test_editor_export_tempo_clamped(client):
    r = client.post('/api/editor-export',
                    json={'notes': [{'pitch': 60}], 'tempo': 9999})
    assert r.status_code == 200


def test_editor_export_invalid_pitch(client):
    r = client.post('/api/editor-export', json={'notes': [{'pitch': 'H4'}]})
    assert r.status_code == 400


def test_editor_export_negative_duration_clamped(client):
    r = client.post('/api/editor-export',
                    json={'notes': [{'pitch': 60, 'duration': -5}]})
    assert r.status_code == 200


# ── upload size limit ──────────────────────────────────────────────────────

def test_upload_limit_returns_json_413(client, monkeypatch):
    monkeypatch.setitem(appmod.app.config, 'MAX_CONTENT_LENGTH', 1024)
    big = io.BytesIO(b'x' * 2048)
    r = client.post('/api/transcribe', data={'audio': (big, 'big.wav')},
                    content_type='multipart/form-data')
    assert r.status_code == 413
    assert 'too large' in r.get_json()['error'].lower()


def test_upload_within_limit_still_processed(fake_transcriber, client, monkeypatch):
    monkeypatch.setitem(appmod.app.config, 'MAX_CONTENT_LENGTH', 1024)
    small = io.BytesIO(b'x' * 512)
    r = client.post('/api/transcribe', data={'audio': (small, 'small.wav')},
                    content_type='multipart/form-data')
    assert r.status_code == 200
