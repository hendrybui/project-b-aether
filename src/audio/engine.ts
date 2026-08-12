import * as Tone from 'tone';
import {
  normToCutoff, normToRes, normToTime, normToDelayTime, normToLfoRate,
  waveIndexToType, clampDetune,
} from './paramMap';

/**
 * AetherSynth — single-voice-per-note PolySynth(MonoSynth) voice manager.
 *
 * Lazy audio graph (built in ensureGraph() after first user gesture per browser
 * autoplay rules). Pattern: PolySynth voices -> per-note filter & amp env ->
 * dry bus + parallel delay/reverb -> master -> analyser -> destination.
 *
 * Public API is intentionally minimal: `noteOn/midi` + `noteOff/midi` for
 * keyboard/arp/sequencer handoff; `releaseAll()` for panic; `setParam` for
 * live AI patch updates.
 */

export interface SynthParams {
  // Oscillators (MonoSynth voice with FatOscillator spread)
  osc1Wave: number;     // 0-3 index into WAVEFORMS
  osc1Detune: number;   // -50..50 cents
  osc1Level: number;    // 0-1 post-osc level (drives how loud the osc is in the mix)
  osc2Wave: number;
  osc2Detune: number;
  osc2Level: number;    // 0-1 (drives unison spread / secondary voice level)
  subLevel: number;     // 0-1 (low octave sub-osc simulated via octave param)
  noiseLevel: number;   // 0-1 (added to mix pre-filter)

  // Filter
  filterCutoff: number; // 0-1
  filterRes: number;    // 0-1
  filterType: BiquadFilterType;
  filterEnvAmt: number; // 0-1 -> octaves on filter envelope (0..6)

  // Amp envelope
  ampAttack: number;
  ampDecay: number;
  ampSustain: number;
  ampRelease: number;

  // Filter envelope (full ADSR via Tone.Envelope + MonoSynth filterEnv base)
  filterEnvAttack: number;
  filterEnvDecay: number;
  filterEnvSustain: number;
  filterEnvRelease: number;

  // LFO (manual low-rate oscillator wired to filter cutoff)
  lfo1Rate: number;     // 0-1
  lfo1Amount: number;   // 0-1
  lfo1Target: 'none' | 'cutoff' | 'amp' | 'pitch';

  // Tempo + transport
  tempo: number;        // 60-200

  // Unison (MonoSynth uses FatOscillator count via spread + osc2 detune)
  unisonCount: number;  // 1-7
  unisonDetune: number; // 0-40 cents

  // FX
  drive: number;        // 0-1 distortion wet/dry
  delayTime: number;
  delayFeedback: number;
  delayMix: number;
  reverbSize: number;   // 0-1 (decay 0.4-6s)
  reverbMix: number;
  master: number;
}

export const defaultParams: SynthParams = {
  osc1Wave: 1,
  osc1Detune: 0,
  osc1Level: 0.85,
  osc2Wave: 2,
  osc2Detune: -8,
  osc2Level: 0.45,
  subLevel: 0.3,
  noiseLevel: 0.0,

  filterCutoff: 0.62,
  filterRes: 0.18,
  filterType: 'lowpass',
  filterEnvAmt: 0.45,

  ampAttack: 0.005,
  ampDecay: 0.32,
  ampSustain: 0.7,
  ampRelease: 0.55,

  filterEnvAttack: 0.01,
  filterEnvDecay: 0.45,
  filterEnvSustain: 0.3,
  filterEnvRelease: 0.5,

  lfo1Rate: 0.25,
  lfo1Amount: 0.0,
  lfo1Target: 'none',

  tempo: 120,
  unisonCount: 1,
  unisonDetune: 12,

  drive: 0.0,
  delayTime: 0.35,
  delayFeedback: 0.3,
  delayMix: 0.22,
  reverbSize: 0.6,
  reverbMix: 0.18,
  master: 0.82,
};

