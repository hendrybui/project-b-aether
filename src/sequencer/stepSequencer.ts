import * as Tone from 'tone';
import { DrumKit } from '../audio/drumKit';
import type { AetherSynth } from '../audio/engine';

/**
 * StepSequencer — Transport-scheduled 16-step sequencer.
 *
 * Pattern model:
 *   - N tracks (5 drum + 1 synth), each holding `length` steps
 *   - Each step stores: { on, vel, midi } (drum tracks ignore `midi`)
 *   - Drum triggers call DrumKit.trigger()
 *   - Synth lane triggers AetherSynth with a finite duration (no leak)
 *
 * Scheduling:
 *   - Tone.Transport.scheduleRepeat(cb, "16n", 0)
 *   - Tempo via Tone.Transport.bpm.value
 *   - Swing via Tone.Transport.swing (audio-rate, no artifacts)
 *   - Per-step index is derived from Transport.position subdivision
 *
 * No setTimeout, no manual envelopes: leaks are impossible.
 */

export type TrackType = 'drum' | 'synth';

export interface Step {
  on: boolean;
  vel: number;        // 0..1
  midi?: number;      // only used for synth lane
}

export interface Track {
  id: string;
  name: string;
  type: TrackType;
  steps: Step[];
  muted: boolean;
}

export interface Pattern {
  length: number;
  tracks: Track[];
}

export type StepCallback = (trackId: string, velocity: number, stepIndex: number, time: number, midi: number) => void;

const TRACK_IDS = ['kick', 'snare', 'closedhat', 'openhat', 'perc', 'synth'] as const;
const TRACK_NAMES: Record<string, string> = {
  kick: 'KICK', snare: 'SNARE', closedhat: 'CH', openhat: 'OH', perc: 'PERC', synth: 'SYN',
};

export class StepSequencer {
  private pattern: Pattern;
  private drumKit: DrumKit | null = null;
  private synth: AetherSynth | null = null;
  private scheduleId: number | null = null;
  private currentStep = 0;
  private isPlaying = false;
  private onStep?: StepCallback;
  private onTransportStep?: (step: number) => void;
  private onAudioStep?: (step: number, seconds: number) => void;
  private poller: number | null = null;

  constructor(initialLength = 16) {
    this.pattern = this.createDefaultPattern(initialLength);
  }

  // === Wiring ===

  setDrumKit(kit: DrumKit) { this.drumKit = kit; }
  setSynth(s: AetherSynth) { this.synth = s; }

  setOnStep(cb: StepCallback) { this.onStep = cb; }
  setOnTransportStep(cb: (step: number) => void) { this.onTransportStep = cb; }
  // Audio-rate step hook (fires from the scheduleRepeat callback, not the
  // 35ms UI poller). Used by the smoke suite: Tone's lookahead scheduler
  // fires every missed boundary after a main-thread stall, each with its
  // exact scheduled `time`, so the trace stays complete and in order even
  // when the renderer is blocked — wall-clock DOM sampling cannot.
  setOnAudioStep(cb: (step: number, seconds: number) => void) { this.onAudioStep = cb; }

  // === Pattern access ===

  getPattern(): Pattern {
    return {
      length: this.pattern.length,
      tracks: this.pattern.tracks.map(t => ({
        ...t,
        steps: t.steps.map(s => ({ ...s })),
        muted: t.muted,
      })),
    };
  }

  setPattern(p: Pattern) {
    this.pattern = {
      length: p.length,
      tracks: p.tracks.map(t => ({
        ...t,
        steps: t.steps.map(s => ({ ...s })),
        muted: t.muted,
      })),
    };
  }

  getTrack(id: string): Track | undefined {
    return this.pattern.tracks.find(t => t.id === id);
  }

  setStep(trackId: string, index: number, step: Step) {
    const t = this.getTrack(trackId);
    if (!t) return;
    if (index < 0 || index >= this.pattern.length) return;
    t.steps[index] = { ...step };
  }

  /** Cycle a step's velocity for the UI click handler. */
  toggleStep(trackId: string, index: number, values: number[] = [0, 1.0, 0.7, 0.3]): number | undefined {
    const t = this.getTrack(trackId);
    if (!t || index < 0 || index >= this.pattern.length) return;
    const cur = t.steps[index].vel;
    const idx = values.indexOf(cur);
    const next = values[(idx + 1) % values.length];
    t.steps[index] = { ...t.steps[index], vel: next, on: next > 0 };
    return next;
  }

  clearAll() {
    this.pattern.tracks.forEach(t => {
      t.steps = t.steps.map(s => ({ on: false, vel: 0, midi: s.midi }));
    });
  }

  clearTrack(id: string) {
    const t = this.getTrack(id);
    if (!t) return;
    t.steps = t.steps.map(s => ({ on: false, vel: 0, midi: s.midi }));
  }

  setLength(newLength: number) {
    const len = Math.max(4, Math.min(32, newLength));
    if (len === this.pattern.length) return;
    this.pattern.tracks.forEach(t => {
      const old = t.steps;
      const neu: Step[] = [];
      for (let i = 0; i < len; i++) neu.push(old[i] ?? { on: false, vel: 0, midi: t.type === 'synth' ? 60 : undefined });
      t.steps = neu;
    });
    this.pattern.length = len;
    this.currentStep = 0;
  }

