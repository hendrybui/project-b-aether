/**
 * Shared in-browser score preview, export, and print helpers.
 *
 * Used by the Analyzer, MP3-to-Sheet, and Harmony pages to render MusicXML
 * with OpenSheetMusicDisplay (osmd.min.js), save the rendered score as an
 * SVG/PNG, and print it score-only. Exposed as window.ScorePreview.
 *
 * Expects a <div id="scoreContainer"> on the page (and optionally a wrapper
 * <div id="scorePreview"> that gets unhidden, as on the harmony page), plus
 * optional #svgLink / #pngLink / #printBtn controls.
 */
(function () {
    'use strict';

    var CONTAINER_ID = 'scoreContainer';
    var PANEL_ID = 'scorePreview';

    function el(id) {
        return document.getElementById(id);
    }

    function setStatus(container, html) {
        container.innerHTML = html;
    }

    function show(panel) {
        panel.classList.remove('hidden');
    }

    /** The rendered <svg> inside the score container, or null. */
    function svgNode() {
        var c = el(CONTAINER_ID);
        return c ? c.querySelector('svg') : null;
    }

    /**
     * Fetch a MusicXML url and render it with OSMD.
     *
     * Options:
     *   container: element to render into (defaults to #scoreContainer).
     *   panel:     element to unhide after a successful render (defaults to
     *              the container itself).
     *   onSuccess: callback invoked after a successful render.
     *
     * Resolves true on success, false if the renderer is missing or the
     * render failed (errors are shown inline and logged).
     */
    async function render(xmlUrl, opts) {
        opts = opts || {};
        var container = opts.container || el(CONTAINER_ID);
        var panel = opts.panel ? el(opts.panel) : container;
        if (!container || !panel) return false;
        if (!window.opensheetmusicdisplay) {
            show(panel);
            setStatus(container, '<div class="preview-error">Preview renderer failed to load.</div>');
            return false;
        }
        try {
            var res = await fetch(xmlUrl);
            if (!res.ok) throw new Error('Could not fetch the score (' + res.status + ')');
            var xml = await res.text();
            show(panel);
            // Status goes into the render container, never the wrapper panel
            // (a wrapper like #scorePreview holds the container as a child).
            setStatus(container, '<div class="preview-loading">⏳ Rendering score…</div>');
            // OSMD instances are single-use; build a fresh one per render.
            var osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay(
                container, { autoResize: true, drawTitle: true });
            osmd.EngravingRules.HideEmptyStaves = true;
            osmd.Zoom = 0.9;
            await osmd.load(xml);
            osmd.render();
            if (opts.onSuccess) opts.onSuccess();
            return true;
        } catch (e) {
            console.error('Preview render failed:', e.message);
            show(panel);
            setStatus(container, '<div class="preview-error">Preview unavailable: ' + e.message + '</div>');
            return false;
        }
    }

    /**
     * Show the hidden #svgLink / #pngLink buttons and wire them to save the
     * rendered score as <baseName>.svg / <baseName>.png.
     */
    function enableImageExports(baseName) {
        var name = baseName || 'score';
        var svgLink = el('svgLink');
        if (svgLink) {
            svgLink.style.display = 'inline-block';
            svgLink.onclick = function () { downloadSvg(name); };
        }
        var pngLink = el('pngLink');
        if (pngLink) {
            pngLink.style.display = 'inline-block';
            pngLink.onclick = function () { downloadPng(name); };
        }
    }

    function downloadSvg(name) {
        var svg = svgNode();
        if (!svg) return;
        var xml = new XMLSerializer().serializeToString(svg);
        var url = URL.createObjectURL(new Blob([xml], { type: 'image/svg+xml' }));
        var a = document.createElement('a');
        a.href = url;
        a.download = name + '.svg';
        a.click();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }

    function downloadPng(name) {
        var svg = svgNode();
        if (!svg) return;
        var w = parseFloat(svg.getAttribute('width')) || 800;
        var h = parseFloat(svg.getAttribute('height')) || 600;
        var scale = 2;
        var canvas = document.createElement('canvas');
        canvas.width = w * scale;
        canvas.height = h * scale;
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        var url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],
            { type: 'image/svg+xml;charset=utf-8' }));
        var img = new Image();
        img.onload = function () {
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            URL.revokeObjectURL(url);
            canvas.toBlob(function (b) {
                if (!b) return;
                var a = document.createElement('a');
                a.href = URL.createObjectURL(b);
                a.download = name + '.png';
                a.click();
                setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
            }, 'image/png');
        };
        img.onerror = function () {
            URL.revokeObjectURL(url);
            console.error('PNG export failed: SVG rasterize failed');
        };
        img.src = url;
    }

    /** Attach window.print() to #printBtn so the score prints score-only. */
    function wirePrint() {
        var btn = el('printBtn');
        if (btn) btn.onclick = function () { window.print(); };
    }

    window.ScorePreview = {
        render: render,
        enableImageExports: enableImageExports,
        downloadSvg: downloadSvg,
        downloadPng: downloadPng,
        wirePrint: wirePrint,
    };
})();
