import './style.css';
import * as Tone from 'tone';
import { AetherSynth, defaultParams } from './audio/engine';
import type { SynthParams } from './audio/engine';
import { DrumKit } from './audio/drumKit';
import { StepSequencer } from './sequencer/stepSequencer';
import type { Pattern } from './sequencer/stepSequencer';
import { attachKnobs } from './ui/knob';
import { PianoKeyboard } from './ui/keyboard';
import { promptToPatch, mutatePatch, surprisePatch } from './ai/patchGenerator';
import { generateMelody, generateChordProgression } from './ai/melodyGenerator';
import {
  generateDrumPattern, applyEuclidean, mutateSequence, randomizeSequence,
  generateBassline, generateStabs, generateArp,
} from './ai/sequenceGenerators';
import {
  describeToPatchWithOllama,
  generateAudioMassIdeaWithOllama,
  getCloudLLMConfig,
  setCloudLLMConfig,
  getLastBackend,
} from './ai/ollama';

// ===== State =====
let synth: AetherSynth;
let drumKit: DrumKit;
let sequencer: StepSequencer;
let piano: PianoKeyboard;
let knobs: Map<string, any> = new Map();
let currentOctave = 3;
let currentRoot = 0;
let currentScale = 'minor';
let isAudioStarted = false;
let tempoBpm = 120;
let sustainOn = false;
let heldSustained = new Set<number>();

// ===== AudioMass LLM Bridge =====
let currentAudioMassContext = '';
const DEFAULT_AM_BASE = 'http://localhost:5055';
let amBaseUrl = DEFAULT_AM_BASE;

// A fetch that never settles would wedge the job poller permanently (busy
// stays true, every interval tick bails) — e.g. localhost resolving to a
// blackholed ::1 with no server. Bound every request so the monitor and the
// bounce auto-upload self-heal.
function fetchWithTimeout(url: string, init: RequestInit = {}, ms = 3000): Promise<Response> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), ms);
  return fetch(url, { ...init, signal: ctrl.signal }).finally(() => window.clearTimeout(timer));
}

function getCurrentPatchDescription(): string {
  if (!synth) return 'neutral synth patch with moderate filter and space';
  const p = synth.getParams();
  const waveNames = ['sine', 'saw', 'square', 'triangle'];
  const w1 = waveNames[Math.max(0, Math.min(3, Math.round(p.osc1Wave)))] || 'saw';
  const w2 = waveNames[Math.max(0, Math.min(3, Math.round(p.osc2Wave)))] || 'square';
  return [
    `Osc1 ${w1} lvl${(p.osc1Level * 100 | 0)}% det${p.osc1Detune}`,
    `Osc2 ${w2} lvl${(p.osc2Level * 100 | 0)}% det${p.osc2Detune}`,
    `Sub${(p.subLevel * 100 | 0)}% Noise${(p.noiseLevel * 100 | 0)}%`,
    `Filt cut${(p.filterCutoff * 100 | 0)} res${(p.filterRes * 100 | 0)} ${p.filterType}`,
    `Amp A${(p.ampAttack * 100 | 0)} D${(p.ampDecay * 100 | 0)} S${(p.ampSustain * 100 | 0)} R${(p.ampRelease * 100 | 0)}`,
    `FX delM${(p.delayMix * 100 | 0)} revM${(p.reverbMix * 100 | 0)} drv${(p.drive * 100 | 0)}`,
    `Unison x${p.unisonCount} @${p.tempo}bpm`,
  ].join(' | ');
}

async function fetchAudioMassContext(baseUrl: string): Promise<string> {
  const cleanBase = baseUrl.replace(/\/+$/, '');
  const contexts: string[] = [];
  try {
    const tRes = await fetch(`${cleanBase}/api/tools`, { method: 'GET' });
    if (tRes.ok) {
      const tj = await tRes.json();
      const aj = tj?.active_job;
      if (aj && aj.analysis) {
        const a = aj.analysis;
        const stems = (aj.selected_stems || aj.available_stems || []).slice(0, 6).join(',');
        contexts.push(`Active: bpm ${a.bpm ?? '?'} ${a.key ?? ''} ${a.scale ?? ''} dur~${(a.duration_sec ?? 0).toFixed(0)}s stems[${stems}]`);
      } else if (aj) {
        contexts.push(`Active job: ${aj.job_id || 'unknown'} status ${aj.status || ''}`);
      }
    }
  } catch { /* not running */ }

  const knownIds = ['674ddff71dfe', '9a7af9a571e9', 'bfabdc6eb5cd', 'e2c32252e945'];
  for (const id of knownIds) {
    if (contexts.length >= 2) break;
    try {
      const mRes = await fetch(`${cleanBase}/api/jobs/${id}/manifest`);
      if (mRes.ok) {
        const m = await mRes.json();
        const an = m.analysis || {};
        const stems = (m.available_stems || m.selected_stems || []).join(',');
        contexts.push(`Job ${id.slice(0, 6)}: bpm${an.bpm ?? '?'} ${an.key ?? ''}${an.scale ?? ''} ${(an.duration_sec ?? 0).toFixed(0)}s [${stems}]`);
      }
    } catch { /* ignore */ }
  }
  if (!contexts.length) return '';
  return 'Recent AudioMass context — ' + contexts.join(' ; ');
}

// ===== Helpers =====
function updateStatus(msg: string, ledOn = true): void {
  const el = document.getElementById('status-text');
  if (el) el.textContent = msg;
  const led = document.getElementById('audio-led');
  if (led) led.classList.toggle('on', ledOn);
}

async function ensureAudio(): Promise<void> {
  if (isAudioStarted) return;
  await Tone.start();
  isAudioStarted = true;
  updateStatus('Audio engine running — play!');
  synth.ensureGraph();
  drumKit = new DrumKit();
  sequencer.setDrumKit(drumKit);
}

// ===== Knob metadata =====
function knobMetaFor(name: string): { min: number; max: number; step: number; default: number; format?: (v: number) => string } {
  if (name.includes('Wave')) {
    return { min: 0, max: 3, step: 1, default: name === 'osc1Wave' ? 1 : 2, format: (v) => ['sine','saw','sq','tri'][Math.round(v)] ?? String(v) };
  }
  if (name === 'osc1Detune' || name === 'osc2Detune') {
    return { min: -50, max: 50, step: 0.5, default: 0 };
  }
  if (name === 'unisonCount') return { min: 1, max: 7, step: 1, default: 1 };
  if (name === 'unisonDetune') return { min: 0, max: 40, step: 0.5, default: 12 };
  if (name === 'filterCutoff') return { min: 0, max: 1, step: 0.0005, default: 0.65 };
  if (name === 'drive') return { min: 0, max: 1, step: 0.005, default: 0 };
  return { min: 0, max: 1, step: 0.005, default: 0.3 };
}

