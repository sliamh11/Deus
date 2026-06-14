/* =============================================================
   ArchViews — save / restore / delete NAMED views.
   Wired by app.js via: window.ArchViews.init(window.ArchExplorer)

   A "view" bundles NAVIGATION + APPEARANCE:
     nav   = ArchExplorer.snapshot()  -> {expanded, selectedId, isolated, layoutMode, cam}
     theme = ArchTheme.getTheme()     -> node/edge/scene/camera/label appearance (no dagMode)

   Persistence: localStorage, PER-REPO (keyed by GRAPH.meta.repo) so the same
   explorer origin reused across `deus arch <repo>` runs keeps each repo's views
   separate (deus-cmd.sh reuses one URL across projects). All storage access is
   wrapped in try/catch -> graceful no-op when localStorage is unavailable
   (private mode / quota), mirroring theme.js saveTheme/loadTheme.

   Pattern: a Registry (view-name -> {nav, theme}) rendered as a re-render-on-change
   DOM list. The named, persisted views extend app.js's in-memory Memento
   (snapshot/restore). No external dependencies. Vanilla JS only.
   ============================================================= */

window.ArchViews = (function () {
  'use strict';

  /* ---- state ---- */
  var _api = null;          // window.ArchExplorer
  var _panel = null;
  var _listEl = null;
  var _nameInput = null;
  var _btn = null;
  var STORAGE_PREFIX = 'archexplorer.views.v1::';

  /* ---- helpers ---- */
  function el(tag, cls, attrs) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }

  /* ---- per-repo store (Registry: name -> {nav, theme, savedAt}) ---- */
  var _warnedNoRepo = false;
  function repoKey() {
    var repo = '';
    try { repo = (window.GRAPH && window.GRAPH.meta && window.GRAPH.meta.repo) || ''; } catch (e) { /* noop */ }
    if (!repo && !_warnedNoRepo) {
      // A repo-less graph-data.js means every such session shares one key — flag it
      // once so a misconfigured build is debuggable, without polluting normal runs.
      _warnedNoRepo = true;
      try { console.warn('[ArchViews] GRAPH.meta.repo missing — saved views share a single unscoped key.'); } catch (e) { /* noop */ }
    }
    return STORAGE_PREFIX + repo;
  }
  function loadAll() {
    try {
      var raw = localStorage.getItem(repoKey());
      if (!raw) return {};
      var o = JSON.parse(raw);
      return (o && typeof o === 'object') ? o : {};
    } catch (e) { return {}; }
  }
  function saveAll(obj) {
    try { localStorage.setItem(repoKey(), JSON.stringify(obj)); return true; }
    catch (e) { return false; }   // file:// SecurityError / quota exceeded
  }

  /* ---- actions ---- */
  function saveCurrent() {
    var name = (_nameInput.value || '').trim();
    if (!name) { _nameInput.focus(); return; }
    var all = loadAll();
    if (Object.prototype.hasOwnProperty.call(all, name) &&
        !window.confirm("Overwrite view '" + name + "'?")) return;
    var view;
    try {
      view = {
        nav: (_api && _api.snapshot) ? _api.snapshot() : null,
        theme: (window.ArchTheme && window.ArchTheme.getTheme) ? window.ArchTheme.getTheme() : null,
        savedAt: Date.now()
      };
    } catch (e) { showStatus('Could not capture the current view.'); return; }   // snapshot/getTheme threw — abort cleanly
    all[name] = view;
    if (!saveAll(all)) { showStatus('Storage unavailable — view not saved.'); return; }
    _nameInput.value = '';
    renderList();
  }

  function restoreView(name) {
    var v = loadAll()[name];
    if (!v) return;
    // Appearance FIRST: applyTheme may rebuild the graph (controlType is
    // construction-only); the rebind guard prevents re-entry. Navigation LAST:
    // restore() regenerates graphData (reseeds node positions deterministically in
    // layered mode / re-settles+frames in tree mode) and animates the camera to the
    // saved viewpoint, so it lands correctly even across a controlType rebuild.
    if (v.theme && window.ArchTheme && window.ArchTheme.applyTheme) window.ArchTheme.applyTheme(v.theme);
    if (v.nav && _api && _api.restore) _api.restore(v.nav, 700);
  }

  function deleteView(name) {
    var all = loadAll();
    if (!Object.prototype.hasOwnProperty.call(all, name)) return;
    delete all[name];
    if (!saveAll(all)) { showStatus('Storage unavailable — could not delete view.'); return; }
    renderList();
  }

  /* ---- UI ---- */
  function showStatus(msg) {
    var s = _panel && _panel.querySelector('.av-status');
    if (!s) return;
    s.textContent = msg;          // textContent: never interpret as HTML
    s.style.display = msg ? 'block' : 'none';
  }

  function renderList() {
    if (!_listEl) return;
    _listEl.innerHTML = '';
    var all = loadAll();
    var names = Object.keys(all).sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : 1; });
    if (!names.length) {
      var empty = el('div', 'av-empty');
      empty.textContent = 'No saved views for this repo.';
      _listEl.appendChild(empty);
      return;
    }
    names.forEach(function (name) {
      var row = el('div', 'av-row');
      var label = el('span', 'av-name');
      label.textContent = name;             // user input -> textContent, XSS-safe
      label.title = name;
      var restoreBtn = el('button', 'av-restore', { title: 'Restore this view' });
      restoreBtn.textContent = 'Restore';
      restoreBtn.addEventListener('click', function () { restoreView(name); });
      var delBtn = el('button', 'av-del', { title: 'Delete this view (no undo)' });
      delBtn.textContent = '×';        // ×
      delBtn.addEventListener('click', function () { deleteView(name); });
      row.appendChild(label);
      row.appendChild(restoreBtn);
      row.appendChild(delBtn);
      _listEl.appendChild(row);
    });
  }

  function buildPanel() {
    _panel = el('div', 'av-panel hidden', { id: 'views-panel' });

    var head = el('div', 'av-head');
    head.textContent = 'Saved views';
    _panel.appendChild(head);

    var saveRow = el('div', 'av-save-row');
    _nameInput = el('input', 'av-input', { type: 'text', placeholder: 'View name…', maxlength: '80' });
    _nameInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); saveCurrent(); }
    });
    var saveBtn = el('button', 'av-save', { title: 'Save the current view' });
    saveBtn.textContent = 'Save current';
    saveBtn.addEventListener('click', saveCurrent);
    saveRow.appendChild(_nameInput);
    saveRow.appendChild(saveBtn);
    _panel.appendChild(saveRow);

    var status = el('div', 'av-status');
    status.style.display = 'none';
    _panel.appendChild(status);

    _listEl = el('div', 'av-list');
    _panel.appendChild(_listEl);

    document.body.appendChild(_panel);
    renderList();
  }

  function togglePanel() {
    if (!_panel) return;
    var hidden = _panel.classList.toggle('hidden');
    if (_btn) _btn.classList.toggle('active', !hidden);
    if (!hidden) {
      showStatus('');             // clear any stale status
      renderList();               // reflect any external changes
      if (_nameInput) _nameInput.focus();
    }
  }

  /* ---- public API ---- */
  function init(api) {
    _api = api;
    buildPanel();
    _btn = document.getElementById('btn-views');
    if (_btn) _btn.addEventListener('click', togglePanel);
  }

  return { init: init };
}());
