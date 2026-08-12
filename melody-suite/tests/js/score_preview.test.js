'use strict';

/**
 * Unit tests for static/score_preview.js, exercised in jsdom.
 *
 * Run directly:  node --test score_preview.test.js   (or `npm test`)
 * Run via pytest:  venv/bin/python -m pytest tests/ -q
 *
 * jsdom does not implement fetch / URL.createObjectURL / canvas rendering /
 * window.print, so those surfaces are stubbed to record what the helper does.
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const HELPER_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', '..', 'static', 'score_preview.js'), 'utf8'
);

/**
 * Install the jsdom gaps (fetch, object URLs, print, OSMD, canvas, Image,
 * anchor clicks) on a window. `fetchImpl` is optional; the default succeeds
 * with a fake MusicXML payload.
 */
function installStubs(window, record, { osmd = true, fetchImpl } = {}) {
  window.fetch = fetchImpl || (async (url) => {
    record.fetchUrl = url;
    return { ok: true, status: 200, text: async () => '<score-partwise/>' };
  });

  // URL.createObjectURL / revokeObjectURL are not implemented in jsdom.
  let blobCounter = 0;
  window.URL.createObjectURL = () => 'blob:fake-' + ++blobCounter;
  window.URL.revokeObjectURL = () => {};

  // window.print is a no-op in jsdom; record calls.
  window.print = () => { record.prints += 1; };

  // Fake OSMD: on render() it injects a real <svg> into its container, which
  // is exactly what svgNode()/exports depend on.
  class FakeOsmd {
    constructor(container) { this.container = container; this.EngravingRules = {}; }
    async load(xml) { record.osmdLoads.push(xml); }
    render() {
      this.container.innerHTML =
        '<svg width="120" height="40" viewBox="0 0 120 40"><text>x</text></svg>';
    }
  }
  if (osmd) window.opensheetmusicdisplay = { OpenSheetMusicDisplay: FakeOsmd };

  // Canvas is not rendered by jsdom; record the drawing calls and fake the
  // PNG payload.
  window.HTMLCanvasElement.prototype.getContext = () => ({
    fillStyle: '',
    fillRect: function () { record.canvas.push(['fillRect', this.fillStyle]); },
    drawImage: function () { record.canvas.push(['drawImage']); },
  });
  window.HTMLCanvasElement.prototype.toBlob = function (cb) {
    cb(new Uint8Array([137, 80, 78, 71])); // PNG magic bytes
  };

  // Fake Image: firing onload synchronously when src is set lets the PNG
  // export's callback chain complete deterministically.
  window.Image = class {
    set src(v) { this._src = v; if (this.onload) this.onload(); }
    get src() { return this._src; }
  };

  // Record downloads while still letting the real click() dispatch onclick
  // handlers (so export buttons fire). jsdom logs "navigation not
  // implemented" for blob: hrefs but does not throw.
  const origClick = window.HTMLAnchorElement.prototype.click;
  window.HTMLAnchorElement.prototype.click = function () {
    record.downloads.push({ download: this.download || '', href: this.href || '' });
    origClick.call(this);
  };
}

/**
 * Build a fresh jsdom window with the elements the helper expects and the
 * jsdom gaps stubbed out. Returns { window, record } where record collects
 * downloads, print calls, and canvas usage.
 */
function makeWindow(opts = {}) {
  const dom = new JSDOM(
    `<!DOCTYPE html><html><body>
      <div id="scorePreview" class="score-preview hidden">
        <div id="scoreContainer"></div>
      </div>
      <a id="svgLink" style="display:none"></a>
      <a id="pngLink" style="display:none"></a>
      <button id="printBtn"></button>
    </body></html>`,
    { url: 'http://localhost/', runScripts: 'outside-only' }
  );
  const { window } = dom;
  const record = { downloads: [], prints: 0, canvas: [], osmdLoads: [] };
  installStubs(window, record, opts);

  // Load the helper into the window.
  window.eval(HELPER_SOURCE);
  return { window, record, dom };
}

function container(window) {
  return window.document.getElementById('scoreContainer');
}
function panel(window) {
  return window.document.getElementById('scorePreview');
}

