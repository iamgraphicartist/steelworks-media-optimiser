/* A small stand-in for the Figma plugin API, enough to run dist/code.js in a
 * browser against samples/deck.json. It deliberately mimics Figma's limits:
 *   - fills are read-only snapshots; you must assign a new array
 *   - a VIDEO paint can't be created by a plugin (createVideoAsync is absent),
 *     only existing video hashes may be re-assigned
 *   - images are identified by the SHA-1 of their bytes
 */
/* exported MockFigma, sha1hex */
function sha1hex(bytes) {
  function rotl(n, s) { return (n << s) | (n >>> (32 - s)); }
  var len = bytes.length, words = new Uint32Array(((len + 8 >> 6) + 1) << 4);
  for (var i = 0; i < len; i++) words[i >> 2] |= bytes[i] << (24 - (i & 3) * 8);
  words[len >> 2] |= 0x80 << (24 - (len & 3) * 8);
  words[words.length - 1] = len * 8;
  var h0 = 0x67452301, h1 = 0xEFCDAB89, h2 = 0x98BADCFE, h3 = 0x10325476, h4 = 0xC3D2E1F0, w = new Uint32Array(80);
  for (var b = 0; b < words.length; b += 16) {
    for (var t = 0; t < 16; t++) w[t] = words[b + t];
    for (t = 16; t < 80; t++) w[t] = rotl(w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16], 1);
    var a = h0, bb = h1, c = h2, d = h3, e = h4;
    for (t = 0; t < 80; t++) {
      var f, k;
      if (t < 20) { f = (bb & c) | (~bb & d); k = 0x5A827999; } else if (t < 40) { f = bb ^ c ^ d; k = 0x6ED9EBA1; }
      else if (t < 60) { f = (bb & c) | (bb & d) | (c & d); k = 0x8F1BBCDC; } else { f = bb ^ c ^ d; k = 0xCA62C1D6; }
      var tmp = (rotl(a, 5) + f + e + k + w[t]) >>> 0;
      e = d; d = c; c = rotl(bb, 30) >>> 0; bb = a; a = tmp;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + bb) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
  }
  return [h0, h1, h2, h3, h4].map(function (x) { return ('00000000' + x.toString(16)).slice(-8); }).join('');
}

