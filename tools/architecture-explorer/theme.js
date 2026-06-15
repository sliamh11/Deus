/* ============================================================
   Architecture Explorer — theme.js
   Live appearance system for the 3D force graph.

   Defines window.ArchTheme = { init, refresh, onSelect }.
   app.js calls ArchTheme.init(graph, AE) after graph setup.

   Dependencies: THREE (via window.THREE || ForceGraph3D.THREE),
   lil.GUI (global from lil-gui.umd.min.js), ForceGraph3D.
   No imports. Vanilla JS only.
   ============================================================ */
(function () {
  'use strict';

  /* ----------------------------------------------------------
     THREE resolver — guard access throughout
  ---------------------------------------------------------- */
  function getThree() {
    if (window.THREE) return window.THREE;
    if (typeof ForceGraph3D !== 'undefined' && ForceGraph3D.THREE) return ForceGraph3D.THREE;
    return null;
  }

  /* ----------------------------------------------------------
     Per-layer shape palette (Feature #3)
     11 entries — one per layer; wraps via modulo for extras.
     Only shapes makeGeometry already handles (or has a fallback for).
     dodecahedron / capsule get proper THREE cases below; both degrade
     gracefully (the nodeThreeObject try/catch falls back to sphere).
  ---------------------------------------------------------- */
  var SHAPE_PALETTE = [
    'sphere',       // layer 0
    'box',          // layer 1
    'cone',         // layer 2
    'cylinder',     // layer 3
    'octahedron',   // layer 4
    'icosahedron',  // layer 5
    'tetrahedron',  // layer 6
    'torus',        // layer 7
    'dodecahedron', // layer 8
    'capsule',      // layer 9
    'sphere'        // layer 10
  ];

  /* ----------------------------------------------------------
     Default theme
  ---------------------------------------------------------- */
  var DEFAULTS = {
    // Nodes
    shape:       'per-layer',
    sizeBy:      'loc',
    sizeScale:   1.2,
    opacity:     0.92,
    wireframe:   false,
    flatShading: false,
    // Color
    scheme:      'byLayer',
    monoColor:   '#4f8ef7',
    // Edges
    linkOpacity:      0.55,
    particles:        true,
    particleSpeed:    0.004,
    arrows:           false,
    widthByWeight:    false,
    curvature:        0.1,
    // Scene
    background:  '#0d1117',
    bloom:       false,        // guarded — disabled until THREE bloom reachable
    fog:         false,        // off by default; the scene is ~2400 units wide
    fogDensity:  0.0002,       // scene-scaled (0.0012 fogs everything to grey)
    // Camera
    controlType: 'orbit',
    autoRotate:  false,
    // Layout
    dagMode:     'none',
    // Labels
    labels:      'hover'
  };

  var STORAGE_KEY = 'archexplorer.theme.v1';

  /* ----------------------------------------------------------
     Persistence helpers
  ---------------------------------------------------------- */
  function loadTheme() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  }

  function saveTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(theme));
    } catch (e) { /* file:// SecurityError or storage full — ignore */ }
  }

  /* ----------------------------------------------------------
     Presets
  ---------------------------------------------------------- */
  var PRESETS = {
    Galaxy: {
      shape: 'sphere', sizeBy: 'loc', sizeScale: 1.2, opacity: 0.88,
      wireframe: false, flatShading: false,
      scheme: 'byLayer', monoColor: '#4f8ef7',
      linkOpacity: 0.5, particles: true, particleSpeed: 0.006,
      arrows: false, widthByWeight: false, curvature: 0.12,
      background: '#050810', bloom: true, fog: true, fogDensity: 0.00018,
      controlType: 'orbit', autoRotate: false, dagMode: 'none', labels: 'hover'
    },
    Blueprint: {
      shape: 'box', sizeBy: 'symbols', sizeScale: 0.9, opacity: 0.8,
      wireframe: false, flatShading: true,
      scheme: 'mono', monoColor: '#2a7fff',
      linkOpacity: 0.7, particles: false, particleSpeed: 0.004,
      arrows: true, widthByWeight: true, curvature: 0,
      background: '#001833', bloom: false, fog: true, fogDensity: 0.00022,
      controlType: 'orbit', autoRotate: false, dagMode: 'none', labels: 'hover'
    },
    Minimal: {
      shape: 'sphere', sizeBy: 'uniform', sizeScale: 0.7, opacity: 1,
      wireframe: false, flatShading: false,
      scheme: 'byLayer', monoColor: '#888888',
      linkOpacity: 0.3, particles: false, particleSpeed: 0.004,
      arrows: false, widthByWeight: false, curvature: 0,
      background: '#181c24', bloom: false, fog: false, fogDensity: 0.001,
      controlType: 'orbit', autoRotate: false, dagMode: 'none', labels: 'hover'
    }
  };

  /* ----------------------------------------------------------
     Geometry builder (cached by shape key)
  ---------------------------------------------------------- */
  var geoCache = {};
  function makeGeometry(THREE, shape, baseSize) {
    var s = baseSize;
    switch (shape) {
      case 'box':          return new THREE.BoxGeometry(s, s, s);
      case 'rounded-box':  return new THREE.BoxGeometry(s * 0.9, s * 0.9, s * 0.9); // fallback to box
      case 'cylinder':     return new THREE.CylinderGeometry(s * 0.5, s * 0.5, s, 8);
      case 'cone':         return new THREE.ConeGeometry(s * 0.5, s, 8);
      case 'octahedron':   return new THREE.OctahedronGeometry(s * 0.7);
      case 'icosahedron':  return new THREE.IcosahedronGeometry(s * 0.7, 0);
      case 'tetrahedron':  return new THREE.TetrahedronGeometry(s * 0.8, 0);
      case 'torus':        return new THREE.TorusGeometry(s * 0.45, s * 0.18, 8, 12);
      case 'dodecahedron': return new THREE.DodecahedronGeometry(s * 0.65, 0);
      case 'capsule':      return new THREE.CapsuleGeometry(s * 0.3, s * 0.5, 4, 8);
      default:             return new THREE.SphereGeometry(s * 0.55, 10, 8); // sphere
    }
  }

  /* ----------------------------------------------------------
     Color schemes
  ---------------------------------------------------------- */
  var LANG_COLORS = {
    ts: '#3178c6', js: '#f7df1e', py: '#3572a5',
    go: '#00add8', rs: '#dea584', java: '#b07219',
    cs: '#178600', rb: '#701516', md: '#aaaaaa',
    json: '#8bc34a', css: '#563d7c', html: '#e34c26'
  };

  function langFromPath(path) {
    if (!path) return null;
    var ext = path.split('.').pop().toLowerCase();
    return LANG_COLORS[ext] || null;
  }

  function fanInHeat(fanInVal) {
    // 0 -> blue, high -> red, via HSL
    var t = Math.min(1, (fanInVal || 0) / 20);
    var h = Math.round((1 - t) * 240);
    return 'hsl(' + h + ',70%,55%)';
  }

  function nodeColor(node, AE, theme) {
    switch (theme.scheme) {
      case 'byLanguage':
        return langFromPath(node.file_path) || AE.nodeLayerColor(node);
      case 'faninHeat':
        return fanInHeat(node.fanIn || 0);
      case 'mono':
        return theme.monoColor;
      default: // byLayer
        return AE.nodeLayerColor(node);
    }
  }

  /* ----------------------------------------------------------
     hex -> THREE.Color helper
  ---------------------------------------------------------- */
  function hexToColor(THREE, hex) {
    return new THREE.Color(hex);
  }

  /* ----------------------------------------------------------
     Main ArchTheme object
  ---------------------------------------------------------- */
  window.ArchTheme = (function () {
    var graph = null;
    var AE = null;
    var theme = {};
    var gui = null;
    var autoRotateInterval = null;
    var bloomPass = null;
    var bloomEnabled = false;
    var ctrlController = null;   // lil-gui controller for controlType (kept for updateDisplay)
    var dagModeController = null; // lil-gui controller for dagMode (kept for syncDagMode display)
    var rebinding = false;       // true while rebind() re-applies visuals (recursion guard)

    /* ---- apply helpers ------------------------------------ */

    function applyNodeVisuals() {
      var THREE = getThree();
      if (!THREE) {
        // No THREE available — fall back to nodeColor only (built-in spheres)
        graph.nodeColor(function (n) { return nodeColor(n, AE, theme); });
        graph.nodeThreeObject(null);
        return;
      }

      graph.nodeThreeObjectExtend(false);
      graph.nodeThreeObject(function (node) {
        var nb = AE.selectedNeighbours();
        var sel = AE.selectedId();

        // Determine dim/highlight
        var isSelected = sel && node.id === sel;
        var isNeighbour = nb && nb[node.id];
        var isDimmed = sel && !isSelected && !isNeighbour && !AE.isLayerNode(node);

        var metric = AE.nodeSizeMetric(node, theme.sizeBy);
        // Bounded sizing: layer super-nodes are containers (modest, roughly
        // uniform, well under the inter-layer gap); files are small, sized
        // sub-linearly by their metric. Without bounds, a layer's total LOC
        // (thousands) produces a screen-filling sphere.
        var treeMode = (typeof AE.layoutMode === 'function' && AE.layoutMode() === 'tree');
        var size;
        if (treeMode) {
          // Tree mode packs nodes close (dagged by directory depth), so LOC-based
          // sizing would overlap into a blob — use small/uniform sizes instead:
          // folders are modest hubs (sub-linear by descendant count, capped), files
          // are uniform small leaves.
          if (node.kind === 'folder') {
            size = Math.min(8 + Math.sqrt(node.descendants || 0) * 1.2, 22) * theme.sizeScale;
          } else {
            size = 6 * theme.sizeScale;
          }
        } else if (AE.isLayerNode(node)) {
          size = (40 + 9 * Math.log10(metric + 10)) * theme.sizeScale;     // headline nodes ~75-90
        } else {
          size = (5 + 2.4 * Math.pow(metric, 1 / 3)) * theme.sizeScale;    // ~10-28
        }

        // Feature #3 — per-layer shape resolution
        var effectiveShape;
        if (theme.shape === 'per-layer') {
          effectiveShape = SHAPE_PALETTE[AE.layerOrder(node) % SHAPE_PALETTE.length];
        } else {
          effectiveShape = theme.shape;
        }

        var geo;
        try {
          geo = makeGeometry(THREE, effectiveShape, size);
        } catch (e) {
          geo = new THREE.SphereGeometry(size * 0.55, 8, 6);
        }

        var colorStr = nodeColor(node, AE, theme);
        var color = new THREE.Color(colorStr);

        var opacity = isDimmed ? 0.08 : theme.opacity;
        var transparent = opacity < 0.999;

        var mat;
        try {
          mat = new THREE.MeshLambertMaterial({
            color: color,
            opacity: opacity,
            transparent: transparent,
            wireframe: theme.wireframe,
            flatShading: theme.flatShading
          });
        } catch (e) {
          mat = new THREE.MeshBasicMaterial({ color: color, opacity: opacity, transparent: transparent, wireframe: theme.wireframe });
        }

        var solidMesh = new THREE.Mesh(geo, mat);

        // Feature #1 — expandable "more inside" wireframe shell
        if (AE.isExpandable(node)) {
          var group = new THREE.Group();
          group.add(solidMesh);
          try {
            var shellGeo = makeGeometry(THREE, effectiveShape, size);
            var shellOpacity = isDimmed ? 0.04 : 0.35;
            var shellMat = new THREE.MeshBasicMaterial({
              color: color,
              wireframe: true,
              transparent: true,
              opacity: shellOpacity
            });
            var shellMesh = new THREE.Mesh(shellGeo, shellMat);
            shellMesh.scale.setScalar(1.18);
            group.add(shellMesh);
          } catch (e) {
            // Shell failed — group still contains the solid mesh, degrade gracefully
          }
          return group;
        }

        return solidMesh;
      });

      graph.refresh();
    }

    function applyLinkVisuals() {
      var nb = AE.selectedNeighbours();
      var sel = AE.selectedId();

      graph.linkColor(function (link) {
        if (!sel) return 'rgba(180,200,255,0.6)';
        var src = (typeof link.source === 'object') ? link.source.id : link.source;
        var tgt = (typeof link.target === 'object') ? link.target.id : link.target;
        var ego = (src === sel || tgt === sel);
        return ego ? 'rgba(255,220,80,0.9)' : 'rgba(100,120,160,0.15)';
      });

      graph.linkOpacity(theme.linkOpacity);

      graph.linkWidth(theme.widthByWeight
        ? function (link) { return Math.min(3, Math.max(0.5, (link.weight || 1) * 0.4)); }
        : 1);

      graph.linkCurvature(theme.curvature);

      if (theme.particles) {
        graph.linkDirectionalParticles(function (link) {
          if (sel) {
            var src = (typeof link.source === 'object') ? link.source.id : link.source;
            var tgt = (typeof link.target === 'object') ? link.target.id : link.target;
            return (src === sel || tgt === sel) ? 4 : 0;
          }
          return 2;
        });
        graph.linkDirectionalParticleSpeed(theme.particleSpeed);
        graph.linkDirectionalParticleWidth(1.5);
      } else {
        graph.linkDirectionalParticles(0);
      }

      graph.linkDirectionalArrowLength(theme.arrows ? 4 : 0);
    }

    function applyScene() {
      var THREE = getThree();
      graph.backgroundColor(theme.background);

      // Fog
      if (THREE) {
        try {
          if (theme.fog) {
            graph.scene().fog = new THREE.FogExp2(
              new THREE.Color(theme.background).getHex(),
              theme.fogDensity
            );
          } else {
            graph.scene().fog = null;
          }
        } catch (e) { console.warn('[ArchTheme] fog error:', e); }
      }

      // Bloom
      applyBloom();
    }

    function applyBloom() {
      if (!theme.bloom) {
        // Disable: remove pass if present
        if (bloomPass) {
          try {
            var composer = graph.postProcessingComposer();
            if (composer && composer.passes) {
              var idx = composer.passes.indexOf(bloomPass);
              if (idx !== -1) composer.passes.splice(idx, 1);
            }
          } catch (e) { /* graceful */ }
          bloomPass = null;
        }
        return;
      }

      // Attempt to enable bloom
      try {
        var THREE = getThree();
        if (!THREE || !THREE.UnrealBloomPass) {
          console.warn('[ArchTheme] bloom: THREE.UnrealBloomPass not available — bloom disabled');
          theme.bloom = false;
          return;
        }
        var composer = graph.postProcessingComposer();
        if (!composer) {
          console.warn('[ArchTheme] bloom: postProcessingComposer not available — bloom disabled');
          theme.bloom = false;
          return;
        }
        if (!bloomPass) {
          bloomPass = new THREE.UnrealBloomPass(
            new THREE.Vector2(window.innerWidth, window.innerHeight),
            0.8, 0.4, 0.85
          );
          composer.addPass(bloomPass);
        }
      } catch (e) {
        console.warn('[ArchTheme] bloom error — disabled:', e);
        theme.bloom = false;
        bloomPass = null;
      }
    }

    function applyCamera() {
      // controlType is a CONSTRUCTION option in 3d-force-graph (no runtime setter),
      // so switching it means rebuilding the graph — app.js owns that via AE.setControlType.
      // Skip while a rebind is in flight: app.js already drives the rebuild that
      // called us, and re-entering setControlType here would recurse.
      if (rebinding) return;
      if (AE && typeof AE.setControlType === 'function') { AE.setControlType(theme.controlType); return; }
      try { graph.controlType(theme.controlType); } catch (e) { /* graceful */ }
    }

    function applyAutoRotate() {
      if (autoRotateInterval) { clearInterval(autoRotateInterval); autoRotateInterval = null; }
      if (!theme.autoRotate) return;
      autoRotateInterval = setInterval(function () {
        try {
          var cam = graph.cameraPosition();
          var angle = Math.atan2(cam.x, cam.z) + 0.005;
          var dist = Math.sqrt(cam.x * cam.x + cam.z * cam.z);
          graph.cameraPosition({
            x: dist * Math.sin(angle),
            y: cam.y,
            z: dist * Math.cos(angle)
          });
        } catch (e) { clearInterval(autoRotateInterval); autoRotateInterval = null; }
      }, 33);
    }

    function applyAll() {
      applyNodeVisuals();
      applyLinkVisuals();
      applyScene();
      applyCamera();
      applyAutoRotate();
    }

    /* ---- lil-gui panel ------------------------------------ */

    function buildPanel() {
      try {
        gui = new lil.GUI({ title: 'Appearance', width: 240 });
        gui.domElement.style.zIndex = '9999';
      } catch (e) {
        console.warn('[ArchTheme] lil-gui unavailable — panel skipped:', e);
        return;
      }

      /* Nodes */
      var fNodes = gui.addFolder('Nodes');
      fNodes.add(theme, 'shape', ['per-layer','sphere','box','rounded-box','cylinder','cone','octahedron','icosahedron','tetrahedron','torus','dodecahedron','capsule'])
        .name('Shape').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.add(theme, 'sizeBy', ['loc','symbols','fanin','uniform'])
        .name('Size by').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.add(theme, 'sizeScale', 0.3, 4, 0.05)
        .name('Size scale').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.add(theme, 'opacity', 0.1, 1, 0.01)
        .name('Opacity').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.add(theme, 'wireframe')
        .name('Wireframe').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.add(theme, 'flatShading')
        .name('Flat shading').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fNodes.open();

      /* Color */
      var fColor = gui.addFolder('Color');
      fColor.add(theme, 'scheme', ['byLayer','byLanguage','faninHeat','mono'])
        .name('Scheme').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fColor.addColor(theme, 'monoColor')
        .name('Mono color').onChange(function () { saveTheme(theme); applyNodeVisuals(); });
      fColor.open();

      /* Edges */
      var fEdges = gui.addFolder('Edges');
      fEdges.add(theme, 'linkOpacity', 0, 1, 0.01)
        .name('Opacity').onChange(function () { saveTheme(theme); applyLinkVisuals(); });
      fEdges.add(theme, 'particles')
        .name('Particles').onChange(function () { saveTheme(theme); applyLinkVisuals(); });
      fEdges.add(theme, 'particleSpeed', 0, 0.02, 0.0005)
        .name('Particle speed').onChange(function () { saveTheme(theme); applyLinkVisuals(); });
      fEdges.add(theme, 'arrows')
        .name('Arrows').onChange(function () { saveTheme(theme); applyLinkVisuals(); });
      fEdges.add(theme, 'widthByWeight')
        .name('Width by weight').onChange(function () { saveTheme(theme); applyLinkVisuals(); });
      fEdges.add(theme, 'curvature', 0, 0.5, 0.01)
        .name('Curvature').onChange(function () { saveTheme(theme); applyLinkVisuals(); });

      /* Scene */
      var fScene = gui.addFolder('Scene');
      fScene.addColor(theme, 'background')
        .name('Background').onChange(function () { saveTheme(theme); applyScene(); });
      fScene.add(theme, 'bloom')
        .name('Bloom (guarded)').onChange(function () { saveTheme(theme); applyBloom(); });
      fScene.add(theme, 'fog')
        .name('Fog').onChange(function () { saveTheme(theme); applyScene(); });
      fScene.add(theme, 'fogDensity', 0, 0.004, 0.0001)
        .name('Fog density').onChange(function () { saveTheme(theme); applyScene(); });

      /* Camera */
      var fCamera = gui.addFolder('Camera');
      ctrlController = fCamera.add(theme, 'controlType', ['orbit','fly','trackball'])
        .name('Control').onChange(function () { saveTheme(theme); applyCamera(); });
      fCamera.add(theme, 'autoRotate')
        .name('Auto-rotate').onChange(function () { saveTheme(theme); applyAutoRotate(); });

      /* Layout */
      var fLayout = gui.addFolder('Layout');
      dagModeController = fLayout.add(theme, 'dagMode', ['none','td','lr','zout','radialout'])
        .name('DAG mode').onChange(function (v) { saveTheme(theme); AE.setDagMode(v); });

      /* Labels */
      var fLabels = gui.addFolder('Labels');
      fLabels.add(theme, 'labels', ['hover','always','off'])
        .name('Labels').onChange(function (v) { saveTheme(theme); applyLabels(v); });

      /* Presets */
      var fPresets = gui.addFolder('Presets');
      var presetProxy = { preset: 'Galaxy' };
      fPresets.add(presetProxy, 'preset', Object.keys(PRESETS))
        .name('Load preset').onChange(function (name) {
          applyPreset(name);
        });
      fPresets.add({ reset: function () { applyPreset('__defaults__'); } }, 'reset')
        .name('Reset to defaults');
      fPresets.open();
    }

    function applyLabels(val) {
      // 'hover' and 'off' both rely on the built-in nodeLabel tooltip
      // (ForceGraph3D shows tooltip on hover via nodeLabel, set in app.js).
      // 'always' would need in-scene THREE.Sprite text — skip for simplicity
      // and fall back to hover behavior; document the choice.
      // 'off' disables the tooltip by setting nodeLabel to empty.
      if (val === 'off') {
        graph.nodeLabel('');
      } else {
        // Restore default tooltip (app.js sets this but re-set here for safety)
        graph.nodeLabel(function (n) {
          return AE.isLayerNode(n)
            ? (n.label + ' — ' + n.nFiles + ' files')
            : n.file_path;
        });
      }
      // Note: 'always' in-scene billboard labels are gracefully omitted —
      // implementing THREE.Sprite billboards correctly is non-trivial and
      // the tooltip ('hover') covers the discovery use-case adequately.
    }

    function applyPreset(name) {
      var src = (name === '__defaults__') ? DEFAULTS : PRESETS[name];
      if (!src) return;
      Object.keys(src).forEach(function (k) { theme[k] = src[k]; });
      saveTheme(theme);
      if (gui) {
        try {
          gui.controllersRecursive().forEach(function (c) { c.updateDisplay(); });
        } catch (e) { /* graceful */ }
      }
      applyAll();
    }

    /* ---- public API --------------------------------------- */

    function init(g, ae) {
      graph = g;
      AE = ae;

      // Restore saved theme over defaults
      var saved = loadTheme();
      theme = Object.assign({}, DEFAULTS, saved || {});

      // Build GUI
      buildPanel();

      // Apply immediately
      applyAll();
    }

    function refresh() {
      // Called by app.js after expand/collapse — re-apply so new nodes get styled
      applyNodeVisuals();
      applyLinkVisuals();
    }

    function onSelect(nodeOrNull) {
      // Focus+context highlight — re-draw node visuals and link visuals
      // so dim/highlight logic picks up the new selection state
      applyNodeVisuals();
      applyLinkVisuals();
    }

    // Feature #3 — layer-shape lookup for sidebar / external consumers
    function shapeForLayerOrder(order) {
      if (theme.shape === 'per-layer') {
        return SHAPE_PALETTE[order % SHAPE_PALETTE.length];
      }
      return theme.shape;
    }

    // Re-point the theme to a freshly-rebuilt graph instance (control-mode switch)
    // and re-apply all visuals — WITHOUT rebuilding the lil-gui panel. The
    // `rebinding` guard stops applyAll()→applyCamera() from re-triggering the
    // rebuild app.js is already in the middle of.
    function rebind(g) {
      rebinding = true;
      graph = g;
      try { applyAll(); } finally { rebinding = false; }
    }

    // Keep theme.controlType (+ its GUI control) in sync when the switch is driven
    // from the toolbar button rather than the panel. Does NOT re-apply visuals —
    // app.js performs the actual graph rebuild; this only mirrors the chosen mode
    // so a later applyCamera() sees a matching value (no spurious re-rebuild).
    function syncControlType(mode) {
      theme.controlType = mode;
      saveTheme(theme);
      if (ctrlController) { try { ctrlController.updateDisplay(); } catch (e) { /* graceful */ } }
    }

    // Keep the DAG dropdown display in sync when the directory-tree toggle is driven
    // from the toolbar (#btn-layout in app.js) rather than the panel. Display-only:
    // never calls AE.setDagMode or any apply* (no recursion path). Deliberately does
    // NOT saveTheme — layoutMode isn't persisted (it always inits 'layered'), so
    // persisting the tree-driven 'td' would desync the restored dropdown from the
    // restored layout. localStorage keeps only the user's manual panel choice.
    function syncDagMode(mode) {
      theme.dagMode = mode;
      if (dagModeController) { try { dagModeController.updateDisplay(); } catch (e) { /* graceful */ } }
    }

    // Snapshot the live appearance for a saved view (ArchViews). Returns a shallow
    // copy so callers can't mutate the internal theme. `dagMode` is DELIBERATELY
    // excluded: it tracks the live layout (may be 'td' after a tree toggle) but a
    // saved view drives the dag from its OWN nav.layoutMode via restore()+syncDagMode,
    // so carrying dagMode here would desync the dropdown on restore (same reasoning as
    // syncDagMode's no-saveTheme note above).
    function getTheme() {
      var copy = Object.assign({}, theme);
      delete copy.dagMode;
      return copy;
    }

    // Re-apply a saved appearance blob (ArchViews restore). Mirrors applyPreset:
    // merge over the live theme, persist, sync the GUI controls, re-apply visuals.
    // applyAll()→applyCamera() handles a controlType change by rebuilding the graph
    // (controlType is construction-only); the rebind `rebinding` guard prevents
    // re-entry. Node positions are NOT carried here — the caller restores nav state
    // AFTER this (app.js restore() regenerates graphData), so positions reseed there.
    function applyTheme(obj) {
      if (!obj) return;
      Object.keys(obj).forEach(function (k) {
        if (k === 'dagMode') return;            // never let a saved blob drive the dag
        theme[k] = obj[k];
      });
      saveTheme(theme);
      if (gui) {
        try { gui.controllersRecursive().forEach(function (c) { c.updateDisplay(); }); }
        catch (e) { /* graceful */ }
      }
      applyAll();
    }

    return { init: init, refresh: refresh, onSelect: onSelect, rebind: rebind, syncControlType: syncControlType, syncDagMode: syncDagMode, shapeForLayerOrder: shapeForLayerOrder, getTheme: getTheme, applyTheme: applyTheme };
  })();

})();