test('render() draws the score and reports success', async () => {
  const { window, record } = makeWindow();
  const ok = await window.ScorePreview.render('/out/x.xml');
  assert.strictEqual(ok, true);
  assert.strictEqual(record.fetchUrl, '/out/x.xml');
  assert.strictEqual(record.osmdLoads.length, 1);
  assert.strictEqual(record.osmdLoads[0], '<score-partwise/>');
  assert.ok(container(window).querySelector('svg'), 'score svg should be in the container');
  assert.strictEqual(container(window).querySelector('.preview-error'), null);
  assert.strictEqual(container(window).querySelector('.preview-loading'), null);
  window.dom && window.dom.window.close();
});

test('render() unhides a wrapper panel without replacing the container', async () => {
  // The harmony page wraps #scoreContainer inside #scorePreview; the status
  // message must go into the container, never the wrapper (a regression test
  // for the bug where the loading div deleted the container).
  const { window } = makeWindow();
  assert.ok(panel(window).classList.contains('hidden'));
  const ok = await window.ScorePreview.render('/out/x.xml', { panel: 'scorePreview' });
  assert.strictEqual(ok, true);
  assert.ok(!panel(window).classList.contains('hidden'), 'panel should be unhidden');
  assert.ok(container(window), 'container must still exist');
  assert.ok(container(window).querySelector('svg'), 'svg must render inside the container');
});

test('render() shows an inline error when the renderer is missing', async () => {
  const { window } = makeWindow({ osmd: false });
  const ok = await window.ScorePreview.render('/out/x.xml');
  assert.strictEqual(ok, false);
  const err = container(window).querySelector('.preview-error');
  assert.ok(err, 'error message should be shown');
  assert.match(err.textContent, /failed to load/i);
});

test('render() shows an inline error when the fetch fails', async () => {
  const { window } = makeWindow();
  window.fetch = async () => ({ ok: false, status: 500 });
  const ok = await window.ScorePreview.render('/out/missing.xml');
  assert.strictEqual(ok, false);
  assert.match(container(window).querySelector('.preview-error').textContent, /500/);
});

test('render() shows an inline error when the XML is unparseable', async () => {
  const { window } = makeWindow();
  class BrokenOsmd {
    constructor() { this.EngravingRules = {}; }
    async load() { throw new Error('bad musicxml'); }
  }
  window.opensheetmusicdisplay = { OpenSheetMusicDisplay: BrokenOsmd };
  const ok = await window.ScorePreview.render('/out/bad.xml');
  assert.strictEqual(ok, false);
  assert.match(container(window).querySelector('.preview-error').textContent, /bad musicxml/);
});

test('enableImageExports() reveals and wires the SVG/PNG buttons', async () => {
  const { window, record } = makeWindow();
  await window.ScorePreview.render('/out/x.xml');
  window.ScorePreview.enableImageExports('transcription');

  const svgLink = window.document.getElementById('svgLink');
  const pngLink = window.document.getElementById('pngLink');
  assert.strictEqual(svgLink.style.display, 'inline-block');
  assert.strictEqual(pngLink.style.display, 'inline-block');

  // SVG download: serialized <svg> → anchor with transcription.svg.
  svgLink.click();
  const svg = record.downloads.find((d) => d.download === 'transcription.svg');
  assert.ok(svg, 'svg download should fire');
  assert.match(svg.href, /^blob:/);

  // PNG download: rasterize via canvas → anchor with transcription.png.
  pngLink.click();
  const png = record.downloads.find((d) => d.download === 'transcription.png');
  assert.ok(png, 'png download should fire');
  assert.ok(record.canvas.some((c) => c[0] === 'fillRect'), 'canvas should be filled white');
  assert.ok(record.canvas.some((c) => c[0] === 'drawImage'), 'svg should be drawn to canvas');
});

test('downloadSvg() serializes the rendered score', async () => {
  const { window, record } = makeWindow();
  await window.ScorePreview.render('/out/x.xml');
  window.ScorePreview.downloadSvg('score');
  assert.strictEqual(record.downloads[0].download, 'score.svg');
  assert.match(record.downloads[0].href, /^blob:/);
});

test('downloadPng() refuses to run without a rendered score', () => {
  const { window, record } = makeWindow();
  window.ScorePreview.downloadPng('score'); // no render happened
  assert.strictEqual(record.downloads.length, 0);
});

