// ---- core.js
/* Steelworks Media Optimiser — pure plugin logic (no figma.* calls).
 * Shared by the plugin (dist/code.js) and the browser test page (tests/js).
 */
var SMOCore = (function () {
  'use strict';

  var GAP = 16;
  var GRID = 20; // 20x20 sample points per layer for coverage

  // ---------------------------------------------------------------- geometry
  // Figma transforms are [[a, c, tx], [b, d, ty]] mapping local -> parent/absolute.
  function apply(m, x, y) {
    return { x: m[0][0] * x + m[0][1] * y + m[0][2], y: m[1][0] * x + m[1][1] * y + m[1][2] };
  }
  function invert(m) {
    var a = m[0][0], c = m[0][1], tx = m[0][2], b = m[1][0], d = m[1][1], ty = m[1][2];
    var det = a * d - b * c;
    if (Math.abs(det) < 1e-12) return null;
    return [[d / det, -c / det, (c * ty - d * tx) / det], [-b / det, a / det, (b * tx - a * ty) / det]];
  }
  function axisScale(m) {
    return { x: Math.hypot(m[0][0], m[1][0]), y: Math.hypot(m[0][1], m[1][1]) };
  }
  function isRotated(m) {
    return Math.abs(m[1][0]) > 1e-6 || Math.abs(m[0][1]) > 1e-6;
  }
  function inRoundedRect(x, y, w, h, r) {
    if (x < 0 || y < 0 || x > w || y > h) return false;
    r = Math.min(r || 0, w / 2, h / 2);
    if (r <= 0) return true;
    var cx = x < r ? r : (x > w - r ? w - r : x);
    var cy = y < r ? r : (y > h - r ? h - r : y);
    var dx = x - cx, dy = y - cy;
    return dx * dx + dy * dy <= r * r + 1e-9;
  }
  function inBox(p, b) {
    return p.x >= b.x - 1e-6 && p.y >= b.y - 1e-6 && p.x <= b.x + b.width + 1e-6 && p.y <= b.y + b.height + 1e-6;
  }

  // ---------------------------------------------------- display / need size
  /** Size (slide px) at which the WHOLE image is effectively displayed, so a
   * resize to 1.5x of it keeps whatever part of the image is visible sharp.
   * paint: {scaleMode, imageTransform, scalingFactor}; node size w x h;
   * scale: absolute scale of the node (usually 1); img: natural px size. */
  function requiredSize(paint, w, h, imgW, imgH, scale) {
    var sx = (scale && scale.x) || 1, sy = (scale && scale.y) || 1;
    var W = w * sx, H = h * sy;
    var mode = paint.scaleMode || 'FILL';
    if (!imgW || !imgH) return { w: W, h: H };
    if (mode === 'FIT') {
      var f = Math.min(W / imgW, H / imgH);
      return { w: imgW * f, h: imgH * f };
    }
    if (mode === 'TILE') {
      var k = paint.scalingFactor || 1;
      return { w: imgW * k * sx, h: imgH * k * sy };
    }
    if (mode === 'CROP' || mode === 'STRETCH') {
      var t = paint.imageTransform;
      if (t) {
        // imageTransform maps node space (0..1) into image space (0..1):
        // the visible fraction of the image across the node is |column|.
        var fx = Math.hypot(t[0][0], t[1][0]) || 1;
        var fy = Math.hypot(t[0][1], t[1][1]) || 1;
        return { w: W / fx, h: H / fy };
      }
      return { w: W, h: H };
    }
    var s = Math.max(W / imgW, H / imgH); // FILL
    return { w: imgW * s, h: imgH * s };
  }

  // ------------------------------------------------------------- image sniff
  /** {format, opaque} from the first bytes. opaque=true only when the format
   * cannot carry transparency (conservative: RGBA PNGs count as transparent). */
  function sniff(bytes) {
    var b = bytes;
    if (!b || b.length < 12) return { format: 'unknown', opaque: false };
    if (b[0] === 0xFF && b[1] === 0xD8) return { format: 'jpeg', opaque: true };
    if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4E && b[3] === 0x47) {
      var colorType = b[25];
      var opaque = colorType === 0 || colorType === 2;
      // a tRNS chunk anywhere before IDAT makes it transparent
      for (var i = 33; i < Math.min(b.length - 8, 4096); ) {
        var len = (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3];
        var type = String.fromCharCode(b[i + 4], b[i + 5], b[i + 6], b[i + 7]);
        if (type === 'tRNS') { opaque = false; break; }
        if (type === 'IDAT' || len < 0) break;
        i += 12 + len;
      }
      return { format: 'png', opaque: opaque };
    }
    if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return { format: 'gif', opaque: false };
    if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45) {
      var chunk = String.fromCharCode(b[12], b[13], b[14], b[15]);
      if (chunk === 'VP8 ') return { format: 'webp', opaque: true };
      if (chunk === 'VP8X') return { format: 'webp', opaque: (b[20] & 0x10) === 0 };
      return { format: 'webp', opaque: false };
    }
    return { format: 'unknown', opaque: false };
  }

  // ---------------------------------------------------------------- coverage
  /** items: paint-ordered (bottom -> top) list of
   *   {id, m: absoluteTransform, w, h, r: cornerRadius, clips: [abs boxes], opaque: bool, media: bool}
   * slide: abs box {x,y,width,height}.
   * Returns {id: {state: 'covered'|'offslide'|'visible', by: [ids], visibleSamples}} for media items. */
  function coverage(items, slide, n) {
    n = n || GRID;
    var out = {};
    var inv = items.map(function (it) { return invert(it.m); });
    function visibleAt(it, p) {
      if (!inBox(p, slide)) return false;
      for (var c = 0; c < (it.clips || []).length; c++) if (!inBox(p, it.clips[c])) return false;
      return true;
    }
    function covers(j, p) {
      var it = items[j], iv = inv[j];
      if (!it.opaque || !iv) return false;
      var l = apply(iv, p.x, p.y);
      if (!inRoundedRect(l.x, l.y, it.w, it.h, it.r)) return false;
      return visibleAt(it, p);
    }
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it.media) continue;
      var samples = 0, coveredCount = 0, by = {};
      // 20x20 grid of cell centres, plus a ring just inside the edges so a
      // thin strip peeking out from under the cover isn't missed.
      var pts = [];
      for (var gy = 0; gy < n; gy++) for (var gx = 0; gx < n; gx++) pts.push([(gx + 0.5) / n * it.w, (gy + 0.5) / n * it.h]);
      var ex = Math.min(0.5, it.w / 4), ey = Math.min(0.5, it.h / 4);
      for (var k = 0; k <= n; k++) {
        var fx = ex + (it.w - 2 * ex) * k / n, fy = ey + (it.h - 2 * ey) * k / n;
        pts.push([fx, ey], [fx, it.h - ey], [ex, fy], [it.w - ex, fy]);
      }
      for (var s = 0; s < pts.length; s++) {
        {
          var lx = pts[s][0], ly = pts[s][1];
          if (!inRoundedRect(lx, ly, it.w, it.h, it.r)) continue;
          var p = apply(it.m, lx, ly);
          if (!visibleAt(it, p)) continue;
          samples++;
          var hit = -1;
          for (var j = items.length - 1; j > i; j--) { if (covers(j, p)) { hit = j; break; } }
          if (hit >= 0) { coveredCount++; by[items[hit].id] = true; }
        }
      }
      var state = samples === 0 ? 'offslide' : (coveredCount === samples ? 'covered' : 'visible');
      out[it.id] = { state: state, by: Object.keys(by), visibleSamples: samples };
    }
    return out;
  }

  // ------------------------------------------------------------------ grids
  function cluster(values, tol) {
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var groups = [];
    sorted.forEach(function (v) {
      var g = groups[groups.length - 1];
      if (g && Math.abs(v - g[g.length - 1]) <= tol) g.push(v); else groups.push([v]);
    });
    return groups.map(function (g) { return g[0]; });
  }
  function indexOfCluster(starts, v) {
    var best = -1, bd = Infinity;
    for (var i = 0; i < starts.length; i++) {
      var d = Math.abs(starts[i] - v);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
  function overlaps(a, b) {
    return a.x < b.x + b.w - 0.5 && b.x < a.x + a.w - 0.5 && a.y < b.y + b.h - 0.5 && b.y < a.y + a.h - 0.5;
  }

  /** items: [{id, x, y, w, h, rotated}] siblings in one container.
   * Returns null when this isn't a clean thumbnail grid, else
   * {rows, cols, gapsX, gapsY, even, plan: [{id, x, y, w, h}]}. */
  function analyseGrid(items, opts) {
    opts = opts || {};
    var gap = opts.gap == null ? GAP : opts.gap;
    var sizeTol = opts.sizeTol == null ? 2 : opts.sizeTol;
    var alignTol = opts.alignTol == null ? 2 : opts.alignTol;
    if (!items || items.length < 3) return null;
    if (items.some(function (t) { return t.rotated; })) return null;
    var w0 = items[0].w, h0 = items[0].h;
    // Collage check: every cell the same size.
    if (items.some(function (t) { return Math.abs(t.w - w0) > sizeTol || Math.abs(t.h - h0) > sizeTol; })) return null;
    for (var i = 0; i < items.length; i++) for (var j = i + 1; j < items.length; j++)
      if (overlaps(items[i], items[j])) return null;
    var xs = cluster(items.map(function (t) { return t.x; }), alignTol);
    var ys = cluster(items.map(function (t) { return t.y; }), alignTol);
    var cols = xs.length, rows = ys.length;
    if (cols * rows < items.length) return null;
    var cells = {};
    for (var k = 0; k < items.length; k++) {
      var c = indexOfCluster(xs, items[k].x), r = indexOfCluster(ys, items[k].y);
      if (c < 0 || r < 0 || Math.abs(xs[c] - items[k].x) > alignTol || Math.abs(ys[r] - items[k].y) > alignTol) return null;
      if (cells[r + ',' + c]) return null;
      cells[r + ',' + c] = items[k];
    }
    // Must be a real grid, not scattered: at least 2 per row on average, and
    // the gaps look like gutters (smaller than a cell).
    var avgW = items.reduce(function (s, t) { return s + t.w; }, 0) / items.length;
    var avgH = items.reduce(function (s, t) { return s + t.h; }, 0) / items.length;
    var gapsX = [], gapsY = [];
    for (var a = 0; a + 1 < cols; a++) gapsX.push(xs[a + 1] - (xs[a] + avgW));
    for (var b = 0; b + 1 < rows; b++) gapsY.push(ys[b + 1] - (ys[b] + avgH));
    var all = gapsX.concat(gapsY);
    if (all.some(function (g) { return g < -0.5 || g > Math.min(avgW, avgH) * 0.6; })) return null;
    var even = all.every(function (g) { return Math.abs(g - gap) <= 0.5; }) &&
      items.every(function (t) { return Math.abs(t.w - w0) <= 0.01 && Math.abs(t.h - h0) <= 0.01; });
    // Keep the outer edges, redistribute.
    var left = Math.min.apply(null, items.map(function (t) { return t.x; }));
    var top = Math.min.apply(null, items.map(function (t) { return t.y; }));
    var right = Math.max.apply(null, items.map(function (t) { return t.x + t.w; }));
    var bottom = Math.max.apply(null, items.map(function (t) { return t.y + t.h; }));
    var cw = (right - left - gap * (cols - 1)) / cols;
    var ch = (bottom - top - gap * (rows - 1)) / rows;
    if (cw <= 1 || ch <= 1) return null;
    var r2 = function (v) { return Math.round(v * 100) / 100; };
    var plan = [];
    Object.keys(cells).forEach(function (key) {
      var rc = key.split(',').map(Number), t = cells[key];
      plan.push({ id: t.id, x: r2(left + rc[1] * (cw + gap)), y: r2(top + rc[0] * (ch + gap)), w: r2(cw), h: r2(ch) });
    });
    return { rows: rows, cols: cols, gapsX: gapsX.map(r2), gapsY: gapsY.map(r2), even: even,
             bounds: { x: left, y: top, w: right - left, h: bottom - top }, plan: plan };
  }

  // -------------------------------------------------------------- alignment
  /* Layers are given in slide space: {id, slide, name, type, x, y, w, h, text, rotated, fullBleed}.
   * W, H: slide size. */

  function modeOf(values, bucket, minCount) {
    var counts = {}, best = null, bestN = 0;
    values.forEach(function (v) {
      var k = Math.round(v / bucket) * bucket;
      counts[k] = (counts[k] || 0) + 1;
      if (counts[k] > bestN || (counts[k] === bestN && k < best)) { best = k; bestN = counts[k]; }
    });
    if (bestN < (minCount || 1)) return null;
    // refine: the exact value most used inside the winning bucket
    var exact = {}, ev = null, en = 0;
    values.forEach(function (v) {
      if (Math.abs(v - best) <= bucket / 2) { var r = Math.round(v * 100) / 100; exact[r] = (exact[r] || 0) + 1; if (exact[r] > en) { en = exact[r]; ev = r; } }
    });
    return { value: ev, count: bestN };
  }

  /** Layout guides from Figma layout grids if a slide has them, otherwise
   * inferred from where content most often starts and ends. */
  function detectGuides(layers, W, H, figmaGrids) {
    var g = { W: W, H: H, left: null, right: null, top: null, bottom: null, columns: [], source: 'inferred' };
    var cols = (figmaGrids || []).filter(function (lg) { return lg.pattern === 'COLUMNS' && lg.alignment === 'STRETCH' && lg.visible !== false; })[0];
    if (cols) {
      g.source = 'layout grid';
      g.left = g.right = cols.offset || 0;
      var n = cols.count || 1, gut = cols.gutterSize || 0, cw = (W - 2 * g.left - gut * (n - 1)) / n;
      for (var i = 0; i < n; i++) { var x0 = g.left + i * (cw + gut); g.columns.push(x0, x0 + cw); }
    }
    var rows = (figmaGrids || []).filter(function (lg) { return lg.pattern === 'ROWS' && lg.alignment === 'STRETCH' && lg.visible !== false; })[0];
    if (rows) { g.top = g.bottom = rows.offset || 0; }
    var free = layers.filter(function (l) { return !l.fullBleed && !l.rotated; });
    var slidesWith = function (pred) { var s = {}; free.forEach(function (l) { if (pred(l)) s[l.slide] = 1; }); return Object.keys(s).length; };
    if (g.left == null) {
      var m = modeOf(free.filter(function (l) { return l.x > 4 && l.x < W * 0.25; }).map(function (l) { return l.x; }), 8, 3);
      if (m && slidesWith(function (l) { return Math.abs(l.x - m.value) <= 4; }) >= 2) g.left = m.value;
    }
    if (g.right == null) {
      var mr = modeOf(free.filter(function (l) { var r = W - l.x - l.w; return r > 4 && r < W * 0.25; }).map(function (l) { return W - l.x - l.w; }), 8, 3);
      if (mr && slidesWith(function (l) { return Math.abs(W - l.x - l.w - mr.value) <= 4; }) >= 2) g.right = mr.value;
    }
    if (g.left != null && g.right == null) g.right = g.left;
    if (g.right != null && g.left == null) g.left = g.right;
    if (g.top == null) {
      var mt = modeOf(free.filter(function (l) { return l.y > 4 && l.y < H * 0.2; }).map(function (l) { return l.y; }), 8, 3);
      if (mt) g.top = mt.value;
    }
    if (g.bottom == null) {
      var mb = modeOf(free.filter(function (l) { var b = H - l.y - l.h; return b > 4 && b < H * 0.2; }).map(function (l) { return H - l.y - l.h; }), 8, 3);
      if (mb) g.bottom = mb.value;
    }
    return g;
  }

  function roleKey(l) {
    var base = (l.name || '').toLowerCase().replace(/\d+/g, '#').replace(/\s+/g, ' ').trim();
    return l.type + '|' + base;
  }

  /** Plan: [{id, slide, name, role, reason, before:{x,y,w,h}, after:{x,y,w,h}}] */
  function alignmentPlan(layers, W, H, guides, opts) {
    opts = opts || {};
    var snapTol = opts.snapTol == null ? 12 : opts.snapTol;
    var hfTol = opts.hfTol == null ? 40 : opts.hfTol;
    var g = guides;
    var plan = {}, out = [];
    function propose(l, nx, ny, role, reason) {
      if (Math.abs(nx - l.x) < 0.5 && Math.abs(ny - l.y) < 0.5) return;
      if (plan[l.id]) return;
      plan[l.id] = true;
      out.push({ id: l.id, slide: l.slide, name: l.name, type: l.type, role: role, reason: reason,
        before: { x: l.x, y: l.y, w: l.w, h: l.h }, after: { x: Math.round(nx * 100) / 100, y: Math.round(ny * 100) / 100, w: l.w, h: l.h } });
    }
    var usable = layers.filter(function (l) { return !l.rotated && !l.fullBleed; });

    // 1. Headers & footers: same kind of layer repeated across slides in the top/bottom band.
    var groups = {};
    usable.forEach(function (l) {
      var cy = l.y + l.h / 2, band = cy < H * 0.15 ? 'header' : cy > H * 0.85 ? 'footer' : null;
      if (!band) return;
      var k = band + '|' + roleKey(l);
      (groups[k] = groups[k] || { band: band, items: [] }).items.push(l);
    });
    Object.keys(groups).forEach(function (k) {
      var gr = groups[k], items = gr.items;
      var slides = {}; items.forEach(function (l) { slides[l.slide] = 1; });
      if (Object.keys(slides).length < 3) return;
      // Anchor on whichever edge the group agrees on most: left, right or centre.
      var cand = [
        { edge: 'left', m: modeOf(items.map(function (l) { return l.x; }), 2, 2) },
        { edge: 'right', m: modeOf(items.map(function (l) { return l.x + l.w; }), 2, 2) },
        { edge: 'center', m: modeOf(items.map(function (l) { return l.x + l.w / 2; }), 2, 2) }
      ].filter(function (c) { return c.m; }).sort(function (a, b) { return b.m.count - a.m.count; });
      var my = modeOf(items.map(function (l) { return gr.band === 'footer' ? l.y + l.h : l.y; }), 2, 2);
      if (!cand.length || !my) return;
      var ax = cand[0];
      items.forEach(function (l) {
        var nx = ax.edge === 'left' ? ax.m.value : ax.edge === 'right' ? ax.m.value - l.w : ax.m.value - l.w / 2;
        var ny = gr.band === 'footer' ? my.value - l.h : my.value;
        if (Math.abs(nx - l.x) > hfTol || Math.abs(ny - l.y) > hfTol) return; // intentionally different
        propose(l, nx, ny, gr.band, 'Out of line with the same ' + gr.band + ' on other slides');
      });
    });

    // 2. Snap everything else (text first) that's a hair off a guide.
    var xs = [], ys = [];
    if (g.left != null) xs.push({ v: g.left, edge: 'left', label: g.left + ' px margin' });
    if (g.right != null) xs.push({ v: W - g.right, edge: 'right', label: 'right margin' });
    g.columns.forEach(function (c, i) { xs.push({ v: c, edge: i % 2 ? 'right' : 'left', label: 'column ' + (Math.floor(i / 2) + 1) }); });
    if (g.top != null) ys.push({ v: g.top, edge: 'top', label: 'top margin' });
    if (g.bottom != null) ys.push({ v: H - g.bottom, edge: 'bottom', label: 'bottom margin' });
    var ordered = usable.slice().sort(function (a, b) { return (a.type === 'TEXT' ? 0 : 1) - (b.type === 'TEXT' ? 0 : 1); });
    ordered.forEach(function (l) {
      if (plan[l.id]) return;
      var best = null;
      xs.forEach(function (t) {
        var cur = t.edge === 'left' ? l.x : l.x + l.w, d = t.v - cur;
        if (Math.abs(d) > 0.5 && Math.abs(d) <= snapTol && (!best || Math.abs(d) < Math.abs(best.d))) best = { d: d, t: t };
      });
      var cx = l.x + l.w / 2;
      if (!best && Math.abs(W / 2 - cx) > 0.5 && Math.abs(W / 2 - cx) <= snapTol / 2 && l.w < W * 0.9) best = { d: W / 2 - cx, t: { label: 'slide centre' } };
      var bestY = null;
      var cyl = l.y + l.h / 2;
      ys.forEach(function (t) {
        // Top/bottom lines only apply to layers that live in the header/footer bands.
        if (t.edge === 'top' ? cyl > H * 0.15 : cyl < H * 0.85) return;
        var cur = t.edge === 'top' ? l.y : l.y + l.h, d = t.v - cur;
        if (Math.abs(d) > 0.5 && Math.abs(d) <= snapTol && (!bestY || Math.abs(d) < Math.abs(bestY.d))) bestY = { d: d, t: t };
      });
      if (!best && !bestY) return;
      var why = [];
      if (best) why.push(Math.abs(Math.round(best.d * 10) / 10) + ' px off the ' + best.t.label);
      if (bestY) why.push(Math.abs(Math.round(bestY.d * 10) / 10) + ' px off the ' + bestY.t.label);
      propose(l, l.x + (best ? best.d : 0), l.y + (bestY ? bestY.d : 0), l.type === 'TEXT' ? 'text' : 'layer', why.join(', '));
    });
    return out.sort(function (a, b) { return a.slide - b.slide || a.before.y - b.before.y; });
  }

  // ------------------------------------------------------------------- misc
  function fmtBytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  return {
    GAP: GAP, GRID: GRID, apply: apply, invert: invert, axisScale: axisScale, isRotated: isRotated,
    inRoundedRect: inRoundedRect, requiredSize: requiredSize, sniff: sniff, coverage: coverage,
    analyseGrid: analyseGrid, fmtBytes: fmtBytes, detectGuides: detectGuides, alignmentPlan: alignmentPlan, modeOf: modeOf
  };
})();
if (typeof module !== 'undefined') module.exports = SMOCore;

