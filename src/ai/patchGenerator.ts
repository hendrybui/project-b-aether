import type { SynthParams } from '../audio/engine';
import { defaultParams } from '../audio/engine';

export interface Patch {
  name?: string;
  params: Partial<SynthParams>;
}

// Very effective keyword → parameter mapper.
// This is the "local AI" that works instantly and surprisingly well.
export function promptToPatch(prompt: string): Patch {
  const p = prompt.toLowerCase().trim();
  const out: Partial<SynthParams> = {};

  // === Timbre / character words ===
  const warm = /warm|fat|analog|thick|round|smooth|soft|deep/.test(p);
  const bright = /bright|shiny|glassy|crisp|clear|airy|ice/.test(p);
  const aggressive = /aggressive|sharp|cutting|harsh|acid|bite|screech|distort/.test(p);
  const dark = /dark|moody|brooding|sub|deep|under/.test(p);
  const plucky = /pluck|plucky|perc|short|tight|snappy|mallet|bell|kalimba/.test(p);
  const pad = /pad|atm|wash|drone|soft|long|evolve/.test(p);
  const bass = /bass|sub|808|kick|low|end|rumble/.test(p);
  const lead = /lead|solo|cut|through|pierce/.test(p);
  const noisy = /noise|grit|dirty|lofi|vinyl|crunch/.test(p);

  // Osc balance
  if (warm) {
    out.osc1Wave = 1; // saw
    out.osc2Wave = 1;
    out.osc2Level = 0.55;
    out.osc1Detune = 6;
    out.osc2Detune = -7;
    out.subLevel = 0.55;
  }
  if (bright) {
    out.osc1Wave = 0; // sine for glassy
    out.osc2Wave = 3; // tri
    out.osc2Detune = 11;
    out.filterCutoff = 0.78;
    out.filterRes = 0.38;
  }
  if (aggressive || /acid/.test(p)) {
    out.osc1Wave = 2; // square
    out.osc2Wave = 2;
    out.osc1Level = 0.95;
    out.osc2Level = 0.7;
    out.osc2Detune = -12;
    out.filterRes = 0.72;
    out.filterCutoff = 0.48;
    out.ampDecay = 0.18;
    out.filterEnvAmt = 0.88;
  }
  if (dark) {
    out.filterCutoff = 0.32;
    out.osc1Wave = 1;
    out.subLevel = 0.8;
    out.osc2Level = 0.25;
  }
  if (plucky) {
    out.ampAttack = 0.001;
    out.ampDecay = 0.22;
    out.ampSustain = 0.0;
    out.ampRelease = 0.35;
    out.filterEnvAmt = 0.75;
    out.filterEnvDecay = 0.18;
    out.filterRes = 0.55;
    out.osc2Level = 0.25;
    out.subLevel = 0.15;
  }
  if (pad) {
    out.ampAttack = 0.65;
    out.ampDecay = 0.9;
    out.ampSustain = 0.82;
    out.ampRelease = 1.6;
    out.filterEnvAttack = 0.6;
    out.filterEnvDecay = 1.1;
    out.filterEnvAmt = 0.35;
    out.osc1Detune = 4;
    out.osc2Detune = -5;
    out.osc2Level = 0.6;
    out.subLevel = 0.4;
    out.delayMix = 0.28;
    out.reverbMix = 0.38;
  }
  if (bass) {
    out.osc1Wave = 1; // saw
    out.osc2Wave = 2;
    out.osc2Level = 0.35;
    out.subLevel = 0.95;
    out.noiseLevel = 0.02;
    out.filterCutoff = 0.38;
    out.filterRes = 0.18;
    out.ampDecay = 0.55;
    out.ampSustain = 0.65;
    out.ampRelease = 0.45;
  }
  if (lead) {
    out.osc1Wave = 2;
    out.osc2Wave = 3;
    out.osc1Level = 0.9;
    out.osc2Level = 0.55;
    out.filterCutoff = 0.82;
    out.filterRes = 0.25;
    out.ampDecay = 0.32;
    out.ampSustain = 0.55;
  }
  if (noisy) {
    out.noiseLevel = 0.22;
    out.osc2Level = 0.35;
    out.filterRes = 0.4;
  }

  // === Specific descriptors ===
  if (/glass|crystal|bell|kalimba|mallet/.test(p)) {
    out.osc1Wave = 0;
    out.osc2Wave = 3;
    out.osc2Detune = 19;
    out.filterCutoff = 0.82;
    out.ampDecay = 0.28;
    out.ampSustain = 0.0;
    out.filterEnvAmt = 0.65;
  }
  if (/saw|classic|prophet|juno/.test(p)) {
    out.osc1Wave = 1;
    out.osc2Wave = 1;
    out.osc2Detune = -6;
    out.filterRes = 0.28;
  }
  if (/square|chiptune|retro|8bit|chip/.test(p)) {
    out.osc1Wave = 2;
    out.osc2Wave = 2;
    out.osc2Detune = 0;
    out.filterCutoff = 0.62;
    out.ampDecay = 0.18;
    out.ampSustain = 0.0;
  }
  if (/fm|metallic|clav|rhodes|electric piano/.test(p)) {
    out.osc1Wave = 0;
    out.osc2Wave = 0;
    out.osc2Detune = 7;
    out.osc2Level = 0.6;
    out.filterCutoff = 0.7;
    out.ampDecay = 0.4;
  }

  // Filter character
  if (/resonant|res|peaky|singing/.test(p)) out.filterRes = 0.65;
  if (/closed|tight|lowpass heavy/.test(p)) out.filterCutoff = 0.28;
  if (/open|wide|bright filter/.test(p)) out.filterCutoff = 0.85;

  // Envelopes
  if (/slow|long|pad|drone/.test(p) && !plucky) {
    out.ampAttack = Math.max(out.ampAttack ?? 0.01, 0.4);
    out.ampRelease = Math.max(out.ampRelease ?? 0.6, 1.1);
  }
  if (/fast|snappy|punchy|percussive/.test(p)) {
    out.ampAttack = 0.001;
    out.ampDecay = 0.16;
    out.ampSustain = 0.0;
    out.ampRelease = 0.22;
  }

  // FX
  if (/space|ambient|wash|hall|big/.test(p)) {
    out.reverbMix = 0.42;
    out.reverbSize = 0.82;
    out.delayMix = 0.25;
  }
  if (/echo|delay|slap|ping/.test(p)) {
    out.delayMix = 0.38;
    out.delayFeedback = 0.48;
    out.delayTime = 0.42;
  }

  // Clamp & defaults
  return {
    name: prompt.slice(0, 42),
    params: {
      ...out,
      // always keep master reasonable
      master: out.master ?? defaultParams.master,
    },
  };
}