function MockFigma(opts) {
  var self = this, nextId = 1, listeners = {}, images = {}, videos = {}, storage = {};
  self.log = opts.log || function () {};
  self.onMutate = opts.onMutate || function () {};
  var mutate = function () { self.onMutate(); };

  function mul(A, B) {
    return [[A[0][0] * B[0][0] + A[0][1] * B[1][0], A[0][0] * B[0][1] + A[0][1] * B[1][1], A[0][0] * B[0][2] + A[0][1] * B[1][2] + A[0][2]],
            [A[1][0] * B[0][0] + A[1][1] * B[1][0], A[1][0] * B[0][1] + A[1][1] * B[1][1], A[1][0] * B[0][2] + A[1][1] * B[1][2] + A[1][2]]];
  }
  function freeze(v) { return JSON.parse(JSON.stringify(v)); }

  function Node(type, p) {
    p = p || {};
    this.id = (p.idPrefix || '1') + ':' + (nextId++);
    this.type = type; this.name = p.name || type;
    this.visible = p.visible !== false; this.opacity = p.opacity == null ? 1 : p.opacity;
    this.blendMode = 'PASS_THROUGH'; this.effects = []; this.isMask = false; this.locked = false;
    this.cornerRadius = p.cornerRadius || 0; this.clipsContent = p.clipsContent !== false && (type === 'FRAME' || type === 'SLIDE');
    this.layoutMode = 'NONE'; this.constraints = { horizontal: 'MIN', vertical: 'MIN' };
    this.relativeTransform = [[1, 0, p.x || 0], [0, 1, p.y || 0]];
    this.width = p.w || 100; this.height = p.h || 100;
    this._fills = freeze(p.fills || []);
    this.parent = null; this.removed = false;
    if (['FRAME', 'SLIDE', 'GROUP', 'PAGE', 'SLIDE_GRID', 'SLIDE_ROW', 'DOCUMENT'].indexOf(type) >= 0) this.children = [];
  }
  Object.defineProperties(Node.prototype, {
    x: { get: function () { return this.relativeTransform[0][2]; }, set: function (v) { this.relativeTransform[0][2] = v; mutate(); } },
    y: { get: function () { return this.relativeTransform[1][2]; }, set: function (v) { this.relativeTransform[1][2] = v; mutate(); } },
    rotation: { get: function () { return 0; } },
    absoluteTransform: { get: function () {
      var m = freeze(this.relativeTransform), n = this.parent;
      while (n && n.type !== 'PAGE' && n.type !== 'SLIDE_ROW' && n.type !== 'SLIDE_GRID') { m = mul(n.relativeTransform, m); n = n.parent; }
      return m;
    } },
    absoluteBoundingBox: { get: function () { var m = this.absoluteTransform; return { x: m[0][2], y: m[1][2], width: this.width, height: this.height }; } },
    fills: {
      get: function () { return Object.freeze(freeze(this._fills)); },
      set: function (v) {
        var old = this._fills;
        v.forEach(function (paint) {
          if (paint.type === 'IMAGE' && !images[paint.imageHash]) throw new Error('Image with hash ' + paint.imageHash + ' not found');
          if (paint.type === 'VIDEO') {
            // Figma: plugins can't create video fills. Only keep an existing one.
            var had = old.some(function (o) { return o.type === 'VIDEO' && o.videoHash === paint.videoHash; });
            if (!had) throw new Error('in set_fills: Cannot set a VIDEO paint from a plugin');
          }
        });
        this._fills = freeze(v); mutate();
      }
    }
  });
  Node.prototype.resize = function (w, h) { this.width = w; this.height = h; mutate(); };
  Node.prototype.remove = function () {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null; this.removed = true; mutate();
  };
  Node.prototype.appendChild = function (c) { return this.insertChild(this.children.length, c); };
  Node.prototype.insertChild = function (i, c) {
    if (c.parent) { var j = c.parent.children.indexOf(c); c.parent.children.splice(j, 1); if (c.parent === this && j < i) i--; }
    // keep the child's absolute position like Figma does when re-parenting
    this.children.splice(i, 0, c); c.parent = this; mutate(); return c;
  };
  Node.prototype.findAll = function (fn) {
    var out = [];
    (function rec(n) { (n.children || []).forEach(function (c) { if (!fn || fn(c)) out.push(c); rec(c); }); })(this);
    return out;
  };

  // --- build the document from deck.json
  var doc = new Node('DOCUMENT', { name: opts.deck.name });
  var page = new Node('PAGE', { name: 'Slides' }); doc.appendChild(page);
  var grid = new Node('SLIDE_GRID', {}), row = new Node('SLIDE_ROW', {});
  page.appendChild(grid); grid.appendChild(row);
  var slides = [];
  opts.deck.slides.forEach(function (s, i) {
    var slide = new Node('SLIDE', { name: s.name, x: i * 2120, y: 0, w: 1920, h: 1080 });
    row.appendChild(slide); slides.push(slide);
    (function add(parent, kids) {
      kids.forEach(function (k) {
        var n = new Node(k.type, k);
        parent.appendChild(n);
        if (k.children) add(n, k.children);
      });
    })(slide, s.children);
  });
  doc.name = opts.deck.name;
  Object.keys(opts.bytes).forEach(function (h) { images[h] = opts.bytes[h]; });
  (opts.videoHashes || []).forEach(function (h) { videos[h] = true; });
  self.slides = slides; self.page = page; self.images = images;

  function nodeById(id) {
    var found = null;
    (function rec(n) { if (found) return; if (n.id === id) { found = n; return; } (n.children || []).forEach(rec); })(doc);
    return found;
  }
  function imageObj(hash) {
    var bytes = images[hash];
    if (!bytes) return null;
    return {
      hash: hash,
      getBytesAsync: function () { return Promise.resolve(new Uint8Array(bytes)); },
      getSizeAsync: function () {
        return createImageBitmap(new Blob([bytes])).then(function (b) { var r = { width: b.width, height: b.height }; b.close(); return r; });
      }
    };
  }

  var uiFrame = null, uiHandler = null;
  self.api = {
    editorType: 'slides',
    fileKey: opts.deck.fileKey,
    root: doc,
    get currentPage() { return page; },
    currentUser: { name: 'Demo user' },
    showUI: function (html, o) { uiFrame = opts.mountUI(html, o); },
    ui: {
      postMessage: function (m) { if (opts.onUIMessage) opts.onUIMessage(m); uiFrame.contentWindow.postMessage({ pluginMessage: m }, '*'); },
      set onmessage(fn) { uiHandler = fn; }, get onmessage() { return uiHandler; }
    },
    getSlideGrid: function () { return [slides.filter(function (s) { return !s.removed; })]; },
    loadAllPagesAsync: function () { return Promise.resolve(); },
    getNodeByIdAsync: function (id) { return Promise.resolve(nodeById(id)); },
    getNodeById: nodeById,
    getImageByHash: imageObj,
    createImage: function (bytes) {
      var h = sha1hex(bytes); images[h] = bytes; self.log('createImage ' + h.slice(0, 10) + ' (' + bytes.length + ' bytes)');
      return imageObj(h);
    },
    saveVersionHistoryAsync: function (title) { self.log('Saved version: ' + title); return Promise.resolve({ id: 'v' + Date.now() }); },
    viewport: { scrollAndZoomIntoView: function (n) { if (opts.onFocus) opts.onFocus(n[0]); } },
    notify: function (t) { self.log('notify: ' + t); },
    clientStorage: {
      getAsync: function (k) { return Promise.resolve(storage[k]); },
      setAsync: function (k, v) { storage[k] = v; return Promise.resolve(); }
    },
    on: function (ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    off: function (ev, fn) { listeners[ev] = (listeners[ev] || []).filter(function (f) { return f !== fn; }); }
  };
  Object.defineProperty(page, 'selection', { get: function () { return this._sel || []; }, set: function (v) { this._sel = v; if (opts.onFocus && v[0]) opts.onFocus(v[0]); } });
  self.storage = storage;

  /** What Figma does when the user drags a video file onto the canvas. */
  self.simulateDrop = function (bytes, name, slide) {
    var h = sha1hex(bytes); videos[h] = true;
    var n = new Node('RECTANGLE', { name: name, x: 760, y: 340, w: 400, h: 225, fills: [{ type: 'VIDEO', videoHash: h, scaleMode: 'FILL' }] });
    (slide || slides[0]).appendChild(n);
    (listeners.documentchange || []).forEach(function (fn) { fn({ documentChanges: [{ type: 'CREATE', id: n.id, node: n, origin: 'LOCAL' }] }); });
    return n;
  };
  self.deliver = function (m) { if (uiHandler) uiHandler(m); };
}