// ---- main.js
/* Steelworks Media Optimiser — Figma plugin main thread.
 * The plugin is the app's hands inside Figma. The desktop app sends commands
 * over the localhost bridge (via ui.html); everything that touches the
 * document happens here.
 */
/* global SMOCore, figma, __html__ */
var PLUGIN_VERSION = '1.0.1';
var MEDIA = { IMAGE: true, VIDEO: true };

figma.showUI(__html__, { width: 340, height: 480, themeColors: true, title: 'Steelworks Media Optimiser' });
// documentchange events need every page loaded when documentAccess is dynamic-page.
if (typeof figma.loadAllPagesAsync === 'function') figma.loadAllPagesAsync();

// ------------------------------------------------------------ UI messaging
var uiSeq = 0, uiPending = {};
function callUI(method, params) {
  return new Promise(function (resolve, reject) {
    var id = ++uiSeq;
    uiPending[id] = { resolve: resolve, reject: reject };
    figma.ui.postMessage({ type: 'call', id: id, method: method, params: params });
  });
}
function progress(rpcId, data) { figma.ui.postMessage({ type: 'rpc-progress', id: rpcId, data: data }); }

var guided = null; // pending guided video placement

figma.ui.onmessage = function (msg) {
  if (!msg || !msg.type) return;
  if (msg.type === 'reply') {
    var p = uiPending[msg.id]; delete uiPending[msg.id];
    if (p) { if (msg.error) p.reject(new Error(msg.error)); else p.resolve(msg.result); }
  } else if (msg.type === 'rpc') {
    var h = handlers[msg.method];
    if (!h) { figma.ui.postMessage({ type: 'rpc-result', id: msg.id, error: 'Unknown command ' + msg.method }); return; }
    Promise.resolve().then(function () { return h(msg.params || {}, msg.id); }).then(function (result) {
      figma.ui.postMessage({ type: 'rpc-result', id: msg.id, result: result });
    }, function (err) {
      figma.ui.postMessage({ type: 'rpc-result', id: msg.id, error: String((err && err.message) || err) });
    });
  } else if (msg.type === 'storage-get') {
    figma.clientStorage.getAsync(msg.key).then(function (v) { figma.ui.postMessage({ type: 'storage', key: msg.key, value: v }); });
  } else if (msg.type === 'storage-set') {
    figma.clientStorage.setAsync(msg.key, msg.value);
  } else if (msg.type === 'guided-skip' && guided) {
    guided.finish(null, 'skipped');
  } else if (msg.type === 'guided-use-selection' && guided) {
    var sel = figma.currentPage.selection[0];
    if (sel && sel.id !== guided.origId) guided.finish(sel, 'selection');
    else figma.notify('Select the new video layer first.');
  } else if (msg.type === 'notify') {
    figma.notify(msg.text, { timeout: 2500 });
  }
};

