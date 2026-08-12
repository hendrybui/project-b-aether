/* DJ Toolkit — shared frontend logic.
 * Handles: health badge, drag-drop, AudioMass job polling, MIDI upload.
 * No framework; plain DOM. Each page wires its own root element.
 */
(function () {
  "use strict";

  // ── Health badge ────────────────────────────────────────────────────
  const badge = document.getElementById("health-badge");
  async function refreshHealth() {
    if (!badge) return;
    try {
      const r = await fetch("/api/health");
      const j = await r.json();
      const ok = j.status === "ok";
      const am = j.components && j.components.audiomass;
      const midi = j.components && j.components.midi;
      badge.className = "badge ms-lg-3 " + (ok ? "badge-ok" : "badge-degraded");
      const amShort = am === "ok" ? "AudioMass ✓" : "AudioMass ✗";
      const midiShort = midi === "ok" ? "MIDI ✓" : "MIDI ✗";
      badge.textContent = amShort + " · " + midiShort;
      badge.title =
        "AudioMass: " + am + "\nMIDI (basic-pitch): " + midi;
    } catch (e) {
      badge.className = "badge ms-lg-3 badge-down";
      badge.textContent = "offline";
    }
  }
  refreshHealth();
  setInterval(refreshHealth, 20000);

  // ── Drag-drop helper ────────────────────────────────────────────────
  function wireDropzone(zone, input, onFile) {
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", () => {
      if (input.files[0]) onFile(input.files[0]);
    });
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
      })
    );
    zone.addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) onFile(f);
    });
  }

  // ── Stems + BPM/Key page ───────────────────────────────────────────
  const dz = document.getElementById("dropzone");
  if (dz) {
    const fileInput = document.getElementById("file-input");
    const progress = document.getElementById("progress");
    const stageEl = document.getElementById("progress-stage");
    const pctEl = document.getElementById("progress-pct");
    const barEl = document.getElementById("progress-bar");
    const msgEl = document.getElementById("progress-message");

    wireDropzone(dz, fileInput, startJob);

    let currentJobId = null;
    let pollTimer = null;

    async function startJob(file) {
      resetUI();
      progress.classList.remove("d-none");
      stageEl.textContent = "Uploading…";
      pctEl.textContent = "0%";
      msgEl.textContent = file.name;

      const fd = new FormData();
      fd.append("file", file);
      const modelSel = document.getElementById("model-select");
      if (modelSel) fd.append("model", modelSel.value);

      let resp;
      try {
        resp = await fetch("/api/analyze", { method: "POST", body: fd });
      } catch (e) {
        return fail("Upload failed: " + e.message);
      }
      if (!resp.ok && resp.status !== 201) {
        const j = await resp.json().catch(() => ({}));
        return fail(j.error || ("Upload rejected (" + resp.status + ")"));
      }
      const snap = await resp.json();
      currentJobId = snap.job_id;
      poll();
    }

    async function poll() {
      if (!currentJobId) return;
      try {
        const r = await fetch("/api/job/" + currentJobId);
        const j = await r.json();
        stageEl.textContent = j.stage || j.status;
        pctEl.textContent = Math.round(j.progress) + "%";
        barEl.style.width = j.progress + "%";
        msgEl.textContent = j.message || "";
        if (j.done) {
          barEl.classList.remove("progress-bar-animated", "progress-bar-striped");
          if (j.failed) {
            return fail(j.error || ("Job " + j.status + ": " + (j.message || "no detail")));
          }
          return loadResult(currentJobId);
        }
      } catch (e) {
        return fail("Lost connection to server: " + e.message);
      }
      pollTimer = setTimeout(poll, 1500);
    }

    async function loadResult(jobId) {
      try {
        const r = await fetch("/api/result/" + jobId);
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          return fail(j.error || "Could not load analysis result");
        }
        renderAnalysis(await r.json(), jobId);
      } catch (e) {
        fail("Could not load result: " + e.message);
      }
    }

    function renderAnalysis(a, jobId) {
      // Analysis panel
      document.getElementById("analysis-empty").classList.add("d-none");
      const res = document.getElementById("analysis-result");
      res.classList.remove("d-none");
      document.getElementById("stat-bpm").textContent =
        a.bpm != null ? Number(a.bpm).toFixed(1) : "—";
      const keyTxt = [a.key, a.scale].filter(Boolean).join(" ");
      document.getElementById("stat-key").textContent = keyTxt || "—";
      document.getElementById("stat-camelot").textContent = a.camelot || "—";
      document.getElementById("stat-confidence").textContent =
        a.confidence != null ? Number(a.confidence).toFixed(2) : "—";
      document.getElementById("stat-lufs").textContent =
        a.lufs_integrated != null ? Number(a.lufs_integrated).toFixed(1) + " LUFS" : "—";
      document.getElementById("stat-peak").textContent =
        a.peak_dbfs != null ? Number(a.peak_dbfs).toFixed(1) + " dB" : "—";
      document.getElementById("stat-duration").textContent = fmtDuration(a.duration_sec);
      const modelEl = document.getElementById("stat-model");
      if (modelEl) modelEl.textContent = a.model_label || a.model_id || "—";

      // Downloads panel
      document.getElementById("downloads-empty").classList.add("d-none");
      const dl = document.getElementById("downloads-result");
      dl.classList.remove("d-none");
      setHref("dl-instrumental", "/api/instrumental/" + jobId + "?format=mp3");
      setHref("dl-vocals", "/api/stem/" + jobId + "/vocals?format=mp3");
      setHref("dl-zip", "/api/stems-zip/" + jobId + "?format=wav");

      const stemList = document.getElementById("stem-list");
      stemList.innerHTML = "";
      (a.stems || []).forEach((s) => {
        // Each stem: two buttons (wav + mp3)
        const wrap = document.createElement("div");
        wrap.className = "d-flex";
        const wav = document.createElement("a");
        wav.className = "btn btn-sm btn-outline-light rounded-end-0";
        wav.href = "/api/stem/" + jobId + "/" + s + "?format=wav";
        wav.innerHTML = '<i class="bi bi-download me-1"></i>' + s;
        const mp3 = document.createElement("a");
        mp3.className = "btn btn-sm btn-outline-secondary rounded-start-0";
        mp3.href = "/api/stem/" + jobId + "/" + s + "?format=mp3";
        mp3.textContent = "mp3";
        wrap.appendChild(wav);
        wrap.appendChild(mp3);
        stemList.appendChild(wrap);
      });
    }

    function fail(msg) {
      if (pollTimer) clearTimeout(pollTimer);
      barEl.classList.remove("progress-bar-animated", "progress-bar-striped");
      barEl.style.width = "100%";
      barEl.classList.add("bg-danger");
      stageEl.textContent = "Error";
      msgEl.textContent = msg;
    }

    function resetUI() {
      if (pollTimer) clearTimeout(pollTimer);
      barEl.style.width = "0%";
      barEl.classList.remove("bg-danger");
      barEl.classList.add("progress-bar-striped", "progress-bar-animated");
      document.getElementById("analysis-empty").classList.remove("d-none");
      document.getElementById("analysis-result").classList.add("d-none");
      document.getElementById("downloads-empty").classList.remove("d-none");
      document.getElementById("downloads-result").classList.add("d-none");
    }
  }

  // ── MIDI page ──────────────────────────────────────────────────────
  const mdz = document.getElementById("midi-dropzone");
  if (mdz) {
    const minput = document.getElementById("midi-file-input");
    const mprogress = document.getElementById("midi-progress");
    const mstage = document.getElementById("midi-stage");
    const mEmpty = document.getElementById("midi-empty");
    const mResult = document.getElementById("midi-result");
    const dlMidi = document.getElementById("dl-midi");

    // The MIDI dropzone is a <form>; prevent it from submitting on click/drop
    // and wire it with the same clean helper as the stems page.
    mdz.addEventListener("submit", (e) => e.preventDefault());
    wireDropzone(mdz, minput, convert);

    async function convert(file) {
      mEmpty.classList.add("d-none");
      mResult.classList.add("d-none");
      mprogress.classList.remove("d-none");
      mstage.textContent = "Converting “" + file.name + "” to MIDI… (10–30s on CPU)";

      const fd = new FormData();
      fd.append("file", file);
      const onset = document.getElementById("onset").value;
      const frame = document.getElementById("frame").value;
      fd.append("onset", onset);
      fd.append("frame", frame);

      try {
        const r = await fetch("/api/to-midi?stats=1", { method: "POST", body: fd });
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          return midiFail(j.error || "Conversion failed (" + r.status + ")");
        }
        const j = await r.json();
        mprogress.classList.add("d-none");
        mResult.classList.remove("d-none");
        if (j.download_url) dlMidi.href = j.download_url;
        const s = j.stats || {};
        document.getElementById("midi-stats").textContent =
          [s.note_count != null ? s.note_count + " notes" : null,
           fmtDuration(s.duration_sec)].filter(Boolean).join(" · ") ||
          "Conversion complete.";
      } catch (e) {
        midiFail(e.message);
      }
    }

    function midiFail(msg) {
      mprogress.classList.add("d-none");
      mEmpty.classList.remove("d-none");
      mEmpty.querySelector("p").textContent = "Error: " + msg;
    }
  }

  // ── Utilities ──────────────────────────────────────────────────────
  function setHref(id, href) {
    const el = document.getElementById(id);
    if (el) el.href = href;
  }
  function fmtDuration(sec) {
    if (sec == null) return "—";
    const s = Math.round(Number(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + ":" + (r < 10 ? "0" : "") + r;
  }
})();
