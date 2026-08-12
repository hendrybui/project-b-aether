/**
 * Maps between the 0-1 normalized SynthParams used by the UI/AI layer
 * and the concrete Tone.js instrument/FX settings.
 */

const WAVEFORMS = ['sine', 'sawtooth', 'square', 'triangle'] as const;
export type Waveform = typeof WAVEFORMS[number];

export function waveIndexToType(idx: number): Waveform {
  const i = Math.max(0, Math.min(3, Math.round(idx)));
  return WAVEFORMS[i];
}

/** 0-1 -> seconds, exponential feel (fast at low end). */
export function normToTime(v: number): number {
  return 0.002 + Math.pow(Math.max(0, Math.min(1, v)), 1.9) * 3.4;
}

/** 0-1 -> filter cutoff in Hz (28Hz .. ~16kHz, log). */
export function normToCutoff(v: number): number {
  const min = 28;
  const max = 16000;
  return min * Math.pow(max / min, Math.max(0, Math.min(1, v)));
}

/** 0-1 -> filter Q. */
export function normToRes(v: number): number {
  return 0.4 + Math.max(0, Math.min(1, v)) * 10.5;
}

/** 0-1 -> LFO rate in Hz (~0.1 .. ~13). */
export function normToLfoRate(v: number): number {
  return 0.1 + Math.pow(Math.max(0, Math.min(1, v)), 1.6) * 13;
}

/** 0-1 -> delay time in seconds (80ms .. 600ms). */
export function normToDelayTime(v: number): number {
  return 0.08 + Math.max(0, Math.min(1, v)) * 0.52;
}

/** 0-1 -> oscillator detune in cents (-50..50 range handled by knob). */
export function clampDetune(cents: number): number {
  return Math.max(-50, Math.min(50, cents));
}

/** Convert a fade speed param (0-1) to a Tone time string for envelopes. */
export function timeToToneSeconds(v: number): number {
  return normToTime(v);
}

/** frequency -> midi note (for internal conversions). */
export function freqToMidi(freq: number): number {
  return Math.round(69 + 12 * Math.log2(freq / 440));
}

export function midiToFreq(midi: number): number {
  return 440 * Math.pow(2, (midi - 69) / 12);
}