// ----------------------------------------------------------------- helpers
function clone(v) { return JSON.parse(JSON.stringify(v)); }
function fillsOf(n) { return Array.isArray(n.fills) ? n.fills : []; }
function mediaFills(n) {
  var out = [];
  fillsOf(n).forEach(function (p, i) { if (MEDIA[p.type]) out.push({ paint: p, index: i }); });
  return out;
}
function paintHash(p) { return p.type === 'VIDEO' ? p.videoHash : p.imageHash; }
function radius(n, which) {
  if (typeof n.cornerRadius === 'number') return n.cornerRadius;
  var rs = ['topLeftRadius', 'topRightRadius', 'bottomLeftRadius', 'bottomRightRadius']
    .map(function (k) { return typeof n[k] === 'number' ? n[k] : 0; });
  return which === 'max' ? Math.max.apply(null, rs) : Math.min.apply(null, rs);
}
var RECT_SHAPED = { RECTANGLE: true, FRAME: true, COMPONENT: true, INSTANCE: true };

async function getSlides() {
  if (figma.loadAllPagesAsync) await figma.loadAllPagesAsync();
  if (figma.editorType === 'slides' && typeof figma.getSlideGrid === 'function') {
    var out = [];
    figma.getSlideGrid().forEach(function (row) { row.forEach(function (s) { out.push(s); }); });
    return out;
  }
  return figma.currentPage.children.filter(function (n) {
    return n.type === 'FRAME' || n.type === 'COMPONENT' || n.type === 'SECTION';
  });
}