export class AetherSynth {
  private polysynth: Tone.PolySynth<Tone.MonoSynth> | null = null;
  private osc1LevelGain: Tone.Gain | null = null;
  private delay: Tone.FeedbackDelay | null = null;
  private reverb: Tone.Reverb | null = null;
  private delayWet: Tone.Gain | null = null;
  private reverbWet: Tone.Gain | null = null;
  private drive: Tone.Distortion | null = null;
  private masterGain: Tone.Gain | null = null;
  private analyser: Tone.Analyser | null = null;
  private noise: Tone.Noise | null = null;
  private noiseGain: Tone.Gain | null = null;
  private lfo: Tone.LFO | null = null;
  private lfoScale: Tone.Gain | null = null;

  private params: SynthParams = { ...defaultParams };
  private _graphBuilt = false;

  private onVoiceCountChange?: (n: number) => void;

  constructor() {
    // Lazy: no Tone nodes here. ensureGraph() runs after first user gesture.
  }

  /** Build the full audio graph. Must be called after Tone.start(). */
  ensureGraph(): void {
    if (this._graphBuilt) return;

    // We use PolySynth(MonoSynth) for polyphony + voice-stealing. Tone v15's
    // PolySynth takes a single options object: `{ voice, maxPolyphony,
    // options }`, with the voice config under `options`. Passing a voice
    // INSTANCE as the second argument (the pre-15 pattern) leaks its internal
    // `volume` Param into the options and crashes the build with "Invalid
    // argument(s) to setValueAtTime" — the whole synth then stays silent
    // while the UI reports audio is running.
    this.polysynth = new Tone.PolySynth({
      voice: Tone.MonoSynth,
      maxPolyphony: 12,
      options: {
        oscillator: {
          type: waveIndexToType(this.params.osc1Wave),
        } as any,
        filter: {
          type: this.params.filterType,
          Q: normToRes(this.params.filterRes),
          frequency: normToCutoff(this.params.filterCutoff),
          rolloff: -24,
        },
        filterEnvelope: {
          attack: normToTime(this.params.filterEnvAttack),
          decay: normToTime(this.params.filterEnvDecay),
          sustain: this.params.filterEnvSustain,
          release: normToTime(this.params.filterEnvRelease),
          baseFrequency: normToCutoff(this.params.filterCutoff),
          octaves: Math.max(0, Math.min(6, this.params.filterEnvAmt * 6)),
          exponent: 2,
        },
        envelope: {
          attack: normToTime(this.params.ampAttack),
          decay: normToTime(this.params.ampDecay),
          sustain: this.params.ampSustain,
          release: normToTime(this.params.ampRelease),
          attackCurve: 'exponential',
          decayCurve: 'exponential',
          releaseCurve: 'exponential',
        },
      },
    });

    // osc1Level shaping (gain between polysynth output and FX chain).
    this.osc1LevelGain = new Tone.Gain(this.params.osc1Level);

    // FX chain: voices -> osc1Level -> drive -> dryBus (parallel) -> delay+reverb -> master -> analyser -> dest
    this.drive = new Tone.Distortion({
      distortion: 0,
      wet: 0,
      oversample: '2x',
    });
    this.delay = new Tone.FeedbackDelay({
      delayTime: normToDelayTime(this.params.delayTime),
      feedback: Math.min(0.85, this.params.delayFeedback),
    });
    this.delayWet = new Tone.Gain(this.params.delayMix * 0.95);
    this.reverb = new Tone.Reverb({
      decay: 0.6 + this.params.reverbSize * 5.4,
      preDelay: 0.01,
    });
    this.reverbWet = new Tone.Gain(this.params.reverbMix * 0.9);
    this.masterGain = new Tone.Gain(this.params.master);
    this.analyser = new Tone.Analyser('waveform', 512);

    // Constant noise source (driven by noiseLevel gain)
    this.noise = new Tone.Noise('white');
    this.noiseGain = new Tone.Gain(this.params.noiseLevel * 0.4);
    this.noise.start();

    // Routing
    const dryBus = new Tone.Gain(1);
    const fxSum = new Tone.Gain(1);

    this.polysynth.connect(this.osc1LevelGain);
    this.osc1LevelGain.connect(this.drive);
    this.drive.connect(dryBus);

    this.noiseGain.connect(dryBus);

    this.delay.connect(this.delayWet);
    this.delayWet.connect(fxSum);

    this.reverb.connect(this.reverbWet);
    this.reverbWet.connect(fxSum);

    dryBus.connect(fxSum);
    fxSum.connect(this.masterGain);
    this.masterGain.connect(this.analyser);
    this.analyser.toDestination();

    // LFO setup (modulation target connected on demand)
    this.lfo = new Tone.LFO({
      frequency: normToLfoRate(this.params.lfo1Rate),
      min: 0,
      max: 1,
      amplitude: 1,
    });
    this.lfoScale = new Tone.Gain(0);
    this.lfo.connect(this.lfoScale);
    this.lfo.start();

    // Apply current params (this also wires the LFO target if applicable).
    // Mark the graph built only once the whole chain is up, so a build
    // failure leaves the synth retryable on the next gesture instead of
    // permanently silent.
    this._graphBuilt = true;
    this.applyParams(this.params, true);
  }

