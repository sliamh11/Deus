---
name: repo-visualiser
description: Render a repository as a rotatable, animated isometric system map — varied 3D buildings on a grid, with live payload pulses tracing real control and data paths, plus a legend and an explainer panel. Use when the user wants a repo's infrastructure seen as an isometric or 3D city map. Not for a knowledge-graph dashboard (`/understand-dashboard`) or a plain Mermaid architecture diagram (`architecture-snapshot`).
---

Analyze the repository the user names — defaulting to the current one — at latest main, and render it as an **isometric** system map: every component is a **building** on a **grid**, every dependency is an edge carrying a **payload**, and everything drawn carries a **citation** to the code that proves it.

The citations are the point. A building you inferred but never read is a fiction rendered in 3D. Draw only what you have read.

## 1. Pin the target at latest main

Resolve the repo path, then pin the exact tree you will analyze:

- **Has a remote:** `git fetch origin`, then read the tree at `origin/main` — or at the remote HEAD's default branch when it isn't called `main` (`git symbolic-ref refs/remotes/origin/HEAD`). Never analyze the working tree; it drifts from main, and the user asked for main.
- **No remote:** use the local default branch and say so on the map.

**Done when** the resolved branch and short sha are recorded, and they get stamped into the rendered output.

## 2. Survey the system

Go codegraph-first, not grep-first, where those tools are present: `search_code` / `codegraph_context` for semantic candidates → `codegraph_callers` / `codegraph_callees` / `codegraph_impact` for structure → grep and read to confirm exactly. Where the codegraph tools are unavailable, fall back to semantic search plus grep and read, and say which you used in the final report. The citation requirement does not relax with the tooling.

Every node you admit carries three things:

| Field | Meaning |
|---|---|
| **kind** | `entrypoint` \| `service` \| `store` \| `queue` \| `job` \| `external` |
| **responsibility** | one line, in the system's own vocabulary |
| **citation** | `path:line` where the component is defined |

An `external` system has no definition site in this repo. Cite the nearest in-repo proof that the dependency is real — the client construction, the endpoint or binary constant, or the call site — and mark it as a use site rather than a definition, so the map doesn't imply you read code you don't have.

**Done when** every node has a `path:line` you read first-hand. A node you cannot cite does not go on the map.

## 3. Trace the paths

Walk the real paths between components — the calls, writes, publishes, and reads that actually exist in the code. Each edge records:

- **direction** — who initiates
- **channel** — control (a call, a dispatch) or data (a write, a publish, a stream)
- **payload** — what actually moves across it, named concretely (`AgentTurn`, `session_id`, `raw webhook body`), never "data"
- **citation** — `path:line` of the call or write itself, not of the callee's definition

Map steady-state traffic: what moves while the system is doing its job. A one-time **wiring** edge — a callback installed at startup — earns a place only when the reader otherwise cannot see how two buildings are connected, and then it is labelled `wiring` so nobody reads it as traffic.

**Done when** every edge is cited. An edge you believe exists but cannot pin to a line is either dropped or drawn explicitly as unconfirmed — never quietly drawn as if it were proven.

## 4. Render the map

Write **one self-contained HTML file**: inline CSS, JS, and SVG, no CDN and no external fetches, so it opens offline and years from now. Put it in the OS temp dir so nothing lands in the analyzed repo — resolve `$TMPDIR` falling back to `/tmp` (`%TEMP%` on Windows) and write `<tmpdir>/repo-map-<repo>-<timestamp>.html`.

Project with a 2:1 isometric transform carrying a yaw angle `θ`, so the board can be spun without relaying it out. At `θ = 0` this is the classic isometric view; the grid coordinates never change, only the angle you view them from:

```
rx = gx·cosθ − gy·sinθ         # rotated grid coords
ry = gx·sinθ + gy·cosθ
sx = (rx − ry) * TILE_W / 2
sy = (rx + ry) * TILE_H / 2 − height
depth = rx + ry                # painter's order, recomputed per frame
```

Place nodes on integer grid coordinates by depth, where a node's depth is the **longest** path reaching it from any entrypoint — every entrypoint sits at depth 0 in the near corner, and when several paths arrive at one node the longest one wins. Each hop steps one further out, stores and external systems land at the far edge, and the primary flow reads diagonally across the grid. Ordering by longest path — not by "which chain is this on" — is what keeps the rule working once the graph stops being a single chain: a store read by three unrelated services sits behind the furthest of them rather than beside one of them. Draw back-to-front by `depth` ascending so nearer buildings occlude farther ones — and recompute that order on every rotation, because a draw order sorted once at `θ = 0` renders inside-out by the time the board reaches 180°.

**Give the map room to breathe.** Density is what makes these maps unreadable, and the fix is space, not smaller type. Set the tile pitch so the widest label on a building clears its neighbours completely, and leave at least one empty tile between any two buildings. When it still feels tight, grow the canvas — never close the gaps.

**Vary the buildings** — a map of identical boxes communicates nothing:

