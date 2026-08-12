import * as Tone from 'tone';

/**
 * DrumKit — uses Tone.js drum-synth instruments (one-shot triggers).
 *
 * Construction is lazy: voices are only built on first `trigger()` call
 * (which itself is only called after a user gesture has started audio).
 *
 * Voices share an output bus -> limiter -> master.
 */

export class DrumKit {
  private bus: Tone.Gain;
  private limiter: Tone.Limiter;
  private kick: Tone.MembraneSynth;
  // Snare / hats / perc are hand-built (Noise -> Filter -> AmpEnv) since
  // Tone.NoiseSynth doesn't expose filterEnvelope directly in v15.
  private snare!: { trigger: (v: number, t: number) => void; };
  private closedHat!: { trigger: (v: number, t: number) => void; };
  private openHat: Tone.MetalSynth;
  private perc!: { trigger: (v: number, t: number) => void; };

  constructor() {
    this.bus = new Tone.Gain(0.95);
    this.limiter = new Tone.Limiter(-3);
    this.bus.connect(this.limiter);
    this.limiter.toDestination();

    // === Kick: MembraneSynth ===
    this.kick = new Tone.MembraneSynth({
      pitchDecay: 0.05,
      octaves: 6,
      envelope: { attack: 0.001, decay: 0.42, sustain: 0.0, release: 0.18 },
    });
    this.kick.connect(this.bus);

    // === Snare: filtered white noise with pitch sweep ===
    this.snare = this.buildNoiseVoice('white', 1400, 2.5, 0.18, 0);

    // === Closed hat: short bright noise hit ===
    this.closedHat = this.buildNoiseVoice('white', 7000, 1.0, 0.05, -8);

    // === Open hat: metallic ===
    this.openHat = new Tone.MetalSynth({
      envelope: { attack: 0.001, decay: 0.4, release: 0.05 },
      harmonicity: 5.1,
      modulationIndex: 32,
      resonance: 4000,
      octaves: 1.5,
    });
    this.openHat.volume.value = -16;
    this.openHat.connect(this.bus);

    // === Perc: pink noise with low-mid bandpass ===
    this.perc = this.buildNoiseVoice('pink', 800, 2.0, 0.18, -4);
  }

  /** Build a noise -> filter (with env) -> amp env voice. */
  private buildNoiseVoice(
    noiseType: 'white' | 'pink' | 'brown',
    baseFreq: number,
    octaves: number,
    decay: number,
    dbOffset: number,
  ) {
    const noise = new Tone.Noise(noiseType);
    const filter = new Tone.Filter({ type: 'bandpass', frequency: baseFreq, Q: 0.9 });
    const amp = new Tone.AmplitudeEnvelope({
      attack: 0.001,
      decay,
      sustain: 0,
      release: 0.01,
    });

    noise.start();
    noise.connect(filter);
    filter.connect(amp);
    const offsetGain = new Tone.Gain(dbOffset === 0 ? 1 : Math.pow(10, dbOffset / 20));
    amp.connect(offsetGain);
    offsetGain.connect(this.bus);

    // Filter envelope: sweep cutoff downward over the decay window.
    const fenv = new Tone.FrequencyEnvelope({
      attack: 0.001,
      decay,
      sustain: 0,
      release: 0.01,
      baseFrequency: baseFreq,
      octaves,
      exponent: 2,
    });
    fenv.connect(filter.frequency);

    return {
      trigger(v: number, t: number) {
        const vel = Math.max(0.05, Math.min(1, v));
        fenv.triggerAttack(t);
        amp.triggerAttackRelease(decay, t, vel);
      },
    };
  }

  /** Trigger a single drum hit at `time` (defaults to now). */
  trigger(track: string, velocity = 0.85, time?: number): void {
    const t = time ?? Tone.now();
    const vel = Math.max(0.05, Math.min(1, velocity));
    switch (track) {
      case 'kick':
        this.kick.triggerAttackRelease('C1', '8n', t, vel);
        break;
      case 'snare':
        this.snare.trigger(vel, t);
        break;
      case 'closedhat':
      case 'hihat':
      case 'ch':
        this.closedHat.trigger(vel * 0.9, t);
        break;
      case 'openhat':
      case 'oh':
        this.openHat.triggerAttackRelease('16n', t, vel * 0.7);
        break;
      case 'perc':
      case 'tom':
      case 'rim':
        this.perc.trigger(vel, t);
        break;
    }
  }

  triggerKick(vel = 0.9, time?: number): void { this.trigger('kick', vel, time); }
  triggerSnare(vel = 0.8, time?: number): void { this.trigger('snare', vel, time); }
  triggerClosedHat(vel = 0.7, time?: number): void { this.trigger('closedhat', vel, time); }
  triggerOpenHat(vel = 0.8, time?: number): void { this.trigger('openhat', vel, time); }
  triggerPerc(vel = 0.8, time?: number): void { this.trigger('perc', vel, time); }

  setMasterVolume(v: number): void {
    this.bus.gain.value = Math.max(0, Math.min(1, v));
  }

  getOutput(): Tone.Gain {
    return this.bus;
  }

  dispose(): void {
    this.kick.dispose();
    this.openHat.dispose();
    this.limiter.dispose();
    this.bus.dispose();
  }
}