test('wirePrint() attaches window.print to the print button', () => {
  const { window, record } = makeWindow();
  window.ScorePreview.wirePrint();
  const btn = window.document.getElementById('printBtn');
  btn.click();
  assert.strictEqual(record.prints, 1);
});

test('wirePrint() tolerates a page without a print button', () => {
  const { window } = makeWindow();
  window.document.getElementById('printBtn').remove();
  window.ScorePreview.wirePrint(); // must not throw
});

// ── Harmony page layout-switch flow (end to end) ──────────────────────────
// The harmony page's inline script is loaded as-is and driven through a real
// layout <select> change, so the full path is exercised: change event →
// POST /api/harmony/xml → update download link → render the new layout.

const HARMONY_HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'templates', 'harmony.html'), 'utf8'
);

function extractInlineScript(html) {
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  let last = null;
  while ((m = re.exec(html))) last = m[1];
  return last;
}

const HARMONY_INLINE = extractInlineScript(HARMONY_HTML);
if (!HARMONY_INLINE) throw new Error('could not extract harmony inline script');

const HARMONY_FIXTURE = `<!DOCTYPE html><html><body>
  <input id="melodyInput" value="C4, E4, G4">
  <select id="durationSelect"><option value="1">q</option></select>
  <select id="keySelect"><option value="C major">C major</option></select>
  <small id="styleHint"></small>
  <input type="radio" name="style" value="conservative">
  <input type="radio" name="style" value="balanced" checked>
  <input type="radio" name="style" value="adventurous">
  <button id="generateBtn"></button>
  <div id="status" class="hidden"></div>
  <div id="results" class="hidden">
    <select id="soloSelect"><option value="all">all</option></select>
    <div id="voicingDisplay"></div>
    <button id="playBtn"></button>
    <button id="saveArrangement"></button>
    <button id="sendToEditor"></button>
    <select id="xmlLayout">
      <option value="parts4">Four parts (SATB)</option>
      <option value="grand">Grand staff (piano)</option>
    </select>
    <a id="midiLink" style="display:none"></a>
    <a id="xmlLink" style="display:none"></a>
    <button id="exportCsv"></button>
    <button id="printBtn"></button>
    <button id="previewBtn"></button>
    <div id="scorePreview" class="score-preview hidden">
      <div id="scoreContainer"></div>
    </div>
  </div>
  <h3 id="shelfTitle"></h3>
  <div id="shelf"></div>
</body></html>`;

function makeHarmonyWindow() {
  const dom = new JSDOM(HARMONY_FIXTURE, {
    url: 'http://localhost/tools/melody-sheet/multi-part-harmony-generation',
    runScripts: 'outside-only',
  });
  const { window } = dom;
  const record = {
    downloads: [], prints: 0, canvas: [], osmdLoads: [],
    harmonyXmlPosts: [], xmlFetches: [],
  };
  installStubs(window, record, {
    fetchImpl: async (url, opts) => {
      if (url === '/api/harmony/xml') {
        record.harmonyXmlPosts.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ xml_url: '/output/harmony_x.xml' }) };
      }
      record.xmlFetches.push(url);
      return { ok: true, status: 200, text: async () => '<score-partwise/>' };
    },
  });
  window.eval(HELPER_SOURCE);
  // The inline script declares `let lastVoices`/`lastKey` in eval scope, so
  // append a hook inside the same eval to seed and read that state.
  window.eval(HARMONY_INLINE + ';window.__hook={setState:function(v,k){lastVoices=v;lastKey=k;},voices:function(){return lastVoices;},key:function(){return lastKey;}};');
  return { window, record, dom };
}

const VOICES_SAMPLE = [
  { soprano: 60, alto: 64, tenor: 55, bass: 48, duration: 1.0 },
  { soprano: 62, alto: 67, tenor: 57, bass: 50, duration: 1.0 },
];

async function switchLayout(window, value) {
  const layout = window.document.getElementById('xmlLayout');
  layout.value = value;
  layout.dispatchEvent(new window.Event('change', { bubbles: true }));
  // refreshXmlLink is async (fetch + OSMD render); give the chain a beat.
  await new Promise((r) => setTimeout(r, 100));
}

