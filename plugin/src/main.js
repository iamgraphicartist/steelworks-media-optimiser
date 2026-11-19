/* Steelworks Media Optimiser — Figma plugin main thread.
 * The plugin is the app's hands inside Figma. The desktop app sends commands
 * over the localhost bridge (via ui.html); everything that touches the
 * document happens here.
 */
/* global SMOCore, figma, __html__ */
var PLUGIN_VERSION = '1.0.9';
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
          findGrids(node, slide, si + 1, grids, params.gap);
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

function findGrids(container, slide, slideIndex, out, gap) {
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
    }), { gap: gap == null ? SMOCore.GAP : gap });
    if (!res || res.even) return;
    out.push({ id: container.id + '/' + k, parentId: container.id, parentName: container.name, slideIndex: slideIndex,
      slideId: slide.id, rows: res.rows, cols: res.cols, gapsX: res.gapsX, gapsY: res.gapsY, bounds: res.bounds,
      count: g.length, plan: res.plan,
      before: g.map(function (c) { return { id: c.id, x: c.x, y: c.y, w: c.width, h: c.height }; }) });
  });
}

function fileInfo() {
  // Only read what needs no extra permission. (figma.currentUser needs the "currentuser"
  // permission, and figma.fileKey is only given to private plugins, so read it defensively.)
  var key = null;
  try { key = figma.fileKey || null; } catch (e) { key = null; }
  return { fileKey: key, name: figma.root.name, editorType: figma.editorType, pluginVersion: PLUGIN_VERSION,
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

  /** Deck details: cover lines (first slide) and text repeated in the header/footer of the slides. */
  readDetails: async function () {
    var slides = await getSlides();
    if (!slides.length) return { cover: [], groups: [] };
    var groups = {}, inBand = {};
    slides.forEach(function (slide, si) {
      var o = slide.absoluteTransform, H = slide.height;
      slide.findAll(function (n) { return n.type === 'TEXT' && n.visible !== false; }).forEach(function (t) {
        var y = t.absoluteTransform[1][2] - o[1][2], cy = y + t.height / 2;
        var band = cy < H * 0.15 ? 'header' : cy > H * 0.85 ? 'footer' : null;
        var text = (t.characters || '').trim();
        if (!band || !text || text.length > 80 || text.indexOf('\n') >= 0) return;
        if (/^\d{1,3}$/.test(text)) return;                       // page numbers aren't details
        var k = band + '|' + text;
        var g = groups[k] = groups[k] || { key: k, band: band, text: text, nodes: [], slides: {} };
        g.nodes.push(t.id); g.slides[si] = 1; inBand[t.id] = true;
      });
    });
    var list = Object.keys(groups).map(function (k) {
      var g = groups[k];
      return { key: g.key, band: g.band, text: g.text, nodes: g.nodes, slides: Object.keys(g.slides).length,
        isDate: !!SMODates.detect(g.text) };
    }).filter(function (g) { return g.slides >= 2; })
      .sort(function (a, b) { return (a.band === b.band ? 0 : a.band === 'header' ? -1 : 1) || b.slides - a.slides; }).slice(0, 6);
    // Cover: the first slide's own lines (not the repeated header/footer), top to bottom.
    var cover = [], first = slides[0], o0 = first.absoluteTransform;
    first.findAll(function (n) { return n.type === 'TEXT' && n.visible !== false && !inBand[n.id]; })
      .sort(function (a, b) { return (a.absoluteTransform[1][2] - b.absoluteTransform[1][2]) || (a.absoluteTransform[0][2] - b.absoluteTransform[0][2]); })
      .forEach(function (t) {
        (t.characters || '').split('\n').forEach(function (line, li) {
          if (line.trim() && cover.length < 8) cover.push({ nodeId: t.id, line: li, text: line, isDate: !!SMODates.detect(line), slide: 1 });
        });
      });
    var groupsOnCover = list.filter(function (g) { return g.nodes.some(function (id) { return first.findOne(function (n) { return n.id === id; }); }); });
    return { cover: cover, groups: list, deck: figma.root.name, coverAlsoHas: groupsOnCover.map(function (g) { return g.key; }) };
  },

  /** edits: [{nodeId, line (index, or null for the whole layer), text}] — keeps each line's own font and style. */
  setText: async function (p) {
    var byNode = {}, log = [];
    p.edits.forEach(function (e) { (byNode[e.nodeId] = byNode[e.nodeId] || []).push(e); });
    var ids = Object.keys(byNode);
    for (var i = 0; i < ids.length; i++) {
      var node = await nodeById(ids[i]);
      if (!node || node.type !== 'TEXT') { log.push({ nodeId: ids[i], ok: false, error: 'Text layer not found' }); continue; }
      try {
        var fonts = node.characters.length ? node.getRangeAllFontNames(0, node.characters.length) : [node.fontName];
        for (var f = 0; f < fonts.length; f++) await figma.loadFontAsync(fonts[f]);
        // Highest line first, so earlier offsets stay valid.
        var edits = byNode[ids[i]].slice().sort(function (a, b) { return (b.line == null ? -1 : b.line) - (a.line == null ? -1 : a.line); });
        var before = node.characters;
        edits.forEach(function (e) {
          var start = 0, end = node.characters.length;
          if (e.line != null) {
            var lines = node.characters.split('\n');
            if (e.line >= lines.length) return;
            for (var l = 0; l < e.line; l++) start += lines[l].length + 1;
            end = start + lines[e.line].length;
          }
          if (end === start) { node.insertCharacters(start, e.text, 'BEFORE'); return; }
          // Insert the new text using the style of the first old character, then remove the old text.
          node.insertCharacters(start, e.text, 'AFTER');
          node.deleteCharacters(start + e.text.length, end + e.text.length);
        });
        log.push({ type: 'text', nodeId: node.id, before: before, after: node.characters, ok: true });
      } catch (err) {
        var msg = String(err.message || err);
        if (/font/i.test(msg)) msg = 'A font used here isn’t available on this computer, so this text was left as it is.';
        log.push({ nodeId: ids[i], ok: false, error: msg });
      }
    }
    return { log: log };
  },

  /** Set every date on the cover and in the footers to this month, keeping each one's style. */
  datesToThisMonth: async function (p) {
    var style = (p && p.style) || 'keep';
    var d = await handlers.readDetails();
    var edits = [];
    d.cover.forEach(function (c) { var v = c.isDate && SMODates.toDate(c.text, style); if (v && v !== c.text) edits.push({ nodeId: c.nodeId, line: c.line, text: v }); });
    d.groups.forEach(function (g) {
      var v = g.isDate && SMODates.toDate(g.text, style);
      if (v && v !== g.text) g.nodes.forEach(function (id) { edits.push({ nodeId: id, line: null, text: v }); });
    });
    if (!edits.length) return { changed: 0, log: [] };
    var r = await handlers.setText({ edits: edits });
    return { changed: r.log.filter(function (e) { return e.ok; }).length, log: r.log };
  },

  /** The app is switching to another deck: close this plugin window. */
  close: async function () {
    setTimeout(function () { figma.closePlugin(); }, 150);
    return { ok: true };
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