async function nodeById(id) {
  return figma.getNodeByIdAsync ? await figma.getNodeByIdAsync(id) : figma.getNodeById(id);
}

async function runPool(items, size, fn) {
  var i = 0;
  async function worker() { while (i < items.length) { var k = i++; await fn(items[k], k); } }
  var ws = []; for (var w = 0; w < Math.min(size, items.length); w++) ws.push(worker());
  await Promise.all(ws);
}

// -------------------------------------------------------------- inventory
async function inventory(params, rpcId) {
  var slides = await getSlides();
  var media = [], images = {}, videos = {}, issues = [], grids = [];
  var perSlideItems = [];

  for (var si = 0; si < slides.length; si++) {
    var slide = slides[si];
    progress(rpcId, { phase: 'scan', done: si, total: slides.length, label: 'Reading slide ' + (si + 1) + ' of ' + slides.length });
    var items = [];
    var slideBox = slide.absoluteBoundingBox || { x: slide.x, y: slide.y, width: slide.width, height: slide.height };

    (function rec(node, clips, hiddenBy, opacity, masked) {
      var hidden = hiddenBy || (node !== slide && node.visible === false ? node : null);
      var op = opacity * (node.opacity == null ? 1 : node.opacity);
      if (node !== slide) {
        var mfs = mediaFills(node);
        var visibleMedia = mfs.filter(function (f) { return f.paint.visible !== false && (f.paint.opacity == null || f.paint.opacity > 0); });
        var m = node.absoluteTransform, sc = SMOCore.axisScale(m);
        mfs.forEach(function (f) {
          var p = f.paint, hash = paintHash(p);
          var entry = {
            id: node.id, name: node.name, type: node.type, slideId: slide.id, slideIndex: si + 1, slideName: slide.name,
            fillIndex: f.index, kind: p.type === 'VIDEO' ? 'video' : 'image', hash: hash,
            scaleMode: p.scaleMode, imageTransform: p.imageTransform || null, scalingFactor: p.scalingFactor || null,
            w: node.width, h: node.height, display: { w: Math.round(node.width * sc.x), h: Math.round(node.height * sc.y) },
            hasChildren: 'children' in node && node.children.length > 0,
            parentId: node.parent && node.parent.id, paint: clone(p)
          };
          media.push(entry);
          var bucket = p.type === 'VIDEO' ? videos : images;
          if (hash) {
            if (!bucket[hash]) bucket[hash] = { hash: hash, uses: [] };
            bucket[hash].uses.push({ nodeId: node.id, fillIndex: f.index, slideIndex: si + 1, name: node.name,
              scaleMode: p.scaleMode, imageTransform: p.imageTransform || null, scalingFactor: p.scalingFactor || null,
              w: node.width, h: node.height, scale: sc });
          }
          var removeKind = entry.hasChildren ? 'fill' : 'node';
          if (hidden || op === 0) {
            issues.push({ type: 'hidden', nodeId: node.id, fillIndex: f.index, hash: hash, kind: entry.kind, removeKind: removeKind,
              slideIndex: si + 1, name: node.name,
              reason: op === 0 && !hidden ? 'Opacity is 0%' : (hidden === node ? 'Hidden layer' : 'Inside hidden layer “' + hidden.name + '”') });
          } else if (p.visible === false || p.opacity === 0) {
            issues.push({ type: 'disabled', nodeId: node.id, fillIndex: f.index, hash: hash, kind: entry.kind, removeKind: 'fill',
              slideIndex: si + 1, name: node.name, reason: p.visible === false ? 'Fill is switched off' : 'Fill opacity is 0%' });
          }
        });
        var isMask = !!node.isMask;
        var blur = (node.effects || []).some(function (e) { return e.visible !== false && e.type === 'LAYER_BLUR'; });
        var normal = !node.blendMode || node.blendMode === 'NORMAL' || node.blendMode === 'PASS_THROUGH';
        items.push({
          id: node.id, m: m, w: node.width, h: node.height, clips: clips,
          r: radius(node, 'min'), rCover: radius(node, 'max'),
          media: visibleMedia.length > 0 && !hidden && op > 0,
          coverCandidate: !hidden && op === 1 && normal && !blur && !isMask && !masked && !!RECT_SHAPED[node.type] &&
            visibleMedia.length > 0,
          coverFills: visibleMedia.map(function (f) { return f.paint; })
        });
      }
      if ('children' in node) {
        var next = node.clipsContent && node.absoluteBoundingBox ? clips.concat([node.absoluteBoundingBox]) : clips;
        var maskSeen = false;
        node.children.forEach(function (c) {
          rec(c, next, hidden, op, masked || maskSeen);
          if (c.isMask) maskSeen = true;
        });
        if (params.grids !== false && node.type !== 'GROUP' && (!node.layoutMode || node.layoutMode === 'NONE')) {
          findGrids(node, slide, si + 1, grids);
        }
      }
    })(slide, slide.clipsContent === false ? [] : [slideBox], null, 1, false);
    perSlideItems.push({ slide: slide, box: slideBox, items: items });
  }

  // Read image bytes (pixel size, format, opacity) and hand them to the app.
  var hashes = Object.keys(images);
  var done = 0;
  await runPool(hashes, 4, async function (hash) {
    var info = images[hash];
    try {
      var img = figma.getImageByHash(hash);
      if (!img) throw new Error('image not loaded');
      var size = await img.getSizeAsync();
      var bytes = await img.getBytesAsync();
      var sn = SMOCore.sniff(bytes);
      info.width = size.width; info.height = size.height; info.bytes = bytes.length;
      info.format = sn.format; info.opaque = sn.opaque;
      info.uses.forEach(function (u) {
        u.need = SMOCore.requiredSize(u, u.w, u.h, size.width, size.height, u.scale);
      });
      if (params.upload !== false) await callUI('upload', { hash: hash, bytes: bytes });
    } catch (e) {
      info.error = String(e.message || e);
    }
    done++;
    progress(rpcId, { phase: 'read', done: done, total: hashes.length, label: 'Reading images ' + done + ' of ' + hashes.length });
  });

  // Coverage per slide (needs image opacity, so after the byte pass).
  perSlideItems.forEach(function (s, idx) {
    var items = s.items.map(function (it) {
      var opaque = it.coverCandidate && it.coverFills.some(function (p) {
        if ((p.opacity != null && p.opacity < 1) || (p.blendMode && p.blendMode !== 'NORMAL')) return false;
        if (p.scaleMode === 'FIT') return false;
        if (p.type === 'VIDEO') return true;
        var info = images[p.imageHash];
        return !!(info && info.opaque);
      });
      return { id: it.id, m: it.m, w: it.w, h: it.h, r: opaque ? it.rCover : it.r, clips: it.clips, media: it.media, opaque: opaque };
    });
    var cov = SMOCore.coverage(items, s.box);
    Object.keys(cov).forEach(function (id) {
      var c = cov[id];
      if (c.state === 'visible') return;
      media.filter(function (m) { return m.id === id; }).forEach(function (m) {
        if (issues.some(function (i) { return i.nodeId === id && i.fillIndex === m.fillIndex; })) return;
        issues.push({ type: c.state, nodeId: id, fillIndex: m.fillIndex, hash: m.hash, kind: m.kind,
          removeKind: m.hasChildren ? 'fill' : 'node', slideIndex: idx + 1, name: m.name, coveredBy: c.by,
          reason: c.state === 'offslide' ? 'Completely outside the slide' : 'Completely covered by media above it' });
      });
    });
  });

  return {
    file: fileInfo(), slides: slides.map(function (s, i) { return { id: s.id, name: s.name, index: i + 1 }; }),
    media: media, images: images, videos: videos, issues: issues, grids: grids
  };
}