function harmonyAssertions(window, record, expectedLayout) {
  // One POST carried the seeded arrangement, the new layout, and the key.
  assert.strictEqual(record.harmonyXmlPosts.length, 1);
  assert.strictEqual(record.harmonyXmlPosts[0].layout, expectedLayout);
  assert.strictEqual(record.harmonyXmlPosts[0].key, 'G major');
  assert.deepStrictEqual(record.harmonyXmlPosts[0].voices, VOICES_SAMPLE);
  // The score re-rendered inside the wrapper panel.
  const panel = window.document.getElementById('scorePreview');
  const container = window.document.getElementById('scoreContainer');
  assert.ok(!panel.classList.contains('hidden'), 'panel should be unhidden');
  assert.ok(container.querySelector('svg'), 'score should be re-rendered');
  assert.ok(record.xmlFetches.includes('/output/harmony_x.xml'));
}

test('harmony: layout switch re-renders the score end to end', async () => {
  const { window, record } = makeHarmonyWindow();
  window.__hook.setState(VOICES_SAMPLE, 'G major');

  const layout = window.document.getElementById('xmlLayout');
  assert.strictEqual(layout.value, 'parts4');
  await switchLayout(window, 'grand');

  harmonyAssertions(window, record, 'grand');
  // The layout-switch path (refreshXmlLink) also points the download link
  // at the served MusicXML.
  const xmlLink = window.document.getElementById('xmlLink');
  assert.strictEqual(xmlLink.style.display, 'inline-block');
  assert.ok(xmlLink.href.endsWith('/output/harmony_x.xml'));
  assert.strictEqual(window.__hook.key(), 'G major');
});

test('harmony: preview re-render button refreshes the score', async () => {
  const { window, record } = makeHarmonyWindow();
  window.__hook.setState(VOICES_SAMPLE, 'G major');

  window.document.getElementById('previewBtn').click();
  await new Promise((r) => setTimeout(r, 100));

  // The re-render button posts the current layout (default parts4) and
  // re-renders without ever regenerating the arrangement.
  harmonyAssertions(window, record, 'parts4');
});

// ── Analyzer + MP3-to-Sheet transcription flow (end to end) ───────────────
// Both pages share the analysis-panel include (which defines
// getAnalysisPrefs) and the demo → transcribe → sheet-music pipeline. The
// panel script and page script are concatenated into one eval, matching the
// real page where they share global scope.

const ANALYSIS_PANEL_HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'templates', '_analysis_panel.html'), 'utf8'
);
const ANALYZER_HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'templates', 'analyzer.html'), 'utf8'
);
const MP3SHEET_HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'templates', 'mp3_to_sheet.html'), 'utf8'
);

const ANALYSIS_PANEL_INLINE = extractInlineScript(ANALYSIS_PANEL_HTML);
const ANALYZER_INLINE = extractInlineScript(ANALYZER_HTML);
const MP3SHEET_INLINE = extractInlineScript(MP3SHEET_HTML);
for (const [name, script] of [
  ['_analysis_panel', ANALYSIS_PANEL_INLINE],
  ['analyzer', ANALYZER_INLINE],
  ['mp3_to_sheet', MP3SHEET_INLINE],
]) {
  if (!script) throw new Error(`could not extract ${name} inline script`);
}

// Elements getAnalysisPrefs() reads from the shared analysis panel.
const ANALYSIS_PREFS_FIXTURE = `
  <input type="radio" name="instrument" value="balanced" checked>
  <input type="radio" name="instrument" value="piano">
  <input type="radio" name="noise" value="off">
  <input type="radio" name="noise" value="smart" checked>
  <input type="radio" name="noise" value="studio">
  <small id="noiseHint"></small>
  <input type="checkbox" id="tempoEstimation" checked>
  <input type="checkbox" id="customPitchEnable">
  <select id="lowestNote"><option value="48">C3</option></select>
  <select id="highestNote"><option value="76">E5</option></select>
  <input type="radio" name="sensitivity" value="relaxed" checked>
  <input type="radio" name="sensitivity" value="balanced">
  <input type="radio" name="sensitivity" value="strict">
`;

