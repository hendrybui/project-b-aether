// app/melody.js — AI Melody Generator (music-theory + Tone.js synth)
// No server dependency. Generates 3 melody ideas from key/scale/tempo,
// plays them, and exports the selected one as a MIDI file.

(function () {
  'use strict';

  // ---- Music theory tables ----------------------------------------------
  var NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

  // Scale intervals (semitones from root) for each supported scale.
  var SCALES = {
    major: [0, 2, 4, 5, 7, 9, 11],
    minor: [0, 2, 3, 5, 7, 8, 10],
    pentatonic: [0, 2, 4, 7, 9],
    minorPentatonic: [0, 3, 5, 7, 10],
    blues: [0, 3, 5, 6, 7, 10],
    dorian: [0, 2, 3, 5, 7, 9, 10],
    mixolydian: [0, 2, 4, 5, 7, 9, 10],
    lydian: [0, 2, 4, 6, 7, 9, 11]
  };

  // Build a multi-octave pool of MIDI notes for a key + scale.
  // Returns an array of MIDI numbers spanning ~3 octaves above the root.
  function buildScalePool(rootMidi, intervals) {
    var pool = [];
    for (var oct = 0; oct <= 2; oct++) {
      for (var i = 0; i < intervals.length; i++) {
        pool.push(rootMidi + intervals[i] + oct * 12);
      }
    }
    return pool;
  }

  function midiToName(midi) {
    var name = NOTE_NAMES[((midi % 12) + 12) % 12];
    var octave = Math.floor(midi / 12) - 1; // MIDI standard: 60 = C4
    return name + octave;
  }

  // ---- Melody generation -------------------------------------------------
  // A note is {midi:number, beats:number} where beats is a duration in beats.
  // REST is represented as {midi:null, beats:n}.
  var DURATIONS = [0.5, 0.5, 1, 1, 1, 2]; // weighted toward quarters/halves
  var REST_CHANCE = 0.12;

  // Weighted random pick helper.
  function weightedPick(arr, weights) {
    var total = 0;
    for (var i = 0; i < weights.length; i++) total += weights[i];
    var r = Math.random() * total;
    for (var j = 0; j < arr.length; j++) {
      r -= weights[j];
      if (r <= 0) return arr[j];
    }
    return arr[arr.length - 1];
  }

  function pickDuration() {
    return DURATIONS[Math.floor(Math.random() * DURATIONS.length)];
  }

  // Generate a single melody. opts: {rootMidi, scale(intervals), totalBeats,
  // motifBeats}. Approach: random walk over scale degrees with small motifs
  // (short repeating patterns) for musicality.
  function generateMelody(opts) {
    var pool = buildScalePool(opts.rootMidi, opts.scale);
    var center = Math.floor(pool.length / 2);
    var idx = center;
    var melody = [];
    var beatsSoFar = 0;

    // Build a short motif once; replay it occasionally.
    var motif = [];
    var motifLen = 3 + Math.floor(Math.random() * 2); // 3-4 notes
    var mIdx = center;
    for (var m = 0; m < motifLen; m++) {
      mIdx += (Math.random() < 0.5 ? -1 : 1) * (Math.random() < 0.5 ? 1 : 2);
      if (mIdx < 0) mIdx = 0;
      if (mIdx >= pool.length) mIdx = pool.length - 1;
      motif.push({ midi: pool[mIdx], beats: 0.5 });
    }

    while (beatsSoFar < opts.totalBeats) {
      // 25% chance to echo the motif if it fits.
      if (Math.random() < 0.25 && beatsSoFar + motifBeats(motif) <= opts.totalBeats) {
        for (var k = 0; k < motif.length; k++) {
          melody.push({ midi: motif[k].midi, beats: motif[k].beats });
          beatsSoFar += motif[k].beats;
        }
        continue;
      }

      var beats = pickDuration();
      if (beatsSoFar + beats > opts.totalBeats) {
        beats = opts.totalBeats - beatsSoFar;
      }

      // Rest or a note?
      if (Math.random() < REST_CHANCE) {
        melody.push({ midi: null, beats: beats });
      } else {
        // Stepwise motion (favored) with occasional leaps.
        var step = 0;
        var roll = Math.random();
        if (roll < 0.55) step = Math.random() < 0.5 ? -1 : 1;       // step
        else if (roll < 0.8) step = Math.random() < 0.5 ? -2 : 2;   // third
        else step = Math.random() < 0.5 ? -3 : 3;                   // leap
        idx += step;
        if (idx < 0) idx = 0;
        if (idx >= pool.length) idx = pool.length - 1;
        melody.push({ midi: pool[idx], beats: beats });
      }
      beatsSoFar += beats;
    }
    return melody;
  }

  function motifBeats(motif) {
    var s = 0;
    for (var i = 0; i < motif.length; i++) s += motif[i].beats;
    return s;
  }

  // ---- Playback (Tone.js) ------------------------------------------------
  var synth = null;
  var isPlaying = false;

  function ensureSynth() {
    if (!synth) {
      synth = new Tone.PolySynth(Tone.Synth, {
        maxPolyphony: 4,
        oscillator: { type: 'triangle' },
        envelope: { attack: 0.02, decay: 0.2, sustain: 0.4, release: 0.8 }
      }).toDestination();
      synth.volume.value = -8;
    }
    return synth;
  }

  // Play a melody. Tempo in BPM. onDone callback when finished.
  function playMelody(melody, bpm, onDone) {
    Tone.start(); // satisfy autoplay policy (called from a user gesture)
    ensureSynth();
    Tone.Transport.cancel(0);
    Tone.Transport.bpm.value = bpm;

    var now = 0; // beats
    var events = [];
    for (var i = 0; i < melody.length; i++) {
      var note = melody[i];
      if (note.midi !== null) {
        var name = midiToName(note.midi);
        events.push({ time: now, note: name, dur: note.beats });
      }
      now += note.beats;
    }

    var part = new Tone.Part(function (time, value) {
      synth.triggerAttackRelease(value.note, value.dur * 0.9, time);
    }, events.map(function (e) { return [e.time / bpm * 60, e]; }));

    part.start(0);
    isPlaying = true;
    Tone.Transport.start();

    var totalSec = (now / bpm) * 60 + 1;
    setTimeout(function () {
      stopMelody();
      if (onDone) onDone();
    }, totalSec * 1000);
  }

  function stopMelody() {
    if (!isPlaying) return;
    Tone.Transport.stop();
    Tone.Transport.cancel(0);
    isPlaying = false;
  }

  // ---- MIDI export (single track, format 0) -----------------------------
  // Minimal valid MIDI file. tempo + note on/off events.
  function writeVarLen(ticks) {
    var buffer = [ticks & 0x7f];
    while ((ticks >>= 7)) {
      buffer.unshift((ticks & 0x7f) | 0x80);
    }
    return buffer;
  }

  function melodyToMidi(melody, bpm, rootMidi) {
    var PPQ = 480;
    var microsPerQuarter = Math.round(60000000 / bpm);
    // Header chunk
    var header = strToBytes('MThd').concat([0, 0, 0, 6, 0, 0, 0, 1]);
    var tpqn = [(PPQ >> 8) & 0xff, PPQ & 0xff];
    header = header.concat(tpqn);

    // Track events: tempo, then note on/off.
    var events = [];
    events.push({ tick: 0, data: [0xff, 0x51, 0x03,
      (microsPerQuarter >> 16) & 0xff,
      (microsPerQuarter >> 8) & 0xff,
      microsPerQuarter & 0xff] });

    var cursor = 0;
    for (var i = 0; i < melody.length; i++) {
      var n = melody[i];
      var durTicks = Math.round(n.beats * PPQ);
      if (n.midi !== null) {
        events.push({ tick: cursor, data: [0x90, n.midi, 96] });          // note on
        events.push({ tick: cursor + durTicks, data: [0x80, n.midi, 0] }); // note off
      }
      cursor += durTicks;
    }

    // Sort by tick (stable-ish: note-offs at same tick as next on are fine).
    events.sort(function (a, b) { return a.tick - b.tick; });

    var bytes = [];
    var lastTick = 0;
    for (var e = 0; e < events.length; e++) {
      var delta = events[e].tick - lastTick;
      lastTick = events[e].tick;
      bytes = bytes.concat(writeVarLen(delta)).concat(events[e].data);
    }
    // End of track
    bytes = bytes.concat([0x00, 0xff, 0x2f, 0x00]);

    var trackLen = bytes.length;
    var trackHeader = strToBytes('MTrk').concat([
      (trackLen >> 24) & 0xff, (trackLen >> 16) & 0xff,
      (trackLen >> 8) & 0xff, trackLen & 0xff]);

    return new Uint8Array(header.concat(trackHeader).concat(bytes));
  }

  function strToBytes(s) {
    var out = [];
    for (var i = 0; i < s.length; i++) out.push(s.charCodeAt(i) & 0xff);
    return out;
  }

  function downloadMidi(filename, bytes) {
    var blob = new Blob([bytes], { type: 'audio/midi' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // ---- Expose API --------------------------------------------------------
  window.MelodyTool = {
    SCALES: SCALES,
    NOTE_NAMES: NOTE_NAMES,
    generateMelody: generateMelody,
    midiToName: midiToName,
    playMelody: playMelody,
    stopMelody: stopMelody,
    melodyToMidi: melodyToMidi,
    downloadMidi: downloadMidi
  };
})();
