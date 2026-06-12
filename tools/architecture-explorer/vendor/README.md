# Vendored libraries

Bundled locally so the explorer works **offline** (no CDN, no network) when opened
via `file://`. All are MIT-licensed.

| File | Library | Version | License | Source |
|---|---|---|---|---|
| `three.min.js` | Three.js | 0.149.0 | MIT | https://unpkg.com/three@0.149.0/build/three.min.js |
| `3d-force-graph.min.js` | 3d-force-graph (vasturiano) | 1.70.5 | MIT | https://unpkg.com/3d-force-graph@1.70.5/dist/3d-force-graph.min.js |
| `lil-gui.umd.min.js` | lil-gui (live control panel) | 0.19.2 | MIT | https://unpkg.com/lil-gui@0.19.2/dist/lil-gui.umd.min.js |

## Why a separate `three.min.js` (do NOT delete it)

The standalone `3d-force-graph` build bundles Three.js *privately* and does **not**
expose a global `window.THREE`. The appearance layer (`theme.js`) needs THREE to
build custom node geometries (per-layer shapes, wireframe shells), so a separate
global Three is loaded **first** in `index.html`. Version pairing matters:
`3d-force-graph` uses whatever global `THREE` is present, and Three removed its
UMD/global build after r0.149 — so `3d-force-graph` is pinned to **1.70.5** (the
last version whose internals work with the r0.149 global, i.e. no `THREE.Timer`
dependency). Bumping either one requires re-checking that pairing.

Globals exposed: `THREE`, `ForceGraph3D`, `lil` (instantiate with `new lil.GUI()`).

To update: download the same pinned path at the new version, replace the file,
re-verify the THREE ↔ 3d-force-graph pairing, and confirm the UI renders
(open `../index.html`).