const TRANS_RESULT = {
  notes: [
    { pitch: 60, pitch_name: 'C4', start: 0.0, duration: 0.5, confidence: 0.9 },
    { pitch: 64, pitch_name: 'E4', start: 0.5, duration: 0.5, confidence: 0.9 },
    { pitch: 67, pitch_name: 'G4', start: 1.0, duration: 0.5, confidence: 0.9 },
    { pitch: 72, pitch_name: 'C5', start: 1.5, duration: 0.5, confidence: 0.9 },
    { pitch: 67, pitch_name: 'G4', start: 2.0, duration: 0.5, confidence: 0.9 },
    { pitch: 64, pitch_name: 'E4', start: 2.5, duration: 0.5, confidence: 0.9 },
    { pitch: 62, pitch_name: 'D4', start: 3.0, duration: 0.5, confidence: 0.9 },
    { pitch: 60, pitch_name: 'C4', start: 3.5, duration: 0.5, confidence: 0.9 },
  ],
  tempo: 117,
  duration: 5.0,
  num_notes: 8,
  pitch_range: [60, 84],
  key: 'C major',
  key_confidence: 29.4,
  midi_url: '/output/trans_abc.mid',
};

const TRANSFER_FIXTURE_COMMON = `
  <div id="dropZone"><input type="file" id="fileInput" hidden></div>
  <button id="browseBtn"></button>
  <button id="demoBtn"></button>
  <button id="analyzeBtn" disabled></button>
  <div id="status" class="hidden"></div>
`;

const ANALYZER_FIXTURE_EXTRA = TRANSFER_FIXTURE_COMMON + `
  <div id="results" class="hidden">
    <span id="resNoteCount">—</span><span id="resTempo">—</span>
    <span id="resDuration">—</span><span id="resRange">—</span>
    <span id="resKey">—</span>
    <div id="noteStrip"></div>
    <button id="viewSheetBtn"></button><button id="playBtn"></button>
    <a id="midiLink" style="display:none"></a>
    <a id="svgLink" style="display:none"></a>
    <a id="pngLink" style="display:none"></a>
    <button id="printBtn"></button>
    <div id="scoreContainer"></div>
  </div>`;

const MP3SHEET_FIXTURE_EXTRA = TRANSFER_FIXTURE_COMMON + `
  <div id="results" class="hidden">
    <span id="resNotes">—</span><span id="resTempo">—</span>
    <span id="resKey">—</span><span id="resMeasures">—</span>
    <div id="scoreContainer" class="score-preview hidden"></div>
    <div id="staffDisplay"></div>
    <button id="playBtn"></button>
    <a id="midiLink" style="display:none"></a>
    <a id="xmlLink" style="display:none"></a>
    <a id="pdfLink" style="display:none"></a>
    <button id="printBtn"></button>
  </div>`;

function makeTranscribeWindow(extraHtml, pageInline) {
  const dom = new JSDOM(
    `<!DOCTYPE html><html><body>${ANALYSIS_PREFS_FIXTURE}${extraHtml}</body></html>`,
    { url: 'http://localhost/', runScripts: 'outside-only' }
  );
  const { window } = dom;
  const record = {
    downloads: [], prints: 0, canvas: [], osmdLoads: [],
    demoFetches: 0, transcribePosts: [], sheetMusicPosts: [], xmlFetches: [],
  };
  installStubs(window, record, {
    fetchImpl: async (url, opts) => {
      if (url === '/api/demo-audio') {
        record.demoFetches += 1;
        return { ok: true, status: 200, blob: async () => new window.Blob(['fake-wav']) };
      }
      if (url === '/api/transcribe') {
        const fd = opts.body;
        record.transcribePosts.push({
          audio: fd.get('audio'),
          instrument: fd.get('instrument'),
          noise: fd.get('noise'),
          tempo: fd.get('tempo'),
          sensitivity: fd.get('sensitivity'),
          lowest: fd.get('lowest'),
          highest: fd.get('highest'),
        });
        return { ok: true, status: 200, json: async () => TRANS_RESULT };
      }
      if (url === '/api/sheet-music') {
        record.sheetMusicPosts.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ xml_url: '/output/sheet_x.xml' }) };
      }
      record.xmlFetches.push(url);
      return { ok: true, status: 200, text: async () => '<score-partwise/>' };
    },
  });
  window.eval(HELPER_SOURCE);
  // Panel script defines getAnalysisPrefs; page script uses it — same eval
  // scope, as on the real page. A hook exposes the page's selectedFile.
  window.eval(ANALYSIS_PANEL_INLINE + '\n' + pageInline +
    ';window.__hook={file:function(){return typeof selectedFile!=="undefined"?selectedFile:null;}};');
  return { window, record, dom };
}

