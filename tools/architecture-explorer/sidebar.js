/* =============================================================
   ArchSidebar — IDE-style file-tree navigator + layer key
   Wired by app.js via: window.ArchSidebar.init(window.ArchExplorer)
   No external dependencies. Vanilla JS only.
   ============================================================= */

window.ArchSidebar = (function () {
  'use strict';

  /* ---- state ---- */
  var _api = null;
  var _expandedLayers = {};   // layerId → bool  (tree sections)
  var _expandedDirs   = {};   // "layerId/dir/path" → bool
  var _isDragging     = false;
  var _dragOffX = 0, _dragOffY = 0;
  var _filterText = '';

  /* ---- helpers ---- */
  function el(tag, cls, attrs) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /* ---- shape glyph helper ---- */
  function shapeGlyph(order) {
    if (window.ArchTheme && typeof window.ArchTheme.shapeForLayerOrder === 'function') {
      var name = window.ArchTheme.shapeForLayerOrder(order);
      var map = {
        box:         '▪',
        sphere:      '●',
        cone:        '▲',
        cylinder:    '⬤',
        torus:       '◎',
        octahedron:  '◆',
        tetrahedron: '△',
        icosahedron: '⬡',
        dodecahedron:'⬢',
        capsule:     '⬭',
        plane:       '▬',
      };
      return map[name] || '▪';
    }
    return null;  // caller falls back to color-swatch only
  }

  /* ---- build directory tree from flat node list ---- */
  // Returns: { [layerId]: { layer, dirs: <nested-tree> } }
  // nested-tree: node is either { __file: nodeObj } or { [dirSegment]: <nested-tree> }
  function buildTree(graph) {
    var layers  = graph.layers || [];
    var nodes   = graph.nodes  || [];

    // index layers by id
    var layerById = {};
    layers.forEach(function (l) { layerById[l.id] = l; });

    // group nodes by layer
    var byLayer = {};
    nodes.forEach(function (node) {
      var lid = node.layer;
      if (!byLayer[lid]) byLayer[lid] = [];
      byLayer[lid].push(node);
    });

    var result = {};
    // process in layer.order order
    var sorted = layers.slice().sort(function (a, b) { return a.order - b.order; });
    sorted.forEach(function (layer) {
      var layerNodes = byLayer[layer.id] || [];
      var dirs = {};
      layerNodes.forEach(function (node) {
        var parts = (node.file_path || node.label || node.id).split('/');
        var cur = dirs;
        for (var i = 0; i < parts.length - 1; i++) {
          var seg = parts[i];
          if (!cur[seg]) cur[seg] = {};
          cur = cur[seg];
        }
        // leaf — store by filename; handle duplicate basenames with index suffix
        var basename = parts[parts.length - 1];
        var key = '__file__' + basename;
        // if already occupied (two files same basename in same dir), disambiguate
        while (cur[key]) key += '_';
        cur[key] = node;
      });
      result[layer.id] = { layer: layer, dirs: dirs, nodes: layerNodes };
    });
    return result;
  }

  /* ---- render layer key ---- */
  function renderLayerKey(container, graph, treeData) {
    var header = el('div', 'sb-section-title');
    header.textContent = 'Layers';
    container.appendChild(header);

    var list = el('div', 'sb-layer-key');
    var sorted = (graph.layers || []).slice().sort(function (a, b) { return a.order - b.order; });

    sorted.forEach(function (layer) {
      var count = (treeData[layer.id] && treeData[layer.id].nodes.length) || 0;
      var row = el('div', 'sb-layer-row');
      row.dataset.layerId = layer.id;

      // swatch / glyph
      var glyph = shapeGlyph(layer.order);
      var swatch = el('span', 'sb-layer-swatch');
      if (glyph) {
        swatch.textContent = glyph;
        swatch.style.color = layer.color || '#aaa';
      } else {
        swatch.style.background = layer.color || '#888';
      }

      var label = el('span', 'sb-layer-label');
      label.textContent = layer.label || layer.id;

      var cnt = el('span', 'sb-layer-count');
      cnt.textContent = count;

      row.appendChild(swatch);
      row.appendChild(label);
      row.appendChild(cnt);

      row.addEventListener('click', function () {
        scrollToLayer(layer.id);
        // expand that layer's subtree
        _expandedLayers[layer.id] = true;
        rebuildTree();
      });

      list.appendChild(row);
    });
    container.appendChild(list);
  }

  /* ---- render search box ---- */
  function renderSearch(container) {
    var wrap = el('div', 'sb-search-wrap');
    var input = el('input', 'sb-search-input', { type: 'text', placeholder: 'Filter files…', spellcheck: 'false' });
    wrap.appendChild(input);
    container.appendChild(wrap);

    input.addEventListener('input', function () {
      _filterText = input.value.trim().toLowerCase();
      rebuildTree();
    });
  }

  /* ---- recursive tree renderer ---- */
  var _treeRoot    = null;   // the <div> that holds the file tree
  var _treeData    = null;   // result of buildTree()
  var _graphRef    = null;

  function rebuildTree() {
    if (!_treeRoot || !_treeData) return;
    _treeRoot.innerHTML = '';
    var sorted = Object.keys(_treeData).sort(function (a, b) {
      return _treeData[a].layer.order - _treeData[b].layer.order;
    });
    sorted.forEach(function (lid) {
      var entry = _treeData[lid];
      renderLayerSection(_treeRoot, entry.layer, entry.dirs, entry.nodes);
    });
  }

  function renderLayerSection(container, layer, dirs, nodes) {
    var hasMatch = _filterText ? nodes.some(function (n) {
      return (n.file_path || n.label || '').toLowerCase().indexOf(_filterText) !== -1;
    }) : true;
    if (!hasMatch) return;

    var expanded = !!_expandedLayers[layer.id];

    // layer header row
    var header = el('div', 'sb-tree-layer' + (expanded ? ' expanded' : ''));
    header.style.borderLeft = '3px solid ' + (layer.color || '#555');
    header.dataset.layerId = layer.id;

    var chevron = el('span', 'sb-chevron');
    chevron.textContent = expanded ? '▾' : '▸';

    var swatch = el('span', 'sb-layer-swatch-sm');
    var g = shapeGlyph(layer.order);   // per-layer shape glyph doubles as the legend
    if (g) { swatch.textContent = g; swatch.style.color = layer.color || '#888'; swatch.style.background = 'transparent'; }
    else { swatch.style.background = layer.color || '#888'; }

    var lbl = el('span', 'sb-tree-layer-label');
    lbl.textContent = layer.label || layer.id;

    var cnt = el('span', 'sb-layer-count');
    cnt.textContent = nodes.length;

    header.appendChild(chevron);
    header.appendChild(swatch);
    header.appendChild(lbl);
    header.appendChild(cnt);

    header.addEventListener('click', function () {
      _expandedLayers[layer.id] = !_expandedLayers[layer.id];
      rebuildTree();
    });
    container.appendChild(header);

    if (expanded) {
      var subtree = el('div', 'sb-subtree');
      subtree.style.borderLeft = '1px solid ' + (layer.color || '#555') + '33';
      renderDirNode(subtree, dirs, layer, layer.id, 0);
      container.appendChild(subtree);
    }
  }

  function renderDirNode(container, node, layer, pathKey, depth) {
    // separate dirs and files
    var files = [];
    var subdirs = [];
    Object.keys(node).forEach(function (key) {
      if (key.startsWith('__file__')) {
        files.push({ key: key, nodeObj: node[key] });
      } else {
        subdirs.push(key);
      }
    });
    subdirs.sort();
    files.sort(function (a, b) {
      var la = (a.nodeObj.label || a.key).toLowerCase();
      var lb = (b.nodeObj.label || b.key).toLowerCase();
      return la < lb ? -1 : la > lb ? 1 : 0;
    });

    // render subdirs first
    subdirs.forEach(function (seg) {
      var childKey = pathKey + '/' + seg;
      var matchInside = _filterText ? nodeHasMatch(node[seg]) : true;
      if (!matchInside) return;

      var expanded = !!_expandedDirs[childKey];
      var dirRow = el('div', 'sb-tree-dir' + (expanded ? ' expanded' : ''));
      dirRow.style.paddingLeft = (12 + depth * 14) + 'px';

      var chev = el('span', 'sb-chevron');
      chev.textContent = expanded ? '▾' : '▸';

      var icon = el('span', 'sb-dir-icon');
      icon.textContent = expanded ? '📂' : '📁';

      var name = el('span', 'sb-dir-name');
      name.textContent = seg;

      dirRow.appendChild(chev);
      dirRow.appendChild(icon);
      dirRow.appendChild(name);

      dirRow.addEventListener('click', function (e) {
        e.stopPropagation();
        _expandedDirs[childKey] = !_expandedDirs[childKey];
        rebuildTree();
      });
      container.appendChild(dirRow);

      if (expanded) {
        var childContainer = el('div', 'sb-subtree');
        renderDirNode(childContainer, node[seg], layer, childKey, depth + 1);
        container.appendChild(childContainer);
      }
    });

    // render files
    files.forEach(function (f) {
      var fileNode = f.nodeObj;
      var filePath = fileNode.file_path || fileNode.id || '';
      var label    = fileNode.label || filePath.split('/').pop();

      if (_filterText && filePath.toLowerCase().indexOf(_filterText) === -1
          && label.toLowerCase().indexOf(_filterText) === -1) {
        return;
      }

      var row = el('div', 'sb-tree-file');
      row.style.paddingLeft = (12 + depth * 14) + 'px';
      row.title = filePath + (fileNode.loc ? '  (' + fileNode.loc + ' lines)' : '');

      var icon = el('span', 'sb-file-icon');
      icon.textContent = fileIconFor(label);

      var name = el('span', 'sb-file-name');
      // highlight filter match
      if (_filterText && label.toLowerCase().indexOf(_filterText) !== -1) {
        var idx = label.toLowerCase().indexOf(_filterText);
        name.innerHTML = esc(label.slice(0, idx))
          + '<mark class="sb-match">' + esc(label.slice(idx, idx + _filterText.length)) + '</mark>'
          + esc(label.slice(idx + _filterText.length));
      } else {
        name.textContent = label;
      }

      var meta = el('span', 'sb-file-meta');
      if (fileNode.loc) meta.textContent = fileNode.loc + 'L';

      row.appendChild(icon);
      row.appendChild(name);
      row.appendChild(meta);

      row.addEventListener('click', function (e) {
        e.stopPropagation();
        // deselect previous
        var prev = document.querySelector('.sb-tree-file.active');
        if (prev) prev.classList.remove('active');
        row.classList.add('active');
        if (_api && typeof _api.focusFile === 'function') {
          _api.focusFile(fileNode.id || filePath);
        }
      });

      container.appendChild(row);
    });
  }

  // recursively check if any leaf in a subtree matches the filter
  function nodeHasMatch(node) {
    return Object.keys(node).some(function (key) {
      if (key.startsWith('__file__')) {
        var n = node[key];
        var fp = n.file_path || n.id || '';
        var lb = n.label || fp.split('/').pop();
        return fp.toLowerCase().indexOf(_filterText) !== -1
            || lb.toLowerCase().indexOf(_filterText) !== -1;
      }
      return nodeHasMatch(node[key]);
    });
  }

  /* ---- file icon heuristic ---- */
  var _iconMap = {
    ts: '⟨⟩', tsx: '⚛', js: '⟨⟩', jsx: '⚛',
    py: '🐍', json: '{}', md: '✎', sh: '⌘',
    css: '🎨', html: '🌐', yml: 'Y', yaml: 'Y',
    toml: 'T', env: '🔑', lock: '🔒', txt: '📄',
    rs: '⚙', go: '⛳', rb: 'Rb', java: '☕',
  };
  function fileIconFor(name) {
    var ext = (name.split('.').pop() || '').toLowerCase();
    return _iconMap[ext] || '📄';
  }

  /* ---- scroll to a layer section in the tree ---- */
  function scrollToLayer(layerId) {
    if (!_treeRoot) return;
    var target = _treeRoot.querySelector('[data-layer-id="' + layerId + '"]');
    if (target) target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  /* ---- drag-to-reposition ---- */
  function setupDrag(panel, dragHandle) {
    dragHandle.addEventListener('mousedown', function (e) {
      // Only primary button; don't intercept clicks on child buttons
      if (e.button !== 0) return;
      var rect = panel.getBoundingClientRect();
      _dragOffX = e.clientX - rect.left;
      _dragOffY = e.clientY - rect.top;
      _isDragging = true;
      panel.style.transition = 'none';
      e.preventDefault();
    });

    document.addEventListener('mousemove', function (e) {
      if (!_isDragging) return;
      var x = e.clientX - _dragOffX;
      var y = e.clientY - _dragOffY;
      // clamp inside viewport
      x = Math.max(0, Math.min(x, window.innerWidth  - panel.offsetWidth));
      y = Math.max(0, Math.min(y, window.innerHeight - panel.offsetHeight));
      panel.style.left   = x + 'px';
      panel.style.top    = y + 'px';
      panel.style.right  = 'auto';
      panel.style.bottom = 'auto';
    });

    document.addEventListener('mouseup', function () {
      if (_isDragging) {
        _isDragging = false;
        panel.style.transition = '';
      }
    });
  }

  /* ---- toggle wiring ---- */
  function setupToggle(panel) {
    var btn = document.getElementById('btn-sidebar');
    if (!btn) return;
    btn.addEventListener('click', function () {
      panel.classList.toggle('hidden');
      btn.classList.toggle('active');
    });
  }

  /* ---- public init ---- */
  function init(api) {
    if (!api) { console.warn('[ArchSidebar] init called without api'); return; }
    _api = api;

    var panel = document.getElementById('sidebar');
    if (!panel) { console.warn('[ArchSidebar] #sidebar not found in DOM'); return; }

    var graph = api.GRAPH;
    if (!graph) { console.warn('[ArchSidebar] api.GRAPH is missing'); return; }

    /* ---- build DOM structure ---- */

    // drag header
    var headerBar = el('div', 'sb-header');
    var headerTitle = el('span', 'sb-header-title');
    headerTitle.textContent = 'Explorer';
    var collapseBtn = el('button', 'sb-collapse-btn');
    collapseBtn.textContent = '×';
    collapseBtn.title = 'Hide sidebar';
    collapseBtn.addEventListener('click', function () {
      panel.classList.add('hidden');
      var tb = document.getElementById('btn-sidebar');
      if (tb) tb.classList.remove('active');
    });
    headerBar.appendChild(headerTitle);
    headerBar.appendChild(collapseBtn);
    panel.appendChild(headerBar);

    // single file-tree section (the per-layer shape glyph doubles as the legend,
    // so a separate LAYERS key section is redundant — merged into the tree rows).
    _treeData = buildTree(graph);
    var treeSection = el('div', 'sb-tree-section');

    renderSearch(treeSection);

    _treeRoot = el('div', 'sb-tree-root');
    treeSection.appendChild(_treeRoot);
    panel.appendChild(treeSection);

    // initial render (all layers collapsed)
    rebuildTree();

    /* ---- wiring ---- */
    setupDrag(panel, headerBar);
    setupToggle(panel);

    // mark btn-sidebar active initially (sidebar is visible by default)
    var tb = document.getElementById('btn-sidebar');
    if (tb) tb.classList.add('active');
  }

  return { init: init };
}());