// ===== Wire synth + knobs =====
function setupSynth(): void {
  synth = new AetherSynth();
  synth.setVoiceCountListener((n) => {
    const el = document.getElementById('voice-count');
    if (el) el.textContent = String(n);
  });

  // Master volume
  const master = document.getElementById('master-vol') as HTMLInputElement;
  if (master) {
    master.value = String(defaultParams.master);
    master.addEventListener('input', () => synth.setParam('master', parseFloat(master.value)));
  }

  // Filter type select
  const fType = document.getElementById('filter-type') as HTMLSelectElement;
  if (fType) {
    fType.value = defaultParams.filterType;
    fType.addEventListener('change', () => synth.setParam('filterType', fType.value as BiquadFilterType));
  }

  // LFO target
  const lfoTarget = document.getElementById('lfo1-target') as HTMLSelectElement;
  if (lfoTarget) {
    lfoTarget.value = defaultParams.lfo1Target;
    lfoTarget.addEventListener('change', () => synth.setParam('lfo1Target', lfoTarget.value as any));
  }

  // Tempo
  const tempoSlider = document.getElementById('tempo-slider') as HTMLInputElement;
  const tempoDisplay = document.getElementById('tempo-display');
  if (tempoSlider) {
    tempoSlider.value = String(tempoBpm);
    if (tempoDisplay) tempoDisplay.textContent = String(tempoBpm);
    tempoSlider.addEventListener('input', () => {
      tempoBpm = parseInt(tempoSlider.value, 10);
      if (tempoDisplay) tempoDisplay.textContent = String(tempoBpm);
      synth.setParam('tempo', tempoBpm);
      sequencer.setTempo(tempoBpm);
    });
  }

  // Attach knobs
  knobs = attachKnobs(
    document.body,
    (name) => {
      const p = synth.getParams();
      return (p as any)[name] ?? 0.5;
    },
    (name, value) => {
      (synth as any).setParam(name, value);
    },
    (name) => knobMetaFor(name),
  );

  // Initial sync
  const initial = synth.getParams();
  knobs.forEach((knob, name) => {
    const v = (initial as any)[name];
    if (typeof v === 'number') knob.setValue(v);
  });

  setupVisualizer();
}

// ===== Visualizer =====
function setupVisualizer(): void {
  const canvas = document.getElementById('scope') as HTMLCanvasElement;
  if (!canvas) return;
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) return;
  function draw() {
    const w = canvas.width;
    const h = canvas.height;
    ctx!.fillStyle = '#0c0e14';
    ctx!.fillRect(0, 0, w, h);
    if (synth) {
      const data = synth.getWaveform();
      if (data && data.length > 0) {
        ctx!.strokeStyle = '#7c9cff';
        ctx!.lineWidth = 1.5;
        ctx!.beginPath();
        const step = Math.max(1, Math.floor(data.length / w));
        for (let x = 0; x < w; x++) {
          const i = x * step;
          const v = data[i] ?? 0;
          const y = h / 2 + v * h * 0.48;
          if (x === 0) ctx!.moveTo(x, y);
          else ctx!.lineTo(x, y);
        }
        ctx!.stroke();
        ctx!.strokeStyle = '#1f243088';
        ctx!.lineWidth = 1;
        ctx!.beginPath();
        ctx!.moveTo(0, h / 2);
        ctx!.lineTo(w, h / 2);
        ctx!.stroke();
      }
    }
    requestAnimationFrame(draw);
  }
  draw();
}

// ===== Sequencer UI =====
let seqStepEls: HTMLElement[][] = [];
// Step-boundary trace for the automated smoke suite: each entry is a step
// index paired with the AUDIO clock time at which the UI poller observed the
// boundary. Cadence assertions read this instead of wall time, which the
// renderer/poller lag corrupts on loaded machines.
const transportTrace: { step: number; seconds: number }[] = [];

function setupSequencer(): void {
  sequencer = new StepSequencer(16);
  sequencer.setSynth(synth);
  // Audio-rate trace (see __aetherTransport): recorded from the scheduleRepeat
  // callback with the event's own scheduled time, so a main-thread stall that
  // blocks the 35ms UI poller cannot lose steps or corrupt cadence — Tone's
  // lookahead fires every missed boundary back-to-back with exact times.
  sequencer.setOnAudioStep((step, seconds) => {
    const last = transportTrace[transportTrace.length - 1];
    if (!last || last.step !== step) {
      transportTrace.push({ step, seconds });
    }
  });
  sequencer.setOnTransportStep((step) => {
    document.querySelectorAll('.seq-step').forEach(el => el.classList.remove('current'));
    seqStepEls.forEach(row => {
      const el = row[step];
      if (el) el.classList.add('current');
    });
  });

  const playBtn = document.getElementById('seq-play')!;
  const stopBtn = document.getElementById('seq-stop')!;
  const clearBtn = document.getElementById('seq-clear')!;
  const lengthSel = document.getElementById('seq-length') as HTMLSelectElement;
  const swingSlider = document.getElementById('seq-swing') as HTMLInputElement;
  const swingVal = document.getElementById('swing-val')!;

  playBtn.addEventListener('click', async () => {
    await ensureAudio();
    sequencer.start();
    playBtn.textContent = '⏸ PAUSE';
  });
  stopBtn.addEventListener('click', () => {
    sequencer.stop();
    playBtn.textContent = '▶ PLAY';
    document.querySelectorAll('.seq-step').forEach(el => el.classList.remove('current'));
  });
  clearBtn.addEventListener('click', () => {
    sequencer.clearAll();
    refreshSequencerGrid();
  });
  lengthSel.addEventListener('change', () => {
    sequencer.setLength(parseInt(lengthSel.value, 10));
    buildSequencerGrid();
  });
  if (swingSlider) {
    swingSlider.addEventListener('input', () => {
      sequencer.setSwing(parseFloat(swingSlider.value));
      if (swingVal) swingVal.textContent = Math.round(parseFloat(swingSlider.value) * 100) + '%';
    });
  }

  // Generators
  bindButton('gen-drums-techno', () => runOnPattern(p => generateDrumPattern(p, 'techno')));
  bindButton('gen-drums-house',   () => runOnPattern(p => generateDrumPattern(p, 'house')));
  bindButton('gen-drums-hiphop',  () => runOnPattern(p => generateDrumPattern(p, 'hiphop')));
  bindButton('gen-drums-break',   () => runOnPattern(p => generateDrumPattern(p, 'breakbeat')));
  bindButton('gen-euclid-kick',   () => runOnPattern(p => {
    const t = p.tracks.find(tr => tr.id === 'kick');
    if (t) applyEuclidean(t, 5);
  }));
  bindButton('gen-bassline',      () => runOnPattern(p => generateBassline(p, currentRoot, currentScale, currentOctave)));
  bindButton('gen-stabs',         () => runOnPattern(p => generateStabs(p, currentRoot, currentScale, currentOctave + 1)));
  bindButton('gen-arp',           () => runOnPattern(p => generateArp(p, currentRoot, currentScale, currentOctave + 1, 'up')));
  bindButton('gen-mutate-seq',    () => runOnPattern(p => mutateSequence(p)));
  bindButton('gen-random-seq',    () => runOnPattern(p => randomizeSequence(p)));

  buildSequencerGrid();
}

function bindButton(id: string, fn: () => void): void {
  const btn = document.getElementById(id);
  if (btn) btn.addEventListener('click', fn);
}

function runOnPattern(mutator: (p: Pattern) => void): void {
  const p = sequencer.getPattern();
  mutator(p);
  sequencer.setPattern(p);
  refreshSequencerGrid();
}