async function runDemoAndAnalyze(window) {
  window.document.getElementById('demoBtn').click();
  await new Promise((r) => setTimeout(r, 50));
  window.document.getElementById('analyzeBtn').click();
  await new Promise((r) => setTimeout(r, 100));
}

test('analyzer: demo → analyze renders the score and enables exports', async () => {
  const { window, record } = makeTranscribeWindow(ANALYZER_FIXTURE_EXTRA, ANALYZER_INLINE);
  await runDemoAndAnalyze(window);

  assert.ok(window.__hook.file() instanceof window.File, 'demo file should be loaded');
  // Transcribe POST carried the prefs and the uploaded demo audio.
  assert.strictEqual(record.transcribePosts.length, 1);
  assert.ok(record.transcribePosts[0].audio instanceof window.File);
  assert.strictEqual(record.transcribePosts[0].instrument, 'balanced');
  assert.strictEqual(record.transcribePosts[0].sensitivity, 'relaxed');
  assert.strictEqual(record.transcribePosts[0].lowest, null); // custom range off
  // Sheet-music POST forwarded notes + tempo + the detected key.
  assert.strictEqual(record.sheetMusicPosts.length, 1);
  assert.deepStrictEqual(record.sheetMusicPosts[0].notes, TRANS_RESULT.notes);
  assert.strictEqual(record.sheetMusicPosts[0].tempo, 117);
  assert.strictEqual(record.sheetMusicPosts[0].key, 'C major');
  // Result cards populated.
  const $ = (id) => window.document.getElementById(id);
  assert.strictEqual($('resNoteCount').textContent, '8');
  assert.strictEqual($('resTempo').textContent, '117 BPM');
  assert.strictEqual($('resKey').textContent, 'C major');
  assert.strictEqual($('resRange').textContent, 'C4–C6');
  assert.strictEqual($('noteStrip').querySelectorAll('.note-pill').length, 8);
  assert.strictEqual($('midiLink').style.display, 'inline-block');
  assert.ok($('midiLink').href.endsWith('/output/trans_abc.mid'));
  // Score rendered; SVG/PNG exports revealed; print wired.
  assert.ok($('scoreContainer').querySelector('svg'));
  assert.strictEqual($('svgLink').style.display, 'inline-block');
  assert.strictEqual($('pngLink').style.display, 'inline-block');
  $('printBtn').click();
  assert.strictEqual(record.prints, 1);
});

test('mp3-to-sheet: demo → convert renders the score and links exports', async () => {
  const { window, record } = makeTranscribeWindow(MP3SHEET_FIXTURE_EXTRA, MP3SHEET_INLINE);
  await runDemoAndAnalyze(window);

  assert.strictEqual(record.transcribePosts.length, 1);
  assert.strictEqual(record.sheetMusicPosts.length, 1);
  assert.strictEqual(record.sheetMusicPosts[0].key, 'C major');
  const $ = (id) => window.document.getElementById(id);
  assert.strictEqual($('resNotes').textContent, '8');
  assert.strictEqual($('resTempo').textContent, '117 BPM');
  assert.strictEqual($('resKey').textContent, 'C major');
  assert.strictEqual($('resMeasures').textContent, '2');
  assert.strictEqual($('staffDisplay').querySelectorAll('.staff-line').length, 1);
  assert.strictEqual($('midiLink').style.display, 'inline-block');
  assert.strictEqual($('xmlLink').style.display, 'inline-block');
  assert.strictEqual($('pdfLink').style.display, 'inline-block');
  assert.ok($('scoreContainer').querySelector('svg'));
  assert.strictEqual(record.xmlFetches[0], '/output/sheet_x.xml');
  $('printBtn').click();
  assert.strictEqual(record.prints, 1);
});
