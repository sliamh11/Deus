/* ============================================================
   Architecture Explorer — app.js  (3D)
   Structure + behavior. Reads window.GRAPH (graph-data.js),
   renders a 3D force graph with the 11 layers PINNED to fixed
   z-depth planes (NOT dagMode — our layers are fixed, assigned
   by layers.json). Click a layer to expand its files onto that
   layer's plane; click a file for the detail panel.

   Appearance (node shape/color/size, edges, scene, labels) is
   owned entirely by theme.js, attached via ArchTheme.init(graph, AE).
   This file only builds the graph + interaction and exposes the
   AE contract that theme.js reads.
   ============================================================ */
(function () {
  'use strict';

  var G = window.GRAPH;
  if (!G || !G.nodes || !G.layers) {
    document.getElementById('no-data').classList.remove('hidden');
    return;
  }

  // ---- indexes -------------------------------------------------
  var layersById = {};
  G.layers.forEach(function (l) { layersById[l.id] = l; });
  var nLayers = G.layers.length;

  var filesByLayer = {};       // layerId -> [fileNode,...]
  var fileById = {};           // file_path -> fileNode (raw GRAPH node)
  G.nodes.forEach(function (n) {
    (filesByLayer[n.layer] = filesByLayer[n.layer] || []).push(n);
    fileById[n.id] = n;
  });

  // fan-in / fan-out per file (for size encoding + detail panel)
  var fanIn = {}, fanOut = {};
  G.edges.forEach(function (e) {
    fanOut[e.source] = (fanOut[e.source] || 0) + 1;
    fanIn[e.target] = (fanIn[e.target] || 0) + 1;
  });

  // layer aggregate: total LOC + file count + layer<->layer edges
  var layerLoc = {}, layerEdgeAgg = {};
  G.layers.forEach(function (l) { layerLoc[l.id] = 0; });
  G.nodes.forEach(function (n) { layerLoc[n.layer] += (n.loc || 0); });
  G.edges.forEach(function (e) {
    var s = fileById[e.source], t = fileById[e.target];
    if (!s || !t || s.layer === t.layer) return;
    // Space is the join/split delimiter (see the split at the link-building step):
    // layer ids and edge-kind values are single tokens with no spaces, so this
    // round-trips. A space in a layers.json id would corrupt the parsed `kind`.
    var key = s.layer + ' ' + t.layer + ' ' + e.kind;
    layerEdgeAgg[key] = (layerEdgeAgg[key] || 0) + (e.weight || 1);
  });

  // ---- layout constants ---------------------------------------
  // Layers form a horizontal ROW along x (channels left -> other right), all
  // visible at once. A layer's files expand into the DEPTH (y/z disc) at that
  // layer's x. This avoids the colinear z-stack that occludes + breaks framing.
  // Wide "space-like" gaps: a layer super-node (~90 wide) in a 900 gap reads ~1:10
  // (open, roomy) vs the cramped ~1:3.5 a 360 gap gave.
  var LAYER_GAP = 900;          // x distance between layer columns — wide, "space-like"
  function layerX(order) { return (order - (nLayers - 1) / 2) * LAYER_GAP; }
  // Count-aware y/z disc radius for an expanded layer's files (sunflower seed below):
  // grows with sqrt(count) so files don't crowd as a layer gets bigger. Capped just
  // below LAYER_GAP so a single layer's disc never exceeds the inter-layer gap. NOTE:
  // two ADJACENT large layers expanded at once can still visually overlap in y/z from
  // a side-on view — explore one large layer at a time for the cleanest read.
  function FILE_R(n) { return Math.min(260 + Math.sqrt(n) * 70, LAYER_GAP * 0.95); }

  // ---- state ---------------------------------------------------
  var expanded = {};            // layerId -> true when expanded
  var selectedId = null;        // pinned file selection
  var isolatedLayer = null;     // when set: explore ONLY this layer (right-click)

  // ---- node/link id helpers -----------------------------------
  function layerNodeId(id) { return '__layer__' + id; }
  function isLayerNode(node) { return node && node.kind === 'layer'; }

  // ISOLATION: only one layer's files, spread in a free 3D cloud (fibonacci
  // sphere seed), with their intra-layer edges. Entered by right-clicking a layer.
  function buildIsolated(layerId) {
    var nodes = [], links = [];
    var lf = filesByLayer[layerId] || [];
    var ids = {};
    var R = 70 + Math.sqrt(lf.length) * 34;
    var GA = Math.PI * (1 + Math.sqrt(5));
    lf.forEach(function (f, i) {
      ids[f.id] = true;
      var phi = Math.acos(1 - 2 * (i + 0.5) / Math.max(lf.length, 1));
      var th = GA * i;
      nodes.push({
        id: f.id, kind: 'file', layer: layerId, label: f.label, file_path: f.file_path,
        loc: f.loc || 0, n_symbols: f.n_symbols || 0, color: layersById[layerId].color,
        fanIn: fanIn[f.id] || 0, fanOut: fanOut[f.id] || 0,
        x: R * Math.sin(phi) * Math.cos(th), y: R * Math.sin(phi) * Math.sin(th), z: R * Math.cos(phi)
      });
    });
    G.edges.forEach(function (e) {
      if (ids[e.source] && ids[e.target] && e.source !== e.target) {
        links.push({ source: e.source, target: e.target, kind: e.kind, weight: e.weight || 1 });
      }
    });
    return { nodes: nodes, links: links };
  }

  // Build the graphData {nodes, links} for the current expanded set.
  function buildData() {
    if (isolatedLayer) return buildIsolated(isolatedLayer);
    var nodes = [];
    var links = [];
    var present = {};           // file_path present as an expanded file node

    // Layer nodes — always present, PINNED to their x-column (a horizontal row).
    G.layers.forEach(function (l) {
      var x = layerX(l.order);
      nodes.push({
        id: layerNodeId(l.id),
        kind: 'layer',
        layer: l.id,
        label: l.label,
        color: l.color,
        nFiles: (filesByLayer[l.id] || []).length,
        loc: layerLoc[l.id],
        // hard pin so layers never move (row along x, centered in y/z)
        fx: x, fy: 0, fz: 0,
        x: x, y: 0, z: 0
      });
    });

    // Expanded layers -> their file nodes, pinned to the layer's x-column,
    // seeded as a sunflower/Fibonacci DISC in the y/z plane: radius grows as
    // sqrt(i) so area-per-file is constant (no center crowding), with a
    // golden-angle rotation between successive files (even, organic, no banding).
    G.layers.forEach(function (l) {
      if (!expanded[l.id]) return;
      var x = layerX(l.order);
      var files = filesByLayer[l.id] || [];
      var n = Math.max(files.length, 1);
      var R = FILE_R(n);
      files.forEach(function (f, i) {
        present[f.id] = true;
        var rr = R * Math.sqrt((i + 0.5) / n);
        var th = i * 2.39996323;       // golden angle ≈ 137.5° (2π·(2−φ)) in radians
        nodes.push({
          id: f.id,
          kind: 'file',
          layer: l.id,
          label: f.label,
          file_path: f.file_path,
          loc: f.loc || 0,
          n_symbols: f.n_symbols || 0,
          color: l.color,
          fanIn: fanIn[f.id] || 0,
          fanOut: fanOut[f.id] || 0,
          fx: x,                       // pin x to the layer column; y/z free
          x: x, y: Math.sin(th) * rr, z: Math.cos(th) * rr
        });
      });
    });

    // Links.
    // 1) Aggregated layer<->layer edges when BOTH endpoints are collapsed.
    Object.keys(layerEdgeAgg).forEach(function (key) {
      var parts = key.split(' ');
      var sL = parts[0], tL = parts[1], kind = parts[2], w = layerEdgeAgg[key];
      // only draw the aggregate if at least one side is collapsed
      if (expanded[sL] && expanded[tL]) return;
      var src = expanded[sL] ? null : layerNodeId(sL);
      var tgt = expanded[tL] ? null : layerNodeId(tL);
      // if a side is expanded, route its individual file edges instead (below)
      if (src && tgt) {
        links.push({ source: src, target: tgt, kind: kind, weight: w, agg: true });
      }
    });
    // 2) File-level edges where at least one endpoint is an expanded file.
    G.edges.forEach(function (e) {
      var s = fileById[e.source], t = fileById[e.target];
      if (!s || !t) return;
      var sExp = present[e.source], tExp = present[e.target];
      if (!sExp && !tExp) return;                 // neither expanded -> aggregate handled it
      var src = sExp ? e.source : layerNodeId(s.layer);
      var tgt = tExp ? e.target : layerNodeId(t.layer);
      if (src === tgt) return;
      links.push({ source: src, target: tgt, kind: e.kind, weight: e.weight || 1 });
    });

    return { nodes: nodes, links: links };
  }

  // ---- create the graph ---------------------------------------
  var el = document.getElementById('graph');
  var controlMode = 'orbit';                 // 'orbit' | 'fly' | 'trackball'

  // Apply node/link config + forces to a fresh ForceGraph3D instance. Factored
  // out so the control-type switch can REBUILD the instance: `controlType` is a
  // CONSTRUCTION option in 3d-force-graph 1.70.5 (no runtime setter), so changing
  // it means recreating the graph rather than calling a method on it.
  function configureGraph(g) {
    g.graphData(buildData())
      .nodeId('id')
      .nodeLabel(function (n) {
        return isLayerNode(n)
          ? (n.label + ' — ' + n.nFiles + ' files')
          : n.file_path;
      })
      .onNodeClick(onNodeClick)
      .onNodeRightClick(onNodeRightClick)
      .onBackgroundClick(clearSelection)
      .enableNodeDrag(false)         // drag intercepts node clicks; click-to-expand wins
      .warmupTicks(0)                // animate the spring-out (don't pre-settle)
      .cooldownTicks(160);           // longer settle for the wider, more energetic layout
    // Strong repulsion = roomy, dynamic spacing; long links so files spring far out
    // into a spacious cloud when a layer expands (layer nodes stay pinned anchors).
    g.d3Force('charge').strength(-750);
    if (g.d3Force('link')) g.d3Force('link').distance(200);
    return g;
  }

  var Graph = configureGraph(ForceGraph3D({ controlType: controlMode })(el));

  // Rebuild the graph with a new camera-control scheme, preserving expand/select
  // state, node positions, camera viewpoint, and the appearance binding. See
  // configureGraph() for why a rebuild (not a setter) is required.
  function rebuildGraph(newMode) {
    controlMode = newMode;
    var cam = Graph.cameraPosition();
    // Snapshot positions so switching modes doesn't re-scatter the layout.
    var pos = {};
    Graph.graphData().nodes.forEach(function (n) {
      pos[n.id] = { x: n.x, y: n.y, z: n.z, fx: n.fx, fy: n.fy, fz: n.fz };
    });
    // Tear the old instance down. Each step is guarded independently — these are
    // vendored internals. renderer().dispose() explicitly frees the WebGL context
    // (browsers cap live contexts ~8-16 per page; without this, repeated mode
    // switches would eventually exhaust them and blank the canvas).
    try { if (typeof Graph.pauseAnimation === 'function') Graph.pauseAnimation(); } catch (e) { /* noop */ }
    try { if (typeof Graph._destructor === 'function') Graph._destructor(); } catch (e) { /* noop */ }
    try { if (typeof Graph.renderer === 'function') Graph.renderer().dispose(); } catch (e) { /* noop */ }
    el.innerHTML = '';
    Graph = configureGraph(ForceGraph3D({ controlType: newMode })(el));
    // Seed the saved positions onto the new node objects (graphData() builds the
    // node array synchronously, so they exist immediately).
    Graph.graphData().nodes.forEach(function (n) {
      var p = pos[n.id];
      if (p) { n.x = p.x; n.y = p.y; n.z = p.z; n.fx = p.fx; n.fy = p.fy; n.fz = p.fz; }
    });
    // Re-point the appearance layer at the new instance.
    if (window.ArchTheme && window.ArchTheme.rebind) window.ArchTheme.rebind(Graph);
    else if (window.ArchTheme && window.ArchTheme.init) window.ArchTheme.init(Graph, AE);
    // window.ArchExplorer.graph is a VALUE captured in an object literal — re-point
    // it so any external consumer reading the instance gets the live one.
    if (window.ArchExplorer) window.ArchExplorer.graph = Graph;
    // Restore the camera after the new renderer/camera have initialized (next
    // frame) to avoid a deferred-init race swallowing the call.
    if (cam) {
      var restore = function () { try { Graph.cameraPosition(cam, undefined, 0); } catch (e) { /* noop */ } };
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(restore);
      else restore();
    }
  }

  // Single entry point for control-scheme changes — the toolbar button and the
  // theme panel both route here. Sync theme.controlType BEFORE the rebuild so the
  // rebind's applyCamera() sees a matching value and doesn't re-trigger.
  function setControlType(mode) {
    if (mode !== 'orbit' && mode !== 'fly' && mode !== 'trackball') mode = 'orbit';
    if (mode === controlMode) return;        // no-op (also makes init-time call a no-op)
    if (window.ArchTheme && window.ArchTheme.syncControlType) window.ArchTheme.syncControlType(mode);
    rebuildGraph(mode);
    var btn = document.getElementById('btn-control');
    if (btn) btn.textContent = mode === 'orbit' ? 'Orbit' : (mode === 'fly' ? 'Fly' : 'Trackball');
    var hint = document.getElementById('hint');
    if (hint) hint.textContent = mode === 'fly'
      ? 'WASD/RF: fly · drag: look · click a layer to expand · click a file for details'
      : 'WASD/arrows: fly · left-drag: rotate · wheel: zoom · right-drag: pan · click a layer to expand · click a file for details';
  }

  // ---- interaction --------------------------------------------
  function onNodeClick(node) {
    if (isLayerNode(node)) {
      var wasExpanded = !!expanded[node.layer];
      toggleLayer(node.layer);
      if (!wasExpanded) { selectedId = node.id; if (window.ArchTheme && window.ArchTheme.onSelect) window.ArchTheme.onSelect(node); }
      else { clearSelection(); }
      flyToNode(node, 200);          // get closer to the (now expanded) layer + its files
    } else {
      selectFile(node);
      flyToNode(node, 90);           // fly right up to the file
    }
  }

  // Fly the camera to a node, framing it from `dist` units away. Handles a node
  // at the origin (channels/evolution etc.) where the direction vector is zero.
  function flyToNode(node, dist) {
    var n = Graph.graphData().nodes.find(function (x) { return x.id === node.id; });
    if (!n) return;
    var len = Math.hypot(n.x || 0, n.y || 0, n.z || 0);
    var target;
    if (len < 1) {
      target = { x: (n.x || 0) + dist * 0.4, y: (n.y || 0) + dist * 0.3, z: (n.z || 0) + dist }; // default approach
    } else {
      var ratio = 1 + dist / len;
      target = { x: (n.x || 0) * ratio, y: (n.y || 0) * ratio, z: (n.z || 0) * ratio };
    }
    Graph.cameraPosition(target, { x: n.x || 0, y: n.y || 0, z: n.z || 0 }, 1100);
  }

  // ---- isolation (explore one layer alone) --------------------
  function onNodeRightClick(node) {
    if (isLayerNode(node)) isolateLayer(node.layer);
  }
  function isolateLayer(layerId) {
    pushHistory();
    isolatedLayer = layerId;
    selectedId = null; hideDetail();
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
    if (window.ArchTheme && window.ArchTheme.onSelect) window.ArchTheme.onSelect(null);
    updateIsolationHint();
    setTimeout(function () { Graph.zoomToFit(900, 90); }, 400);   // the cloud has real 3D extent → zoomToFit is safe here
  }
  function exitIsolation() {
    if (!isolatedLayer) return;
    pushHistory();
    isolatedLayer = null;
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
    updateIsolationHint();
    frameAll(700);
  }
  function updateIsolationHint() {
    var h = document.getElementById('hint');
    if (!h) return;
    if (isolatedLayer) {
      var l = layersById[isolatedLayer];
      // esc() the dynamic label/id for consistency with the rest of the file (the
      // data is local config, but innerHTML interpolation should stay uniform).
      h.innerHTML = 'Exploring <b style="color:' + (l ? l.color : '#fff') + '">' + esc(l ? l.label : isolatedLayer) +
        '</b> only — press <b>Esc</b> or <b>Reset</b> to exit · right-click a layer to isolate it';
    } else {
      h.textContent = 'WASD/arrows: fly · left-drag: rotate · wheel: zoom · right-drag: pan · click a layer to expand · right-click to isolate · click a file for details';
    }
  }

  // ---- view-state history (Back) ------------------------------
  var history = [];
  function snapshot() {
    var exp = {}; Object.keys(expanded).forEach(function (k) { if (expanded[k]) exp[k] = true; });
    return { expanded: exp, selectedId: selectedId, isolated: isolatedLayer, cam: Graph.cameraPosition() };
  }
  function pushHistory() {
    history.push(snapshot());
    if (history.length > 50) history.shift();
    updateBackBtn();
  }
  function updateBackBtn() {
    var b = document.getElementById('btn-back');
    if (b) b.disabled = history.length === 0;
  }
  function restore(state, animMs) {
    expanded = {}; Object.keys(state.expanded).forEach(function (k) { expanded[k] = true; });
    selectedId = state.selectedId;
    isolatedLayer = state.isolated || null;
    updateIsolationHint();
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
    if (selectedId) {
      var fn = Graph.graphData().nodes.find(function (n) { return n.id === selectedId; });
      if (fn) { showDetail(fn); } else { hideDetail(); selectedId = null; }
    } else { hideDetail(); }
    if (window.ArchTheme && window.ArchTheme.onSelect) {
      window.ArchTheme.onSelect(selectedId ? Graph.graphData().nodes.find(function (n) { return n.id === selectedId; }) : null);
    }
    if (state.cam) Graph.cameraPosition(state.cam, undefined, animMs == null ? 500 : animMs);
  }
  function goBack() {
    if (!history.length) return;
    restore(history.pop(), 500);
    updateBackBtn();
  }

  function toggleLayer(layerId) {
    pushHistory();
    expanded[layerId] = !expanded[layerId];
    var cam = Graph.cameraPosition();         // preserve viewpoint across rebuild
    Graph.graphData(buildData());
    Graph.cameraPosition(cam, undefined, 0);
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
  }

  function selectFile(node) {
    pushHistory();
    selectedId = node.id;
    showDetail(node);
    if (window.ArchTheme && window.ArchTheme.onSelect) window.ArchTheme.onSelect(node);
  }

  function clearSelection() {
    if (selectedId !== null) pushHistory();
    selectedId = null;
    hideDetail();
    if (window.ArchTheme && window.ArchTheme.onSelect) window.ArchTheme.onSelect(null);
  }

  // Highlight set for focus+context. Uses the CURRENTLY RENDERED links (so both
  // file nodes and layer super-nodes get every direct connection lit — fixes the
  // "some directly-related nodes stay dim" bug), plus, for a layer node, all of
  // that layer's member files.
  function neighbours(nodeId) {
    var set = {};
    set[nodeId] = true;
    Graph.graphData().links.forEach(function (l) {
      var s = (l.source && l.source.id != null) ? l.source.id : l.source;
      var t = (l.target && l.target.id != null) ? l.target.id : l.target;
      if (s === nodeId) set[t] = true;
      if (t === nodeId) set[s] = true;
    });
    if (typeof nodeId === 'string' && nodeId.indexOf('__layer__') === 0) {
      var lid = nodeId.slice('__layer__'.length);
      (filesByLayer[lid] || []).forEach(function (f) { set[f.id] = true; });
    }
    return set;
  }

  // ---- detail panel -------------------------------------------
  var detail = document.getElementById('detail');
  var detailBody = document.getElementById('detail-body');

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
    return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;'; }); }

  // ---- open in editor -----------------------------------------
  var EDITOR_SCHEMES = { vscode: 'vscode://file', cursor: 'cursor://file', zed: 'zed://file', idea: 'idea://open?file=' };
  function editorPref() { try { return localStorage.getItem('archexplorer.editor') || 'vscode'; } catch (e) { return 'vscode'; } }
  function openInEditor(filePath, line) {
    var root = (G.meta && G.meta.repo_root) || '';
    var abs = root ? (root.replace(/\/$/, '') + '/' + filePath) : filePath;
    var ed = editorPref();
    var uri = ed === 'idea'
      ? EDITOR_SCHEMES.idea + encodeURIComponent(abs) + (line ? '&line=' + line : '')
      : EDITOR_SCHEMES[ed] + abs + (line ? ':' + line : '');
    var a = document.createElement('a'); a.href = uri; a.style.display = 'none';
    document.body.appendChild(a); a.click(); setTimeout(function () { a.remove(); }, 100);
  }

  function depList(fileId, dir) {
    // dir 'out' = Affects (this -> X); 'in' = Affected by (X -> this)
    var rows = [];
    G.edges.forEach(function (e) {
      if (dir === 'out' && e.source === fileId) rows.push({ id: e.target, kind: e.kind, w: e.weight || 1 });
      if (dir === 'in' && e.target === fileId) rows.push({ id: e.source, kind: e.kind, w: e.weight || 1 });
    });
    rows.sort(function (a, b) { return b.w - a.w; });
    if (!rows.length) return '<div class="dep-empty">none</div>';
    return rows.map(function (r) {
      var f = fileById[r.id];
      var lay = f ? layersById[f.layer] : null;
      // clickable -> teleport (focusFile)
      return '<div class="dep dep-click" data-fid="' + esc(r.id) + '" title="Go to ' + esc(r.id) + '">' +
        '<span class="dep-name">' + esc(f ? f.label : r.id) + '</span>' +
        '<span class="dep-meta ek" data-k="' + r.kind + '">' + r.kind + ' · w' + r.w + '</span>' +
        (lay ? '<span class="dep-layer" style="color:' + lay.color + '">' + esc(lay.label) + '</span>' : '') +
        '<span class="dep-go">&rarr;</span>' +
        '</div>';
    }).join('');
  }

  function showDetail(node) {
    var lay = layersById[node.layer];
    var syms = (node.top_symbols || fileById[node.id] && fileById[node.id].top_symbols || []);
    var exp = (fileById[node.id] && fileById[node.id].exported) || [];
    var ed = editorPref();
    var html = '';
    html += '<h2>' + esc(node.label) + '</h2>';
    html += '<div class="path">' + esc(node.file_path) + '</div>';
    html += '<div class="actions">' +
      '<button id="open-editor" data-fp="' + esc(node.file_path) + '">Open in editor &#8599;</button>' +
      '<select id="editor-pick" title="Which editor">' +
      ['vscode', 'cursor', 'zed', 'idea'].map(function (k) {
        return '<option value="' + k + '"' + (k === ed ? ' selected' : '') + '>' +
          (k === 'vscode' ? 'VS Code' : k === 'idea' ? 'IntelliJ' : k.charAt(0).toUpperCase() + k.slice(1)) + '</option>';
      }).join('') + '</select></div>';
    html += '<div class="tags">';
    if (lay) html += '<span class="tag" style="background:' + lay.color + '22;border-color:' + lay.color + '">' + esc(lay.label) + '</span>';
    html += '<span class="tag">' + (node.loc || 0) + ' loc</span>';
    html += '<span class="tag">' + (node.n_symbols || 0) + ' symbols</span>';
    html += '<span class="tag">fan-in ' + (node.fanIn || 0) + '</span>';
    html += '</div>';
    if (exp.length) {
      html += '<div class="sec-title">exports</div><div class="chips">' +
        exp.slice(0, 24).map(function (s) { return '<span class="chip">' + esc(s) + '</span>'; }).join('') + '</div>';
    }
    if (syms && syms.length) {
      html += '<div class="sec-title">top symbols</div>';
      html += syms.slice(0, 12).map(function (s) {
        return '<div class="sym" data-line="' + (s.line || 1) + '" data-fp="' + esc(node.file_path) + '"' +
          (s.doc ? ' data-doc="' + esc(s.doc) + '"' : '') + '>' +
          '<b>' + esc(s.name) + '</b> <span class="sym-kind">' + esc(s.kind) + '</span>' +
          (s.doc ? ' <span class="sym-doc-dot" title="has docs">&#9432;</span>' : '') +
          (s.signature ? '<div class="sig">' + esc(s.signature) + '</div>' : '') +
          '</div>';
      }).join('');
    }
    html += '<div class="sec-title">Affects &rarr;</div>' + depList(node.id, 'out');
    html += '<div class="sec-title">&larr; Affected by</div>' + depList(node.id, 'in');
    detailBody.innerHTML = html;
    detail.classList.remove('hidden');
  }
  function hideDetail() { detail.classList.add('hidden'); }
  document.getElementById('detail-close').onclick = clearSelection;

  // delegated panel interactions: teleport via dep rows, open-in-editor, symbol-line open
  detailBody.addEventListener('click', function (e) {
    var dep = e.target.closest('.dep-click');
    if (dep && dep.dataset.fid) { focusFile(dep.dataset.fid); return; }
    var ob = e.target.closest('#open-editor');
    if (ob) { openInEditor(ob.dataset.fp, 1); return; }
    var sym = e.target.closest('.sym[data-fp]');
    if (sym) { openInEditor(sym.dataset.fp, parseInt(sym.dataset.line, 10) || 1); return; }
  });
  detailBody.addEventListener('change', function (e) {
    if (e.target.id === 'editor-pick') { try { localStorage.setItem('archexplorer.editor', e.target.value); } catch (x) {} }
  });

  // function-doc popup: hover a symbol -> floating popup with its docstring
  var symTip = document.createElement('div');
  symTip.id = 'sym-tip'; symTip.style.display = 'none';
  document.body.appendChild(symTip);
  detailBody.addEventListener('mouseover', function (e) {
    var sym = e.target.closest('.sym[data-doc]');
    if (!sym) return;
    symTip.textContent = sym.getAttribute('data-doc');
    symTip.style.display = 'block';
  });
  detailBody.addEventListener('mousemove', function (e) {
    if (symTip.style.display !== 'block') return;
    var x = e.clientX, y = e.clientY;
    var w = symTip.offsetWidth, h = symTip.offsetHeight;
    // place to the LEFT of the cursor (panel is on the right); clamp to viewport
    symTip.style.left = Math.max(8, x - w - 16) + 'px';
    symTip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y + 12)) + 'px';
  });
  detailBody.addEventListener('mouseout', function (e) {
    if (e.target.closest('.sym[data-doc]')) symTip.style.display = 'none';
  });

  // ---- toolbar -------------------------------------------------
  var btnControl = document.getElementById('btn-control');
  btnControl.onclick = function () {
    // button toggles orbit <-> fly; trackball is reachable via the theme panel only
    setControlType(controlMode === 'orbit' ? 'fly' : 'orbit');
  };

  document.getElementById('btn-back').onclick = goBack;
  document.getElementById('btn-expand-all').onclick = function () {
    pushHistory();
    G.layers.forEach(function (l) { expanded[l.id] = true; });
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
  };
  document.getElementById('btn-collapse-all').onclick = function () {
    pushHistory();
    expanded = {};
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
  };
  // Frame all layers (the row spans x; layer nodes are pinned in y=z=0, so the
  // bbox is a thin line — zoomToFit() degenerates on it; use an explicit distance).
  function frameAll(ms) {
    var span = LAYER_GAP * nLayers;                 // total width of the layer row
    Graph.cameraPosition(
      { x: 0, y: span * 0.26, z: span * 0.62 },     // centered, elevated, pulled back
      { x: 0, y: 0, z: 0 },
      ms == null ? 0 : ms
    );
  }

  // ---- camera navigation (rotate / zoom / view presets) -------
  // Orbit the camera around the scene centre — keeps the radius, changes the
  // viewing angle. Drives the arrow keys + the on-screen nav pad.
  function orbitBy(daz, del) {
    var p = Graph.cameraPosition();
    var r = Math.hypot(p.x, p.y, p.z) || 1;
    var az = Math.atan2(p.x, p.z);
    var elv = Math.asin(Math.max(-1, Math.min(1, p.y / r)));
    az += daz;
    elv = Math.max(-1.45, Math.min(1.45, elv + del));
    var rc = r * Math.cos(elv);
    Graph.cameraPosition({ x: rc * Math.sin(az), y: r * Math.sin(elv), z: rc * Math.cos(az) }, { x: 0, y: 0, z: 0 }, 180);
  }
  function zoomBy(factor) {
    var p = Graph.cameraPosition();
    Graph.cameraPosition({ x: p.x * factor, y: p.y * factor, z: p.z * factor }, undefined, 180);
  }
  function setView(name) {
    var p = Graph.cameraPosition();
    var r = Math.hypot(p.x, p.y, p.z) || (LAYER_GAP * nLayers * 0.6);
    var pos = name === 'front' ? { x: 0, y: 0, z: r }
      : name === 'side' ? { x: r, y: 0, z: 0 }
      : name === 'top' ? { x: 0, y: r, z: 0.001 }
      : { x: r * 0.5, y: r * 0.42, z: r * 0.62 };   // iso
    Graph.cameraPosition(pos, { x: 0, y: 0, z: 0 }, 700);
  }

  // Fly the camera THROUGH the scene (keyboard WASD/arrows + R/F): translate the
  // camera AND its orbit target by the same world delta, so OrbitControls doesn't
  // snap the view back (a true pan-through, not a dolly). fwd/rgt/vert = signed steps.
  function flyMove(fwd, rgt, vert) {
    var THREE = window.THREE || (typeof ForceGraph3D !== 'undefined' && ForceGraph3D.THREE);
    var cam = (typeof Graph.camera === 'function') ? Graph.camera() : null;
    var controls = (typeof Graph.controls === 'function') ? Graph.controls() : null;
    // No-op unless we have THREE, the camera, and a movable orbit target — moving
    // the camera without carrying the target would just get snapped back.
    if (!THREE || !cam || !controls || !controls.target || typeof controls.target.add !== 'function') return;
    var f = new THREE.Vector3();
    cam.getWorldDirection(f);                                          // forward (look direction)
    var up = cam.up.clone().normalize();
    var right = new THREE.Vector3().crossVectors(f, up).normalize();   // forward × up = screen-right
    var step = Math.max(cam.position.distanceTo(controls.target) * 0.1, 20);  // scale with zoom for a steady feel
    var delta = new THREE.Vector3()
      .addScaledVector(f, fwd * step)
      .addScaledVector(right, rgt * step)
      .addScaledVector(up, vert * step);
    cam.position.add(delta);
    controls.target.add(delta);
    controls.update();
  }

  // small on-screen nav pad (bottom-right): rotate arrows + zoom + view presets
  (function buildNavPad() {
    var pad = document.createElement('div');
    pad.id = 'navpad';
    pad.innerHTML =
      '<div class="np-rot">' +
      '<button data-a="up" title="Rotate up (↑)">▲</button>' +
      '<div class="np-mid">' +
      '<button data-a="left" title="Rotate left (←)">◀</button>' +
      '<button data-a="home" title="Frame all (Home)">⟳</button>' +
      '<button data-a="right" title="Rotate right (→)">▶</button>' +
      '</div>' +
      '<button data-a="down" title="Rotate down (↓)">▼</button>' +
      '</div>' +
      '<div class="np-row">' +
      '<button data-a="zin" title="Zoom in (+)">＋</button>' +
      '<button data-a="zout" title="Zoom out (-)">－</button>' +
      '</div>' +
      '<div class="np-views">' +
      '<button data-v="front" title="Front (1)">Front</button>' +
      '<button data-v="top" title="Top (2)">Top</button>' +
      '<button data-v="side" title="Side (3)">Side</button>' +
      '<button data-v="iso" title="Iso (4)">Iso</button>' +
      '</div>';
    document.body.appendChild(pad);
    pad.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var a = b.dataset.a, v = b.dataset.v;
      if (v) return setView(v);
      if (a === 'left') orbitBy(-0.22, 0);
      else if (a === 'right') orbitBy(0.22, 0);
      else if (a === 'up') orbitBy(0, 0.18);
      else if (a === 'down') orbitBy(0, -0.18);
      else if (a === 'zin') zoomBy(0.82);
      else if (a === 'zout') zoomBy(1.22);
      else if (a === 'home') frameAll(600);
    });
  })();
  // Reset = full return to the clean overview: collapse all, clear selection,
  // clear history, reframe.
  document.getElementById('btn-reset-cam').onclick = function () {
    expanded = {}; selectedId = null; isolatedLayer = null; history = []; updateBackBtn();
    hideDetail(); updateIsolationHint();
    Graph.graphData(buildData());
    if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
    if (window.ArchTheme && window.ArchTheme.onSelect) window.ArchTheme.onSelect(null);
    frameAll(600);
  };
  // keyboard navigation: WASD/arrows FLY through the scene (R/F = up/down), +/-
  // zoom, 1-4 view presets, Esc exits, Home frames all. Ignored while typing in an
  // input, and when a modifier is held (so Cmd/Ctrl+F/R/S etc. still reach the
  // browser). In the built-in Fly control mode FlyControls already binds WASD/arrows,
  // so we defer to it there. (Orbit-by-keyboard moved to the on-screen nav pad.)
  document.addEventListener('keydown', function (e) {
    var tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;        // leave browser shortcuts alone
    var k = (e.key && e.key.length === 1) ? e.key.toLowerCase() : e.key;
    if (controlMode !== 'fly') {
      switch (k) {
        case 'w': case 'ArrowUp':    flyMove(1, 0, 0);  e.preventDefault(); return;
        case 's': case 'ArrowDown':  flyMove(-1, 0, 0); e.preventDefault(); return;
        case 'a': case 'ArrowLeft':  flyMove(0, -1, 0); e.preventDefault(); return;
        case 'd': case 'ArrowRight': flyMove(0, 1, 0);  e.preventDefault(); return;
        case 'r':                    flyMove(0, 0, 1);  e.preventDefault(); return;
        case 'f':                    flyMove(0, 0, -1); e.preventDefault(); return;
      }
    }
    switch (e.key) {
      case 'Escape': if (isolatedLayer) exitIsolation(); else clearSelection(); break;
      case '+': case '=': zoomBy(0.85); break;
      case '-': case '_': zoomBy(1.18); break;
      case 'Home': frameAll(600); break;
      case '1': setView('front'); break;
      case '2': setView('top'); break;
      case '3': setView('side'); break;
      case '4': setView('iso'); break;
    }
  });

  // Focus a file by id (used by search + the file-tree panel): expand its layer
  // if needed, then select + fly to it once the sim has placed it.
  function focusFile(fileId) {
    var f = fileById[fileId];
    if (!f) return;
    if (!expanded[f.layer]) {
      pushHistory();
      expanded[f.layer] = true;
      Graph.graphData(buildData());
      if (window.ArchTheme && window.ArchTheme.refresh) window.ArchTheme.refresh();
    }
    setTimeout(function () {
      var fn = Graph.graphData().nodes.find(function (n) { return n.id === fileId; });
      if (fn) { selectFile(fn); flyToNode(fn, 90); }
    }, 360);
  }

  // ---- search --------------------------------------------------
  var search = document.getElementById('search');
  search.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    var q = search.value.trim().toLowerCase();
    if (!q) return;
    var hit = G.nodes.find(function (n) {
      return n.file_path.toLowerCase().indexOf(q) >= 0 || n.label.toLowerCase().indexOf(q) >= 0;
    });
    if (hit) focusFile(hit.id);
  });

  // ---- meta ----------------------------------------------------
  // (the per-layer colour key now lives in the sidebar, not the bottom legend)
  document.getElementById('meta').textContent =
    (G.meta ? G.meta.repo + ' · ' : '') + G.nodes.length + ' files · ' + G.edges.length + ' edges';

  // ---- AE contract: read-only helpers theme.js consumes -------
  var AE = {
    GRAPH: G,
    layerX: layerX,
    layersById: layersById,
    isLayerNode: isLayerNode,
    nodeLayerColor: function (n) { var l = layersById[n.layer]; return l ? l.color : '#888'; },
    nodeSizeMetric: function (n, metric) {
      if (isLayerNode(n)) return Math.max(n.loc || 1, 1);
      if (metric === 'symbols') return Math.max(n.n_symbols || 1, 1);
      if (metric === 'fanin') return Math.max((n.fanIn || 0) + 1, 1);
      if (metric === 'uniform') return 1;
      return Math.max(n.loc || 1, 1); // default: loc
    },
    isSelected: function (id) { return selectedId === id; },
    selectedNeighbours: function () { return selectedId ? neighbours(selectedId) : null; },
    selectedId: function () { return selectedId; },
    layerOrder: function (n) { var l = layersById[n.layer]; return l ? l.order : 0; },
    nLayers: nLayers,
    // expandable = a collapsed layer super-node (it has files to drill into).
    // A file node is a leaf. (Drives the "more inside" visual cue.)
    isExpandable: function (n) { return isLayerNode(n) && !expanded[n.layer]; },
    rebuild: function () { Graph.graphData(buildData()); },
    // dagMode toggle done correctly: clear pins before enabling, restore after.
    setDagMode: function (mode) {
      var data = Graph.graphData();
      if (mode && mode !== 'none') {
        data.nodes.forEach(function (n) { n.fx = n.fy = n.fz = undefined; });
        Graph.dagMode(mode).dagLevelDistance(LAYER_GAP);
      } else {
        Graph.dagMode(null);
        // restore layer-plane pinning
        Graph.graphData(buildData());
      }
    },
    // Switch camera-control scheme (orbit/fly/trackball) by rebuilding the graph
    // instance — controlType is construction-only in 3d-force-graph 1.70.5.
    setControlType: function (mode) { setControlType(mode); }
  };

  // hand the graph to the appearance layer (theme.js defines ArchTheme)
  if (window.ArchTheme && window.ArchTheme.init) {
    window.ArchTheme.init(Graph, AE);
  }

  // ---- draggable panels ---------------------------------------
  // Make `el` draggable by `handle`. Handle-scoped + bubble-phase only — never a
  // capture-phase global stopPropagation, which would swallow lil-gui's slider
  // mouseup and make values "stick" to the cursor.
  function makeDraggable(el, handle, ignoreSel) {
    if (!el || !handle) return;
    handle.style.cursor = 'move';
    var dragging = false, moved = false, ox = 0, oy = 0;
    function onMove(e) {
      if (!dragging) return;
      moved = true;
      el.style.left = Math.max(0, Math.min(window.innerWidth - 40, e.clientX - ox)) + 'px';
      el.style.top = Math.max(0, Math.min(window.innerHeight - 30, e.clientY - oy)) + 'px';
      el.style.right = 'auto'; el.style.bottom = 'auto';
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    handle.addEventListener('mousedown', function (ev) {
      if (ev.button !== 0) return;
      if (ignoreSel && ev.target.closest && ev.target.closest(ignoreSel)) return;
      var r = el.getBoundingClientRect();
      ox = ev.clientX - r.left; oy = ev.clientY - r.top;
      dragging = true; moved = false;
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    // Only suppress the click that immediately follows a real drag (scoped to the
    // handle), so a drag of the lil-gui title doesn't also toggle its collapse.
    handle.addEventListener('click', function (e) {
      if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; }
    }, true);
  }
  // detail panel: drag by its header zone (ignore buttons + scrollable content)
  makeDraggable(detail, detail, 'button, a, .chips, .sym, .dep, #detail-body');
  // appearance panel (lil-gui): drag by its title bar (created async by theme.js)
  setTimeout(function () {
    var gui = document.querySelector('.lil-gui.root');
    if (gui) makeDraggable(gui, gui.querySelector('.title') || gui);
  }, 300);

  // initial framing — angled view once the sim has placed the layer nodes
  setTimeout(function () { frameAll(800); }, 700);

  // public API (debugging, the file-tree panel, phase-2 chat)
  window.ArchExplorer = {
    graph: Graph, AE: AE, GRAPH: G,
    buildData: buildData, toggleLayer: toggleLayer, focusFile: focusFile,
    setControlType: setControlType,
    isExpanded: function (layerId) { return !!expanded[layerId]; }
  };
  // let the file-tree panel (sidebar.js) mount once everything is ready
  if (window.ArchSidebar && window.ArchSidebar.init) window.ArchSidebar.init(window.ArchExplorer);
})();
