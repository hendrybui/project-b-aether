export type NoteOn = (midi: number, velocity: number) => void;
export type NoteOff = (midi: number) => void;

const BLACK_KEYS = [1, 3, 6, 8, 10]; // WHITE_KEYS not needed for current layout

export class PianoKeyboard {
  private container: HTMLElement;
  private onNoteOn: NoteOn;
  private onNoteOff: NoteOff;
  private active = new Set<number>();
  private baseOctave = 3; // middle C is C4 = 60, so starting at C3
  private root = 0; // 0 = C
  private scale = 'minor';

  private keyEls = new Map<number, HTMLElement>();

  constructor(container: HTMLElement, onNoteOn: NoteOn, onNoteOff: NoteOff) {
    this.container = container;
    this.onNoteOn = onNoteOn;
    this.onNoteOff = onNoteOff;

    this.container.style.display = 'flex';
    this.container.style.gap = '2px';

    this.render();
    this.attachGlobalKeyboard();
  }

  setBaseOctave(oct: number) {
    this.baseOctave = Math.max(1, Math.min(6, oct));
    this.render();
  }

  getBaseOctave() { return this.baseOctave; }

  setScale(root: number, scale: string) {
    this.root = root;
    this.scale = scale;
    this.render();
  }

  private getScaleNotes(): Set<number> {
    const scales: Record<string, number[]> = {
      major: [0,2,4,5,7,9,11],
      minor: [0,2,3,5,7,8,10],
      dorian: [0,2,3,5,7,9,10],
      phrygian: [0,1,3,5,7,8,10],
      mixolydian: [0,2,4,5,7,9,10],
      pentatonic: [0,3,5,7,10],
    };
    const intervals = scales[this.scale] || scales.minor;
    const notes = new Set<number>();
    for (let oct = this.baseOctave; oct <= this.baseOctave + 2; oct++) {
      for (const i of intervals) {
        notes.add(oct * 12 + ((this.root + i) % 12));
      }
    }
    return notes;
  }

  private render() {
    this.container.innerHTML = '';
    this.keyEls.clear();

    const startMidi = this.baseOctave * 12; // C in that octave
    const endMidi = startMidi + 24; // two octaves + a bit

    const scaleNotes = this.getScaleNotes();

    for (let m = startMidi; m < endMidi; m++) {
      const isBlack = BLACK_KEYS.includes(m % 12);
      const key = document.createElement('div');
      key.className = `key ${isBlack ? 'black' : ''}`;
      // Highlight scale notes, dim non-scale slightly
      if (!scaleNotes.has(m)) {
        key.style.opacity = '0.7';
      }

      // label
      const label = document.createElement('div');
      label.className = 'key-label';
      const noteName = this.midiToName(m);
      label.textContent = noteName;
      key.appendChild(label);

      // events
      key.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this.press(m, 0.85, key);
      });
      key.addEventListener('mouseup', () => this.release(m, key));
      key.addEventListener('mouseleave', () => this.release(m, key));

      // touch
      key.addEventListener('touchstart', (e) => {
        e.preventDefault();
        this.press(m, 0.85, key);
      });
      key.addEventListener('touchend', (e) => {
        e.preventDefault();
        this.release(m, key);
      });

      this.container.appendChild(key);
      this.keyEls.set(m, key);
    }
  }

  private midiToName(m: number): string {
    const names = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
    const n = m % 12;
    const oct = Math.floor(m / 12) - 1;
    return `${names[n]}${oct}`;
  }

  private press(midi: number, vel: number, el: HTMLElement) {
    if (this.active.has(midi)) return;
    this.active.add(midi);
    el.classList.add('active');
    this.onNoteOn(midi, vel);
  }

  private release(midi: number, el: HTMLElement) {
    if (!this.active.has(midi)) return;
    this.active.delete(midi);
    el.classList.remove('active');
    this.onNoteOff(midi);
  }

  // External control (from MIDI, computer keys, AI generator)
  public triggerNoteOn(midi: number, vel = 0.8) {
    const el = this.keyEls.get(midi);
    if (el) el.classList.add('active');
    this.active.add(midi);
    this.onNoteOn(midi, vel);
  }

  public triggerNoteOff(midi: number) {
    const el = this.keyEls.get(midi);
    if (el) el.classList.remove('active');
    this.active.delete(midi);
    this.onNoteOff(midi);
  }

  public releaseAll() {
    for (const m of this.active) {
      const el = this.keyEls.get(m);
      if (el) el.classList.remove('active');
      this.onNoteOff(m);
    }
    this.active.clear();
  }

  private attachGlobalKeyboard() {
    // Computer keyboard mapping — full chromatic two octaves
    // Lower octave: Z=C, S=C#, X=D, D=D#, C=E, V=F, G=F#, B=G, H=G#, N=A, J=A#, M=B
    // Upper octave: Q=C, 2=C#, W=D, 3=D#, E=E, R=F, 5=F#, T=G, 6=G#, Y=A, 7=A#, U=B
    const lowerWhite = ['z','x','c','v','b','n','m'];
    const lowerBlack = ['s','d','g','h','j'];
    const upperWhite = ['q','w','e','r','t','y','u'];
    const upperBlack = ['2','3','5','6','7'];

    const whiteIntervals = [0, 2, 4, 5, 7, 9, 11]; // C D E F G A B
    const blackIntervals = [1, 3, 6, 8, 10];       // C# D# F# G# A#

    const lowerBase = this.baseOctave * 12;
    const upperBase = (this.baseOctave + 1) * 12;

    const keyToMidi = new Map<string, number>();
    lowerWhite.forEach((k, i) => keyToMidi.set(k, lowerBase + whiteIntervals[i]));
    lowerBlack.forEach((k, i) => keyToMidi.set(k, lowerBase + blackIntervals[i]));
    upperWhite.forEach((k, i) => keyToMidi.set(k, upperBase + whiteIntervals[i]));
    upperBlack.forEach((k, i) => keyToMidi.set(k, upperBase + blackIntervals[i]));

    // Also support shifted for black keys if wanted, but keep simple

    window.addEventListener('keydown', (e) => {
      if (e.repeat) return;
      const k = e.key.toLowerCase();
      if (keyToMidi.has(k)) {
        const midi = keyToMidi.get(k)!;
        const el = this.keyEls.get(midi);
        if (el) el.classList.add('active');
        this.active.add(midi);
        this.onNoteOn(midi, 0.78);
      }
      if (k === ' ' && document.activeElement?.tagName !== 'INPUT') {
        e.preventDefault();
        // could be used for sustain later
      }
    });

    window.addEventListener('keyup', (e) => {
      const k = e.key.toLowerCase();
      if (keyToMidi.has(k)) {
        const midi = keyToMidi.get(k)!;
        const el = this.keyEls.get(midi);
        if (el) el.classList.remove('active');
        this.active.delete(midi);
        this.onNoteOff(midi);
      }
    });
  }
}