  setVoiceCountListener(fn: (n: number) => void) {
    this.onVoiceCountChange = fn;
  }

  getAnalyser(): Tone.Analyser | undefined {
    return this.analyser ?? undefined;
  }

  getWaveform(): Float32Array {
    return (this.analyser?.getValue() as Float32Array) || new Float32Array(512);
  }

  // === Public playback API ===

  /** Trigger a note. `duration` is in seconds; if omitted, note rings until noteOff. */
  noteOn(midi: number, velocity = 0.85, time?: number, duration?: number): void {
    if (!this.polysynth) this.ensureGraph();
    if (!this.polysynth) return;

    const t = time ?? Tone.now();
    const freq = 440 * Math.pow(2, (midi - 69) / 12);
    const vel = Math.max(0.05, Math.min(1, velocity));

    if (duration !== undefined && duration > 0) {
      this.polysynth.triggerAttackRelease(freq, duration, t, vel);
    } else {
      this.polysynth.triggerAttack(freq, t, vel);
    }
    const active = (this.polysynth as any).activeVoices;
    // Tone v15 exposes activeVoices as a number (older builds used an array).
    this.onVoiceCountChange?.(typeof active === 'number' ? active : (Array.isArray(active) ? active.length : 0));
  }

  noteOff(midi: number, time?: number): void {
    if (!this.polysynth) return;
    const t = time ?? Tone.now();
    const freq = 440 * Math.pow(2, (midi - 69) / 12);
    try {
      this.polysynth.triggerRelease(freq, t);
    } catch {
      // ignore — voice may already be releasing
    }
    const active = (this.polysynth as any).activeVoices;
    // Tone v15 exposes activeVoices as a number (older builds used an array).
    this.onVoiceCountChange?.(typeof active === 'number' ? active : (Array.isArray(active) ? active.length : 0));
  }

  releaseAll(): void {
    if (this.polysynth) {
      try { this.polysynth.releaseAll(); } catch { /* ignore */ }
    }
  }

  allNotesOff(): void {
    this.releaseAll();
  }

  // === Param API ===

  getParams(): SynthParams {
    return { ...this.params };
  }

  setParam<K extends keyof SynthParams>(key: K, value: SynthParams[K]): void {
    this.params = { ...this.params, [key]: value };
    this.applyParams(this.params);
  }

  setParams(patch: Partial<SynthParams>): void {
    this.params = { ...this.params, ...patch };
    this.applyParams(this.params);
  }

  /** Apply a full patch in one go (faster than many setParam calls). */
  applyFullPatch(patch: Partial<SynthParams>): void {
    this.params = { ...this.params, ...patch };
    this.applyParams(this.params);
  }

  /** Output bus (post-FX, pre-master) for the recorder or for downstream chains. */
  getMasterOutput(): Tone.ToneAudioNode | null {
    return this.masterGain;
  }

