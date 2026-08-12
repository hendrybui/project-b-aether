import type { Pattern, Track } from '../sequencer/stepSequencer';

/**
 * sequenceGenerators — write real, scale-quantized notes into a Pattern.
 *
 * Each generator mutates `pattern.tracks[].steps[]` so the user can see,
 * edit, and replay the loop in the sequencer (vs. one-shot setTimeout playback).
 */

export const SCALES: Record<string, number[]> = {
  major:       [0, 2, 4, 5, 7, 9, 11],
  minor:       [0, 2, 3, 5, 7, 8, 10],
  dorian:      [0, 2, 3, 5, 7, 9, 10],
  phrygian:    [0, 1, 3, 5, 7, 8, 10],
  mixolydian:  [0, 2, 4, 5, 7, 9, 10],
  pentatonic:  [0, 3, 5, 7, 10],
};

export function getScaleNotes(root: number, scaleName: string, baseOctave: number, octaves = 2): number[] {
  const intervals = SCALES[scaleName] || SCALES.minor;
  const out: number[] = [];
  for (let o = 0; o < octaves; o++) {
    for (const iv of intervals) out.push((baseOctave + o) * 12 + ((root + iv) % 12));
  }
  return out.sort((a, b) => a - b);
}

/** Pick a scale-quantized midi note near `targetMidi`, snapping to the given scale. */
export function quantize(targetMidi: number, root: number, scaleName: string): number {
  const intervals = SCALES[scaleName] || SCALES.minor;
  const pc = ((targetMidi % 12) + 12) % 12;
  const oct = Math.floor(targetMidi / 12);
  // find nearest scale interval (in pitch class)
  let best = intervals[0];
  let bestDist = Math.abs(pc - best);
  for (const iv of intervals) {
    const d = Math.min(Math.abs(pc - iv), 12 - Math.abs(pc - iv));
    if (d < bestDist) { bestDist = d; best = iv; }
  }
  const octShift = (targetMidi % 12) - pc; // -11..+11
  void octShift;
  return oct * 12 + ((root + best) % 12);
}

// ===== Generic helpers =====

function clearTrackSteps(track: Track) {
  for (let i = 0; i < track.steps.length; i++) {
    track.steps[i] = { on: false, vel: 0, midi: track.type === 'synth' ? track.steps[i].midi : undefined };
  }
}

function setStepOn(track: Track, idx: number, vel: number, midi?: number) {
  if (idx < 0 || idx >= track.steps.length) return;
  track.steps[idx] = { on: true, vel, midi: midi ?? track.steps[idx].midi };
}

// ===== Drum generators =====

export function generateDrumPattern(pattern: Pattern, style: string): void {
  pattern.tracks.forEach(t => clearTrackSteps(t));
  const len = pattern.length;
  const kick = pattern.tracks.find(t => t.id === 'kick')!;
  const snare = pattern.tracks.find(t => t.id === 'snare')!;
  const ch = pattern.tracks.find(t => t.id === 'closedhat')!;
  const oh = pattern.tracks.find(t => t.id === 'openhat')!;
  const perc = pattern.tracks.find(t => t.id === 'perc')!;

  const set = (track: Track, idx: number, vel: number) => setStepOn(track, idx, vel);

  switch (style) {
    case 'techno':
    case 'minimal':
      for (let i = 0; i < len; i += 4) set(kick, i, 1.0);
      if (len >= 16) {
        set(snare, 4, 1.0);
        set(snare, 12, 0.8);
      } else {
        set(snare, 2, 1.0);
      }
      for (let i = 0; i < len; i += 2) set(ch, i, 0.7);
      if (len >= 16) set(ch, 2, 0.35);
      set(oh, 6, 0.9);
      if (len > 8) set(perc, 10, 0.65);
      break;
    case 'house':
      for (let i = 0; i < len; i += 4) set(kick, i, 1.0);
      set(snare, 4, 1.0);
      set(snare, 12, 0.8);
      for (let i = 0; i < len; i += 2) set(ch, i, 0.65);
      set(oh, 6, 0.95);
      set(oh, 14, 0.6);
      if (len >= 16) set(perc, 8, 0.6);
      break;
    case 'hiphop':
    case 'boom':
      set(kick, 0, 1.0);
      set(kick, 3, 0.7);
      set(kick, 10, 0.9);
      set(snare, 4, 1.0);
      set(snare, 12, 1.0);
      for (let i = 1; i < len; i += 2) set(ch, i, 0.6);
      if (len >= 16) {
        set(ch, 2, 0.35);
        set(perc, 6, 0.75);
        set(perc, 14, 0.55);
      }
      break;
    case 'breakbeat':
    case 'jungle':
      [0, 3, 6, 10, 13].forEach(i => set(kick, i, 1.0));
      [4, 11].forEach(i => set(snare, i, 1.0));
      for (let i = 0; i < len; i++) if (i % 2 === 1) set(ch, i, 0.65);
      if (len >= 16) {
        set(oh, 7, 0.85);
        set(perc, 2, 0.75);
        set(perc, 9, 0.6);
      }
      break;
    case 'latin':
    case 'bossa':
      [0, 6, 10].forEach(i => set(kick, i, 0.95));
      [3, 7, 11].forEach(i => set(snare, i, 0.5));
      for (let i = 0; i < len; i += 2) set(ch, i, 0.55);
      for (let i = 1; i < len; i += 4) set(perc, i, 0.7);
      break;
    default:
      for (let i = 0; i < len; i += 4) set(kick, i, 1.0);
      set(snare, Math.floor(len / 2), 1.0);
      for (let i = 0; i < len; i += 2) set(ch, i, 0.6);
      if (len > 8) set(oh, 6, 0.8);
  }
}

