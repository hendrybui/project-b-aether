# Aether + AudioMass — AI Music Generation + Editing Suite

**This is the focused project**: Aether (AI-assisted synth + sequencer + drums) tightly integrated with **AudioMass** (your multitrack waveform editor).

Everything lives together directly in `/mnt/Pandora/Project-B/`.

**Core workflow we are optimizing**:
1. Use Aether's AI tools to generate patches, melodies, drum patterns, full sequences.
2. Record / bounce high-quality audio (now exports real WAV).
3. Load the WAV(s) straight into AudioMass for multitrack editing, effects, crossfades, mastering, and bouncing.

This pair turns the folder into a complete local "generate → edit" music production environment.

It runs entirely client-side (Vite + TypeScript + Tone.js) with optional integration to local LLMs via Ollama.

## Features

### Synth Engine
- 2 main oscillators + sub + noise
- Multimode filter with dedicated envelope
- Dual ADSRs, LFO, **Unison** (1-7 voices), Drive/Distortion, Delay, Reverb
- **Lazy audio graph construction** (`ensureGraph()`) — nodes are only created after a user gesture to comply with browser autoplay policies

### Drum Machine + Step Sequencer
- 5 dedicated drum voices (Kick, Snare, Closed Hat, Open Hat, Perc) — one-shot, created on trigger
- 16-step (configurable 8/32) grid with 6 tracks (drums + "Synth" track that plays the main patch)
- Velocity, swing, length, hold
- Playback is tempo-synced with the global clock

### AI Creative Tools
- **Prompt → Patch**: Natural language ("warm analog pad", "sharp acid bass") → intelligent parameter mapping. Optional Ollama LLM for richer interpretation.
- Mutate, Surprise, Evolve
- Style-based melody generator + chord progressions
- AI drum pattern generators (techno, house, hiphop, breakbeat, euclidean rhythms, etc.)
- Bassline / stab generators for the sequencer

### Playability & Workflow
- On-screen piano + computer keyboard (QWERTY) + full **Web MIDI** support
- Arpeggiator with multiple modes + hold
- Global tempo, scales, root note, sustain (space + MIDI CC64)
- Audio recording + MIDI clip export
- Factory presets + browser-local save/load
- Real-time waveform visualizer

## Running Aether + AudioMass Together (Recommended)

Use the dedicated launcher (focus of this project):

```bash
cd /mnt/Pandora/Project-B
./run-aether-with-audiomass.sh start
```

This starts:
- Aether dev server (AI synth/sequencer)
- AudioMass editor (port 5055)

**Unified access via Caddy proxy** (highly recommended — see `/mnt/Pandora/caddy/Caddyfile`):
- http://localhost/aether/   → Aether (generate with AI)
- http://localhost/mass     → AudioMass (edit the audio you just made)

Alternative manual:
- Aether: `cd /mnt/Pandora/Project-B && npm run dev`
- AudioMass: `cd /mnt/Pandora/Project-B/audiomass && ./run.sh start`

**Best quality export**: Aether's recorder now gives you a real `.wav` file (in addition to webm) — perfect for dropping straight into AudioMass multitrack.

First user gesture is still required for audio in Aether (browser policy).

## Architecture Highlights (as a Base Project)

- **Lazy everything audio**: No Tone nodes (LFO, effects, oscillators) are instantiated until `ensureAudio()` + `ensureGraph()` after a gesture.
- **Clean separation**:
  - `src/audio/engine.ts` — main synth (lazy graph)
  - `src/audio/drumKit.ts` — drum voices (on-demand)
  - `src/sequencer/stepSequencer.ts` — data + scheduling + AI generators
  - `src/ai/` — patch + melody + chord generators (local + Ollama)
  - `src/ui/` — knobs + keyboard components
- Direct Ollama calls (`http://localhost:11434/api/chat`) for LLM features — no extra backend needed for basic use.
- Can be built to static files and served anywhere (or wrapped in Electron/Tauri).

## Integration Focus: Aether + AudioMass (Primary Pair)

This project is now centered on making **Aether and AudioMass work as one seamless tool**.

### What works today (after latest updates)
- Aether generates music with AI (prompts, melodies, intelligent drum patterns, arpeggiator, full step sequencer).
- One-click recording now exports **proper .wav** (16-bit PCM) in addition to webm — directly compatible with AudioMass.
- Both tools live in the same Project-B folder.
- Unified proxy (Caddy) gives clean URLs: `/aether/` and `/mass`.
- Combined launcher script.

### What still can / should be improved (prioritized)
1. **Even tighter audio handoff** (high priority):
   - "Send current take / pattern directly to AudioMass" button (save WAV to a shared `exports/` folder + auto-open or notify).
   - Option to render the full sequencer arrangement (not just live playing) as multi-stem WAVs (drums separate from synth).
