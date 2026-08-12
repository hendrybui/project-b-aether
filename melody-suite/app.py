"""
Melody Suite — Flask Application (10 tools, all functional)
Run: .venv/bin/python app.py
"""

import io
import os
import time
import wave
from datetime import datetime

import numpy as np
from flask import Flask, request, jsonify, render_template, send_from_directory, session, Response

from engines.bpm_key import analyze_audio
from engines.harmony import generate_satb, _note_to_pitch, NOTE_NAMES
from engines.melody import generate_candidates, melody_to_midi
from engines.transcription import transcribe_audio, notes_to_midi
from engines.sheet_render import (HARMONY_LAYOUTS, harmony_to_musicxml,
                                   notes_to_musicxml)
from engines.paths import (
    UPLOAD_DIR, OUTPUT_DIR, upload_path, output_path, output_url,
    save_bytes, save_text, is_safe_output_name, file_exists_url,
)
from engines.url_fetch import fetch_audio

# Chromatic note options for the pitch-range dropdowns (C1–E7).
_NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
NOTE_OPTIONS = [{'midi': (oct+1)*12 + i, 'name': f"{_NOTE_NAMES[i]}{oct}"}
                for oct in range(1, 8) for i in range(12) if (oct+1)*12 + i <= 100][:84]

app = Flask(__name__)

# Persistent session secret: the cookie-signed bpm-history (and any future
# per-session state) survives server restarts instead of being invalidated
# by a fresh random key on every launch.
_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
def _load_or_create_secret():
    try:
        with open(_SECRET_FILE, 'rb') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = os.urandom(24)
    try:
        with open(_SECRET_FILE, 'wb') as f:
            f.write(key)
    except OSError:
        pass  # read-only checkout: fall back to an ephemeral key
    return key

app.secret_key = _load_or_create_secret()
SAFE_EXT = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'}
INSTRUMENT_PROFILES = {'balanced', 'piano', 'guitar', 'vocal'}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB, matching the UI's stated limit
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

# Generated exports (MIDI/MusicXML) accumulate forever otherwise; prune
# anything older than a week at startup.
_OUTPUT_TTL_DAYS = 7