- **footprint** — how much surface the module owns (files, exported symbols)
- **height** — size or centrality (LOC, caller count)
- **colour** — the architectural layer
- **shape** — the `kind`, and pin all six so two runs of this skill produce the same vocabulary: stepped tower for `entrypoint`, flat-roofed slab for `service`, cylinder for `store`, stacked slabs for `queue`, pitched-roof block for `job`, dashed ghost block for `external`

Route each edge along the grid as an isometric polyline, styled by channel — solid for control, dashed for data — with an arrowhead at the receiving end and its **payload** labelled on the edge. Label every building and every edge with its `path:line`.

**Keep the labels readable** — this is where these maps actually fail, and the denser the corner the worse it gets. Reserve a label band above each building and push it away from the flow direction, so a title never lands on a neighbour's roof. Never shrink text to make room and never let two labels overlap: when a citation will not fit, move it into a numbered side table keyed to the building and leave the number on the map. Stagger payload labels along convergent edges — three arrows into one store must not stack their labels at the same midpoint.

**Let the viewer walk around it.** A fixed camera hides whatever the front row occludes, so make the board spin a full 360°: drag horizontally to turn it, `←` / `→` to step 15° at a time, and a control that returns it to 0°. Rotation is a camera move, not a re-layout — the grid coordinates stay put, so a building never changes neighbours as the board turns. Three things must follow the camera:

- **Re-sort** the painter's order by `depth` every frame.
- **Billboard the labels** — upright and horizontal at every angle, never rotated with the board — and re-resolve label collisions once the spin settles, since two buildings far apart at 0° can stack up at 90°.
- **Re-fit the view.** A board that fits at 0° does not fit at 45°: the rotated bounding box is wider than the square one. After every rotation, recompute the projected bounds of all buildings *and their labels*, then scale and pan so the whole board stays inside the canvas. Without this the map silently walks off its own edges as it turns, and the corner you rotated to see is the corner that left the frame.

**Show the system moving.** A still map cannot distinguish a hot path from a dead one, so animate the traffic: send pulses along each edge from source to target, continuously, at a cadence that matches the channel — discrete pulses for control (a dispatch fires, one pulse), a steady stream of dots for data (a flow that runs). Direction of travel must match the arrowhead; an animation running backwards up an edge is worse than no animation. Drive it from `requestAnimationFrame` with transform-only updates so a large map stays smooth, and honour `prefers-reduced-motion: reduce` by holding the pulses still — the map must stay fully readable frozen.

Two pieces of chrome are required:

- **Legend** — every shape, colour, and edge style the render actually used. No glyph on the canvas missing from the legend; no entry in the legend missing from the canvas.
- **Explainer panel** — prose narrating the primary path end-to-end, from entrypoint to store and back, naming each building and payload in order, carrying the branch and sha from step 1.

Give the chrome its own space — a reserved column or row that the board is fitted around, never a floating overlay sitting on top of it. A panel that covers buildings has traded the map for its own caption. Size those panels to their content, or let them scroll: a legend cut off at the fourth of six colours is a legend that lies, and it lies most convincingly to whoever built it, because the entries are all there in the markup.

**Done when** the file is written, contains no external references, and spins and animates with the panels still legible.

## 5. Open it

`xdg-open "<path>"` on Linux, `open "<path>"` on macOS, `start "" "<path>"` on Windows — quote the path, and give Windows the empty first argument, or a temp dir containing a space swallows it as a window title.

## 6. Verify the render, then report

The deliverable is a picture, so a file that merely exists proves nothing. Look at the opened map at full size — never a thumbnail, where collisions vanish and everything passes — and confirm all five:

1. Buildings are visibly varied by kind and size — not a field of identical cubes.
2. The legend and the canvas match exactly, in both directions: no glyph unlisted, no entry undrawn. Count the legend's visible entries with your eyes against the glyphs on the board — counting them in the DOM proves the data and not the picture, and a legend clipped by its own panel passes every DOM check while still lying to the reader.
3. The explainer's narrated path is one you can point at in the code. Re-open two of its citations and confirm the lines say what the panel claims.
4. Read three citations off the canvas, one of them from the densest corner. A character you cannot make out fails the check.
5. No label sits on another label, on an arrow, or on a neighbouring building — and the buildings have real space between them, not a huddle.
6. Spin the board to 90°, 180° and 270°, **and to two angles that are not multiples of 90** — the fit is widest and the labels are worst on the diagonals, so a board checked only at the square angles ships its re-fit and collision bugs intact. At every stop the whole board is still in frame — nothing clipped by an edge of the canvas or hidden under a panel — the near buildings still cover the far ones, labels are still upright and readable, and nothing has collided that was clear at 0°.
7. Pulses travel from source to target — the direction the arrowheads point — and the map still reads with motion disabled.

Judge the densest corner and the worst angle, not the average of the canvas — a map that is mostly fine at 0° is how this check gets rubber-stamped. Any failure means fix and re-render before showing the user.

**Report** the absolute path, then say plainly what you left off the map and why — the parts you could not cite are the most useful thing you can tell them.