function buildSequencerGrid(): void {
  const grid = document.getElementById('seq-grid');
  if (!grid) return;
  grid.innerHTML = '';
  seqStepEls = [];

  const pat = sequencer.getPattern();
  const numSteps = pat.length;
  (grid as HTMLElement).style.gridTemplateColumns = `78px repeat(${numSteps}, 1fr)`;

  pat.tracks.forEach((track) => {
    const label = document.createElement('div');
    label.className = 'seq-track-label';
    label.textContent = track.name;
    label.dataset.track = track.id;
    grid.appendChild(label);

    const rowEls: HTMLElement[] = [];
    for (let s = 0; s < numSteps; s++) {
      const stepEl = document.createElement('div');
      stepEl.className = 'seq-step';
      stepEl.dataset.track = track.id;
      stepEl.dataset.step = String(s);
      stepEl.addEventListener('click', () => {
        const newVel = sequencer.toggleStep(track.id, s);
        updateStepVisual(stepEl, newVel ?? 0);
      });
      stepEl.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        sequencer.setStep(track.id, s, { on: false, vel: 0, midi: track.type === 'synth' ? (sequencer.getTrack(track.id)?.steps[s]?.midi ?? 60) : undefined });
        updateStepVisual(stepEl, 0);
      });
      grid.appendChild(stepEl);
      rowEls.push(stepEl);
    }
    seqStepEls.push(rowEls);
  });
  refreshSequencerGrid();
}

function updateStepVisual(el: HTMLElement, val: number): void {
  el.classList.remove('active', 'hard', 'med', 'soft');
  if (val === 0) return;
  el.classList.add('active');
  if (val >= 0.95) el.classList.add('hard');
  else if (val >= 0.6) el.classList.add('med');
  else el.classList.add('soft');
}

function refreshSequencerGrid(): void {
  if (!seqStepEls.length) return;
  const pat = sequencer.getPattern();
  pat.tracks.forEach((track, tIdx) => {
    const row = seqStepEls[tIdx];
    if (!row) return;
    track.steps.forEach((s, i) => {
      const el = row[i];
      if (el) updateStepVisual(el, s.on ? s.vel : 0);
    });
  });
}

// ===== Piano + MIDI =====
function setupPiano(): void {
  const container = document.getElementById('piano');
  if (!container) return;

  piano = new PianoKeyboard(
    container,
    async (midi, vel) => {
      await ensureAudio();
      synth.noteOn(midi, vel);
    },
    (midi) => {
      handleNoteOffWithSustain(midi);
    },
  );

  const octDisp = document.getElementById('octave-display');
  if (octDisp) octDisp.textContent = String(currentOctave);

  const octUp = document.getElementById('oct-up');
  const octDown = document.getElementById('oct-down');
  if (octUp) octUp.addEventListener('click', () => {
    currentOctave = Math.min(5, currentOctave + 1);
    if (octDisp) octDisp.textContent = String(currentOctave);
    piano.setBaseOctave(currentOctave);
  });
  if (octDown) octDown.addEventListener('click', () => {
    currentOctave = Math.max(1, currentOctave - 1);
    if (octDisp) octDisp.textContent = String(currentOctave);
    piano.setBaseOctave(currentOctave);
  });

  const scaleSel = document.getElementById('scale-select') as HTMLSelectElement;
  const rootSel = document.getElementById('root-select') as HTMLSelectElement;
  if (scaleSel) scaleSel.value = currentScale;
  if (rootSel) rootSel.value = String(currentRoot);
  const updateScale = () => {
    currentScale = scaleSel.value;
    currentRoot = parseInt(rootSel.value, 10);
    piano.setScale(currentRoot, currentScale);
    const ck = document.getElementById('current-key');
    if (ck) ck.textContent = `Key: ${['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][currentRoot]} ${currentScale}`;
  };
  if (scaleSel) scaleSel.addEventListener('change', updateScale);
  if (rootSel) rootSel.addEventListener('change', updateScale);
  updateScale();
}

async function setupMIDI(): Promise<void> {
  const btn = document.getElementById('midi-btn');
  if (!btn) return;
  if (!('requestMIDIAccess' in navigator)) {
    btn.textContent = 'MIDI: Not supported';
    btn.setAttribute('disabled', 'true');
    return;
  }
  try {
    const access = await (navigator as any).requestMIDIAccess();
    const inputs = Array.from(access.inputs.values()) as any[];
    if (inputs.length === 0) {
      btn.textContent = 'MIDI: No devices';
      return;
    }
    const input = inputs[0];
    btn.textContent = `MIDI: ${input.name || 'connected'}`;
    input.onmidimessage = (msg: any) => {
      const [status, data1, data2] = msg.data;
      const cmd = status & 0xf0;
      if (cmd === 0x90 && data2 > 0) {
        ensureAudio();
        const vel = Math.max(0.2, data2 / 127);
        synth.noteOn(data1, vel);
        piano?.triggerNoteOn(data1, vel);
      } else if (cmd === 0x80 || (cmd === 0x90 && data2 === 0)) {
        // Clears the key highlight and releases the voice via the onNoteOff
        // handler (which applies sustain semantics).
        piano?.triggerNoteOff(data1);
      } else if (cmd === 0xb0 && data1 === 64) {
        setSustain(data2 >= 64);
      }
    };
  } catch {
    btn.textContent = 'MIDI: Access denied';
  }
}

// ===== Sustain =====
function setSustain(on: boolean): void {
  sustainOn = on;
  if (!sustainOn) {
    for (const n of heldSustained) {
      synth?.noteOff(n);
      piano?.triggerNoteOff(n);
    }
    heldSustained.clear();
  }
}

function handleNoteOffWithSustain(midi: number): void {
  // Wired as the keyboard's onNoteOff callback. Do NOT call
  // piano.triggerNoteOff here: that method itself invokes onNoteOff, so this
  // would recurse forever (RangeError: Maximum call stack size exceeded) on
  // every key release. Visual highlighting is already cleared by the caller
  // (keyboard release() / triggerNoteOff) before the callback fires.
  if (sustainOn) heldSustained.add(midi);
  else {
    synth?.noteOff(midi);
  }
}

// ===== AI =====
function applyPatch(patch: Partial<SynthParams>, source = ''): void {
  synth.applyFullPatch(patch);
  const current = synth.getParams();
  knobs.forEach((knob, name) => {
    const v = (current as any)[name];
    if (typeof v === 'number') knob.setValue(v, false);
  });
  const fType = document.getElementById('filter-type') as HTMLSelectElement;
  if (patch.filterType && fType) fType.value = patch.filterType;
  const lfoT = document.getElementById('lfo1-target') as HTMLSelectElement;
  if (patch.lfo1Target && lfoT) lfoT.value = patch.lfo1Target;

  const status = document.getElementById('ai-status');
  if (status) {
    status.textContent = source ? `Loaded: ${source}` : '';
    setTimeout(() => { if (status.textContent === `Loaded: ${source}`) status.textContent = ''; }, 2200);
  }
}