  setTrackMute(id: string, muted: boolean) {
    const t = this.getTrack(id);
    if (t) t.muted = muted;
  }

  // === Transport ===

  setTempo(bpm: number) {
    Tone.getTransport().bpm.value = Math.max(40, Math.min(300, bpm));
  }

  getTempo(): number {
    return Tone.getTransport().bpm.value;
  }

  setSwing(amount: number) {
    // Tone.Transport.swing expects 0..1 (note+half-subdivision at 1)
    Tone.getTransport().swing = Math.max(0, Math.min(1, amount));
    // swingSubdivision defaults to "16n" — matches our 16-step grid
    Tone.getTransport().swingSubdivision = '16n';
  }

  getSwing(): number {
    return Tone.getTransport().swing;
  }

  start(): void {
    if (this.isPlaying) return;
    this.isPlaying = true;
    this.currentStep = 0;

    // Schedule the per-step callback on the transport clock.
    this.scheduleId = Tone.getTransport().scheduleRepeat((time) => {
      // Derive the step from the transport's 16n subdivision. The callback's
      // `time` is AudioContext-relative, so the transport ticks at that time
      // (getTicksAtTime) are the per-event position. Under a main-thread
      // stall Tone fires every missed boundary back-to-back, and reading each
      // event's OWN scheduled tick (rather than the current global position,
      // which reads the same value for all caught-up events) keeps every
      // boundary's step correct.
      const pos = Tone.getTransport().position as string | number;
      const totalSixteenths = this.parseBarsBeatsSixteenths(pos);
      const stepFromTicks = Math.round(
        Tone.getTransport().getTicksAtTime(time as number) / (Tone.getTransport().PPQ / 4),
      ) % this.pattern.length;
      const step = totalSixteenths % this.pattern.length;
      this.currentStep = step;
      this.onAudioStep?.(stepFromTicks, Tone.getTransport().getSecondsAtTime(time as number));

      const swing = Tone.getTransport().swing;
      const baseTime = time;
      const swingOffset = (step % 2 === 1) ? swing * 0.05 : 0;

      for (const track of this.pattern.tracks) {
        if (track.muted) continue;
        const s = track.steps[step];
        if (!s || !s.on) continue;
        if (track.type === 'drum') {
          this.drumKit?.trigger(track.id, s.vel, baseTime + swingOffset);
        } else if (track.type === 'synth' && this.synth) {
          const midi = s.midi ?? 60;
          // 16th note duration (~ 60/bpm/4 sec)
          const dur = 60 / this.getTempo() / 4;
          this.synth.noteOn(midi, s.vel, (baseTime + swingOffset) as number, dur as number);
        }
        this.onStep?.(track.id, s.vel, step, baseTime + swingOffset, s.midi ?? 0);
      }
    }, '16n');

    Tone.getTransport().start();

    // Polling for UI highlight (Tone has no native step event).
    if (this.poller !== null) window.clearInterval(this.poller);
    this.poller = window.setInterval(() => {
      if (!this.isPlaying) return;
      this.onTransportStep?.(this.currentStep);
    }, 35);
  }

  stop(): void {
    if (!this.isPlaying) return;
    this.isPlaying = false;
    if (this.scheduleId !== null) {
      Tone.getTransport().clear(this.scheduleId);
      this.scheduleId = null;
    }
    if (this.poller !== null) {
      window.clearInterval(this.poller);
      this.poller = null;
    }
    // release any ringing synth notes — no more setTimeout-tracked ones!
    this.synth?.releaseAll();
    // Reset the transport: Tone v15's stop() only halts the clock, keeping
    // position — without an explicit reset the next start() resumes mid-bar
    // (the poller fakes step 0, then the audio jumps to the stale position —
    // a real sequencing bug).
    Tone.getTransport().stop();
    Tone.getTransport().position = 0;
    this.currentStep = 0;
  }

  togglePlay(): void {
    this.isPlaying ? this.stop() : this.start();
  }

  isRunning(): boolean {
    return this.isPlaying;
  }

  getCurrentStep(): number {
    return this.currentStep;
  }

  // === Helpers ===

  /** Parse Tone.Transport.position (BarsBeats:Sixteenths-style) to total 16ths from origin. */
  private parseBarsBeatsSixteenths(position: string | number): number {
    if (typeof position === 'number') return Math.floor(position * 4);
    const parts = position.split(':');
    if (parts.length < 3) return 0;
    const bars = parseInt(parts[0], 10) || 0;
    const beats = parseFloat(parts[1]) || 0;
    const sixteenths = parseFloat(parts[2]) || 0;
    return bars * 16 + Math.floor(beats * 4) + Math.floor(sixteenths);
  }

  private createDefaultPattern(length: number): Pattern {
    const tracks: Track[] = TRACK_IDS.map(id => ({
      id,
      name: TRACK_NAMES[id] ?? id,
      type: id === 'synth' ? 'synth' : 'drum',
      steps: Array.from({ length }, () => ({ on: false, vel: 0, midi: id === 'synth' ? 60 : undefined })),
      muted: false,
    }));
    return { length, tracks };
  }
}
