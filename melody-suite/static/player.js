/**
 * Melody Suite — Web Audio Player
 * Minimal note-sequence playback using oscillators.
 * No external libraries — pure Web Audio API.
 */

const NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];

function midiToFreq(midi) {
    return 440 * Math.pow(2, (midi - 69) / 12);
}

function midiToName(midi) {
    return NOTE_NAMES[midi % 12] + (Math.floor(midi / 12) - 1);
}

/**
 * Play a list of notes via Web Audio oscillators.
 * @param {Array<{pitch: number, start: number, duration: number}>} notes
 * @param {number} tempo - BPM
 * @param {string} waveType - oscillator type
 */
function playNotes(notes, tempo = 96, waveType = 'sine') {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const beatDur = 60 / tempo;
    const startTime = ctx.currentTime + 0.1;

    notes.forEach(n => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.frequency.value = midiToFreq(n.pitch);
        osc.type = waveType;

        const t = startTime + n.start * beatDur;
        const d = n.duration * beatDur;

        gain.gain.setValueAtTime(0, t);
        gain.gain.linearRampToValueAtTime(0.3, t + 0.02);
        gain.gain.linearRampToValueAtTime(0, t + d * 0.95);

        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t);
        osc.stop(t + d);
    });

    return ctx;
}