// Simple but musical "mutate" — small random but musical changes
export function mutatePatch(current: SynthParams): Partial<SynthParams> {
  const p: Partial<SynthParams> = {};

  const jitter = (v: number, amt: number, min = 0, max = 1) =>
    Math.max(min, Math.min(max, v + (Math.random() - 0.5) * amt));

  p.osc1Detune = jitter(current.osc1Detune, 9, -50, 50);
  p.osc2Detune = jitter(current.osc2Detune, 9, -50, 50);
  p.filterCutoff = jitter(current.filterCutoff, 0.12);
  p.filterRes = jitter(current.filterRes, 0.18);
  p.ampDecay = jitter(current.ampDecay, 0.18);
  p.filterEnvAmt = jitter(current.filterEnvAmt, 0.22);

  if (Math.random() < 0.4) p.osc2Level = jitter(current.osc2Level, 0.25);
  if (Math.random() < 0.3) p.subLevel = jitter(current.subLevel, 0.25);
  if (Math.random() < 0.25) p.noiseLevel = jitter(current.noiseLevel, 0.12);

  if (Math.random() < 0.35) {
    p.delayMix = jitter(current.delayMix, 0.18);
    p.reverbMix = jitter(current.reverbMix, 0.15);
  }

  return p;
}

// "Surprise me" — completely new interesting starting point
export function surprisePatch(): Partial<SynthParams> {
  const styles = ['warm pad', 'acid bass', 'glassy pluck', 'dark drone', 'bright lead', 'noisy texture', 'retro square', 'ethereal'];
  const style = styles[Math.floor(Math.random() * styles.length)];
  const base = promptToPatch(style).params;

  // Add extra randomness
  base.osc1Detune = (Math.random() - 0.5) * 28;
  base.osc2Detune = (Math.random() - 0.5) * 32;
  base.filterCutoff = 0.25 + Math.random() * 0.6;
  base.filterRes = 0.1 + Math.random() * 0.65;
  if (Math.random() < 0.5) base.lfo1Amount = 0.15 + Math.random() * 0.55;
  if (Math.random() < 0.5) base.lfo1Target = 'cutoff';
  return base;
}