function findGrids(container, slide, slideIndex, out) {
  var kids = container.children.filter(function (c) {
    return c.visible !== false && mediaFills(c).some(function (f) { return f.paint.visible !== false; }) &&
      c.layoutPositioning !== 'ABSOLUTE';
  });
  if (kids.length < 3) return;
  // Separate groups of equal-sized thumbnails within the same container.
  var groups = {};
  kids.forEach(function (c) {
    var k = Math.round(c.width / 4) + 'x' + Math.round(c.height / 4);
    (groups[k] = groups[k] || []).push(c);
  });
  Object.keys(groups).forEach(function (k) {
    var g = groups[k];
    if (g.length < 3) return;
    var res = SMOCore.analyseGrid(g.map(function (c) {
      return { id: c.id, x: c.x, y: c.y, w: c.width, h: c.height, rotated: Math.abs(c.rotation || 0) > 0.01 };
    }));
    if (!res || res.even) return;
    out.push({ id: container.id + '/' + k, parentId: container.id, parentName: container.name, slideIndex: slideIndex,
      slideId: slide.id, rows: res.rows, cols: res.cols, gapsX: res.gapsX, gapsY: res.gapsY, bounds: res.bounds,
      count: g.length, plan: res.plan,
      before: g.map(function (c) { return { id: c.id, x: c.x, y: c.y, w: c.width, h: c.height }; }) });
  });
}

