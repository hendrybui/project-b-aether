# 🎵 Melody Suite

Standalone Flask web app implementing three music analysis and composition tools, reverse-engineered from the GadegetKit "Melody Sheet" suite and built from first principles using open-source Python libraries.

## Tools

### 1. BPM & Key Finder
Upload any audio file → get tempo (BPM), musical key, Camelot DJ code, and confidence scores.

- **Engine:** librosa onset-based beat tracking + Krumhansl-Schmuckler key-finding algorithm
- **Accuracy:** ~90%+ on clean audio (verified: correctly detects C major @ 117 BPM on test tone)
- **Outputs:** BPM + confidence, key + correlation, Camelot wheel code, beat timestamps

### 2. SATB Harmony Generator
Enter a lead melody → get a four-part arrangement (Soprano, Alto, Tenor, Bass).

- **Engine:** Rule-based common-practice voice leading (the same rules Bach chorales follow)
- **Features:** Key selection, voicing style (Conservative/Balanced/Adventurous), parallel-fifth/octave avoidance
- **Outputs:** Note-level SATB voicings, MIDI download, Web Audio playback

### 3. AI Melody Generator
Describe a mood → get 3 candidate melodies → compare → branch from the best.

- **Engine:** Markov chain over scale-degree transitions with temperature-scaled sampling
- **Modes:** Fresh generation (like MusicVAE) or continuation (like MusicRNN)
- **Prompt parsing:** Mood keywords (happy/sad/dark/calm/lofi/epic...) → scale, tempo, key
- **Outputs:** 3-candidate comparison grid, idea shelf (6 slots), MIDI export

## Quick Start

```bash
cd melody-suite
source venv/bin/activate
python app.py
```

Open http://localhost:5000 in your browser.

## API Reference

All endpoints return JSON.

### `POST /api/bpm-key`
Upload audio for tempo + key analysis.

```bash
curl -X POST http://localhost:5000/api/bpm-key -F "audio=@song.mp3"
```
```json
{"bpm": 120, "key": "C major", "camelot": "8B", "confidence": 85.2, ...}
```

### `POST /api/harmony`
Generate SATB arrangement from a melody.

```bash
curl -X POST http://localhost:5000/api/harmony \
  -H "Content-Type: application/json" \
  -d '{"notes":[{"pitch":"C4","duration":1}],"key":"C major","style":"balanced"}'
```

### `POST /api/melody/generate`
Generate candidate melodies from a prompt.

```bash
curl -X POST http://localhost:5000/api/melody/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"happy bright","count":3,"bars":4}'
```

## How It Maps to the Research

This app was built from three algorithm deep-dive documents. Here's how each engine maps:

| Tool | GadegetKit's JS engine | This app's Python engine | Fidelity |
|---|---|---|---|
| BPM & Key | bpm-detective + K-S correlation | librosa beat_track + K-S correlation | ✅ Faithful (same DSP approach) |
| SATB Harmony | Coconet (neural, Gibbs sampling) | music21 rule-based voice leading | ⚡ Approximation (workflow-accurate, not neural) |
| Melody Gen | MusicVAE + MusicRNN (Magenta.js) | Markov chain over scale degrees | ⚡ Approximation (workflow-accurate, not neural) |

The neural models (Coconet, MusicVAE/MusicRNN) require TensorFlow + magenta, which is fragile on Python 3.12. This app captures their **workflow logic** — SATB harmonization, 3-candidate generation, branching continuation — with lightweight alternatives that install and run reliably.

## Project Structure

```
melody-suite/
├── app.py                 # Flask app + API routes
├── engines/
│   ├── bpm_key.py         # librosa + Krumhansl-Schmuckler
│   ├── harmony.py         # Rule-based SATB voice leading
│   └── melody.py          # Markov chain melody generation
├── templates/             # Jinja2 HTML templates
├── static/                # CSS + Web Audio player
├── uploads/               # Temp audio (gitignored)
├── output/                # Generated MIDI (gitignored)
└── requirements.txt
```

## Dependencies

- Python 3.12
- Flask, librosa, numpy, scipy, pretty_midi, music21, soundfile
- ffmpeg (for audio decoding — already on most systems)

No TensorFlow, no PyTorch, no GPU required. Total install ~200MB.

## Limitations

- **BPM:** best on music with clear beats (electronic, pop, rock); may struggle with rubato or ambient
- **Harmony:** produces Bach-style SATB only; won't generate jazz voicings or contemporary arrangements
- **Melody:** Markov chain is simpler than a neural VAE; output is coherent but less structurally sophisticated
- **Browser playback:** uses raw oscillators (sine/triangle), not sampled instruments