function setupAI(): void {
  const promptInput = document.getElementById('prompt-input') as HTMLInputElement;
  const genBtn = document.getElementById('gen-patch-btn')!;
  const mutateBtn = document.getElementById('mutate-btn')!;
  const randomBtn = document.getElementById('random-btn')!;
  const ollamaBtn = document.getElementById('ollama-btn')!;

  genBtn.addEventListener('click', async () => {
    await ensureAudio();
    const text = promptInput.value.trim() || 'warm analog pad';
    const { params, name } = promptToPatch(text);
    applyPatch(params, name || text);
  });
  mutateBtn.addEventListener('click', async () => {
    await ensureAudio();
    const current = synth.getParams();
    applyPatch(mutatePatch(current), 'mutated');
  });
  randomBtn.addEventListener('click', async () => {
    await ensureAudio();
    applyPatch(surprisePatch(), 'surprise');
  });
  ollamaBtn.addEventListener('click', async () => {
    await ensureAudio();
    const text = promptInput.value.trim() || 'beautiful evolving texture';
    const status = document.getElementById('ai-status')!;
    const cloud = getCloudLLMConfig();
    status.textContent = cloud
      ? `Asking cloud LLM (${cloud.model})...`
      : 'Asking local LLM (GPU: llama.cpp)...';
    const result = await describeToPatchWithOllama(text);
    if (result) {
      const clean: Partial<SynthParams> = {};
      for (const k of Object.keys(result)) {
        if (k in defaultParams) (clean as any)[k] = result[k];
      }
      const backend = getLastBackend() ?? 'gpu';
      const label = backend === 'cloud'
        ? `Cloud LLM: ${text}`
        : backend === 'gpu'
          ? `Local LLM (GPU): ${text}`
          : `Local LLM (Ollama): ${text}`;
      applyPatch(clean, label);
      refreshEngineFn?.(); // show the answering LLM backend in the bridge row now
    } else {
      status.textContent = 'LLM not responding — using local generator instead';
      const { params } = promptToPatch(text);
      applyPatch(params, text);
      setTimeout(() => { status.textContent = ''; }, 1800);
    }
  });

  if (promptInput) {
    promptInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') genBtn.click();
    });
  }

  // Cloud LLM config (optional OpenAI-compatible base URL; else local GPU server)
  const llmBase = document.getElementById('llm-base') as HTMLInputElement;
  const llmModel = document.getElementById('llm-model') as HTMLInputElement;
  const llmKey = document.getElementById('llm-key') as HTMLInputElement;
  const llmSave = document.getElementById('llm-save');
  const llmClear = document.getElementById('llm-clear');
  const aiStatusEl = document.getElementById('ai-status');
  if (llmBase) {
    const cur = getCloudLLMConfig();
    if (cur) {
      llmBase.value = cur.baseUrl;
      llmModel.value = cur.model;
      llmKey.value = cur.apiKey;
    }
  }
  if (llmSave) {
    llmSave.addEventListener('click', () => {
      setCloudLLMConfig({
        baseUrl: llmBase.value.trim(),
        model: llmModel.value.trim(),
        apiKey: llmKey.value.trim(),
      });
      try { localStorage.removeItem('aether-llm-cloud-optout'); } catch { /* ignore */ }
      const c = getCloudLLMConfig();
      if (aiStatusEl) {
        aiStatusEl.textContent = c
          ? `Cloud LLM enabled: ${c.model} @ ${c.baseUrl} — tried first, falls back to GPU`
          : 'Enter a base URL to enable the cloud LLM';
        setTimeout(() => { aiStatusEl.textContent = ''; }, 2600);
      }
    });
  }
  if (llmClear) {
    llmClear.addEventListener('click', () => {
      setCloudLLMConfig(null);
      try { localStorage.setItem('aether-llm-cloud-optout', '1'); } catch { /* ignore */ }
      if (llmBase) { llmBase.value = ''; llmModel.value = ''; llmKey.value = ''; }
      if (aiStatusEl) {
        aiStatusEl.textContent = 'Cloud LLM disabled — using the local GPU server';
        setTimeout(() => { aiStatusEl.textContent = ''; }, 2600);
      }
    });
  }

  // Auto-apply the launcher-written cloud-LLM seed (public/llm-seed.json,
  // written by seed-llm-config.sh via run-aether-with-audiomass.sh) so a
  // fresh browser gets the 9router config with zero manual setup. Only fires
  // when no config is saved AND the user hasn't opted out (LOCAL ONLY) — any
  // manual config always wins.
  (async () => {
    try {
      if (getCloudLLMConfig()) return;
      if (localStorage.getItem('aether-llm-cloud-optout')) return;
      const res = await fetch('./llm-seed.json', { cache: 'no-store' });
      if (!res.ok) return;
      const seed = await res.json();
      if (!seed || !seed.baseUrl || !seed.model || !seed.apiKey) return;
      setCloudLLMConfig(seed);
      if (llmBase && !llmBase.value.trim()) {
        llmBase.value = seed.baseUrl;
        llmModel.value = seed.model;
        llmKey.value = seed.apiKey;
      }
      if (aiStatusEl) {
        aiStatusEl.textContent = `Cloud LLM seeded: ${seed.model} @ ${seed.baseUrl}`;
        setTimeout(() => { aiStatusEl.textContent = ''; }, 2600);
      }
    } catch { /* no seed file — normal outside the launcher */ }
  })();

  // Melody generator — writes into the sequencer (looping, editable)
  const styleSel = document.getElementById('gen-style') as HTMLSelectElement;
  const genMelBtn = document.getElementById('gen-melody-btn')!;
  if (genMelBtn) {
    genMelBtn.addEventListener('click', async () => {
      await ensureAudio();
      const style = styleSel.value;
      const events = generateMelody({
        root: currentRoot, scale: currentScale, style,
        baseOctave: currentOctave + 1, tempoBpm: tempoBpm,
      });
      const beat = 60 / tempoBpm;
      runOnPattern(p => {
        const t = p.tracks.find(tr => tr.id === 'synth');
        if (!t) return;
        for (let i = 0; i < t.steps.length; i++) t.steps[i] = { on: false, vel: 0, midi: t.steps[i].midi };
        for (const ev of events) {
          const step = Math.floor(ev.time / beat);
          if (step < 0 || step >= p.length) continue;
          t.steps[step] = { on: true, vel: ev.velocity, midi: ev.midi };
        }
      });
    });
  }

  // Chord generator — plays a one-shot progression on the synth
  const genChordsBtn = document.getElementById('gen-chords-btn')!;
  const stopChordsBtn = document.getElementById('stop-chords-btn')!;
  let chordTimers: number[] = [];
  function stopChords() {
    chordTimers.forEach(t => window.clearTimeout(t));
    chordTimers = [];
    synth.releaseAll();
  }
  if (genChordsBtn) {
    genChordsBtn.addEventListener('click', async () => {
      await ensureAudio();
      stopChords();
      const chords = generateChordProgression(currentRoot, currentScale, 4, currentOctave + 1);
      const beat = 60 / tempoBpm;
      let t = 0;
      chords.forEach(chord => {
        const durationSec = chord[0]?.duration ?? 1;
        chordTimers.push(window.setTimeout(() => {
          chord.forEach(tone => {
            synth.noteOn(tone.midi, 0.82);
            piano?.triggerNoteOn(tone.midi, 0.82);
          });
          chordTimers.push(window.setTimeout(() => {
            chord.forEach(tone => {
              synth.noteOff(tone.midi);
              piano?.triggerNoteOff(tone.midi);
            });
          }, durationSec * beat * 1000 * 0.85));
        }, t));
        t += beat * 1000;
      });
    });
  }
  if (stopChordsBtn) stopChordsBtn.addEventListener('click', stopChords);

  // Bounce to AudioMass
  bindButton('bounce-full-btn', () => bounceCurrentPattern('full'));
  bindButton('bounce-drums-btn', () => bounceCurrentPattern('drums'));
  bindButton('bounce-synth-btn', () => bounceCurrentPattern('synth'));

  // AudioMass LLM bridge
  setupAudioMassLLMBridge();

  // Music Tools melody bridge (companion app popup -> sequencer)
  setupMelodyGeneratorBridge();
}

// ===== Bounce (WAV export for AudioMass) =====
let recorder: Tone.Recorder | null = null;