function fileInfo() {
  return { fileKey: figma.fileKey || null, name: figma.root.name, editorType: figma.editorType,
    user: figma.currentUser ? figma.currentUser.name : null, pluginVersion: PLUGIN_VERSION,
    canCreateVideo: typeof figma.createVideoAsync === 'function' };
}

// ------------------------------------------------------------ the commands
var handlers = {
  hello: async function () { return fileInfo(); },

  inventory: inventory,

  saveVersion: async function (p) {
    if (typeof figma.saveVersionHistoryAsync !== 'function') return { saved: false };
    try { var v = await figma.saveVersionHistoryAsync(p.title || 'Before Media Optimiser', p.description || ''); return { saved: true, id: v && v.id }; }
    catch (e) { return { saved: false, error: String(e.message || e) }; }
  },

  focus: async function (p) {
    var n = await nodeById(p.nodeId);
    if (!n) throw new Error('That layer no longer exists.');
    var page = n; while (page && page.type !== 'PAGE') page = page.parent;
    if (page && figma.currentPage !== page && figma.setCurrentPageAsync) await figma.setCurrentPageAsync(page);
    figma.currentPage.selection = [n];
    figma.viewport.scrollAndZoomIntoView([n]);
    return { ok: true };
  },

  /** items: [{oldHash, url, nodes:[{nodeId, fillIndex}]}] */
  swapImages: async function (p, rpcId) {
    var log = [], done = 0;
    for (var i = 0; i < p.items.length; i++) {
      var it = p.items[i];
      try {
        var bytes = await callUI('download', { url: it.url });
        var img = figma.createImage(bytes);
        for (var k = 0; k < it.nodes.length; k++) {
          var ref = it.nodes[k], node = await nodeById(ref.nodeId);
          if (!node) { log.push({ type: 'image', nodeId: ref.nodeId, ok: false, error: 'Layer not found' }); continue; }
          var fills = clone(fillsOf(node)), changed = false;
          fills.forEach(function (f, idx) {
            if (f.type === 'IMAGE' && f.imageHash === it.oldHash) { f.imageHash = img.hash; changed = true; }
          });
          if (changed) {
            node.fills = fills;
            log.push({ type: 'image', nodeId: node.id, name: node.name, oldHash: it.oldHash, newHash: img.hash, ok: true });
          }
        }
      } catch (e) {
        log.push({ type: 'image', oldHash: it.oldHash, ok: false, error: String(e.message || e) });
      }
      done++;
      progress(rpcId, { phase: 'swap', done: done, total: p.items.length, label: 'Swapping image ' + done + ' of ' + p.items.length });
    }
    return { log: log };
  },

  /** items: [{nodeId, removeKind: 'node'|'fill', fillIndex, hash}] */
  removeItems: async function (p) {
    var log = [];
    // Fills first (indices stay valid), highest index first.
    var items = p.items.slice().sort(function (a, b) { return (b.fillIndex || 0) - (a.fillIndex || 0); });
    for (var i = 0; i < items.length; i++) {
      var it = items[i], node = await nodeById(it.nodeId);
      if (!node || node.removed) { log.push({ type: 'remove', nodeId: it.nodeId, ok: false, error: 'Already gone' }); continue; }
      var entry = { type: 'remove', nodeId: node.id, name: node.name, removeKind: it.removeKind,
        parentId: node.parent && node.parent.id, index: node.parent ? node.parent.children.indexOf(node) : -1,
        x: node.x, y: node.y, w: node.width, h: node.height };
      try {
        if (it.removeKind === 'fill') {
          var fills = clone(fillsOf(node));
          entry.paint = fills[it.fillIndex];
          if (fills[it.fillIndex] && paintHash(fills[it.fillIndex]) === it.hash) fills.splice(it.fillIndex, 1);
          else fills = fills.filter(function (f) { return !(MEDIA[f.type] && paintHash(f) === it.hash); });
          node.fills = fills;
        } else {
          node.remove();
        }
        entry.ok = true;
      } catch (e) { entry.ok = false; entry.error = String(e.message || e); }
      log.push(entry);
    }
    return { log: log };
  },

  /** grids: [{plan:[{id,x,y,w,h}]}] */
  applySpacing: async function (p) {
    var log = [];
    for (var g = 0; g < p.grids.length; g++) {
      var plan = p.grids[g].plan;
      for (var i = 0; i < plan.length; i++) {
        var t = plan[i], n = await nodeById(t.id);
        if (!n) continue;
        var before = { x: n.x, y: n.y, w: n.width, h: n.height };
        try {
          if ((Math.abs(n.width - t.w) > 0.001 || Math.abs(n.height - t.h) > 0.001) && typeof n.resize === 'function' && n.type !== 'GROUP') n.resize(t.w, t.h);
          n.x = t.x; n.y = t.y;
          log.push({ type: 'move', nodeId: n.id, name: n.name, before: before, after: { x: n.x, y: n.y, w: n.width, h: n.height }, ok: true });
        } catch (e) { log.push({ type: 'move', nodeId: n.id, ok: false, error: String(e.message || e) }); }
      }
    }
    return { log: log };
  },

  /** Collect layers in slide coordinates and propose alignment fixes. */
  alignmentScan: async function (p) {
    var slides = await getSlides();
    var layers = [], W = 1920, H = 1080, grids = null;
    slides.forEach(function (slide, si) {
      W = slide.width; H = slide.height;
      if (!grids && Array.isArray(slide.layoutGrids) && slide.layoutGrids.length) grids = clone(slide.layoutGrids);
      var o = slide.absoluteTransform;
      (function rec(node, depth, blocked) {
        ('children' in node ? node.children : []).forEach(function (c) {
          if (c.visible === false || c.locked) return;
          var inAuto = node.layoutMode && node.layoutMode !== 'NONE' && c.layoutPositioning !== 'ABSOLUTE';
          var rot = Math.abs(c.rotation || 0) > 0.01;
          var ok = !blocked && !inAuto && (depth === 0 || c.type === 'TEXT');
          if (ok) {
            var m = c.absoluteTransform;
            var x = m[0][2] - o[0][2], y = m[1][2] - o[1][2];
            layers.push({ id: c.id, slide: si + 1, name: c.name, type: c.type, x: x, y: y, w: c.width, h: c.height, rotated: rot,
              fullBleed: (x <= 1 && x + c.width >= W - 1) || (y <= 1 && y + c.height >= H - 1 && c.width >= W * 0.5) });
          }
          if (c.type !== 'INSTANCE' && c.type !== 'TEXT') rec(c, depth + 1, blocked || rot || inAuto);
        });
      })(slide, 0, false);
    });
    var guides = SMOCore.detectGuides(layers, W, H, grids);
    var plan = SMOCore.alignmentPlan(layers, W, H, guides, { snapTol: p.snapTol, hfTol: p.hfTol });
    return { guides: guides, plan: plan, layers: layers.length };
  },

  /** moves: [{id, dx, dy}] relative moves, safe for nested layers. */
  applyMoves: async function (p) {
    var log = [];
    for (var i = 0; i < p.moves.length; i++) {
      var t = p.moves[i], n = await nodeById(t.id);
      if (!n) { log.push({ type: 'move', nodeId: t.id, ok: false, error: 'Layer not found' }); continue; }
      var before = { x: n.x, y: n.y, w: n.width, h: n.height };
      try {
        n.x = n.x + (t.dx || 0); n.y = n.y + (t.dy || 0);
        log.push({ type: 'move', nodeId: n.id, name: n.name, before: before, after: { x: n.x, y: n.y, w: n.width, h: n.height }, ok: true });
      } catch (e) { log.push({ type: 'move', nodeId: n.id, ok: false, error: String(e.message || e) }); }
    }
    return { log: log };
  },

  /** Undo the reversible parts: image swaps and spacing moves. */
  revert: async function (p) {
    var res = [];
    var entries = p.entries.slice().reverse();
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i], n = await nodeById(e.nodeId);
      if (!n) { res.push({ nodeId: e.nodeId, ok: false, error: 'Layer not found' }); continue; }
      try {
        if (e.type === 'image') {
          var fills = clone(fillsOf(n));
          fills.forEach(function (f) { if (f.type === 'IMAGE' && f.imageHash === e.newHash) f.imageHash = e.oldHash; });
          n.fills = fills;
        } else if (e.type === 'move') {
          if (typeof n.resize === 'function' && n.type !== 'GROUP') n.resize(e.before.w, e.before.h);
          n.x = e.before.x; n.y = e.before.y;
        } else { res.push({ nodeId: e.nodeId, ok: false, error: 'Use Figma version history for this change' }); continue; }
        res.push({ nodeId: e.nodeId, ok: true });
      } catch (err) { res.push({ nodeId: e.nodeId, ok: false, error: String(err.message || err) }); }
    }
    return { results: res };
  },

  /** Replace one video/GIF layer. {nodeId, fillIndex, url, fileName, kind, playback} */
  placeVideo: async function (p, rpcId) {
    var node = await nodeById(p.nodeId);
    if (!node) throw new Error('That layer no longer exists.');
    var origPaint = clone(fillsOf(node)[p.fillIndex] || {});
    // Fast path: Figma builds that can create videos from a plugin.
    if (typeof figma.createVideoAsync === 'function' && !p.forceGuided) {
      try {
        var bytes = await callUI('download', { url: p.url });
        var video = await figma.createVideoAsync(bytes);
        var fills = clone(fillsOf(node));
        fills[p.fillIndex] = videoPaint(origPaint, video.hash, p);
        node.fills = fills;
        return { mode: 'auto', nodeId: node.id, newHash: video.hash, playbackApplied: playbackKnown(fills[p.fillIndex]) };
      } catch (e) {
        // fall through to the guided path
        p.autoError = String(e.message || e);
      }
    }
    progress(rpcId, { phase: 'guided', label: 'Waiting for the file to be dragged in' });
    return await guidedPlace(node, origPaint, p);
  },

  cancelGuided: async function () { if (guided) guided.finish(null, 'cancelled'); return { ok: true }; }
};

