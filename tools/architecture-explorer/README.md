# Architecture Explorer (3D)

An interactive, browser-based **3D fly-through map** of the repo, generated from the
[codegraph](https://www.npmjs.com/package/@colbymchenry/codegraph) index. Files are
grouped into architectural layers (channels, host runtime, security, container,
memory, evolution, …); each layer is a **fixed plane receding into depth**. Fly
through the space, click a layer to expand its files onto that layer's plane, and
click a file to see what it **affects** and is **affected by**. The whole look is
**live-customizable** (node shape, color, size, edges, glow, camera) via the control
panel. It stays current — re-run the generator after the codegraph index updates.

```
codegraph.db ──> build.py ──> graph-data.js ──> index.html (3d-force-graph)
                    ▲                                  ▲
                layers.json                         theme.js  (live appearance panel)
            (path-glob → layer rules)            (shape/color/edges/scene; saved to localStorage)
```

## Use it

```bash
# 1. make sure codegraph is indexed (creates .codegraph/codegraph.db)
codegraph index            # run from the repo root if the DB is stale

# 2. generate the graph data
python3 tools/architecture-explorer/build.py
#    or against an explicit DB / another repo:
python3 tools/architecture-explorer/build.py --db /path/to/.codegraph/codegraph.db

# 3. open it (serving over http is recommended — gives full localStorage for saved themes)
cd tools/architecture-explorer && python3 -m http.server 8000
# then open http://localhost:8000/index.html
#   (python3 -m webbrowser index.html also works; saved themes may not persist under file://)
```

## What you can do in the view

- **Fly / orbit** — drag to rotate, wheel to zoom, right-drag to pan; toggle **Fly**
  mode (WASD, first-person) from the toolbar.
- **Layers as depth planes** — the 11 architectural layers are stacked front-to-back.
- **Click a layer** → its files appear on that layer's plane (click again to collapse).
  Other layers never move.
- **Click a file** → detail panel: path, layer, LOC, symbols, exports, and the
  **Affects → / ← Affected by** lists (the change-awareness view — the blast radius of
  a file before you touch it), plus its connections light up (focus + context).
- **Directional particles** flow along edges to show import/call direction.
- **Search** → locate a file, fly the camera to it, expand its layer.

## Customize the look (live)

The control panel (top-right) changes appearance instantly, no code:

- **Nodes** — shape (sphere / box / cylinder / cone / octahedron / …), size-by
  (LOC / symbols / fan-in / uniform) + scale, opacity, wireframe.
- **Color** — by layer / by language / fan-in heatmap / mono.
- **Edges** — opacity, directional particles + speed, arrows, curvature, width-by-weight.
- **Scene** — background, bloom/glow, fog. **Camera** — orbit / fly / trackball, auto-rotate.
- **Presets** (Blueprint / Galaxy / Minimal) + **Reset**. Your look is saved to
  `localStorage` and restored on reload.

## Reusing / re-layering (and other projects)

It's a **self-contained, portable tool** — copy `tools/architecture-explorer/` into
any codegraph-indexed repo and run `build.py --db <that repo>/.codegraph/codegraph.db`.
To change the layers (or adapt to a different repo's structure), edit **`layers.json`**
— ordered rules, first matching glob wins, no code change. `build.py` is standard-library
only (no third-party deps). A one-command `deus arch <project>` wrapper is planned (Phase 2).

## Files

| File | Responsibility |
|---|---|
| `build.py` | generator: codegraph DB + `layers.json` → `graph-data.js` (`window.GRAPH`) |
| `layers.json` | ordered path-glob → layer rules |
| `index.html` | page shell; loads vendor + `graph-data.js` + `theme.js` + `app.js` |
| `app.js` | 3D graph structure + behavior (pinned-z layers, expand/collapse, detail panel, search, camera) |
| `theme.js` | live appearance system (lil-gui panel; shape/color/edges/scene; localStorage) |
| `style.css` | dark UI chrome (topbar, detail panel, legend) |
| `vendor/3d-force-graph.min.js` | 3D renderer (bundles Three.js; offline — see `vendor/README.md`) |
| `vendor/lil-gui.umd.min.js` | live control panel |
| `tests/test_build.py` | pytest for the generator |
| `graph-data.js` | **generated** (gitignored) |

## Verification

`tests/test_build.py` (`python3 -m pytest tools/architecture-explorer/tests/`)
covers the generator: layer assignment, edge aggregation, filtering, schema,
determinism. The **UI is verified manually** — run `build.py`, open `index.html`,
confirm the 3D scene renders (layers as depth planes), fly/orbit work, click-a-layer
expands its files, the detail panel shows Affects/Affected-by, and the appearance
panel changes the look live.

## Roadmap

- **Phase 2 — chat panel:** ask "where does X happen / what does Y affect" answered
  from the graph data + codegraph FTS (grounded, not hallucinated).
- **`deus arch <project>`** — one-command portable invocation for any indexed repo.
- Theme export/import; git-churn coloring; a semantic-coupling layer (e.g. the shared
  judge RUBRIC → its 4 dimensions) so the map doubles as a change-awareness instrument.