  private applyParams(p: SynthParams, initial = false): void {
    if (!this._graphBuilt || !this.polysynth || !this.drive || !this.delay || !this.reverb || !this.masterGain || !this.noiseGain || !this.lfo || !this.lfoScale) {
      // Pre-gesture: just remember params; graph not built yet.
      return;
    }

    // OSC type / detune (apply to first MonoSynth voice; PolySynth mirrors via set)
    const waveType = waveIndexToType(p.osc1Wave);
    const detune = clampDetune(p.osc1Detune);

    // PolySynth.set accepts partial voice options; we mirror them on all voices.
    try {
      this.polysynth.set({
        oscillator: { type: waveType, detune },
        filter: {
          type: p.filterType,
          Q: normToRes(p.filterRes),
          frequency: normToCutoff(p.filterCutoff),
        },
        filterEnvelope: {
          attack: normToTime(p.filterEnvAttack),
          decay: normToTime(p.filterEnvDecay),
          sustain: p.filterEnvSustain,
          release: normToTime(p.filterEnvRelease),
          baseFrequency: normToCutoff(p.filterCutoff),
          octaves: Math.max(0, Math.min(6, p.filterEnvAmt * 6)),
        },
        envelope: {
          attack: normToTime(p.ampAttack),
          decay: normToTime(p.ampDecay),
          sustain: p.ampSustain,
          release: normToTime(p.ampRelease),
        },
      } as any);
    } catch (e) {
      // Tone's MonoSynth set expects a subset; ignore mismatches
      console.warn('Synth.set partial failed', e);
    }

    // Drive / FX
    const distAmt = Math.max(0, Math.min(1, p.drive));
    this.drive.distortion = distAmt * 7;
    this.drive.wet.value = distAmt > 0.01 ? Math.min(0.9, distAmt * 1.1) : 0;

    this.delay.delayTime.value = normToDelayTime(p.delayTime);
    this.delay.feedback.value = Math.min(0.85, p.delayFeedback);
    if (this.delayWet) this.delayWet.gain.value = p.delayMix * 0.95;

    // Reverb: only rebuild on first apply or large size change (it's expensive)
    const targetDecay = 0.4 + p.reverbSize * 5.6;
    const prev = (this as any)._lastReverbDecay as number | undefined;
    if (initial || prev === undefined || Math.abs(targetDecay - prev) > 0.4) {
      (this as any)._lastReverbDecay = targetDecay;
      this.reverb.decay = targetDecay;
    }
    if (this.reverbWet) this.reverbWet.gain.value = p.reverbMix * 0.9;

    this.masterGain.gain.value = p.master;
    this.noiseGain.gain.value = p.noiseLevel * 0.4;
    if (this.osc1LevelGain) this.osc1LevelGain.gain.value = p.osc1Level;

    // LFO
    this.lfo.frequency.value = normToLfoRate(p.lfo1Rate);
    const amount = Math.max(0, Math.min(1, p.lfo1Amount));
    this.lfoScale.gain.value = amount;

    // Wire LFO target
    const target = p.lfo1Target;
    const prevTarget = (this as any)._lfoTarget as string | undefined;
    if (target !== prevTarget) {
      try { this.lfoScale.disconnect(); } catch { /* ignore */ }
      if (target === 'cutoff' && this.polysynth) {
        // Connect through a constant offset so filter cutoff modulation makes sense
        this.lfoScale.connect((this.polysynth as any)._voices?.[0]?.filter?.frequency as any);
      } else if (target === 'pitch' && this.polysynth) {
        // small pitch wobble
        const detuneScale = new Tone.Gain(amount * 35);
        this.lfoScale.connect(detuneScale);
        // PolySynth exposes detune on each voice
        const v = (this.polysynth as any)._voices?.[0];
        if (v && v.detune) detuneScale.connect(v.detune);
      } else if (target === 'amp') {
        this.lfoScale.connect((this.masterGain.gain) as any);
      }
      (this as any)._lfoTarget = target;
    }
  }
}