var PLAYBACK_KEYS = ['autoplay', 'loop', 'muted', 'showControls', 'controls', 'playbackSettings', 'startTime', 'endTime'];
function playbackKnown(paint) { return PLAYBACK_KEYS.some(function (k) { return paint && k in paint; }); }

function videoPaint(orig, hash, p) {
  var vp = { type: 'VIDEO', videoHash: hash };
  ['scaleMode', 'imageTransform', 'scalingFactor', 'rotation', 'filters', 'opacity', 'blendMode', 'visible']
    .forEach(function (k) { if (orig[k] != null) vp[k] = orig[k]; });
  PLAYBACK_KEYS.forEach(function (k) { if (k in orig) vp[k] = orig[k]; });
  if (p.kind === 'gif') { // GIFs become looping, silent, autoplaying clips without controls
    var pb = p.playback || { autoplay: true, loop: true, muted: true, showControls: false };
    Object.keys(pb).forEach(function (k) { vp[k] = pb[k]; });
  }
  return vp;
}

function findVideoNode(n) {
  if (!n || n.removed) return null;
  if (mediaFills(n).some(function (f) { return f.paint.type === 'VIDEO'; })) return n;
  if ('children' in n) for (var i = 0; i < n.children.length; i++) { var r = findVideoNode(n.children[i]); if (r) return r; }
  return null;
}