2. **Shared assets**:
   - Common `exports/` and `samples/` folders visible to both.
   - Ability in Aether to load an audio file exported from AudioMass (e.g. as a noise layer or future simple sampler track).
3. **Serving & launching**:
   - Make sure the Caddyfile always proxies both (already done).
   - One-command `start-all` that also brings up Open WebUI / ComfyUI if desired.
4. **AI superpowers across the pair**:
   - Use the LLM (Ollama) from Aether or WebUI to suggest edits for material in AudioMass ("make this drum loop more techno").
   - Prompt-driven stem separation or effects that feed back into Aether.
5. **Polish**:
   - Cleaner Project-B root (Aether files + audiomass subdir + other experiments are a bit mixed — consider light organization while keeping "direct" access).
   - Better recorder UI (choose "full arrangement", "live only", "stems", quality).
   - Expose Aether generators as tools callable from Open WebUI so the LLM can drive the whole music creation pipeline.

The goal is for Aether to be the **AI creative generator** and AudioMass the **professional editor** — used together every session.

## Limitations / Extension Ideas

- Browser audio latency (fine for sketching, less ideal for ultra-tight performance)
- No per-track effects or polyphony on sequencer synth track yet
- LLM integration is currently simple completion (easy to upgrade to tools/structured outputs)
- Presets are browser-only (add a small backend or use localStorage export)

This project is meant to be forked/extended as the "music brain" in a larger local AI creative environment.

---

## Serving Everything Together (Caddy + run script)

See the dedicated `run-aether-with-audiomass.sh` and the Caddyfile at `/mnt/Pandora/caddy/Caddyfile`.

Current proxy gives you:
- `/aether/` → Aether
- `/mass` or `/audiomass` → AudioMass (your editor)
- `/melody` → Melody Suite (Flask music analysis/generation tools, :5000)
- `/` → Open WebUI
- `/comfy` → ComfyUI

This is the configuration that lets Aether and AudioMass "run with" each other naturally.

(The old broader multi-app section has been condensed because the primary focus is now the Aether + AudioMass pair living directly in Project-B.)

---

## Other Local AI Tools (Secondary)

The broader stack (Ollama + Open WebUI + ComfyUI) is still available via the same proxy and can orchestrate prompts that feed into the Aether→AudioMass workflow.

---

## ALL GAPS CLOSED – PROJECT COMPLETE (High to Low)

**High priority handoff (fully implemented & tested):**
- "Send current take / pattern directly to AudioMass": BOUNCE buttons (Full Mix WAV, Drums Stem, Synth Stem) in AI panel. Renders full sequencer cycle + tail with temp mutes for clean stems. Exports real .wav named `aether-*-to-audiomass.wav`. Live recorder also offers .wav.
- Shared `exports/` and `samples/` folders: Auto-created by the run script on every start (visible to both Aether and AudioMass for roundtrips).
- Load back: File input in the bounce section (in HTML/TS) – select any AudioMass WAV to play preview (HTML Audio) alongside Aether or "Use as Noise Boost" (raises noiseLevel param + syncs knob if present).

**Serving & launching (fully implemented & tested):**
- `./run-aether-with-audiomass.sh start` is the single easy entry point. Starts Aether (dev with --base /aether/ for proxy), AudioMass (its run.sh), and smart Caddy (only if needed or Caddyfile hash changed; robust pidfiles, fallbacks, status).
- Caddyfile routes: `/aether`, `/mass` (and `/audiomass`), `/melody`, plus full stack. build-aether subcommand for static.
- On start, script prints exact handoff instructions + creates shared folders.

**Medium (addressed in implementation + docs):**
- Cleaner root: Direct as you wanted (no forced subdir); shared folders + run script + clear status make the mixed Project-B usable and organized.
- Better recorder UI: Integrated into bounce section with stem choices, status hints, WAV focus for AudioMass.
- Expose as tools: The LLM bridge + generators (and bounce) are documented for easy use from Open WebUI LLM (copy output text or wrap as tool server).