async function bounceCurrentPattern(stemType: 'full' | 'drums' | 'synth'): Promise<void> {
  await ensureAudio();
  if (!sequencer) return;

  const seq = sequencer;
  const pat = seq.getPattern();
  const tempo = seq.getTempo();
  const stepSec = 60 / tempo / 4;
  const cycleSec = pat.length * stepSec;
  const renderSec = cycleSec + 1.0;

  const savedMutes: Record<string, boolean> = {};
  pat.tracks.forEach(t => { savedMutes[t.id] = !!t.muted; });
  if (stemType === 'drums') {
    pat.tracks.forEach(t => seq.setTrackMute(t.id, t.id === 'synth'));
  } else if (stemType === 'synth') {
    pat.tracks.forEach(t => seq.setTrackMute(t.id, t.id !== 'synth'));
  } else {
    pat.tracks.forEach(t => seq.setTrackMute(t.id, false));
  }

  // Build a one-off recorder at the master output bus (mix of synth + drums)
  const dest = Tone.getDestination();
  if (!recorder) recorder = new Tone.Recorder();
  dest.connect(recorder);
  await recorder.start();

  seq.start();
  await new Promise(resolve => window.setTimeout(resolve, renderSec * 1000));
  seq.stop();

  const blob = await recorder.stop();
  dest.disconnect(recorder);

  Object.keys(savedMutes).forEach(id => seq.setTrackMute(id, savedMutes[id]));

  // Encode WAV
  let finalBlob: Blob = blob;
  try {
    const ab = await blob.arrayBuffer();
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const buf = await ctx.decodeAudioData(ab);
    finalBlob = encodeAudioBufferToWav(buf);
  } catch (e) {
    console.warn('WAV encode fallback', e);
  }

  const url = URL.createObjectURL(finalBlob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `aether-${stemType}-to-audiomass.wav`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  recorder.dispose();
  recorder = null;

  const status = document.getElementById('ai-status');

  // Chain: auto-upload the bounce to AudioMass and start htdemuc separation.
  // The job monitor (setupAudioMassJobMonitor) polls /api/jobs/active every 2s,
  // so the live progress row appears the moment the job is created.
  const base = (amBaseUrl || DEFAULT_AM_BASE).replace(/\/+$/, '');
  const fd = new FormData();
  fd.append('file', finalBlob, `aether-${stemType}-${Date.now()}.wav`);
  try {
    const res = await fetchWithTimeout(`${base}/api/jobs/upload`, { method: 'POST', body: fd }, 15000);
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* keep status */ }
      throw new Error(detail);
    }
    const job = await res.json() as { job_id?: string };
    if (status) {
      status.textContent = job?.job_id
        ? `Bounced ${stemType} → uploaded to AudioMass, separating (job ${job.job_id.slice(0, 8)}…)`
        : `Bounced ${stemType} → uploaded to AudioMass, separating…`;
    }
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err);
    if (status) {
      status.textContent = `Bounced ${stemType} WAV saved locally — AudioMass upload failed (${why})`;
    }
  }
  if (status) setTimeout(() => { status.textContent = ''; }, 5000);
}

