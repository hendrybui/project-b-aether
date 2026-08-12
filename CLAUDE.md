# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Aether** is an AI-assisted web synthesizer and step sequencer built with Vite, TypeScript, and Tone.js. It generates music with AI tools and exports audio for editing in AudioMass (a multitrack waveform editor) — together forming a complete local "generate → edit" music production environment.

## Development Commands

```bash
# Development
npm run dev          # Start Vite dev server on port 5173

# Build
npm run build       # TypeScript compile + Vite build to dist/
npm run preview     # Preview the built dist/ locally

# Run Aether + AudioMass together (recommended)
./run-aether-with-audiomass.sh start    # Start both apps + Caddy proxy
./run-aether-with-audiomass.sh stop     # Stop all services
./run-aether-with-audiomass.sh status   # Check running status
```

The unified launcher starts:
- Aether dev server on :5173 (proxied to `/aether`)
- AudioMass editor on :5055 (proxied to `/mass`)
- Caddy reverse proxy on :80 (requires one-time setup)

## Architecture

### Lazy Audio Graph Construction

**Critical**: Aether uses lazy initialization for all Tone.js audio nodes to comply with browser autoplay policies. No audio nodes are created in constructors — they are built on first user gesture via `ensureGraph()`.

The flow:
1. `AetherSynth` constructor is lightweight (no Tone nodes)
2. First user interaction → `Tone.start()` → `ensureGraph()` builds FX chain, LFO, and routing
3. `DrumKit` voices are created on-trigger (one-shot synthesis)

Files:
- `src/audio/engine.ts` — Main synth with lazy graph (`ensureGraph()`)
- `src/audio/drumKit.ts` — One-shot drum voices
- `src/sequencer/stepSequencer.ts` — Pattern data, scheduling, AI generators

### Module Structure

```
src/
├── audio/
│   ├── engine.ts          # AetherSynth class (oscillators, filters, FX, unison)
│   └── drumKit.ts         # DrumKit class (5 voices: kick, snare, hats, perc)
├── sequencer/
│   └── stepSequencer.ts   # StepSequencer class (grid, scheduling, AI patterns)
├── ai/
│   ├── ollama.ts          # Local LLM integration (Ollama API at localhost:11434)
│   ├── patchGenerator.ts  # Local "prompt → patch" keyword matcher
│   └── melodyGenerator.ts # Melody/chord progression generators
├── ui/
│   ├── knob.ts            # Rotary knob component
│   └── keyboard.ts        # On-screen piano + QWERTY input
└── main.ts                # Main entry point (wires all UI + audio)
```

### AI Features

**Local Generators** (no LLM required):
- `promptToPatch()` in `src/ai/patchGenerator.ts` — Keyword-based parameter mapping (warm, bright, acid, etc.)
- `mutatePatch()` — Small musical variations
- `surprisePatch()` — Random interesting presets
- `generateMelody()` / `generateChordProgression()` — Scale-aware sequences
- `generateDrumPattern()` / `applyEuclidean()` — Rhythm generators

**Ollama Integration** (optional):
- `describeToPatchWithOllama()` — Natural language → synth parameters
- `generateAudioMassIdeaWithOllama()` — Variation ideas for AudioMass workflow

Calls `http://localhost:11434/api/chat` directly with model `llama3.1:8b` by default.

## Key Patterns

### Synth Parameters

All synth parameters use `SynthParams` interface (`src/audio/engine.ts`). Values are typically 0-1 normalized and mapped to audio ranges internally:

```typescript
export interface SynthParams {
  // Oscillators
  osc1Wave: number;      // 0-3 index into [sine, sawtooth, square, triangle]
  osc1Level: number;     // 0-1
  // ... (see full interface)
}
```

When adding new parameters:
1. Add to `SynthParams` interface
2. Add default to `defaultParams` object
3. Map in `applyParams()` / `updateVoiceFromParams()`
4. Create UI knob in `main.ts` via `attachKnobs()`

### Step Sequencer

The sequencer uses a callback pattern: `setOnStep((trackId, velocity, stepIndex, time) => {...})`. Audio engines (synth, drums) are called from this callback at precisely scheduled times.

Sequencer tracks:
- 5 drum tracks (kick, snare, closedhat, openhat, perc)
- 1 synth track (melodic bass/stabs)

### AudioMass Handoff

The bounce feature (`bounceCurrentPattern()`) renders the sequencer pattern as WAV for AudioMass:
- Records one full cycle + tail
- Applies temporary mutes for stem separation
- Exports as 16-bit PCM WAV (`aether-*-to-audiomass.wav`)
- Uses pure-JS encoder (`encodeAudioBufferToWav()`)

## Web MIDI & Keyboard Input

- QWERTY keyboard rows map to piano keys (via `PianoKeyboard`)
- Web MIDI input supported (first device, CC64 for sustain)
- Space bar toggles sustain pedal
- Escape key = panic (all notes off)

## Common Tasks

**Adding a new synth parameter:**
1. Add to `SynthParams` interface
2. Add to `defaultParams`
3. Wire in `applyParams()` + `updateVoiceFromParams()`
4. Create UI knob with `data-param="paramName"` in HTML

**Adding a drum pattern generator:**
1. Add method to `StepSequencer` class
2. Wire button in `main.ts` `setupDrumsAndSequencer()`

**Running tests for LLM features:**
```bash
# Ensure Ollama is running
curl http://localhost:11434/api/tags
```

## Integration Notes

Aether integrates with AudioMass via:
1. **WAV export** — Bounce full mix or stems to `exports/`
2. **Load back** — File input can preview AudioMass exports or use as noise boost
3. **LLM bridge** — `generateAudioMassIdeaWithOllama()` suggests edits for AudioMass projects

Shared folders (`exports/`, `samples/`) are auto-created by the launcher script.