def _prune_stale_outputs(max_age_days=_OUTPUT_TTL_DAYS):
    """Delete generated export files older than max_age_days from output/."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for name in os.listdir(OUTPUT_DIR):
            if is_safe_output_name(name) is None:
                continue
            path = os.path.join(OUTPUT_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    if removed:
        print(f'🧹 pruned {removed} stale export file(s) from output/')
    return removed


# ── Pages ───────────────────────────────────────────────────────────────────

@app.route('/')
def hub():
    return render_template('hub.html', active_tool='hub')

@app.route('/tools/melody-sheet')
def analyzer():
    return render_template('analyzer.html', active_tool='analyzer', note_options=NOTE_OPTIONS)

@app.route('/tools/melody-sheet/bpm-and-key-find')
def bpm_key():
    return render_template('bpm_key.html', active_tool='bpm_key')

@app.route('/tools/melody-sheet/ai-melody-generator')
def melody_gen():
    return render_template('melody.html', active_tool='melody_gen')

@app.route('/tools/melody-sheet/mp3-to-midi')
def mp3_to_midi():
    return render_template('mp3_to_midi.html', active_tool='mp3_to_midi', note_options=NOTE_OPTIONS)

@app.route('/tools/melody-sheet/mp3-to-sheet')
def mp3_to_sheet():
    return render_template('mp3_to_sheet.html', active_tool='mp3_to_sheet', note_options=NOTE_OPTIONS)

@app.route('/tools/melody-sheet/multi-part-harmony-generation')
def harmony():
    return render_template('harmony.html', active_tool='harmony')

@app.route('/tools/melody-sheet/mobile-support-live-recording')
def mobile_recording():
    return render_template('mobile_recording.html', active_tool='mobile_recording')

@app.route('/tools/melody-sheet/interactive-sheet-music-editor-playback')
def interactive_editor():
    return render_template('interactive_editor.html', active_tool='interactive_editor')

@app.route('/tools/melody-sheet/melody-studio')
def melody_studio():
    return render_template('melody_studio.html', active_tool='melody_studio')

@app.route('/tools/melody-sheet/audio-to-sheet')
def audio_sheet_studio():
    return render_template('audio_sheet_studio.html', active_tool='audio_sheet_studio')


# ── Upload helper ───────────────────────────────────────────────────────────

def _handle_upload():
    if 'audio' not in request.files:
        return None, 'No audio file uploaded'
    f = request.files['audio']
    if not f.filename:
        return None, 'Empty filename'
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in SAFE_EXT:
        return None, f'Unsupported: {ext}'
    path = upload_path(ext)
    f.save(path)
    return path, f.filename


def _clamp(value, default, lo, hi, name, converter=int):
    """Coerce an API input to an int/float within [lo, hi].

    Raises ValueError (→ 400) for values that aren't numeric.
    """
    if value is None:
        value = default
    try:
        v = converter(value)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid {name}: {value!r}') from None
    return max(lo, min(hi, v))


def _validate_notes(notes, max_notes=512, include_start=False):
    """Validate/normalize a client note list.

    - requires a list
    - truncates to max_notes
    - normalizes pitch to a MIDI int in 0-127 (accepts note names like 'C4')
    - clamps duration to 0.01-32 (beats or seconds, matching each caller)
    - optionally keeps start (>= 0)

    Raises ValueError (→ 400) for structurally invalid notes.
    """
    if not isinstance(notes, list):
        raise ValueError('notes must be a list')
    cleaned = []
    for n in notes[:max_notes]:
        if not isinstance(n, dict):
            raise ValueError(f'Invalid note: {n!r}')
        pitch = n.get('pitch', n.get('note'))
        if isinstance(pitch, str):
            s = pitch.strip()
            if not s or s[0].upper() not in NOTE_NAMES:
                raise ValueError(f'Invalid pitch: {pitch!r}')
            try:
                pitch = _note_to_pitch(pitch)
            except (ValueError, IndexError, TypeError):
                raise ValueError(f'Invalid pitch: {pitch!r}') from None
        elif isinstance(pitch, bool) or not isinstance(pitch, (int, float)):
            raise ValueError(f'Invalid pitch: {pitch!r}')
        pitch = int(pitch)
        if not 0 <= pitch <= 127:
            raise ValueError(f'Pitch out of range 0-127: {pitch}')
        duration = _clamp(n.get('duration', 1.0), 1.0, 0.01, 32.0, 'duration', converter=float)
        out = {'pitch': pitch, 'duration': duration}
        if include_start:
            out['start'] = _clamp(n.get('start', 0.0), 0.0, 0.0, 3600.0, 'start', converter=float)
        cleaned.append(out)
    return cleaned


def _validate_key(key):
    """Normalize a key string like 'C major' / 'A# minor'. Raises ValueError."""
    parts = (key or '').split()
    if len(parts) != 2 or parts[0] not in NOTE_NAMES or parts[1].lower() not in ('major', 'minor'):
        raise ValueError(f'Invalid key: {key!r}')
    return f'{parts[0]} {parts[1].lower()}'


@app.errorhandler(413)
def _payload_too_large(e):
    return jsonify({'error': 'File too large (max 100MB)'}), 413


# ── API: BPM & Key ──────────────────────────────────────────────────────────

@app.route('/api/bpm-key', methods=['POST'])
def api_bpm_key():
    path, info = _handle_upload()
    if path is None:
        return jsonify({'error': info}), 400
    try:
        result = analyze_audio(path)
        hist = session.get('bpm_history', [])
        hist.insert(0, {
            'filename': info, 'bpm': result['bpm'], 'key': result['key'],
            'camelot': result['camelot'], 'confidence': result['key_confidence'],
            'timestamp': datetime.now().strftime('%H:%M:%S'),
        })
        session['bpm_history'] = hist[:10]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        _cleanup(path)


def _cleanup(path):
    """Remove a temp file if it exists."""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


@app.route('/api/bpm-key/history')
def api_bpm_history():
    return jsonify({'history': session.get('bpm_history', [])})


@app.route('/api/bpm-key-url', methods=['POST'])
def api_bpm_key_url():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    state = {}

    def _save(data_bytes, ext):
        state['path'] = upload_path(ext)
        with open(state['path'], 'wb') as f:
            f.write(data_bytes)

    try:
        fetch_audio(url, _save)
        path = state.get('path')
        if not path:
            return jsonify({'error': 'Could not download audio'}), 400
        result = analyze_audio(path)
        hist = session.get('bpm_history', [])
        hist.insert(0, {
            'filename': url.rsplit('/', 1)[-1][:60], 'bpm': result['bpm'],
            'key': result['key'], 'camelot': result['camelot'],
            'confidence': result['key_confidence'],
            'timestamp': datetime.now().strftime('%H:%M:%S'),
        })
        session['bpm_history'] = hist[:10]
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        _cleanup(state.get('path'))


# ── Demo audio (for the "Load interactive demo" buttons) ───────────────────

_DEMO_MELODY = [60, 64, 67, 72, 67, 64, 62, 60]  # C4 E4 G4 C5 G4 E4 D4 C4


def _demo_wav_bytes(sr=22050, bpm=120):
    """Synthesize a short monophonic C-major melody as 16-bit WAV bytes."""
    beat = 60.0 / bpm
    total = int(sr * beat * len(_DEMO_MELODY)) + sr  # +1s silence tail
    audio = np.zeros(total)
    t = 0.0
    for midi in _DEMO_MELODY:
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


@app.route('/api/demo-audio')
def api_demo_audio():
    resp = Response(_demo_wav_bytes(), mimetype='audio/wav')
    resp.headers['Content-Disposition'] = 'attachment; filename=demo.wav'
    return resp


# ── API: Transcription ──────────────────────────────────────────────────────

@app.route('/api/transcribe', methods=['POST'])
def api_transcribe():
    path, info = _handle_upload()
    if path is None:
        return jsonify({'error': info}), 400
    instrument = request.form.get('instrument', 'balanced')
    # Map string sensitivity to numeric threshold.
    sens_map = {'relaxed': '0.4', 'balanced': '0.6', 'strict': '0.8'}
    sens_str = request.form.get('sensitivity', '0.6')
    try:
        try:
            sensitivity = float(sens_map.get(sens_str, sens_str))
        except (TypeError, ValueError):
            raise ValueError(f'Invalid sensitivity: {sens_str!r}') from None
        if not 0.0 <= sensitivity <= 1.0:
            raise ValueError(f'Sensitivity must be between 0 and 1, got {sensitivity}')
        if instrument not in INSTRUMENT_PROFILES:
            raise ValueError(f'Unknown instrument profile: {instrument!r}')
        # Custom pitch range: the Analyzer sends MIDI note numbers for the
        # lowest/highest expected note when "Custom pitch range" is enabled.
        # Convert to Hz and let them override the instrument profile's range.
        fmin = fmax = None
        lowest = request.form.get('lowest')
        highest = request.form.get('highest')
        if lowest is not None or highest is not None:
            try:
                lo_midi = int(lowest)
                hi_midi = int(highest)
            except (TypeError, ValueError):
                raise ValueError(
                    f'Invalid custom pitch range: lowest={lowest!r}, highest={highest!r}') from None
            if not (0 <= lo_midi <= 127 and 0 <= hi_midi <= 127):
                raise ValueError(f'Pitch range must be MIDI notes 0-127, got {lo_midi}-{hi_midi}')
            if lo_midi >= hi_midi:
                raise ValueError(f'Lowest note must be below highest, got {lo_midi}-{hi_midi}')
            fmin = 440.0 * 2 ** ((lo_midi - 69) / 12.0)
            fmax = 440.0 * 2 ** ((hi_midi - 69) / 12.0)
        result = transcribe_audio(
            path, threshold=sensitivity, instrument_profile=instrument,
            fmin=fmin, fmax=fmax,
        )
        mpath = output_path('trans_', '.mid')
        notes_to_midi(result['notes'], tempo=result['tempo'], filename=mpath)
        result['midi_url'] = output_url(mpath)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        _cleanup(path)


@app.route('/api/sheet-music', methods=['POST'])
def api_sheet_music():
    data = request.get_json()
    if not data or 'notes' not in data:
        return jsonify({'error': 'No notes'}), 400
    try:
        notes = _validate_notes(data['notes'], max_notes=2048, include_start=True)
        if not notes:
            raise ValueError('notes must not be empty')
        tempo = _clamp(data.get('tempo', 96), 96, 40, 240, 'tempo', converter=float)
        xml_str = notes_to_musicxml(notes, tempo=tempo, key=data.get('key'))
        url = save_text('sheet_', '.xml', xml_str)
        return jsonify({'xml_url': url})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── API: Harmony ────────────────────────────────────────────────────────────

@app.route('/api/harmony', methods=['POST'])
def api_harmony():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON'}), 400
    notes = data.get('notes', [])
    if not notes:
        notes = [
            {'pitch': 'C4', 'duration': 1}, {'pitch': 'E4', 'duration': 1},
            {'pitch': 'G4', 'duration': 1}, {'pitch': 'C5', 'duration': 1},
            {'pitch': 'G4', 'duration': 1}, {'pitch': 'E4', 'duration': 1},
            {'pitch': 'D4', 'duration': 1}, {'pitch': 'C4', 'duration': 2},
        ]
    try:
        notes = _validate_notes(notes, max_notes=512)
        key = _validate_key(data.get('key', 'C major'))
        style = data.get('style', 'balanced')
        if style not in ('conservative', 'balanced', 'adventurous'):
            raise ValueError(f'Unknown style: {style!r}')
        result = generate_satb(notes, key=key, style=style)
        mb = result.get('midi_data')
        if mb:
            result['midi_url'] = save_bytes('harmony_', '.mid', mb)
        result.pop('midi_data', None)
        if result.get('voices'):
            xml_str = harmony_to_musicxml(result['voices'], tempo=96, key=key)
            result['xml_url'] = save_text('harmony_', '.xml', xml_str)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/harmony/xml', methods=['POST'])
def api_harmony_xml():
    """Render an existing voicing sequence to MusicXML in a chosen layout.

    Unlike /api/harmony this does not regenerate the arrangement, so the
    page can switch export layouts without changing the music.
    """
    data = request.get_json()
    voices = (data or {}).get('voices')
    if not isinstance(voices, list) or not voices:
        return jsonify({'error': 'No voices'}), 400
    layout = data.get('layout', 'parts4')
    if layout not in HARMONY_LAYOUTS:
        return jsonify({'error': f'Unknown layout: {layout!r}'}), 400
    try:
        for v in voices:
            if not isinstance(v, dict) or not all(k in v for k in ('soprano', 'alto', 'tenor', 'bass', 'duration')):
                raise ValueError('Each voice needs soprano/alto/tenor/bass pitches and a duration')
        xml_str = harmony_to_musicxml(voices, tempo=data.get('tempo', 96),
                                      layout=layout, key=data.get('key'))
        return jsonify({'xml_url': save_text('harmony_', '.xml', xml_str)})
    except (ValueError, TypeError, KeyError) as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── API: Melody Generator ───────────────────────────────────────────────────

@app.route('/api/melody/generate', methods=['POST'])
def api_melody_generate():
    d = request.get_json() or {}
    try:
        num_candidates = _clamp(d.get('count', 3), 3, 1, 8, 'count')
        num_bars = _clamp(d.get('bars', 4), 4, 1, 32, 'bars')
        temperature = _clamp(d.get('temperature', 1.0), 1.0, 0.1, 2.0, 'temperature', converter=float)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        cands = generate_candidates(
            prompt=d.get('prompt', 'happy'), mode=d.get('mode', 'fresh'),
            num_candidates=num_candidates, temperature=temperature,
            num_bars=num_bars, seed_melody=d.get('seed_melody'),
        )
        for c in cands:
            mpath = output_path('melody_', '.mid')
            melody_to_midi(c, filename=mpath)
            c['midi_url'] = output_url(mpath)
        return jsonify({'candidates': cands})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melody/continue', methods=['POST'])
def api_melody_continue():
    d = request.get_json() or {}
    if not d.get('seed_melody'):
        return jsonify({'error': 'seed required'}), 400
    try:
        num_candidates = _clamp(d.get('count', 3), 3, 1, 8, 'count')
        num_bars = _clamp(d.get('bars', 4), 4, 1, 32, 'bars')
        temperature = _clamp(d.get('temperature', 1.0), 1.0, 0.1, 2.0, 'temperature', converter=float)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        cands = generate_candidates(
            prompt=d.get('prompt', 'continue'), mode='continue',
            num_candidates=num_candidates, temperature=temperature,
            num_bars=num_bars, seed_melody=d['seed_melody'],
        )
        for c in cands:
            mpath = output_path('melody_', '.mid')
            melody_to_midi(c, filename=mpath)
            c['midi_url'] = output_url(mpath)
        return jsonify({'candidates': cands})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── API: Editor export ──────────────────────────────────────────────────────

@app.route('/api/editor-export', methods=['POST'])
def api_editor_export():
    d = request.get_json()
    if not d or 'notes' not in d:
        return jsonify({'error': 'No notes'}), 400
    try:
        tempo = _clamp(d.get('tempo', 120), 120, 40, 240, 'tempo', converter=float)
        notes = _validate_notes(d['notes'], max_notes=2048, include_start=True)
        if not notes:
            raise ValueError('notes must not be empty')
        mpath = output_path('editor_', '.mid')
        notes_to_midi(notes, tempo=tempo, filename=mpath)
        return jsonify({'midi_url': output_url(mpath)})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── File serving ────────────────────────────────────────────────────────────

@app.route('/output/<filename>')
def serve_output(filename):
    safe_name = is_safe_output_name(filename)
    if safe_name is None:
        return jsonify({'error': 'Invalid filename'}), 403
    full_url = '/output/' + safe_name
    if not file_exists_url(full_url):
        return jsonify({'error': 'File not found'}), 404
    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=False)


if __name__ == '__main__':
    _prune_stale_outputs()
    port = int(os.environ.get('MELODY_SUITE_PORT', '5000'))
    print(f"🎵 Melody Suite — http://localhost:{port} — 10 tools")
    # threaded=True: a long librosa analysis must not block the hub, other
    # tools, or static assets for the whole suite while it runs.
    app.run(debug=False, threaded=True, host='127.0.0.1', port=port)
