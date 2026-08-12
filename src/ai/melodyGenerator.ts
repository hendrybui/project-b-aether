import type { Track } from '../sequencer/stepSequencer';

/**
 * NoteEvent — kept for MIDI export and any downstream chord/arp player that
 * still wants scheduled events. The sequencer itself stores notes as
 * Track steps (see stepSequencer.ts).
 */
export interface NoteEvent {
  midi: number;
  time: number;   // seconds from start
  duration: number;
  velocity: number;
}

const SCALES: Record<string, number[]> = {
  major:      [0, 2, 4, 5, 7, 9, 11],
  minor:      [0, 2, 3, 5, 7, 8, 10],
  dorian:     [0, 2, 3, 5, 7, 9, 10],
  phrygian:   [0, 1, 3, 5, 7, 8, 10],
  mixolydian: [0, 2, 4, 5, 7, 9, 10],
  pentatonic: [0, 3, 5, 7, 10],
};

export function getScaleNotes(root: number, scaleName: string, baseOctave = 4, octaves = 2): number[] {
  const intervals = SCALES[scaleName] || SCALES.minor;
  const out: number[] = [];
  for (let o = 0; o < octaves; o++) {
    for (const iv of intervals) out.push((baseOctave + o) * 12 + ((root + iv) % 12));
  }
  return out.sort((a, b) => a - b);
}

/** One-shot phrase generator (used by MIDI export and one-shot chord players). */
export function generateMelody(opts: {
  root: number;
  scale: string;
  style: string;
  baseOctave?: number;
  tempoBpm?: number;
}): NoteEvent[] {
  const { root, scale, style, baseOctave = 4, tempoBpm = 118 } = opts;
  const beat = 60 / tempoBpm;
  const notes = getScaleNotes(root, scale, baseOctave, 2);
  const lowNotes = getScaleNotes(root, scale, baseOctave - 1, 1);
  const events: NoteEvent[] = [];
  let t = 0.02;
  const rand = (arr: any[]) => arr[Math.floor(Math.random() * arr.length)];

  if (style === 'dreamy' || style === 'ethereal') {
    for (let i = 0; i < 7; i++) {
      const n = rand(notes);
      events.push({ midi: n, time: t, duration: beat * (1.6 + Math.random() * 1.8), velocity: 0.6 + Math.random() * 0.3 });
      t += beat * (0.9 + Math.random() * 0.7);
    }
  } else if (style === 'acid') {
    for (let i = 0; i < 16; i++) {
      const useLow = Math.random() < 0.35;
      const pool = useLow ? lowNotes : notes;
      const n = pool[(i * 3 + (i % 2)) % pool.length];
      const dur = (i % 3 === 2) ? beat * 0.55 : beat * 0.28;
      events.push({ midi: n, time: t, duration: dur, velocity: 0.75 + Math.random() * 0.2 });
      t += beat * 0.25;
    }
  } else if (style === 'pluck') {
    const order = [0, 2, 1, 3, 2, 4, 5, 3];
    for (let i = 0; i < 24; i++) {
      const idx = order[i % order.length];
      const n = notes[idx % notes.length];
      events.push({ midi: n, time: t, duration: beat * 0.22, velocity: 0.65 + ((i % 4) === 0 ? 0.25 : 0) });
      t += beat * 0.22;
    }
  } else if (style === 'stabs') {
    const rhythm = [1, 0.5, 0.5, 1, 0.5, 0.75, 0.25];
    for (let i = 0; i < 14; i++) {
      const n = notes[(i * 2) % notes.length];
      const dur = beat * rhythm[i % rhythm.length];
      events.push({ midi: n, time: t, duration: Math.min(dur * 0.9, beat * 1.1), velocity: 0.88 });
      t += dur;
    }
  } else {
    for (let i = 0; i < 11; i++) {
      const n = notes[(i * 3 + (i % 3)) % notes.length];
      const dur = (i % 5 === 0) ? beat * 1.1 : beat * 0.45;
      events.push({ midi: n, time: t, duration: dur, velocity: 0.7 + Math.random() * 0.2 });
      t += beat * (0.55 + (i % 3 === 0 ? 0.3 : 0));
    }
  }
  return events;
}

/** Write a generated phrase directly into a Track's steps (scale-quantized). */
export function writeMelodyToTrack(
  track: Track,
  events: NoteEvent[],
  patternLength: number,
  beatSeconds: number,
): void {
  for (const ev of events) {
    const step = Math.floor(ev.time / beatSeconds);
    if (step < 0 || step >= patternLength) continue;
    track.steps[step] = { on: true, vel: ev.velocity, midi: ev.midi };
  }
}

// ===== Chord / progression helpers (for one-shot playback, unchanged) =====

export function getChordTones(rootMidi: number, scaleName: string, inversion = 0): number[] {
  const intervals = SCALES[scaleName] || SCALES.minor;
  const scaleIndex = intervals.indexOf((rootMidi % 12 + 12) % 12);
  if (scaleIndex === -1) {
    return [rootMidi, rootMidi + 4, rootMidi + 7];
  }
  const third = intervals[(scaleIndex + 2) % intervals.length];
  const fifth = intervals[(scaleIndex + 4) % intervals.length];
  const tones = [
    rootMidi,
    rootMidi - (rootMidi % 12) + third,
    rootMidi - (rootMidi % 12) + fifth,
  ];
  for (let i = 0; i < inversion; i++) tones[i] += 12;
  return tones;
}

export function generateChordProgression(
  root: number,
  scaleName: string,
  length = 4,
  baseOctave = 3,
): { midi: number; duration: number }[][] {
  const common = [
    [0, 5, 3, 4],
    [0, 2, 3, 4],
    [0, 3, 4, 0],
  ];
  const degrees = common[Math.floor(Math.random() * common.length)].slice(0, length);
  const chords: { midi: number; duration: number }[][] = [];
  const intervals = SCALES[scaleName] || SCALES.minor;
  degrees.forEach((deg, idx) => {
    const scaleDeg = intervals[deg % intervals.length];
    const chordRoot = (baseOctave * 12) + ((root + scaleDeg) % 12);
    const inv = idx % 2;
    const tones = getChordTones(chordRoot, scaleName, inv);
    if (Math.random() > 0.5) tones.push(tones[0] + 12);
    const dur = (idx === degrees.length - 1) ? 1.6 : 0.95;
    chords.push(tones.map(m => ({ midi: m, duration: dur })));
  });
  return chords;
}
