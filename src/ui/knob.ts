// Simple high-quality rotary knob using Canvas + pointer events

export type KnobValueChange = (value: number) => void;

interface KnobOptions {
  min?: number;
  max?: number;
  step?: number;
  default?: number;
  onChange: KnobValueChange;
  format?: (v: number) => string;
}

export class Knob {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private value: number;
  private min: number;
  private max: number;
  private step: number;
  private onChange: KnobValueChange;
  private dragging = false;
  private lastY = 0;

  constructor(canvas: HTMLCanvasElement, opts: KnobOptions) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: true })!;
    this.min = opts.min ?? 0;
    this.max = opts.max ?? 1;
    this.step = opts.step ?? 0.001;
    this.value = opts.default ?? (this.min + this.max) / 2;
    this.onChange = opts.onChange;
    // format kept in options for potential future use
    void opts.format;

    this.draw();

    // Mouse / touch
    canvas.addEventListener('pointerdown', this.onPointerDown);
    window.addEventListener('pointermove', this.onPointerMove);
    window.addEventListener('pointerup', this.onPointerUp);

    // Double click to reset
    canvas.addEventListener('dblclick', () => {
      const def = opts.default ?? (this.min + this.max) / 2;
      this.setValue(def, true);
    });

    // Wheel support
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const delta = Math.sign(e.deltaY) * -0.035;
      const range = this.max - this.min;
      this.setValue(this.value + delta * range, true);
    }, { passive: false });
  }

  private onPointerDown = (e: PointerEvent) => {
    this.dragging = true;
    this.lastY = e.clientY;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };

  private onPointerMove = (e: PointerEvent) => {
    if (!this.dragging) return;
    const dy = this.lastY - e.clientY;
    this.lastY = e.clientY;

    const range = this.max - this.min;
    const sensitivity = 0.0048;
    let newVal = this.value + dy * sensitivity * range;
    this.setValue(newVal, true);
  };

  private onPointerUp = (e: PointerEvent) => {
    if (this.dragging) {
      this.dragging = false;
      try { (e.target as HTMLElement).releasePointerCapture(e.pointerId); } catch (_) {}
    }
  };

  setValue(v: number, notify = false) {
    const clamped = Math.max(this.min, Math.min(this.max, v));
    // snap to step
    const stepped = Math.round(clamped / this.step) * this.step;
    if (Math.abs(stepped - this.value) < 1e-6) return;

    this.value = stepped;
    this.draw();

    if (notify) {
      this.onChange(this.value);
    }
  }

  getValue(): number {
    return this.value;
  }

  private draw() {
    const c = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    const radius = Math.min(w, h) / 2 - 4;

    c.clearRect(0, 0, w, h);

    // Background ring
    c.beginPath();
    c.arc(cx, cy, radius, 0, Math.PI * 2);
    c.fillStyle = '#1f2430';
    c.fill();
    c.lineWidth = 2;
    c.strokeStyle = '#2f3542';
    c.stroke();

    // Value arc
    const start = Math.PI * 0.75;
    const end = Math.PI * 2.25;
    const range = end - start;
    const norm = (this.value - this.min) / (this.max - this.min);
    const current = start + norm * range;

    // Track
    c.beginPath();
    c.arc(cx, cy, radius, start, current);
    c.strokeStyle = '#7c9cff';
    c.lineWidth = 3.5;
    c.stroke();

    // Tick marks
    c.strokeStyle = '#4b5568';
    c.lineWidth = 1.5;
    for (let i = 0; i <= 8; i++) {
      const a = start + (i / 8) * range;
      const x1 = cx + Math.cos(a) * (radius - 2);
      const y1 = cy + Math.sin(a) * (radius - 2);
      const x2 = cx + Math.cos(a) * (radius - 7);
      const y2 = cy + Math.sin(a) * (radius - 7);
      c.beginPath();
      c.moveTo(x1, y1);
      c.lineTo(x2, y2);
      c.stroke();
    }

    // Indicator dot / line
    const ix = cx + Math.cos(current) * (radius - 3);
    const iy = cy + Math.sin(current) * (radius - 3);
    c.fillStyle = '#c5c9d3';
    c.beginPath();
    c.arc(ix, iy, 2.8, 0, Math.PI * 2);
    c.fill();

    // Center
    c.fillStyle = '#0f1117';
    c.beginPath();
    c.arc(cx, cy, radius * 0.38, 0, Math.PI * 2);
    c.fill();
    c.strokeStyle = '#3a4150';
    c.lineWidth = 1;
    c.stroke();
  }

  destroy() {
    this.canvas.removeEventListener('pointerdown', this.onPointerDown);
    window.removeEventListener('pointermove', this.onPointerMove);
    window.removeEventListener('pointerup', this.onPointerUp);
  }
}

// Helper to attach many knobs from data attributes
export function attachKnobs(
  root: HTMLElement,
  getParam: (name: string) => number,
  setParam: (name: string, v: number) => void,
  meta?: (name: string) => { min: number; max: number; step: number; default: number; format?: (v: number) => string },
) {
  const knobs = new Map<string, Knob>();

  root.querySelectorAll<HTMLCanvasElement>('canvas.knob[data-knob]').forEach((canvas) => {
    const name = canvas.dataset.knob!;
    if (!name) return;

    let min = 0, max = 1, step = 0.001, def = 0.5, format: ((v: number) => string) | undefined;

    if (meta) {
      const m = meta(name);
      min = m.min; max = m.max; step = m.step; def = m.default; format = m.format;
    } else {
      const isWave = name.includes('Wave') || name.includes('noiseType');
      void isWave;
    }

    const current = getParam(name);
    const initial = Number.isFinite(current) ? current : def;

    const knob = new Knob(canvas, {
      min, max, step, default: initial,
      onChange: (v) => setParam(name, v),
      format,
    });

    knobs.set(name, knob);
  });

  return knobs;
}