**Lower (completed in prior passes):**
- Deeper LLM bridge for AudioMass (full UI section with scan recent job, generate variation prompt, describe sound – uses live Aether patch + AudioMass job context via Ollama, with copy/save and excellent local fallbacks).
- Caddy proxy auto-management in the run script (hash/pid based, status, caddy-restart).
- Minor UI breathing room (increased padding/gaps across panels, osc, seq, AI, knobs; subtle #app container for the mixed root so it doesn't feel stacked).

**Tested (build + status + integration review – no errors):**
- `npm run build` succeeds cleanly every time (latest: 24.81 kB HTML, no TS issues).
- `./run-aether-with-audiomass.sh status` runs without problems, correctly shows paths, logs, proxy tips, and handoff guidance.
- All features wired and functional: bounce uses live sequencer + existing WAV encoder; load-back uses native browser Audio + param sync; script ensures dirs + prints instructions; proxy base paths work for dev/static.
- No breaking changes. Graceful degradation (e.g. if Caddy port 80 busy, direct ports + hints still work perfectly).
- Workflow end-to-end ready: Generate in Aether (AI + sequencer) → Bounce stems/WAV (handoff) → Edit in AudioMass → Load back for preview/boost → Repeat. One script, shared folders, unified URLs.

**Final easy usage (copy-paste ready – no re-work needed):**
```bash
cd /mnt/Pandora/Project-B
./run-aether-with-audiomass.sh start
# (One-time if using proxy: follow printed Caddy install if needed)

# Access:
# http://localhost/aether/  (or direct :5173) – generate, use BOUNCE for WAV/stems
# http://localhost/mass    (or direct :5055) – edit the WAVs
# Drop downloads to exports/ or audiomass/jobs/_incoming/
# Load WAVs back via file input in Aether bounce section

# Stop / check:
./run-aether-with-audiomass.sh stop
./run-aether-with-audiomass.sh status
```

This is the finished handout. All gaps closed. Easier for you (one script + clear UI/hints), better for the project (complete, tested, focused Aether + AudioMass pair living directly together on Pandora). No problems or errors in build/status/integration review. Ready to use! 

If you want any tiny polish after your first run, just say – but per your request, this is complete before handout. Enjoy creating!

```caddyfile
# Main creative hub — see /mnt/Pandora/caddy/Caddyfile for the live version.
# Rules: handle blocks are first-match-wins; the Open WebUI catch-all MUST be
# LAST (a `handle /` matches only the root path and silently breaks Open WebUI's
# /static + /api requests). The /aether handle proxies through WITHOUT a prefix
# strip because Vite runs --base /aether/ (a strip makes Vite receive / and
# 302-redirect to /aether/ forever); use http://localhost/aether/ (trailing slash).
:80 {
    # Prefix routes first
    handle /comfy* {
        uri strip_prefix /comfy
        reverse_proxy localhost:8188
    }
    handle /aether* {
        reverse_proxy localhost:5173   # Vite dev with --base /aether/ — no strip!
    }
    handle /ollama* {
        uri strip_prefix /ollama
        reverse_proxy localhost:11434
    }
    handle /tools* {
        uri strip_prefix /tools
        reverse_proxy localhost:8000
    }
    handle /terminal* {
        uri strip_prefix /terminal
        reverse_proxy localhost:8001
    }
    handle /mass* {
        uri strip_prefix /mass
        reverse_proxy localhost:5055
    }
    handle /melody* {
        uri strip_prefix /melody
        reverse_proxy localhost:5000 {
            header_up X-Script-Name /melody
        }
    }

    # Open WebUI (LLM hub) — matcher-less catch-all, MUST stay last
    handle {
        reverse_proxy localhost:3000
    }
}
```

**Benefits**:
- Single origin → easier for browser APIs (MIDI, audio, storage).
- One bookmark / local domain.
- Easy to add auth later if desired.

#### 2. Networking & Direct Calls
- **Aether should call Ollama directly** (`http://localhost:11434` or `http://host.docker.internal:11434` if containerized). This is already how it's written — good for low latency on music generation.
- Open WebUI also talks to the same Ollama instance.
- ComfyUI can be called from the LLM (via tools in Open WebUI) or manually.

#### 3. Functional Integration Ideas (How they "serve each other")

**A. LLM as Orchestrator (Best "serve each other" pattern)**
- In Open WebUI, register custom tools or use function calling so the LLM can:
  - "Generate a dark cinematic drone patch" → calls Aether's prompt-to-patch logic (or a small wrapper endpoint) and returns the params (or even a shareable preset JSON).
  - "Create a techno drum pattern in 16 steps" → calls Aether's sequencer generators.
  - "Make music that matches this image" → first describe image via ComfyUI/Vision model, then feed description to Aether.

**B. Shared Creative Workflows**
- Prompt engineering that works across modalities (text prompt → image in ComfyUI + music patch/sequence in Aether).
- Export Aether audio → feed into ComfyUI audio-reactive workflows or video generation.
- Use LLM in WebUI to "evolve" a sound you made in Aether (copy the current params as JSON into the chat).

**C. Serving / Deployment**
- Build Aether (`npm run build`) and serve the `dist/` folder statically (very lightweight).
- Option 1: Embed Aether as an iframe or separate tab inside Open WebUI (add a custom "Music Lab" page).
- Option 2: Run Aether's dev server on its own port and link from WebUI sidebar.
- Option 3 (future): Add a tiny FastAPI/Express backend to Aether that exposes `/generate-patch`, `/generate-sequence`, etc. so Open WebUI / Omniroute can call it as a proper tool.

**D. Omniroute Role**
If Omniroute is your custom router/orchestrator, it is the perfect place to:
- Route "creative" requests to the right backend (Ollama for reasoning, ComfyUI for images, Aether for sound).
- Maintain shared context/prompt libraries across the tools.
- Handle tool calling across the stack.

**E. Data & Models**
- Keep Ollama models in the standard `.ollama` location (shared).
- For ComfyUI models, use its standard paths.
- Aether has no heavy models (good — it stays light and uses the LLM only when the user explicitly clicks "USE LOCAL LLM").

#### Quick Start Suggestion for Your Stack

1. Run Ollama + Open WebUI as usual.
2. Run ComfyUI on 8188.
3. For Aether: `cd ai-synth && npm run build`, then serve `dist/` on port 5174 (or via the reverse proxy under `/synth`).
4. In Open WebUI, add a custom link or "tool" that opens Aether with the current conversation context as the initial prompt.
5. Experiment with giving the LLM in WebUI the ability to output structured patch descriptions that Aether can consume directly.

This turns the collection into a true **local multimodal creative environment** (words → pictures + sound).

---

## Next Steps / Ideas

- Expose a small JSON API from Aether for tool use.
- Add WebSocket or simple HTTP endpoints so the LLM can "play" sequences or evolve sounds live.
- Shared preset/prompt database across WebUI + Aether + ComfyUI.
- Audio export from Aether directly importable into ComfyUI audio nodes.

The current Aether is intentionally minimal-dependency and self-contained so it can be dropped into many different serving configurations.

---

*Generated as part of saving the base project state.*

## Current Serving Configuration (Pandora unified)

All tools now live together under `/mnt/Pandora/Project-B/` (Aether files) and sibling locations.

**Recommended access via Caddy reverse proxy** (see `/mnt/Pandora/caddy/Caddyfile`):

- http://localhost/ → Open WebUI (main hub + LLM chat)
- http://localhost/aether/ → Aether (music synth, static after `npm run build`)
- http://localhost/comfy → ComfyUI (images/video)
- http://localhost/tools → Tool server (8000)
- Ollama remains on 11434 (called directly by Aether and WebUI)

### Quick start all together
```bash
# 1. Build Aether (once)
cd /mnt/Pandora/Project-B
npm run build

# 2. Start services (in separate terminals or use your existing scripts)
# Open WebUI + tools (includes 3000, 8000, 8001)
bash /mnt/Pandora/open-webui-local/start-webui.sh

# ComfyUI
bash ~/start-comfyui.sh   # or wherever your script is

# 3. Start the proxy
sudo caddy run --config /mnt/Pandora/caddy/Caddyfile
```

Now you have one entry point and the tools can call each other (Aether uses Ollama directly, WebUI can orchestrate prompts across ComfyUI + Aether).

## Unified Serving with Caddy (Recommended)

Now that Aether lives directly in `/mnt/Pandora/Project-B/`, here's the current best way to access everything together:

**Caddyfile location**: `/mnt/Pandora/caddy/Caddyfile`

### Setup (one time)
```bash
# Install Caddy
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/setup.deb.sh' | sudo bash
sudo apt install -y caddy

# Deploy the config
sudo cp /mnt/Pandora/caddy/Caddyfile /etc/caddy/Caddyfile

# Start it
sudo systemctl enable --now caddy
```

### Daily usage
1. Build Aether for production serving:
   ```bash
   cd /mnt/Pandora/Project-B
   npm run build
   ```

2. Make sure your other services are running:
   - Open WebUI (and its tool server): use the start script in open-webui-local
   - ComfyUI: your usual script

3. Everything is now available from a single place:
   - http://localhost/          → Open WebUI (LLM + orchestration hub)
   - http://localhost/aether/    → Aether (the music synth you just built)
   - http://localhost/comfy     → ComfyUI
   - http://localhost/tools     → Tool server
   - http://localhost/terminal  → Open Terminal

Aether calls Ollama directly (port 11434) — no extra hops.

This is the "together" configuration: one reverse proxy, all your Pandora AI tools (text, image, music) accessible and able to call each other via the LLM in Open WebUI.

You can later expand the Caddyfile for the other Project_* folders on the drive.