function encodeAudioBufferToWav(audioBuffer: AudioBuffer): Blob {
  const numChannels = audioBuffer.numberOfChannels;
  const sampleRate = audioBuffer.sampleRate;
  const bitDepth = 16;
  const samples = audioBuffer.getChannelData(0);
  const dataLength = samples.length * numChannels * (bitDepth / 8);
  const buffer = new ArrayBuffer(44 + dataLength);
  const view = new DataView(buffer);
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataLength, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * numChannels * (bitDepth / 8), true);
  view.setUint16(32, numChannels * (bitDepth / 8), true);
  view.setUint16(34, bitDepth, true);
  writeString(view, 36, 'data');
  view.setUint32(40, dataLength, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    for (let channel = 0; channel < numChannels; channel++) {
      const channelData = audioBuffer.getChannelData(channel);
      const sample = Math.max(-1, Math.min(1, channelData[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
      offset += 2;
    }
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

function writeString(view: DataView, offset: number, s: string): void {
  for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
}

// ===== Recording =====
function setupRecording(): void {
  const recBtn = document.getElementById('record-btn');
  const stopBtn = document.getElementById('stop-rec-btn');
  if (!recBtn || !stopBtn) return;

  recBtn.addEventListener('click', async () => {
    await ensureAudio();
    if (!recorder) recorder = new Tone.Recorder();
    Tone.getDestination().connect(recorder);
    await recorder.start();
    recBtn.classList.add('recording');
    recBtn.textContent = '■ RECORDING';
    stopBtn.style.display = 'inline-block';
  });
  stopBtn.addEventListener('click', async () => {
    if (!recorder) return;
    const blob = await recorder.stop();
    Tone.getDestination().disconnect(recorder);
    recBtn.classList.remove('recording');
    recBtn.textContent = '● REC';
    stopBtn.style.display = 'none';

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `aether-${Date.now()}.webm`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    try {
      const ab = await blob.arrayBuffer();
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
      const buf = await ctx.decodeAudioData(ab);
      const wavBlob = encodeAudioBufferToWav(buf);
      const wavUrl = URL.createObjectURL(wavBlob);
      const aw = document.createElement('a');
      aw.href = wavUrl;
      aw.download = `aether-${Date.now()}.wav`;
      document.body.appendChild(aw);
      aw.click();
      document.body.removeChild(aw);
      URL.revokeObjectURL(wavUrl);
    } catch (e) {
      console.warn('WAV conversion failed', e);
    }
    recorder.dispose();
    recorder = null;
  });
}

// ===== AudioMass LLM bridge =====
function setupAudioMassLLMBridge(): void {
  const baseInput = document.getElementById('am-base-url') as HTMLInputElement | null;
  const scanBtn = document.getElementById('am-scan-btn')!;
  const varBtn = document.getElementById('am-var-btn')!;
  const descBtn = document.getElementById('am-desc-btn')!;
  const output = document.getElementById('am-llm-output') as HTMLTextAreaElement | null;
  const copyBtn = document.getElementById('am-copy-btn')!;
  const saveBtn = document.getElementById('am-save-btn')!;
  const clearBtn = document.getElementById('am-clear-btn')!;
  const statusEl = document.getElementById('am-status')!;
  const contextHint = document.getElementById('am-context-hint')!;

  if (baseInput) {
    baseInput.value = amBaseUrl;
    baseInput.addEventListener('input', () => {
      amBaseUrl = baseInput.value.trim() || DEFAULT_AM_BASE;
    });
  }

  scanBtn.addEventListener('click', async () => {
    const base = (baseInput?.value.trim() || amBaseUrl || DEFAULT_AM_BASE);
    amBaseUrl = base;
    statusEl.textContent = 'Scanning AudioMass jobs via local API...';
    const ctx = await fetchAudioMassContext(base);
    if (ctx) {
      currentAudioMassContext = ctx;
      const short = ctx.length > 92 ? ctx.slice(0, 89) + '...' : ctx;
      contextHint.textContent = short;
      contextHint.title = ctx;
      statusEl.textContent = 'Context loaded.';
    } else {
      currentAudioMassContext = '';
      contextHint.textContent = '(no recent jobs or AudioMass not reachable)';
      statusEl.textContent = 'Tip: start AudioMass on :5055.';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 2400);
  });

  async function runLLM(mode: 'variation' | 'describe') {
    await ensureAudio();
    const patchDesc = getCurrentPatchDescription();
    const userPrompt = (document.getElementById('prompt-input') as HTMLInputElement | null)?.value.trim() || '';
    const fullDesc = userPrompt ? `${patchDesc}. User seed: "${userPrompt}"` : patchDesc;
    const ctx = currentAudioMassContext || '';

    statusEl.textContent = mode === 'variation'
      ? 'Asking Ollama for AudioMass-optimized variation...'
      : 'Asking Ollama to describe sound for AudioMass...';
    if (output) output.value = '...generating with LLM...';

    const result = await generateAudioMassIdeaWithOllama(fullDesc, ctx, mode);
    if (result && output) {
      output.value = result;
      statusEl.textContent = 'LLM output ready.';
    } else if (output) {
      const fb = mode === 'variation'
        ? `FALLBACK (Ollama offline): Variation for current Aether patch.\n\nPatch: ${patchDesc}\n\nEditor ideas:\n• Import exported .wav as new track in AudioMass.\n• Duplicate → low-pass heavily (<120 Hz) as "sub foundation".\n• Duplicate + high-pass (>2k) + medium reverb as "air layer".\n• Sidechain or automate on 1/4 or 1/8 notes.\n• Note: "Aether ${userPrompt || 'patch'} — warm mid + sub for multitrack".`
        : `FALLBACK (Ollama offline): Description of current Aether patch.\n\nSound: ${patchDesc}\n\nEditor usage:\n• Warm harmonic bed or midrange pad layer.\n• Trim start, fade out, light saturation + reverb send.\n• Duplicate pitch down 7-12 semitones for sub.\n• Carve 250-800 Hz if clashing with vocals.`;
      output.value = fb;
      statusEl.textContent = 'Used local fallback.';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 3200);
  }

  varBtn.addEventListener('click', () => runLLM('variation'));
  descBtn.addEventListener('click', () => runLLM('describe'));

  copyBtn.addEventListener('click', () => {
    const txt = output?.value?.trim();
    if (!txt) return;
    navigator.clipboard?.writeText(txt).then(() => {
      statusEl.textContent = 'Copied to clipboard.';
      setTimeout(() => { statusEl.textContent = ''; }, 1600);
    }).catch(() => {
      window.prompt('Copy this AudioMass idea:', txt);
    });
  });

  saveBtn.addEventListener('click', () => {
    const txt = output?.value?.trim();
    if (!txt) return;
    const blob = new Blob([txt], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `aether-audiomass-idea-${Date.now()}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  clearBtn.addEventListener('click', () => {
    if (output) output.value = '';
    statusEl.textContent = '';
  });

  setupAudioMassJobMonitor();
}

// ===== Music Tools / Melody Suite melody bridge (postMessage from the
// companion apps). Both send the same contract: {source, version, bpm, key,
// scale, notes:[{midi, beats}]}, where midi === null encodes a rest.
const MELODY_BRIDGE_SOURCES = ['music-tools-melody', 'melody-suite-melody'] as const;
// The suite is proxied at /melody (Caddy strip + X-Script-Name); direct
// :5000 also still works, but the proxy keeps everything on one origin.
const MELODY_STUDIO_URL = 'http://localhost/melody/tools/melody-sheet/melody-studio';
const MELODY_SUITE_AI_URL = 'http://localhost/melody/tools/melody-sheet/ai-melody-generator';

function setupMelodyGeneratorBridge(): void {
  const openBtn = document.getElementById('mt-open-btn');
  const msOpenBtn = document.getElementById('ms-open-btn');
  const statusEl = document.getElementById('mt-status');
  const say = (msg: string) => {
    if (!statusEl) return;
    statusEl.textContent = msg;
    window.setTimeout(() => {
      if (statusEl.textContent === msg) statusEl.textContent = '';
    }, 6000);
  };

  openBtn?.addEventListener('click', () => {
    window.open(MELODY_STUDIO_URL, 'aether-melody-gen');
    say('Melody Studio (/melody) opened — generate ideas, then click “Send to Aether” on one.');
  });

  msOpenBtn?.addEventListener('click', () => {
    window.open(MELODY_SUITE_AI_URL, 'aether-melody-gen');
    say('Melody Suite AI generator (/melody) opened — generate ideas, then click “⇢ Send to Aether” on one.');
  });

  window.addEventListener('message', async (ev: MessageEvent) => {
    const d = ev.data as
      | { source?: unknown; version?: unknown; bpm?: unknown; key?: unknown; scale?: unknown; notes?: unknown }
      | null;
    if (!d || !(MELODY_BRIDGE_SOURCES as readonly unknown[]).includes(d.source) || d.version !== 1) return;
    let host = '';
    try { host = new URL(ev.origin).hostname; } catch { /* ignore */ }
    if (host !== 'localhost' && host !== '127.0.0.1') return;
    if (!Array.isArray(d.notes) || d.notes.length === 0) {
      say('Melody ignored — empty note list.');
      return;
    }
    const bpm = typeof d.bpm === 'number' && d.bpm >= 40 && d.bpm <= 240 ? Math.round(d.bpm) : tempoBpm;
    const key = typeof d.key === 'string' ? d.key : '';
    const scale = typeof d.scale === 'string' ? d.scale : '';

    await ensureAudio();
    let written = 0;
    runOnPattern((p) => {
      const t = p.tracks.find(tr => tr.id === 'synth');
      if (!t) return;
      // Clear the SYN lane, then quantize the melody: 1 step per beat of the
      // melody (same convention as the built-in melody generator). Rests
      // (midi === null) leave gaps. Notes lasting >1 beat set only their
      // onset step — the sequencer retriggers per step by design.
      for (let i = 0; i < t.steps.length; i++) t.steps[i] = { on: false, vel: 0, midi: t.steps[i].midi };
      let beatPos = 0;
      for (const n of d.notes as { midi?: number | null; beats?: number }[]) {
        const midi = typeof n?.midi === 'number' ? n.midi : null;
        const beats = typeof n?.beats === 'number' && n.beats > 0 ? n.beats : 1;
        if (midi !== null) {
          const step = Math.floor(beatPos + 0.0001);
          if (step >= 0 && step < p.length) {
            t.steps[step] = { on: true, vel: 0.9, midi };
            written++;
          }
        }
        beatPos += beats;
      }
    });

    // Tempo follows the melody so the loop lands at the generated BPM.
    if (bpm !== tempoBpm) {
      tempoBpm = bpm;
      const slider = document.getElementById('tempo-slider') as HTMLInputElement | null;
      const disp = document.getElementById('tempo-display');
      if (slider) slider.value = String(bpm);
      if (disp) disp.textContent = String(bpm);
      sequencer.setTempo(bpm);
      synth.setParam('tempo', bpm);
    }

    const label = [key, scale].filter(Boolean).join(' ');
    say(`Melody received${label ? ` (${label})` : ''} — ${written} notes written to SYN lane at ${bpm} BPM. Press Play.`);
  });
}

// ===== AudioMass live job progress =====
// Set by setupAudioMassJobMonitor so other handlers (e.g. LLM generation)
// can refresh the engine/LLM status row on demand.
let refreshEngineFn: (() => void) | null = null;

function setupAudioMassJobMonitor(): void {
  const row = document.getElementById('am-job-row');
  if (!row) return;
  const bar = document.getElementById('am-job-bar') as HTMLDivElement;
  const label = document.getElementById('am-job-label') as HTMLSpanElement;
  const message = document.getElementById('am-job-message') as HTMLSpanElement;
  const cancelBtn = document.getElementById('am-job-cancel') as HTMLButtonElement;
  const engineEl = document.getElementById('am-engine');

  // Which separation engine AudioMass is using (from /api/diagnostics).
  // Refreshed at startup and after every job reaches a terminal state, so
  // the overhead figure updates after a real container run.
  const refreshEngine = async () => {
    if (!engineEl) return;
    try {
      const res = await fetchWithTimeout(`${amBaseUrl.replace(/\/+$/, '')}/api/diagnostics`);
      if (!res.ok) return;
      const diag = await res.json();
      const sep = diag?.separation;
      if (!sep) return;
      const label = sep.backend === 'rocm_container'
        ? `ROCm GPU · ${sep.image || 'container'}`
        : `CPU worker · ${sep.device || 'cpu'}`;
      let text = `⚙ Separation engine: ${label}`;
      if (sep.last_job) {
        const j = sep.last_job;
        const total = ((j.overhead_sec ?? 0) + (j.compute_sec ?? 0)).toFixed(1);
        text += ` · last job: ${j.audio_sec ?? '?'}s audio in ${total}s `
          + `(fixed ${j.overhead_sec ?? '?'}s + compute ${j.compute_sec ?? '?'}s, ${(j.realtime ?? 0).toFixed(2)}× realtime)`;
      }
      // Warm-pool container state: up, jobs served by this container
      // generation, and the last pool job's compute vs wall (the gap stays
      // tiny because startup is paid once, not per job).
      if (sep.warm_pool) {
        const p = sep.warm_pool;
        if (p.up) {
          text += ` · pool: up`;
          if (p.jobs_served > 0) text += ` · ${p.jobs_served} jobs served`;
          if (p.idle_timeout_sec != null) {
            text += ` · idle evict ${Math.max(1, Math.round(p.idle_timeout_sec / 60))}m`;
          }
          if (p.last_job) {
            const j = p.last_job;
            text += ` · last pool job: ${(j.compute_sec ?? 0).toFixed(1)}s compute / ${(j.wall_sec ?? 0).toFixed(1)}s wall`;
          } else if (p.ready_sec != null) {
            text += ` · startup ${p.ready_sec.toFixed(1)}s (paid once)`;
          }
        } else {
          text += ` · pool: off`;
          if (p.eviction) text += ` (evicted: ${p.eviction})`;
        }
      }
      // Which local/cloud LLM backend answered the last AI generation
      // (cloud via 9router → GPU llama.cpp → Ollama CPU).
      const llmBackend = getLastBackend();
      if (llmBackend) {
        let llmLabel: string;
        if (llmBackend === 'cloud') {
          const cloud = getCloudLLMConfig();
          llmLabel = cloud ? `Cloud (${cloud.model})` : 'Cloud';
        } else if (llmBackend === 'gpu') {
          llmLabel = 'GPU (llama.cpp)';
        } else {
          llmLabel = 'Ollama (CPU)';
        }
        text += ` · LLM: ${llmLabel}`;
      }
      engineEl.textContent = text;
      engineEl.style.display = 'flex';
    } catch { /* AudioMass unreachable — leave the previous state */ }
  };
  refreshEngineFn = refreshEngine;

  const PHASE_LABELS: Record<string, string> = {
    validating_input: 'Validating audio',
    ingesting_source: 'Loading audio',
    transcoding: 'Converting format',
    separating: 'Separating stems (htdemucs)',
    postprocessing: 'Post-processing',
    analyzing: 'Analyzing (BPM/key/energy)',
    generating_waveforms: 'Generating waveforms',
    packaging: 'Packaging results',
  };

  let currentJobId: string | null = null;
  let cancelling = false;

  cancelBtn.addEventListener('click', () => {
    if (!currentJobId || cancelling) return;
    cancelling = true;
    cancelBtn.textContent = 'CANCELLING…';
    fetchWithTimeout(`${amBaseUrl.replace(/\/+$/, '')}/api/jobs/${currentJobId}/cancel`, { method: 'POST' })
      .catch(() => { /* the next poll reflects the truth */ })
      .finally(() => {
        cancelling = false;
        cancelBtn.textContent = 'CANCEL';
      });
  });

  let busy = false;
  const hide = () => {
    currentJobId = null;
    row.style.display = 'none';
  };

  const poll = async () => {
    if (busy) return;
    busy = true;
    try {
      const res = await fetchWithTimeout(`${amBaseUrl.replace(/\/+$/, '')}/api/jobs/active`);
      if (!res.ok) { hide(); return; }
      const job = await res.json();
      if (!job || !job.status) { hide(); return; }
      currentJobId = job.job_id ?? null;
      row.style.display = 'flex';
      const pct = Math.round((job.progress ?? 0) * 100);
      bar.style.width = `${pct}%`;
      cancelBtn.style.display = job.cancellable ? 'inline-block' : 'none';
      if (job.status === 'done') {
        label.textContent = 'AUDIOMASS JOB — DONE';
        message.textContent = 'Stems ready in AudioMass';
        bar.style.width = '100%';
      } else if (job.status === 'failed') {
        label.textContent = 'AUDIOMASS JOB — FAILED';
        message.textContent = job.message || 'Separation failed';
      } else if (job.status === 'cancelled') {
        label.textContent = 'AUDIOMASS JOB — CANCELLED';
        message.textContent = job.message || 'Job cancelled';
      } else {
        const phase = PHASE_LABELS[job.step] || PHASE_LABELS[job.status] || String(job.status);
        label.textContent = `AUDIOMASS JOB — ${phase.toUpperCase()}`;
        message.textContent = job.message || job.status;
      }
      // Terminal state: the job finished — refresh the engine stats (a
      // container run just recorded its measured overhead).
      if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') {
        refreshEngine();
      }
    } catch {
      hide(); // AudioMass unreachable — show nothing rather than stale progress
    } finally {
      busy = false;
    }
  };

  window.setInterval(poll, 2000);
  poll();
  refreshEngine();
}

// ===== Presets + save/load =====
function setupPresets(): void {
  document.querySelectorAll<HTMLButtonElement>('.preset-btn').forEach(btn => {
    const key = btn.dataset.preset!;
    btn.addEventListener('click', async () => {
      await ensureAudio();
      if (key === 'evolve') {
        const current = synth.getParams();
        const mutated = mutatePatch(current);
        const words = ['warm', 'bright', 'dark', 'fat', 'crisp', 'evolving'];
        if (Math.random() < 0.6) {
          const extra = promptToPatch(words[Math.floor(Math.random() * words.length)]).params;
          Object.assign(mutated, extra);
        }
        applyPatch(mutated, 'evolved');
      } else {
        loadFactoryPreset(key);
      }
    });
  });

  const saveBtn = document.getElementById('save-preset-btn');
  const loadBtn = document.getElementById('load-preset-btn');
  const exportMidiBtn = document.getElementById('export-midi-btn');
  const panicBtn = document.getElementById('panic-btn');

  if (saveBtn) saveBtn.addEventListener('click', () => {
    localStorage.setItem('aether-last-preset', JSON.stringify(synth.getParams()));
  });
  if (loadBtn) loadBtn.addEventListener('click', () => {
    const raw = localStorage.getItem('aether-last-preset');
    if (raw) try { applyPatch(JSON.parse(raw), 'loaded preset'); } catch { /* ignore */ }
  });
  if (panicBtn) panicBtn.addEventListener('click', () => {
    synth.releaseAll();
    piano?.releaseAll();
    if (sequencer) sequencer.stop();
  });
  if (exportMidiBtn) exportMidiBtn.addEventListener('click', () => exportMidi());
}

function loadFactoryPreset(key: string): void {
  const val = (FACTORY_PRESETS as any)[key];
  if (!val) return;
  if (typeof val === 'string') {
    const { params } = promptToPatch(val);
    applyPatch(params, val);
  } else {
    applyPatch(val as Partial<SynthParams>, key);
  }
}

// Factory presets (subset of the prior set, kept stable for the UI).
const FACTORY_PRESETS: Record<string, string | Partial<SynthParams>> = {
  init: 'init',
  warmpad: 'warm analog pad with space',
  acid: 'sharp acid bass resonant filter',
  brightlead: 'bright glassy lead cutting through',
  plucky: 'plucky bell mallet short decay',
  darkdrone: 'dark moody drone low sub long',
  noisy: 'gritty noisy texture with drive',
  'my-bass': {
    osc1Wave: 1, osc1Detune: 3, osc1Level: 0.9, osc2Wave: 2, osc2Detune: -5, osc2Level: 0.4,
    subLevel: 1.0, noiseLevel: 0.02, filterCutoff: 0.28, filterRes: 0.12, filterType: 'lowpass',
    filterEnvAmt: 0.35, filterEnvAttack: 0.05, filterEnvDecay: 0.6, filterEnvSustain: 0.25, filterEnvRelease: 0.5,
    ampAttack: 0.005, ampDecay: 0.6, ampSustain: 0.75, ampRelease: 0.8,
    lfo1Rate: 0.15, lfo1Amount: 0.08, lfo1Target: 'cutoff',
    tempo: 95, unisonCount: 3, unisonDetune: 8, drive: 0.15,
    delayTime: 0.3, delayFeedback: 0.2, delayMix: 0.1,
    reverbSize: 0.4, reverbMix: 0.08, master: 0.85,
  },
  'my-kick': {
    osc1Wave: 0, osc1Level: 1.0, osc2Wave: 0, osc2Level: 0.3, subLevel: 0.95,
    noiseLevel: 0.05, filterCutoff: 0.22, filterRes: 0.15, filterType: 'lowpass',
    filterEnvAmt: 0.6, ampAttack: 0.001, ampDecay: 0.5, ampSustain: 0.0, ampRelease: 0.3,
    tempo: 95, drive: 0.25, reverbMix: 0.0, master: 0.95,
  },
  'manyao-dark': {
    osc1Wave: 1, osc1Detune: 7, osc1Level: 0.8, osc2Wave: 2, osc2Detune: -9, osc2Level: 0.35,
    subLevel: 0.9, noiseLevel: 0.06, filterCutoff: 0.3, filterRes: 0.1, filterType: 'lowpass',
    filterEnvAmt: 0.25, ampAttack: 0.02, ampDecay: 0.7, ampSustain: 0.7, ampRelease: 1.0,
    lfo1Rate: 0.1, lfo1Amount: 0.1, lfo1Target: 'cutoff',
    tempo: 82, unisonCount: 3, unisonDetune: 10, drive: 0.2,
    delayTime: 0.45, delayFeedback: 0.4, delayMix: 0.3,
    reverbSize: 0.6, reverbMix: 0.25, master: 0.8,
  },
};

// ===== MIDI export =====
function exportMidi(): void {
  const events = generateMelody({
    root: currentRoot, scale: currentScale, style: 'pluck',
    baseOctave: currentOctave + 1, tempoBpm: 120,
  });
  const midi = createSimpleMidi(events, 120);
  const blob = new Blob([new Uint8Array(midi)], { type: 'audio/midi' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'aether-idea.mid';
  a.click();
  URL.revokeObjectURL(url);
}

function createSimpleMidi(events: { midi: number; time: number; duration: number; velocity: number }[], bpm: number): Uint8Array {
  const ticksPerBeat = 480;
  const usPerBeat = Math.floor(60000000 / bpm);
  const track: number[] = [];

  function writeVarLen(n: number) {
    const bytes: number[] = [];
    let v = n;
    do {
      let b = v & 0x7f;
      v >>= 7;
      if (bytes.length > 0) b |= 0x80;
      bytes.unshift(b);
    } while (v > 0);
    track.push(...bytes);
  }

  track.push(0x00, 0xff, 0x51, 0x03);
  track.push((usPerBeat >> 16) & 0xff, (usPerBeat >> 8) & 0xff, usPerBeat & 0xff);

  let lastTick = 0;
  for (const ev of events) {
    const tick = Math.floor(ev.time * (bpm / 60) * ticksPerBeat);
    const delta = tick - lastTick;
    lastTick = tick;
    const vel = Math.max(1, Math.min(127, Math.floor(ev.velocity * 127)));
    const note = Math.max(0, Math.min(127, ev.midi));
    writeVarLen(delta);
    track.push(0x90, note, vel);
    const offTick = Math.floor((ev.time + ev.duration) * (bpm / 60) * ticksPerBeat);
    writeVarLen(offTick - tick);
    track.push(0x80, note, 64);
  }
  track.push(0x00, 0xff, 0x2f, 0x00);

  const header = [0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, (ticksPerBeat >> 8) & 0xff, ticksPerBeat & 0xff];
  const trackHeader = [0x4d, 0x54, 0x72, 0x6b,
    (track.length >> 24) & 0xff, (track.length >> 16) & 0xff,
    (track.length >> 8) & 0xff, track.length & 0xff];
  return new Uint8Array([...header, ...trackHeader, ...track]);
}

// ===== Init =====
async function init(): Promise<void> {
  setupSynth();
  setupSequencer();
  // Test hook for the automated smoke suite (tests/smoke-audio.test.mjs):
  // the step-boundary trace records each step change against the AUDIO clock
  // (see transportTrace above), so cadence can be asserted on
  // Tone.Transport.seconds rather than wall time — the DOM highlight only
  // mirrors currentStep via a 35ms poller, and wall-clock measurement would
  // conflate that lag (which grows under load) with genuine transport drift.
  (window as any).__aetherTransport = {
    trace: transportTrace,
    clear: () => { transportTrace.length = 0; },
  };
  setupPiano();
  setupAI();
  setupRecording();
  setupPresets();
  await setupMIDI();

  // First-gesture audio activation
  const resumeAudioOnGesture = async () => {
    if (!isAudioStarted) {
      try {
        await Tone.start();
        isAudioStarted = true;
        updateStatus('Audio engine running — play!');
        synth.ensureGraph();
        drumKit = new DrumKit();
        sequencer.setDrumKit(drumKit);
      } catch (err) {
        console.warn('Tone.start() failed on gesture', err);
      }
    }
    window.removeEventListener('pointerdown', resumeAudioOnGesture);
    window.removeEventListener('keydown', resumeAudioOnGesture);
    document.removeEventListener('touchstart', resumeAudioOnGesture);
  };
  window.addEventListener('pointerdown', resumeAudioOnGesture, { passive: true });
  window.addEventListener('keydown', resumeAudioOnGesture);
  document.addEventListener('touchstart', resumeAudioOnGesture, { passive: true });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      synth.releaseAll();
      piano?.releaseAll();
      if (sequencer) sequencer.stop();
    }
    if (e.key === ' ' && document.activeElement?.tagName !== 'INPUT' && document.activeElement?.tagName !== 'TEXTAREA') {
      e.preventDefault();
      setSustain(!sustainOn);
    }
  });

  // Default patch
  setTimeout(() => {
    const nice = promptToPatch('warm analog pad with space').params;
    synth.applyFullPatch({ ...nice, tempo: tempoBpm });
    const current = synth.getParams();
    knobs.forEach((knob, name) => {
      const v = (current as any)[name];
      if (typeof v === 'number') knob.setValue(v, false);
    });

    const ts = document.getElementById('tempo-slider') as HTMLInputElement;
    const td = document.getElementById('tempo-display');
    if (ts) ts.value = String(tempoBpm);
    if (td) td.textContent = String(tempoBpm);
    sequencer.setTempo(tempoBpm);
  }, 180);

  // Initial pattern
  runOnPattern(p => generateDrumPattern(p, 'techno'));

  updateStatus('Ready — click anywhere or press a key to start audio');
  console.log('%c[Aether] AI Synth ready.', 'color:#555');
}

init();
