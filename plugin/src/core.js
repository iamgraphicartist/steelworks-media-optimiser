/* Steelworks Media Optimiser — pure plugin logic (no figma.* calls).
 * Shared by the plugin (dist/code.js) and the browser test page (tests/js).
 */
var SMOCore = (function () {
  'use strict';

  var GAP = 12; // default gap between thumbnails (the app can override it)
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