function guidedPlace(orig, origPaint, p) {
  return new Promise(function (resolve, reject) {
    var origId = orig.id;
    var timer = setTimeout(function () { finish(null, 'timeout'); }, 15 * 60 * 1000);
    function onChange(ev) {
      for (var i = 0; i < ev.documentChanges.length; i++) {
        var c = ev.documentChanges[i];
        if (c.type === 'CREATE' && c.node && !c.node.removed) {
          var v = findVideoNode(c.node);
          if (v && v.id !== origId) { finish(c.node, 'dropped'); return; }
        }
      }
    }
    function finish(newNode, how) {
      if (!guided) return;
      guided = null;
      clearTimeout(timer);
      figma.off('documentchange', onChange);
      figma.ui.postMessage({ type: 'guided-hide' });
      if (!newNode) { resolve({ mode: 'guided', skipped: true, reason: how, autoError: p.autoError }); return; }
      try { resolve(transplant(orig, newNode, origPaint, p, how)); } catch (e) { reject(e); }
    }
    guided = { origId: origId, finish: finish };
    figma.on('documentchange', onChange);
    handlers.focus({ nodeId: origId }).catch(function () {});
    figma.ui.postMessage({ type: 'guided-show', fileName: p.fileName, layerName: orig.name, slideIndex: p.slideIndex,
      index: p.index, total: p.total, kind: p.kind });
  });
}

/** Put the newly dropped video layer exactly where the original was. */
function transplant(orig, dropped, origPaint, p, how) {
  var vnode = findVideoNode(dropped) || dropped;
  var layer = dropped;
  var notes = [];
  var parent = orig.parent;
  var hasKids = 'children' in orig && orig.children.length > 0;
  if (hasKids) {
    // The media was a frame background: put the clip inside the frame, at the back.
    orig.insertChild(0, layer);
    layer.x = 0; layer.y = 0;
    if (typeof layer.resize === 'function') layer.resize(orig.width, orig.height);
    layer.name = orig.name + ' (video)';
  } else {
    var idx = parent.children.indexOf(orig);
    parent.insertChild(idx, layer);          // same stacking position (original moves up one)
    try { layer.relativeTransform = orig.relativeTransform; } catch (e) { layer.x = orig.x; layer.y = orig.y; }
    if (typeof layer.resize === 'function') layer.resize(orig.width, orig.height);
    layer.name = orig.name;
    ['opacity', 'blendMode', 'constraints', 'effects', 'locked', 'layoutAlign', 'layoutGrow', 'layoutPositioning']
      .forEach(function (k) { try { if (k in orig && k in layer) layer[k] = clone(orig[k]); } catch (e) { /* not supported on this node */ } });
    ['cornerRadius', 'topLeftRadius', 'topRightRadius', 'bottomLeftRadius', 'bottomRightRadius', 'cornerSmoothing']
      .forEach(function (k) { try { if (typeof orig[k] === 'number' && k in layer) layer[k] = orig[k]; } catch (e) { /* mixed */ } });
  }
  // Re-apply crop, scale mode and playback onto the new clip's paint.
  var playbackApplied = false;
  try {
    var fills = clone(fillsOf(vnode));
    var vi = fills.findIndex(function (f) { return f.type === 'VIDEO'; });
    if (vi >= 0) {
      var target = videoPaint(origPaint, fills[vi].videoHash, p);
      var base = fills[vi];
      fills[vi] = Object.assign({}, base, target);
      try {
        vnode.fills = fills;
      } catch (e1) {
        // This Figma build doesn't accept playback keys on paints: keep crop/scale only.
        PLAYBACK_KEYS.forEach(function (k) { delete fills[vi][k]; });
        vnode.fills = fills;
        notes.push('Playback settings must be set by hand.');
      }
      var after = fillsOf(vnode)[vi];
      playbackApplied = playbackKnown(after) && (p.kind !== 'gif' || after.loop === true);
    }
  } catch (e) { notes.push('Crop/playback could not be copied automatically: ' + (e.message || e)); }
  var newHash = (mediaFills(vnode).filter(function (f) { return f.paint.type === 'VIDEO'; })[0] || {}).paint;
  if (hasKids) {
    var f2 = clone(fillsOf(orig)); f2.splice(p.fillIndex, 1); orig.fills = f2;
  } else {
    orig.remove();
  }
  figma.currentPage.selection = [layer];
  return { mode: 'guided', how: how, nodeId: layer.id, newHash: newHash && newHash.videoHash, playbackApplied: playbackApplied,
    notes: notes, autoError: p.autoError };
}

figma.on('close', function () { if (guided) guided.finish(null, 'closed'); });
