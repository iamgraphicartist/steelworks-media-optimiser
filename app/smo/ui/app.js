/* Steelworks Media Optimiser — app UI. Talks to the local engine over /ws/ui. */
(function () {
  'use strict';
  var Q = new URLSearchParams(location.search);
  var KEY = Q.get('k') || '';
  if (Q.get('theme')) document.documentElement.dataset.theme = Q.get('theme');
  if (Q.get('v') === '1') document.documentElement.classList.add('vibrancy');
  if (Q.get('settings') || Q.get('still')) document.documentElement.classList.add('still');   // docs screenshots

  var S = null;              // engine state
  var view = Q.get('step') || 'connect';
  var ws, seq = 0, pending = {};
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };

  // ------------------------------------------------------------ connection
  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/ui?k=' + encodeURIComponent(KEY));
    ws.onmessage = function (ev) {
      var m = JSON.parse(ev.data);
      if (m.event === 'state') {
        var first = !S; S = m.data; render();
        if (first && Q.get('settings')) openSettings(Q.get('settings'));   // for docs: ?settings=h_figma
        return;
      }
      var p = pending[m.id]; delete pending[m.id];
      if (p) { if (m.error) p.reject(new Error(m.error)); else p.resolve(m.result); }
    };
    ws.onclose = function () { setTimeout(connect, 800); };
  }
  function call(action, params) {
    return new Promise(function (resolve, reject) {
      var id = ++seq; pending[id] = { resolve: resolve, reject: reject };
      ws.send(JSON.stringify({ id: id, action: action, params: params || {} }));
    });
  }
  function act(action, params, okMsg) {
    return call(action, params).then(function (r) { if (okMsg) toast(okMsg); return r; }, function (e) { toast(e.message, true); throw e; });
  }
  var toastTimer;
  function toast(text, err) {
    var t = $('toast'); t.textContent = text; t.className = 'toast on' + (err ? ' err' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.className = 'toast'; }, err ? 6000 : 3000);
  }

  // ------------------------------------------------------------ formatting
  function mb(n) {
    if (n == null) return '—';
    if (n < 1048576) return Math.round(n / 1024) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }
  function pct(a, b) { return b ? Math.round((1 - a / b) * 100) : 0; }
  function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }

  var ICON = {
    figma: '<svg width="18" height="18" viewBox="0 0 38 57"><path fill="#1abcfe" d="M19 28.5a9.5 9.5 0 1 1 19 0 9.5 9.5 0 0 1-19 0z"/><path fill="#0acf83" d="M0 47.5A9.5 9.5 0 0 1 9.5 38H19v9.5a9.5 9.5 0 1 1-19 0z"/><path fill="#ff7262" d="M19 0v19h9.5a9.5 9.5 0 1 0 0-19H19z"/><path fill="#f24e1e" d="M0 9.5A9.5 9.5 0 0 0 9.5 19H19V0H9.5A9.5 9.5 0 0 0 0 9.5z"/><path fill="#a259ff" d="M0 28.5A9.5 9.5 0 0 0 9.5 38H19V19H9.5A9.5 9.5 0 0 0 0 28.5z"/></svg>',
    dropbox: '<svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M6 2 0 6l6 4 6-4zm12 0-6 4 6 4 6-4zM0 14l6 4 6-4-6-4zm18-4-6 4 6 4 6-4zM6 19.3l6 4 6-4-6-4z"/></svg>',
    plug: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>',
    film: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/></svg>',
    info: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
    image: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>',
    video: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="5" width="15" height="14" rx="3"/><path d="m17 10 5-3v10l-5-3"/></svg>',
    gif: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M8 12h2v2H8a1.5 1.5 0 0 1 0-4h2M13 10v4M16 14v-4h2M16 12h1.5"/></svg>',
    check: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>',
    done: '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></svg>',
    search: '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>'
  };

  // ----------------------------------------------------------------- steps
  var STEPS = [
    { id: 'connect', label: 'Connect', hint: 'Figma, Dropbox, plugin' },
    { id: 'deck', label: 'Choose deck', hint: 'Paste a Figma link' },
    { id: 'review', label: 'Review', hint: 'What makes it heavy' },
    { id: 'cleanup', label: 'Clean up', hint: 'Hidden & covered media' },
    { id: 'images', label: 'Images', hint: 'Resize & recompress' },
    { id: 'videos', label: 'Videos & GIFs', hint: 'Compress & replace' },
    { id: 'spacing', label: 'Spacing', hint: 'Even thumbnail gaps' },
    { id: 'align', label: 'Alignment', hint: 'Headers, footers, text' },
    { id: 'report', label: 'Report', hint: 'Before & after' }
  ];
  function stepDone(id) {
    if (!S) return false;
    var c = S.connections;
    switch (id) {
      case 'connect': return c.plugin.connected && (c.dropbox.signedIn || (!c.dropbox.configured && !!c.dropbox.localRoot));
      case 'deck': return !!(S.deck && S.deck.name);
      case 'review': return S.scan.status === 'done';
      case 'images': return S.images.status === 'done';
      case 'cleanup': return S.cleanup.status === 'done';
      case 'videos': return S.videos.items.length > 0 && S.videos.items.every(function (v) { return ['done', 'removed', 'kept'].indexOf(v.status) >= 0; });
      case 'spacing': return S.spacing.grids.some(function (g) { return g.status === 'done'; });
      case 'align': return S.align && S.align.items.some(function (a) { return a.status === 'done'; });
      case 'report': return !!(S.report && S.report.exportedTo);
    }
  }
  function go(id) { view = id; render(true); $('content').scrollTop = 0; }
  function nextStep() { var i = STEPS.findIndex(function (s) { return s.id === view; }); if (i < STEPS.length - 1) go(STEPS[i + 1].id); }

  function renderSidebar() {
    $('steps').innerHTML = STEPS.map(function (s, i) {
      var done = stepDone(s.id);
      return '<li class="step' + (s.id === view ? ' active' : '') + (done ? ' done' : '') + '" data-step="' + s.id + '">' +
        '<span class="step-num">' + (done ? ICON.check : i + 1) + '</span>' +
        '<span><div class="step-label">' + s.label + '</div><div class="step-hint">' + s.hint + '</div></span></li>';
    }).join('');
    var r = S.report || {}, b = r.before || 0, a = r.after == null ? b : r.after;
    $('savings').innerHTML = b ? '<div class="savings-label">Saved so far</div><div class="savings-value">' + mb(b - a) +
      '</div><div class="savings-label">' + mb(b) + ' → ' + mb(a) + '</div><div class="meter"><i style="width:' + pct(a, b) + '%"></i></div>'
      : '<div class="savings-label">Saved so far</div><div class="savings-value">—</div><div class="savings-label">Run a step to see savings</div>';
  }
  function renderStatus() {
    var a = S.activity || {};
    var p = a.progress;
    $('status').innerHTML = '<span class="dot ' + (p != null && p < 1 ? 'wait' : 'ok') + '"></span><span class="title">' + esc(a.title) +
      '</span><span class="detail">' + esc(a.detail) + '</span>' +
      (p != null && p < 1 ? '<div class="progress"><i style="width:' + Math.round(p * 100) + '%"></i></div>' : '');
  }

  // ----------------------------------------------------------------- views
  var V = {};

  function addStep(f) {
    if (f.registered) return '<span class="chip ok">' + ICON.check + ' Added to Figma</span> <span class="muted">It’s under <b>Plugins › Development</b>.</span>';
    if (!f.installed) return 'Install <a data-url="https://www.figma.com/downloads/">Figma for desktop</a> and open it once. ' +
      '<span class="muted">(Using Figma in a browser? <a data-a="plugin.reveal">Show plugin file</a> and import it with Plugins › Development › Import plugin from manifest.)</span>';
    return '<button class="btn mini" data-a="plugin.install">Add to Figma</button> ' +
      (f.running ? '<span class="muted">Figma restarts for a moment. Your files are saved automatically.</span>' : '<span class="muted">One click, and it’s in Figma.</span>');
  }

  V.connect = function () {
    var c = S.connections, d = c.dropbox, pl = c.plugin, acc = d.account || {};
    function card(icon, cls, title, body, right, done) {
      return '<div class="card"><div class="card-row" style="align-items:flex-start"><div class="icon-badge ' + cls + '">' + icon + '</div><div class="grow"><h3>' + title +
        (done ? ' <span class="chip ok" style="margin-left:6px">' + ICON.check + ' Ready</span>' : '') + '</h3><div class="muted small" style="margin-top:2px">' + body + '</div></div>' + (right || '') + '</div></div>';
    }
    // 1 — Plugin (the only real step)
    var allow = S.pairRequest ? '<div class="callout" style="margin:12px 0 0;align-items:center">' + ICON.info +
      '<div class="grow"><b>The Figma plugin wants to connect.</b> Allow it to work on your decks?</div>' +
      '<button class="btn secondary" data-a="plugin.allow" data-p=\'{"allow":false}\'>Don’t allow</button><button class="btn" data-a="plugin.allow">Allow</button></div>' : '';
    var plBody = pl.connected ? 'Connected to <b>' + esc((pl.file || {}).name || 'your deck') + '</b>. Keep the plugin open while you work.'
      : '<ol class="steps-mini"><li>' + addStep(c.figmaApp || {}) + '</li>' +
        '<li>Open your deck and run <b>Plugins › Development › Steelworks Media Optimiser</b>.</li><li>Click <b>Connect</b> in the plugin, then <b>Allow</b> here.</li></ol>' + allow;
    // 2 — Dropbox: on this computer and/or online — either is enough
    var local = d.localRoot
      ? '<span class="chip ok">' + ICON.check + ' Found</span><div class="mono small" style="margin-top:6px;word-break:break-all">' + esc(d.localRootShort || d.localRoot) + '</div><div class="muted small" style="margin-top:4px">Unsynced files download by themselves when needed.</div>'
      : '<span class="chip">Not installed</span><div class="muted small" style="margin-top:6px">For people with the Dropbox app. <a data-url="https://www.dropbox.com/install">Get Dropbox</a></div>';
    var online = d.signedIn
      ? '<span class="chip ok">' + ICON.check + ' Signed in</span><div class="muted small" style="margin-top:6px">' + esc(acc.name || '') + (acc.team ? ' · ' + esc(acc.team) : '') + '. Searches every team and shared folder.</div>' + (d.demo ? '' : '<a class="small" data-a="dropbox.signout">Sign out</a>')
      : d.configured
        ? '<button class="btn mini" data-a="dropbox.signin">Sign in with Dropbox</button><div class="muted small" style="margin-top:6px">No app needed: works in your browser. Searches every team and shared folder.</div>'
        : '<span class="chip">Not set up yet</span><div class="muted small" style="margin-top:6px">Needs the studio’s Dropbox key (see Settings › Advanced).</div>';
    // Most project files live online, so sign-in is what counts once the studio key exists.
    var dDone = d.signedIn || (!d.configured && !!d.localRoot);
    var hint = d.signedIn ? 'Files already on this computer are used first (faster); everything else comes from Dropbox online.'
      : d.configured ? '<b>Please sign in online.</b> Most project files aren’t synced to computers, so the app needs to reach Dropbox online. Files that are on this computer are still used first.'
      : 'The app uses your Dropbox folder on this computer. Online sign-in appears here once the studio’s Dropbox key is added.';
    var dBody = '<div class="muted small" style="margin-bottom:10px">' + hint + '</div><div class="options">' +
      '<div class="option' + (d.localRoot ? ' on' : '') + '"><h4>On this computer</h4>' + local + '</div>' +
      '<div class="option' + (d.signedIn ? ' on' : (d.configured ? ' want' : '')) + '"><h4>Online <span class="chip info" style="margin-left:4px">Recommended</span></h4>' + online + '</div></div>';
    var dRight = '';
    return '<div class="page"><h1>Connect</h1><p class="lede">' + (pl.connected ? 'You’re all set.' : 'One step and you’re ready. Everything else is set up automatically.') + '</p>' +
      card(ICON.plug, 'plugin', 'Figma plugin', plBody, '', pl.connected) +
      card(ICON.dropbox, 'dropbox', 'Dropbox', dBody, dRight, dDone) +
      card(ICON.film, 'tool', 'Video tools', c.ffmpeg.ok ? 'Built in and ready.' : 'ffmpeg wasn’t found, so images will work but videos won’t. See <a data-open="settings">Settings</a> › ffmpeg ⓘ.', '', c.ffmpeg.ok) +
      '<div class="actions"><span class="spacer"></span><button class="btn large" data-next' + (pl.connected ? '' : ' disabled') + '>Continue</button></div></div>';
  };

  V.deck = function () {
    var d = S.deck || {}, pl = S.connections.plugin;
    var card = '';
    if (d.key) {
      var match = d.pluginMatches === false ? '<div class="callout warn">' + ICON.info + '<div>The plugin is running in <b>' + esc((pl.file || {}).name) +
        '</b>, not this deck. Open this deck in Figma and run the plugin there.</div></div>' : '';
      card = match + '<div class="card"><div class="card-row">' +
        (d.thumbnail ? '<img src="' + esc(d.thumbnail) + '" alt="" style="width:160px;height:90px;object-fit:cover;border-radius:8px;background:var(--card-2)">' :
          '<div class="icon-badge figma" style="width:90px;height:56px">' + ICON.figma + '</div>') +
        '<div class="grow"><h2>' + esc(d.name || 'Figma deck') + '</h2><div class="muted small mono">' + esc(d.key) + '</div>' +
        (d.warning ? '<div class="small" style="color:var(--orange);margin-top:6px">' + esc(d.warning) + '</div>' : '') +
        '<div style="margin-top:8px">' + (pl.connected ? (d.pluginMatches !== false ? '<span class="chip ok">Plugin is running in this deck</span>' : '<span class="chip warn">Different deck open</span>')
          : '<span class="chip warn">Run the plugin in this deck</span>') + '</div></div></div></div>';
    } else if (pl.connected && pl.file) {
      card = '<div class="callout">' + ICON.info + '<div>The plugin is open in <b>' + esc(pl.file.name) + '</b>. Paste its link above, or just continue to use it.</div></div>';
    }
    return '<div class="page"><h1>Choose a deck</h1><p class="lede">Paste the link to a Figma Slides deck. In Figma: <b>Share › Copy link</b>.</p>' +
      '<div class="card"><div class="input-row"><input class="input" id="deckUrl" placeholder="https://www.figma.com/slides/…" value="' + esc(d.url || '') +
      '" spellcheck="false"><button class="btn" id="deckGo">Open</button></div></div>' + '<div id="deckCard">' + card + '</div>' +
      '<div class="actions"><span class="spacer"></span><button class="btn large" data-next' + (pl.connected ? '' : ' disabled') + '>Continue</button></div></div>';
  };

  V.review = function () {
    var sc = S.scan;
    if (sc.status !== 'done') {
      var running = sc.status === 'running', p = sc.progress || {};
      return '<div class="page"><h1>Review the deck</h1><p class="lede">The plugin reads every slide: each image, GIF and video, how big it is, how big it’s shown, and anything hidden or covered up.</p>' +
        '<div class="card empty">' + ICON.search + '<h2>' + (running ? esc(p.label || 'Reading the deck…') : 'Ready to read the deck') + '</h2>' +
        '<p>' + (running ? 'This takes about a minute for a large deck.' : 'Nothing is changed in this step.') + '</p>' +
        (running ? '<div class="progress" style="max-width:320px;margin:16px auto 0"><i style="width:' + (p.total ? Math.round(p.done / p.total * 100) : 5) + '%"></i></div>'
          : '<button class="btn large" data-a="scan"' + (S.connections.plugin.connected ? '' : ' disabled') + '>Read deck</button>' +
            (S.connections.plugin.connected ? '' : '<p class="small" style="margin-top:12px">Connect the Figma plugin first.</p>')) +
        (sc.status === 'error' ? '<p class="small" style="color:var(--red)">' + esc(sc.error) + '</p>' : '') + '</div></div>';
    }
    var s = sc.summary;
    var tiles = '<div class="grid-4">' +
      tile('Slides', s.slides, plural(s.layers, 'media layer')) +
      tile('Images', mb(s.imageBytes), plural(s.images, 'image')) +
      tile('Videos & GIFs', s.videos + s.gifs, s.videos + ' videos · ' + s.gifs + ' GIFs') +
      tile('To tidy', s.issues + s.grids, s.issues + ' hidden/covered · ' + s.grids + ' grids') + '</div>';
    var rows = sc.rows.slice(0, 40).map(function (r) {
      return '<tr class="clickable" data-focus="' + esc(r.nodeId) + '" title="Show in Figma"><td class="num">' + r.slide + '</td><td class="name">' + esc(r.name) +
        '</td><td><span class="kind">' + ICON[r.kind] + ' ' + { image: 'Image', video: 'Video', gif: 'GIF' }[r.kind] + '</span></td><td class="num">' + mb(r.bytes) +
        '</td><td class="num">' + esc(r.px || '—') + '</td><td class="num">' + esc(r.display) + (r.crop ? ' <span class="chip">crop</span>' : '') +
        '</td><td>' + (r.issue ? '<span class="chip warn">' + esc(r.issue) + '</span>' : '') + '</td></tr>';
    }).join('');
    return '<div class="page"><h1>Review the deck</h1><p class="lede">Here’s what’s making <b>' + esc(S.deck.name || 'this deck') + '</b> heavy. Click a row to see it in Figma.</p>' + tiles +
      '<div class="card" style="padding:8px 10px"><div class="table-wrap" style="max-height:420px"><table><thead><tr><th class="num">Slide</th><th>Layer</th><th>Type</th><th class="num">File size</th><th class="num">Pixels</th><th class="num">Shown at</th><th></th></tr></thead><tbody>' +
      rows + '</tbody></table></div></div><div class="actions"><button class="btn secondary" data-a="scan">Read again</button><span class="spacer"></span><button class="btn large" data-next>Continue</button></div></div>';
  };
  function tile(label, value, sub) { return '<div class="card"><div class="tile-label">' + label + '</div><div class="tile-value">' + value + '</div><div class="tile-sub">' + sub + '</div></div>'; }
  function needScan(title, lede) {
    return '<div class="page"><h1>' + title + '</h1><p class="lede">' + lede + '</p><div class="card empty">' + ICON.search +
      '<h2>Read the deck first</h2><p>This step uses what the Review step finds.</p><button class="btn" data-go="review">Go to Review</button></div></div>';
  }
  function statusChip(st, label) {
    var cls = { done: 'ok', ready: 'info', kept: '', skipped: '', removed: '', error: 'err', not_found: 'warn', working: 'info', searching: 'info',
      compressing: 'info', placing: 'info', pending: '', reverted: 'warn' }[st] || '';
    return '<span class="chip ' + cls + '">' + esc(label) + '</span>';
  }

  V.images = function () {
    if (S.scan.status !== 'done') return needScan('Images', 'Shrink oversized images to what the slide actually needs.');
    var st = S.images, items = st.items, t = st.totals;
    var todo = items.filter(function (i) { return i.status === 'pending' || i.status === 'error'; }).length;
    var running = st.status === 'running';
    var head = t ? '<div class="grid-3">' + tile('Before', mb(t.before), plural(items.length, 'image')) + tile('After', mb(t.after), t.done + ' swapped in Figma') +
      tile('Saved', '<span class="better">' + mb(t.before - t.after) + '</span>', pct(t.after, t.before) + '% smaller') + '</div>' : '';
    var LBL = { pending: 'To do', working: 'Working…', ready: 'Swapping…', done: 'Swapped', kept: 'Left as is', skipped: 'Skipped', error: 'Problem', removed: 'Removed', reverted: 'Put back', manual: 'Place by hand' };
    var rows = items.map(function (i) {
      return '<tr class="clickable" data-focus="' + esc(i.nodes[0] && i.nodes[0].nodeId) + '"><td class="num">' + esc(i.slides.join(', ')) + '</td><td class="name" title="' + esc(i.names.join(', ')) + '">' + esc(i.names.join(', ')) +
        '</td><td class="num">' + esc(i.src) + (i.target && i.target !== i.src ? '<span class="arrow">→</span>' + esc(i.target) : '') + '</td><td class="num">' + mb(i.before) +
        (i.after ? '<span class="arrow">→</span><span class="better">' + mb(i.after) + '</span>' : '') + '</td><td>' + statusChip(i.status, LBL[i.status] || i.status) +
        '</td><td class="small muted">' + esc(i.reason || '') + '</td></tr>';
    }).join('');
    return '<div class="page"><h1>Images</h1><p class="lede">Each image is resized to twice the size it’s shown on the slide, so it stays sharp even on high-resolution screens (never upscaled). Photos become high-quality JPEG, transparent images WebP, and logos or graphics are kept lossless. Every result is compared with the original: it’s only used if it looks the same and is at least 30% smaller. Crops and fills don’t change.</p>' + head +
      '<div class="card" style="padding:8px 10px"><div class="table-wrap" style="max-height:440px"><table><thead><tr><th class="num">Slide</th><th>Layer</th><th class="num">Pixels</th><th class="num">Size</th><th>Status</th><th></th></tr></thead><tbody>' + rows +
      '</tbody></table></div></div><div class="actions"><span class="muted small">A version is saved in Figma’s history before anything changes.</span><span class="spacer"></span>' +
      (todo ? '<button class="btn large" data-a="images.run"' + (running ? ' disabled' : '') + '>' + (running ? 'Optimising…' : 'Optimise ' + plural(todo, 'image')) + '</button>' : '<button class="btn large" data-next>Continue</button>') + '</div></div>';
  };

  V.cleanup = function () {
    if (S.scan.status !== 'done') return needScan('Clean up', 'Remove media nobody can see.');
    var items = S.cleanup.items;
    if (!items.length) return '<div class="page"><h1>Clean up</h1><p class="lede">Remove media nobody can see.</p><div class="card empty">' + ICON.done + '<h2>Nothing hidden or covered</h2><p>Every media layer in this deck is visible.</p><button class="btn" data-next>Continue</button></div></div>';
    var pendingSel = items.filter(function (i) { return i.selected && i.status === 'pending'; });
    var bytes = pendingSel.reduce(function (s, i) { return s + (i.bytes || 0); }, 0);
    var rows = items.map(function (i) {
      var dis = i.status !== 'pending';
      return '<tr><td><input type="checkbox" class="check" data-toggle="' + esc(i.key) + '"' + (i.selected ? ' checked' : '') + (dis ? ' disabled' : '') + '></td><td class="num">' + i.slideIndex +
        '</td><td class="name">' + esc(i.name) + '</td><td><span class="kind">' + ICON[i.kind === 'video' ? 'video' : 'image'] + '</span> ' + esc(i.reason) + '</td><td class="small muted">' +
        (i.removeKind === 'fill' ? 'Remove the media fill' : 'Delete the layer') + '</td><td class="num">' + mb(i.bytes) + '</td><td>' +
        (i.status === 'done' ? statusChip('done', 'Removed') : i.status === 'error' ? statusChip('error', i.reason) : '<a data-focus="' + esc(i.nodeId) + '">Show</a>') + '</td></tr>';
    }).join('');
    return '<div class="page"><h1>Clean up</h1><p class="lede">These media layers can’t be seen: they’re hidden, switched off, outside the slide, or completely covered by opaque media above them (checked at 400 points across each layer). Untick anything you want to keep.</p>' +
      '<div class="card" style="padding:8px 10px"><div class="table-wrap" style="max-height:460px"><table><thead><tr><th><input type="checkbox" class="check" data-toggle-all' + (items.every(function (i) { return i.selected; }) ? ' checked' : '') +
      '></th><th class="num">Slide</th><th>Layer</th><th>Why</th><th>Action</th><th class="num">Size</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></div>' +
      '<div class="actions"><span class="muted small">' + (pendingSel.length ? plural(pendingSel.length, 'layer') + ' selected · ' + mb(bytes) + ' of images' : 'Nothing selected') + '</span><span class="spacer"></span>' +
      (pendingSel.length ? '<button class="btn large danger" data-a="cleanup.run">Remove ' + plural(pendingSel.length, 'layer') + '</button>' : '<button class="btn large" data-next>Continue</button>') + '</div></div>';
  };

  V.videos = function () {
    if (S.scan.status !== 'done') return needScan('Videos & GIFs', 'Find the original videos, compress them and put them back.');
    var items = S.videos.items.filter(function (v) { return v.status !== 'removed'; });
    if (!items.length) return '<div class="page"><h1>Videos & GIFs</h1><p class="lede">Find the original videos, compress them and put them back.</p><div class="card empty">' + ICON.done + '<h2>No videos or animated GIFs</h2><p>Nothing to do here.</p><button class="btn" data-next>Continue</button></div></div>';
    var LBL = { pending: 'To do', searching: 'Searching', compressing: 'Compressing', ready: 'Ready', placing: 'Replacing', done: 'Replaced', not_found: 'Not found', kept: 'Left as is', error: 'Problem' };
    var toPrep = items.filter(function (v) { return ['pending', 'not_found', 'error'].indexOf(v.status) >= 0; }).length;
    var ready = items.filter(function (v) { return v.status === 'ready'; }).length;
    var busy = ['running'].indexOf(S.videos.status) >= 0 || items.some(function (v) { return v.status === 'placing'; });
    var rows = items.map(function (v) {
      var prog = v.status === 'compressing' && v.progress != null ? ' <span class="progress mini-progress"><i style="width:' + Math.round(v.progress * 100) + '%"></i></span>' : '';
      return '<tr><td class="num">' + v.slide + '</td><td class="name" title="' + esc(v.name) + '"><a data-focus="' + esc(v.nodeId) + '">' + esc(v.name) + '</a></td><td><span class="kind">' + ICON[v.kind] + ' ' + (v.kind === 'gif' ? 'GIF' : 'Video') +
        '</span></td><td>' + statusChip(v.status, LBL[v.status] || v.status) + prog + '</td><td class="num">' + mb(v.before) + (v.after ? '<span class="arrow">→</span><span class="better">' + mb(v.after) + '</span>' : '') +
        '</td><td class="small muted" style="max-width:260px" title="' + esc(v.source || '') + '">' + esc(v.detail || '') + (v.source ? '<div class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(v.source) + '</div>' : '') +
        (v.status === 'ready' ? ' <a data-reveal="' + esc(v.key) + '">Show file</a>' : '') + '</td></tr>';
    }).join('');
    var how = '<div class="card flat"><h3 style="margin-bottom:10px">How replacing works</h3><ol class="howto">' +
      '<li><b>Find</b>The original is found in Dropbox by name, then proven to be the exact file by its fingerprint (SHA-1), the same one Figma uses.</li>' +
      '<li><b>Compress</b>Visually lossless H.264 (CRF 18), sized to twice the size it’s shown on the slide, up to 4K. A quality check confirms it looks the same as the original. GIFs become silent looping MP4s.</li>' +
      '<li><b>Replace</b>Figma doesn’t let plugins set videos, so the file is shown in a folder. Drag it onto the canvas, and the plugin puts it exactly where the old one was.</li></ol></div>';
    return '<div class="page"><h1>Videos & GIFs</h1><p class="lede">Videos are usually the heaviest part of a deck. This step finds each original, compresses it, and swaps it in with the same position, size, crop and playback settings.</p>' + how +
      '<div class="card" style="padding:8px 10px"><div class="table-wrap" style="max-height:420px"><table><thead><tr><th class="num">Slide</th><th>Layer</th><th>Type</th><th>Status</th><th class="num">Size</th><th>Details</th></tr></thead><tbody>' + rows + '</tbody></table></div></div>' +
      '<div class="actions"><span class="spacer"></span>' +
      (toPrep ? '<button class="btn large' + (ready ? ' secondary' : '') + '" data-a="videos.prepare"' + (busy ? ' disabled' : '') + '>' + (S.videos.status === 'running' ? 'Preparing…' : 'Find & compress ' + toPrep) + '</button>' : '') +
      (ready ? '<button class="btn large" data-a="videos.place"' + (busy ? ' disabled' : '') + '>Replace ' + plural(ready, 'video') + ' in Figma</button>' : '') +
      (!toPrep && !ready ? '<button class="btn large" data-next>Continue</button>' : '') + '</div>' + saveCard() + '</div>';
  };

  function gridSvg(g, after) {
    var items = after ? g.plan : g.before, b = g.bounds, W = 150, H = Math.max(40, Math.min(110, W * b.h / b.w));
    var sx = W / b.w, sy = H / b.h;
    return '<svg class="thumbgrid' + (after ? ' after' : '') + '" width="' + W + '" height="' + H + '" viewBox="-1 -1 ' + (W + 2) + ' ' + (H + 2) + '">' +
      items.map(function (t) { return '<rect rx="2" x="' + ((t.x - b.x) * sx).toFixed(1) + '" y="' + ((t.y - b.y) * sy).toFixed(1) + '" width="' + (t.w * sx).toFixed(1) + '" height="' + (t.h * sy).toFixed(1) + '"/>'; }).join('') + '</svg>';
  }
  V.spacing = function () {
    if (S.scan.status !== 'done') return needScan('Spacing', 'Even out the gaps in thumbnail grids.');
    var grids = S.spacing.grids;
    if (!grids.length) return '<div class="page"><h1>Spacing</h1><p class="lede">Even out the gaps in thumbnail grids.</p><div class="card empty">' + ICON.done + '<h2>All grids are even</h2><p>No thumbnail grid needs new spacing. Collages and overlapping layouts are left alone.</p><button class="btn" data-next>Continue</button></div></div>';
    var sel = grids.filter(function (g) { return g.selected && g.status === 'pending'; }).length;
    var cards = grids.map(function (g) {
      var gaps = g.gapsX.concat(g.gapsY), lo = Math.min.apply(null, gaps), hi = Math.max.apply(null, gaps);
      return '<div class="card"><div class="card-row"><input type="checkbox" class="check" data-grid="' + esc(g.id) + '"' + (g.selected ? ' checked' : '') + (g.status !== 'pending' ? ' disabled' : '') +
        '><div class="grow"><h3>Slide ' + g.slideIndex + ' · ' + esc(g.parentName) + '</h3><div class="muted small">' + g.cols + ' × ' + g.rows + ' grid of ' + g.count + ' · gaps ' +
        (lo === hi ? lo : lo + '–' + hi) + ' px → ' + S.gap + ' px · outer edges stay put</div>' + (g.status === 'done' ? '<div style="margin-top:6px">' + statusChip('done', 'Applied') + '</div>' : '') +
        '</div>' + gridSvg(g, false) + '<span class="arrow">→</span>' + gridSvg(g, true) + '<a data-focus="' + esc(g.plan[0].id) + '">Show</a></div></div>';
    }).join('');
    return '<div class="page"><h1>Spacing</h1><p class="lede">Thumbnail grids with uneven gaps are set to ' + S.gap + ' px between images, keeping the grid’s outer edges where they are. Collages and overlapping layouts are skipped.</p>' + cards +
      '<div class="actions"><span class="spacer"></span>' + (sel ? '<button class="btn large" data-a="spacing.run">Apply to ' + plural(sel, 'grid') + '</button>' : '<button class="btn large" data-next>Continue</button>') + '</div></div>';
  };

  V.align = function () {
    var A = S.align || { status: 'idle', items: [] };
    var lede = 'Lines layers up with the deck’s layout: repeating headers and footers (logos, titles, page numbers) go to the same spot on every slide, and text or images sitting a few pixels off a margin or column snap onto it. Nothing moves more than 12 px, or 40 px for headers and footers, so deliberate layouts stay as they are.';
    if (A.status === 'idle' || A.status === 'error' || A.status === 'running') {
      var running = A.status === 'running';
      return '<div class="page"><h1>Alignment</h1><p class="lede">' + lede + '</p><div class="card empty">' + ICON.search +
        '<h2>' + (running ? 'Checking every slide…' : 'Check the alignment') + '</h2><p>' + (running ? 'Finding margins, headers and footers.' : 'Nothing moves until you approve it.') + '</p>' +
        (running ? '<div class="progress indeterminate" style="max-width:320px;margin:16px auto 0"><i></i></div>' :
          '<button class="btn large" data-a="align.scan"' + (S.connections.plugin.connected ? '' : ' disabled') + '>Check alignment</button>') +
        (A.status === 'error' ? '<p class="small" style="color:var(--red)">' + esc(A.error) + '</p>' : '') + '</div></div>';
    }
    var g = A.guides || {}, items = A.items;
    function px(v) { return v == null ? '—' : (Math.round(v * 10) / 10) + ' px'; }
    var guides = '<div class="grid-4">' + tile('Left margin', px(g.left), g.source === 'layout grid' ? 'from the layout grid' : 'most common left edge') +
      tile('Right margin', px(g.right), g.columns && g.columns.length ? (g.columns.length / 2) + ' columns' : 'mirrors the left') +
      tile('Top', px(g.top), 'header line') + tile('Bottom', px(g.bottom), 'footer line') + '</div>';
    if (!items.length) return '<div class="page"><h1>Alignment</h1><p class="lede">' + lede + '</p>' + guides + '<div class="card empty">' + ICON.done +
      '<h2>Everything lines up</h2><p>No header, footer or text layer is out of line.</p><button class="btn secondary" data-a="align.scan">Check again</button></div></div>';
    var roles = ['header', 'footer', 'text', 'layer'], NAMES = { header: 'Headers', footer: 'Footers', text: 'Text', layer: 'Other layers' };
    var chips = roles.map(function (r) { var n = items.filter(function (i) { return i.role === r; }).length; return n ? '<span class="chip info">' + NAMES[r] + ' · ' + n + '</span>' : ''; }).join(' ');
    var pend = items.filter(function (i) { return i.selected && i.status === 'pending'; }).length;
    var rows = items.map(function (i) {
      var dx = Math.round((i.after.x - i.before.x) * 10) / 10, dy = Math.round((i.after.y - i.before.y) * 10) / 10;
      var mv = (dx ? (dx > 0 ? '→ ' : '← ') + Math.abs(dx) : '') + (dx && dy ? '  ' : '') + (dy ? (dy > 0 ? '↓ ' : '↑ ') + Math.abs(dy) : '');
      return '<tr><td><input type="checkbox" class="check" data-align="' + esc(i.key) + '"' + (i.selected ? ' checked' : '') + (i.status !== 'pending' ? ' disabled' : '') +
        '></td><td class="num">' + i.slide + '</td><td class="name" title="' + esc(i.name) + '">' + esc(i.name) + '</td><td><span class="chip">' + NAMES[i.role].replace(/s$/, '') +
        '</span></td><td class="small muted">' + esc(i.reason) + '</td><td class="num mono">' + mv + ' px</td><td>' +
        (i.status === 'done' ? statusChip('done', 'Aligned') : i.status === 'error' ? statusChip('error', 'Problem') : '<a data-focus="' + esc(i.id) + '">Show</a>') + '</td></tr>';
    }).join('');
    return '<div class="page"><h1>Alignment</h1><p class="lede">' + lede + '</p>' + guides +
      '<div style="margin:0 0 12px">' + chips + '</div>' +
      '<div class="card" style="padding:8px 10px"><div class="table-wrap" style="max-height:420px"><table><thead><tr><th><input type="checkbox" class="check" data-align-all' +
      (items.every(function (i) { return i.selected || i.status !== 'pending'; }) ? ' checked' : '') + '></th><th class="num">Slide</th><th>Layer</th><th>Kind</th><th>Why</th><th class="num">Move</th><th></th></tr></thead><tbody>' + rows +
      '</tbody></table></div></div><div class="actions"><button class="btn secondary" data-a="align.scan">Check again</button><span class="spacer"></span>' +
      (pend ? '<button class="btn large" data-a="align.run">Align ' + plural(pend, 'layer') + '</button>' : '<button class="btn large" data-next>Continue</button>') + '</div></div>';
  };

  function saveCard(always) {
    var r = S.report || {};
    var vids = S.videos.items.filter(function (v) { return v.status === 'ready' || v.status === 'not_found'; }).length;
    var imgs = S.images.items.filter(function (i) { return i.status === 'manual' || i.status === 'error'; }).length;
    var n = vids + imgs;
    if (!n && !always) return '';
    if (!n) return '';
    return '<div class="card" style="margin-top:16px"><div class="card-row"><div class="grow"><h3>Files to replace by hand</h3><div class="muted small">' +
      plural(n, 'item') + ' couldn’t be placed in the deck automatically. Save the ready, already-compressed files to a folder of your choice, named by slide, with a list and simple instructions.' +
      (r.replacementsTo ? '<div class="mono" style="margin-top:6px">' + esc(r.replacementsLabel || r.replacementsTo) + '</div>' : '') +
      '</div></div><button class="btn" data-a="export.replacements">Save files to replace…</button></div></div>';
  }

  V.report = function () {
    var r = S.report || {}, u = S.undo || {};
    var b = r.before || 0, a = r.after == null ? b : r.after;
    var hero = '<div class="grid-3">' + tile('Before', mb(b), 'images ' + mb(r.imagesBefore) + ' · videos ' + mb(r.videosBefore)) +
      tile('After', mb(a), 'images ' + mb(r.imagesAfter) + ' · videos ' + mb(r.videosAfter)) +
      tile('Saved', '<span class="better">' + mb(b - a) + '</span>', pct(a, b) + '% lighter · ' + (r.removed || 0) + ' removed · ' + (r.grids || 0) + ' grids · ' + (r.aligned || 0) + ' aligned') + '</div>';
    var bars = b ? '<div class="card"><h3 style="margin-bottom:12px">Deck media weight</h3><div class="small muted">Before</div><div class="meter" style="height:10px;margin:4px 0 12px"><i style="width:100%;background:var(--fg-3)"></i></div>' +
      '<div class="small muted">After</div><div class="meter" style="height:10px;margin-top:4px"><i style="width:' + Math.max(1, Math.round(a / b * 100)) + '%"></i></div></div>' : '';
    var undo = '<div class="card"><div class="card-row"><div class="grow"><h3>Undo</h3><div class="muted small">' + (u.reversible ? plural(u.reversible, 'image swap or move') + ' can be put back here. ' : 'Nothing to put back here. ') +
      'Removed layers and replaced videos come back through Figma’s <b>File › Show version history</b>' + (u.versionSaved ? ', where a version was saved before the first change.' : '.') + '</div></div>' +
      '<button class="btn secondary" data-a="undo.run"' + (u.reversible ? '' : ' disabled') + '>Undo changes</button></div></div>';
    var exp = '<div class="card"><div class="card-row"><div class="grow"><h3>Export</h3><div class="muted small">A report for the team, a replace list (CSV) for anything that needs a person' +
      (r.replaceCount ? ' (' + plural(r.replaceCount, 'item') + ')' : '') + ', and the undo log.' +
      (r.exportedTo ? '<div class="mono" style="margin-top:6px">' + esc(r.exportedLabel || r.exportedTo) + '</div>' : '') + '</div></div>' +
      (r.exportedTo ? '<button class="btn secondary" data-a="open" data-p="' + esc(JSON.stringify({ path: r.exportedTo })) + '">Show folder</button>' : '') +
      '<button class="btn" data-a="report.export">Export report</button></div></div>';
    var save = saveCard(true);
    return '<div class="page"><h1>Report</h1><p class="lede">What changed in <b>' + esc((S.deck || {}).name || 'the deck') + '</b>.</p>' + hero + bars + save + exp + undo + '</div>';
  };

  // ---------------------------------------------------------------- render
  var lastHtml = '';
  function render(force) {
    if (!S) return;
    renderSidebar();
    renderStatus();
    var focused = document.activeElement && document.activeElement.id === 'deckUrl';
    if (focused && !force) return; // don't steal the cursor while typing
    var html = (V[view] || V.connect)();
    if (html !== lastHtml || force) {
      var scroll = {};
      document.querySelectorAll('.table-wrap').forEach(function (t, i) { scroll[i] = t.scrollTop; });
      var keepScroll = !force && $('content').scrollTop;
      $('content').innerHTML = html;
      lastHtml = html;
      if (!force) {
        $('content').scrollTop = keepScroll;
        document.querySelectorAll('.page').forEach(function (p) { p.style.animation = 'none'; });
        document.querySelectorAll('.table-wrap').forEach(function (t, i) { t.scrollTop = scroll[i] || 0; });
      }
      var go2 = $('deckGo');
      if (go2) {
        go2.onclick = function () { act('deck.set', { url: $('deckUrl').value }).then(function () { render(true); }); };
        $('deckUrl').onkeydown = function (e) { if (e.key === 'Enter') go2.click(); };
      }
    }
  }

  // ------------------------------------------------------------- events
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-step],[data-a],[data-next],[data-go],[data-focus],[data-reveal],[data-open],[data-toggle],[data-toggle-all],[data-grid],[data-align],[data-align-all],[data-url]');
    if (!t) return;
    if (t.dataset.step) return go(t.dataset.step);
    if (t.hasAttribute('data-next')) return nextStep();
    if (t.dataset.go) return go(t.dataset.go);
    if (t.dataset.open === 'settings') return openSettings();
    if (t.dataset.url) { if (!t.closest('#settings')) act('open_url', { url: t.dataset.url }); return; }
    if (t.dataset.reveal) return act('videos.reveal', { key: t.dataset.reveal });
    if (t.hasAttribute('data-toggle-all')) return act('cleanup.toggle', { all: t.checked });
    if (t.dataset.toggle) return act('cleanup.toggle', { key: t.dataset.toggle });
    if (t.dataset.grid) return act('spacing.toggle', { id: t.dataset.grid });
    if (t.dataset.align) return act('align.toggle', { key: t.dataset.align });
    if (t.hasAttribute('data-align-all')) return act('align.toggle', { all: t.checked });
    if (t.dataset.focus) return act('focus', { nodeId: t.dataset.focus }).catch(function () {});
    if (t.dataset.a) {
      var params = t.dataset.p ? JSON.parse(t.dataset.p) : {};
      t.disabled = true;
      act(t.dataset.a, params).catch(function () {}).then(function () { t.disabled = false; });
    }
  });

  function openSettings(expand) {
    call('settings.get').then(function (s) {
      ['figma_client_id', 'dropbox_app_key', 'local_dropbox_root', 'ffmpeg'].forEach(function (k) { $('s_' + k).value = s[k] || ''; });
      $('s_thumbnail_gap').value = s.thumbnail_gap;
      $('s_quality').value = s.quality || 'best';
      $('s_figma_client_secret').value = '';
      var det = s.detected || {};
      $('s_local_dropbox_root').placeholder = det.local_dropbox_root ? 'Found automatically: ' + det.local_dropbox_root : 'Not found — see ⓘ';
      $('s_ffmpeg').placeholder = det.ffmpeg ? 'Found automatically: ' + det.ffmpeg : 'Not found — see ⓘ';
      document.querySelectorAll('.redirect').forEach(function (c) { c.textContent = (s.redirects || {})[c.dataset.provider] || ''; });
      document.querySelectorAll('.help.on, .info.on').forEach(function (e) { e.classList.remove('on'); });
      $('s_figma_client_secret').placeholder = s.figma_client_secret_set ? 'Saved — leave empty to keep it' : 'Paste the client secret';
      var from = Object.keys(s.sources).filter(function (k) { return s.sources[k] === 'environment' || s.sources[k] === 'studio file'; })
        .map(function (k) { return k + ' (' + s.sources[k] + ')'; });
      $('s_info').textContent = 'Plugin bridge: localhost:' + s.port + '. Data: ' + s.dataDir + (from.length ? '. Set elsewhere: ' + from.join(', ') : '');
      $('settings').classList.add('on');
      if (typeof expand === 'string' && $(expand)) {
        var det = $(expand).closest('details'); if (det) det.open = true;
        $(expand).classList.add('on'); document.querySelector('[data-help="' + expand + '"]').classList.add('on');
      } else { $('s_figma_client_id').focus(); }
    });
  }
  $('openSettings').onclick = function () { openSettings(); };

  // Settings help: ⓘ toggles, copy, external links, file/folder pickers.
  document.querySelectorAll('.help').forEach(function (h) {
    var inner = document.createElement('div'); inner.className = 'help-inner';
    while (h.firstChild) inner.appendChild(h.firstChild);
    var wrap = document.createElement('div'); wrap.appendChild(inner); h.appendChild(wrap);
  });
  $('settings').addEventListener('click', function (e) {
    var t = e.target.closest('[data-help],[data-copy],[data-url],[data-pick]');
    if (!t) return;
    e.preventDefault();
    if (t.dataset.help) {
      var h = $(t.dataset.help), open = !h.classList.contains('on');
      h.classList.toggle('on', open); t.classList.toggle('on', open);
      if (open) setTimeout(function () { h.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, 120);
    } else if (t.dataset.copy) {
      var code = document.querySelector('.redirect[data-provider="' + t.dataset.copy + '"]').textContent;
      var done = function () { var o = t.textContent; t.textContent = 'Copied ✓'; setTimeout(function () { t.textContent = o; }, 1400); };
      if (navigator.clipboard) navigator.clipboard.writeText(code).then(done, function () { act('copy', { text: code }).then(done); });
      else act('copy', { text: code }).then(done);
    } else if (t.dataset.url) {
      act('open_url', { url: t.dataset.url });
    } else if (t.dataset.pick) {
      act('pick', { kind: t.dataset.pick }).then(function (p) { if (p) $(t.dataset.target).value = p; });
    }
  });
  $('s_cancel').onclick = function () { $('settings').classList.remove('on'); };
  $('settings').onclick = function (e) { if (e.target === $('settings')) $('settings').classList.remove('on'); };
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') $('settings').classList.remove('on'); });
  $('s_save').onclick = function () {
    var p = {};
    ['figma_client_id', 'dropbox_app_key', 'local_dropbox_root', 'ffmpeg', 'figma_client_secret', 'thumbnail_gap', 'quality'].forEach(function (k) { p[k] = $('s_' + k).value; });
    if (!p.figma_client_secret) delete p.figma_client_secret;
    act('settings.save', p, 'Settings saved').then(function () { $('settings').classList.remove('on'); });
  };
  $('s_forget').onclick = function () { act('plugin.forget', {}, 'Plugins unpaired. A new code is shown on the Connect page.'); };

  connect();
})();