/** Distribute `pulses` hits across `steps` as evenly as possible (Bjorklund / Euclidean). */
export function applyEuclidean(track: Track, pulses: number): void {
  clearTrackSteps(track);
  const len = track.steps.length;
  const p = Math.max(0, Math.min(len, Math.round(pulses)));
  if (p === 0) return;
  // standard Euclidean rhythm
  const pattern: boolean[] = [];
  let bucket = 0;
  for (let i = 0; i < len; i++) {
    bucket += p;
    if (bucket >= len) {
      pattern.push(true);
      bucket -= len;
    } else {
      pattern.push(false);
    }
  }
  for (let i = 0; i < len; i++) if (pattern[i]) setStepOn(track, i, 1.0);
}

export function mutateSequence(pattern: Pattern, density = 0.22): void {
  const vals: number[] = [0, 1.0, 0.7, 0.3];
  pattern.tracks.forEach(track => {
    for (let i = 0; i < track.steps.length; i++) {
      if (Math.random() < density) {
        const v = vals[Math.floor(Math.random() * vals.length)];
        track.steps[i] = { ...track.steps[i], on: v > 0, vel: v };
      }
    }
  });
}

export function randomizeSequence(pattern: Pattern, density = 0.4): void {
  const vals: number[] = [0, 1.0, 0.7];
  pattern.tracks.forEach(track => {
    for (let i = 0; i < track.steps.length; i++) {
      const v = Math.random() < density ? vals[Math.floor(Math.random() * vals.length)] : 0;
      track.steps[i] = { ...track.steps[i], on: v > 0, vel: v };
    }
  });
}

// ===== Synth lane generators (real notes) =====

/**
 * 303-style acid bassline: walks through scale degrees with rhythmic accents.
 * Notes live in the user's current key (root + scale) — editable in the grid.
 */
export function generateBassline(
  pattern: Pattern,
  root: number,
  scale: string,
  baseOctave = 2,
): void {
  const track = pattern.tracks.find(t => t.id === 'synth');
  if (!track) return;
  clearTrackSteps(track);

  const scaleNotes = getScaleNotes(root, scale, baseOctave, 2);
  // step walk
  const walk = [0, 0, 1, 0, 2, 0, 1, 3, 0, 1, 0, 2, 1, 0, 0, 0];
  for (let i = 0; i < track.steps.length; i++) {
    const deg = walk[i % walk.length] % scaleNotes.length;
    const midi = scaleNotes[deg];
    const vel = (i % 4 === 0) ? 1.0 : 0.75;
    setStepOn(track, i, vel, midi);
  }
}

/** Rhythmic stabs: triad-style hits on chord tones. */
export function generateStabs(
  pattern: Pattern,
  root: number,
  scale: string,
  baseOctave = 3,
): void {
  const track = pattern.tracks.find(t => t.id === 'synth');
  if (!track) return;
  clearTrackSteps(track);

  const scaleNotes = getScaleNotes(root, scale, baseOctave, 2);
  // chord-tone degrees 0 (i), 2 (iii), 4 (v), 6 (vii)
  const hits = [0, 2, 4, 6, 8, 11];
  for (let i = 0; i < hits.length && i < track.steps.length; i++) {
    const idx = hits[i] % scaleNotes.length;
    setStepOn(track, i, (i % 4 === 0) ? 1.0 : 0.75, scaleNotes[idx]);
  }
}

/** Fast arpeggio through scale notes — perfect for plucky synths. */
export function generateArp(
  pattern: Pattern,
  root: number,
  scale: string,
  baseOctave = 3,
  direction: 'up' | 'down' | 'updown' | 'random' = 'up',
): void {
  const track = pattern.tracks.find(t => t.id === 'synth');
  if (!track) return;
  clearTrackSteps(track);

  const scaleNotes = getScaleNotes(root, scale, baseOctave, 2);
  const len = track.steps.length;
  for (let i = 0; i < len; i++) {
    let idx: number;
    if (direction === 'down') idx = (scaleNotes.length - 1) - (i % scaleNotes.length);
    else if (direction === 'random') idx = Math.floor(Math.random() * scaleNotes.length);
    else if (direction === 'updown') {
      const cycle = (scaleNotes.length - 1) * 2;
      const p = i % cycle;
      idx = p < scaleNotes.length ? p : cycle - p;
    } else idx = i % scaleNotes.length;
    const vel = (i % 4 === 0) ? 1.0 : 0.65;
    setStepOn(track, i, vel, scaleNotes[idx]);
  }
}

/** Generic melodic phrase — legato runs with rests. */
export function generateMelodicPattern(
  pattern: Pattern,
  root: number,
  scale: string,
  baseOctave = 3,
  style: 'phrase' | 'call' | 'spaced' = 'phrase',
): void {
  const track = pattern.tracks.find(t => t.id === 'synth');
  if (!track) return;
  clearTrackSteps(track);

  const scaleNotes = getScaleNotes(root, scale, baseOctave, 2);
  const len = track.steps.length;
  for (let i = 0; i < len; i++) {
    let on = false; let vel = 0; let midi = scaleNotes[0];
    if (style === 'spaced') {
      if (i % 3 === 0) { on = true; vel = 0.85; midi = scaleNotes[(i * 2) % scaleNotes.length]; }
    } else if (style === 'call') {
      if (i === 0 || i === 4 || i === 8 || i === 12) { on = true; vel = 0.95; midi = scaleNotes[i % scaleNotes.length]; }
    } else {
      // phrase — every step a note, with velocity shape
      on = true;
      vel = 0.55 + ((i % 4 === 0) ? 0.4 : (i % 2 === 0 ? 0.15 : 0));
      midi = scaleNotes[(i * 2 + (i % 3)) % scaleNotes.length];
    }
    if (on) setStepOn(track, i, vel, midi);
  }
}
