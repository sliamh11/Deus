#!/usr/bin/env python3
"""
Architecture Explorer — graph generator.

Reads a codegraph SQLite database and a layer-rule config, aggregates the
symbol-level graph up to FILE/module level, assigns each file to an
architectural layer, and emits `graph-data.js` (a `window.GRAPH = {...}` global)
that the static `index.html` renders with no server and no network.

Self-contained: standard library only (sqlite3, json, argparse, fnmatch,
colorsys, pathlib). Portable — copy this directory into any codegraph-indexed
repo and run it. Layering is automatic on any project: if the bundled
`layers.json` rules don't fit the repo (few/no files match), layers are derived
from the directory structure instead. Edit `layers.json` (or pass `--layers`) to
curate; pass `--auto` to force directory-derived layers.

Usage:
  python3 build.py                       # uses ../../.codegraph/codegraph.db
  python3 build.py --db path/to.db       # explicit DB (auto-layers if layers.json doesn't fit)
  python3 build.py --auto                # force directory-derived layers
  python3 build.py --layers-depth 2      # group by two path levels (src/memory) under --auto
  python3 build.py --layers my.json      # explicit curated layer rules (always trusted)
  python3 build.py --include-tests       # keep *.test.* / test_* / tests/ files
  python3 build.py --json                # print a machine-readable summary

Then open index.html (python3 -m webbrowser index.html).
"""
from __future__ import annotations

import argparse
import ast
import colorsys
import fnmatch
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

# ── Minimal typed exit codes (self-contained; mirrors scripts/_exit_codes.py
#    conventions but intentionally does not import it — this tool is portable). ──
SUCCESS = 0
USAGE_ERROR = 2
NOT_FOUND = 4
INTERNAL_ERROR = 70

# Symbol-relationship edge kinds rolled up into a single "references" class at
# file level. `contains` (file->symbol) is intra-file and dropped.
_REFERENCE_KINDS = {"extends", "implements", "references", "instantiates", "decorates"}
_PRIMARY_KINDS = {"imports", "calls"}
_SYMBOL_KINDS = ("class", "function", "method", "interface", "struct", "trait", "enum")


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return (
        ".test." in base
        or ".spec." in base
        or base.startswith("test_")
        or "/tests/" in p
        or "/__tests__/" in p
        or "/test/" in p
    )


def _load_layers(layers_path: Path) -> list[dict]:
    data = json.loads(layers_path.read_text(encoding="utf-8"))
    layers = data.get("layers", [])
    if not layers:
        raise ValueError(f"{layers_path} has no 'layers'")
    return layers


def _assign_layer(path: str, layers: list[dict]) -> str:
    """First matching glob wins (ordered rules). Falls back to last layer."""
    norm = path.replace("\\", "/")
    for layer in layers:
        for glob in layer.get("globs", []):
            if fnmatch.fnmatch(norm, glob):
                return layer["id"]
    return layers[-1]["id"]


# ── Auto-layering (directory-derived) ─────────────────────────────────────────
# When the curated layers.json doesn't fit a repo, layers are derived from the
# directory structure so the explorer is useful on ANY codegraph-indexed project.
_OTHER_ID = "other"          # catch-all: root files + the tail beyond the cap
_AUTO_MAX_LAYERS = 16        # cap on distinct directory groups (palette + clarity)
_AUTO_MIN_GROUPS = 4         # below this many depth-1 dirs, descend to depth 2
_AUTO_DOMINANT_FRAC = 0.70   # if one depth-1 dir holds more than this share, descend
# Min fraction of files that must land OUTSIDE a config's catch-all/fallback layer
# for the config to count as a "fit". Measured: Deus's own layers.json scores
# 279/389 = 0.72 on the Deus repo (fits → stays curated); the same rules score ~0
# on any other repo (nothing matches → auto-derive). 0.40 cleanly separates them.
_FIT_THRESHOLD = 0.40


def _hsl_to_hex(hue: float, sat: float = 0.55, light: float = 0.55) -> str:
    """Palette color for an evenly-spaced `hue` in [0, 360).

    NOTE: stdlib `colorsys` uses HLS argument order (hue, LIGHTNESS, SATURATION),
    NOT HSL — so saturation is passed LAST. Getting this wrong silently yields
    desaturated colors.
    """
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, sat)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _sanitize_layer_id(name: str) -> str:
    """Layer ids must be whitespace-free.

    app.js encodes edge-aggregation keys as ``src_layer + ' ' + tgt_layer + ' ' +
    kind`` and splits on the SPACE, so a space inside a layer id corrupts parsing
    (see app.js, buildData). Directory names can contain spaces, so collapse any
    run of whitespace to a single '_'. The human-facing label keeps the original
    name. ('/' is safe — it is not the delimiter.)
    """
    return "_".join(name.split()) or _OTHER_ID


def _dir_prefix(path: str, depth: int) -> str | None:
    """Directory prefix of `path` at `depth` segments; None for a root file.

    A file with fewer directory levels than `depth` uses all it has
    ('src/foo.ts' at depth 2 -> 'src'); a file with no directory -> None.
    """
    dirs = path.replace("\\", "/").split("/")[:-1]   # drop the filename
    if not dirs:
        return None
    return "/".join(dirs[:depth])


def _auto_layers(paths: list[str], depth: int | None = None) -> tuple[list[dict], dict, int]:
    """Derive layers + a direct {path -> layer_id} map from directory structure.

    No globs: assignment is exact (a path's layer is its directory prefix), so
    fnmatch metacharacters, prefix-substring collisions, and shallow/deep glob
    ordering are all non-issues.

    `depth`: 1 = top-level dir, 2 = two levels. When None, auto-select: start at
    depth 1 and descend to 2 if depth 1 is too coarse (< _AUTO_MIN_GROUPS distinct
    dirs, or one dir holds > _AUTO_DOMINANT_FRAC of files). The largest
    _AUTO_MAX_LAYERS groups are kept; the smaller tail and all root files merge
    into a trailing catch-all 'other' layer.

    Returns ``(layers, assign, depth)``.
    """
    total = len(paths)

    def counts_at(d: int) -> Counter:
        c: Counter = Counter()
        for p in paths:
            g = _dir_prefix(p, d)
            if g is not None:
                c[g] += 1
        return c

    if depth is None:
        depth = 1
        d1 = counts_at(1)
        dominant = (max(d1.values()) / total) if (d1 and total) else 1.0
        if len(d1) < _AUTO_MIN_GROUPS or dominant > _AUTO_DOMINANT_FRAC:
            depth = 2

    counts = counts_at(depth)
    # Largest groups first; ties broken by first-occurrence order in `paths`
    # (Counter.most_common + CPython 3.7+ dict ordering = deterministic). The tail
    # beyond the cap falls through to 'other'.
    top = [g for g, _ in counts.most_common(_AUTO_MAX_LAYERS)]
    top_id = {g: _sanitize_layer_id(g) for g in top}
    kept = set(top)

    assign: dict = {}
    for p in paths:
        g = _dir_prefix(p, depth)
        assign[p] = top_id[g] if (g in kept) else _OTHER_ID

    has_other = any(v == _OTHER_ID for v in assign.values())
    palette_n = max(len(top) + (1 if has_other else 0), 1)
    layers = [
        {
            "id": top_id[g],
            "label": g,
            "color": _hsl_to_hex(360.0 * i / palette_n),
            "globs": [],   # auto mode assigns via the returned map, not globs
        }
        for i, g in enumerate(top)
    ]
    if has_other:
        layers.append({"id": _OTHER_ID, "label": "Other", "color": "#566573", "globs": []})
    return layers, assign, depth


def _resolve_layers(
    kept_paths: list[str],
    layers_path: Path | None,
    auto: bool,
    layers_depth: int | None,
    allow_auto_fallback: bool,
) -> tuple[list[dict], dict, str, int | None]:
    """Decide the layer set + a {path -> layer_id} assignment for `kept_paths`.

    - ``auto`` True: derive layers from directory structure (ignores layers_path).
    - else: load `layers_path` (a curated config). If ``allow_auto_fallback`` and
      the config is a POOR FIT for this repo (< _FIT_THRESHOLD of files land
      outside its catch-all/fallback layer), auto-derive instead. This is what
      lets the bundled Deus layers.json act as a curated default on the Deus repo
      yet auto-derive on any other repo (copy-in or ``--db``). An EXPLICIT
      ``--layers`` is always trusted (the caller passes allow_auto_fallback=False).

    Returns ``(layers, assign, mode, depth)``; mode in {'auto', 'config'}; depth is
    the auto depth (None for config mode).
    """
    if auto:
        layers, assign, depth = _auto_layers(kept_paths, layers_depth)
        return layers, assign, "auto", depth
    cfg = _load_layers(layers_path)
    assign = {p: _assign_layer(p, cfg) for p in kept_paths}
    if allow_auto_fallback and kept_paths:
        # Fit = fraction of files matched to a NON-fallback layer. Assumes the
        # config's LAST layer is its catch-all (the bundled layers.json ends with a
        # '*' glob; _assign_layer also returns it for no-match) — which holds for
        # the only config this fallback ever runs on (the bundled default).
        fallback_id = cfg[-1]["id"]
        outside = sum(1 for p in kept_paths if assign[p] != fallback_id)
        if (outside / len(kept_paths)) < _FIT_THRESHOLD:
            layers, assign, depth = _auto_layers(kept_paths, layers_depth)
            return layers, assign, "auto", depth
    return cfg, assign, "config", None


def _py_doc_maps(repo_root: Path, rel_path: str, cache: dict) -> tuple[dict, dict]:
    """Real PEP-257 docstrings for a Python file, keyed two ways.

    codegraph stores the leading COMMENT above a Python def as its "docstring"
    (wrong); we recover the actual docstring with the stdlib `ast`. Returns
    ``(by_line, by_name)`` where:
      - ``by_line``: {def/class start line -> docstring} — the precise match
        (line numbers are unique per def in a file, so no collisions).
      - ``by_name``: {symbol name -> docstring} — fallback when codegraph's
        start_line doesn't line up with ast's (e.g. decorator-offset). Last
        writer wins on duplicate names (e.g. two classes' ``__init__`` in one
        file collide) — an accepted limitation for an offline viz tool.
    Docstring value may be ``None`` (def exists but has no docstring). Parsed
    once per file and cached. On any read/parse error the maps are empty and the
    caller falls back to codegraph's value.
    """
    if rel_path in cache:
        return cache[rel_path]
    by_line: dict[int, str | None] = {}
    by_name: dict[str, str | None] = {}
    try:
        src = (repo_root / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                by_line[node.lineno] = doc
                by_name[node.name] = doc
    except (OSError, SyntaxError, ValueError):
        # OSError: unreadable file. SyntaxError: malformed source. ValueError:
        # ast.parse rejects source containing NUL bytes (and UnicodeDecodeError,
        # a ValueError subclass, covers a bad encoding). All -> fall back.
        by_line, by_name = {}, {}
    result = (by_line, by_name)
    cache[rel_path] = result
    return result


def _resolve_doc(repo_root: Path, fp: str, name: str, start_line, raw_doc: str, cache: dict) -> str:
    """Doc string for a symbol's detail panel, capped at 600 chars.

    Non-Python: trust codegraph's value (its TS/JS JSDoc handling is fine).
    Python: prefer the real PEP-257 docstring (by start line, then by name). When
    a Python symbol IS matched but has no real docstring, return "" rather than
    codegraph's leading-comment value (which is misleading). Only fall back to
    codegraph's value when the symbol can't be matched at all (parse/read error).
    """
    if not fp.endswith(".py"):
        return raw_doc[:600]
    by_line, by_name = _py_doc_maps(repo_root, fp, cache)
    if start_line in by_line:
        return (by_line[start_line] or "")[:600]
    if name in by_name:
        return (by_name[name] or "")[:600]
    return raw_doc[:600]


def build(
    db_path: Path,
    layers_path: Path | None = None,
    include_tests: bool = False,
    *,
    auto: bool = False,
    layers_depth: int | None = None,
    allow_auto_fallback: bool = False,
) -> dict:
    """Build the architecture graph from a codegraph DB.

    Layer assignment: with ``auto`` (or when ``allow_auto_fallback`` is set and the
    config is a poor fit for the repo) layers are derived from the directory
    structure; otherwise the curated ``layers_path`` rules are used. ``layers_path``
    may be None ONLY with ``auto=True`` — every non-auto caller must supply a config
    path (the CLI defaults it to the bundled layers.json). See _resolve_layers.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"codegraph DB not found: {db_path}")
    if not auto and layers_path is None:
        raise ValueError("layers_path is required unless auto=True")
    # Repo root = the directory holding `.codegraph/` — used to resolve source
    # files for Python docstring recovery (see _py_doc_maps) and editor deep-links.
    repo_root = db_path.resolve().parent.parent
    py_doc_cache: dict = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Files (the file-level nodes of the architecture graph).
        files = {
            r["path"]: {"language": r["language"], "size": r["size"], "n_symbols": r["node_count"]}
            for r in conn.execute("SELECT path, language, size, node_count FROM files")
        }
        # LOC proxy: max end_line per file.
        loc = {
            r["file_path"]: r["loc"]
            for r in conn.execute(
                "SELECT file_path, MAX(end_line) AS loc FROM nodes GROUP BY file_path"
            )
        }
        # node_id -> file_path (to roll symbol edges up to files).
        node_file = {r["id"]: r["file_path"] for r in conn.execute("SELECT id, file_path FROM nodes")}
        # Top exported/defined symbols per file (for the detail panel).
        top_syms: dict[str, list] = {}
        exported: dict[str, list] = {}
        # The IN-clause is filled with `?` PLACEHOLDERS (one per kind); the kind
        # values are bound via execute() below — not string-interpolated. Safe.
        q = (
            "SELECT file_path, name, kind, signature, docstring, start_line, is_exported "
            "FROM nodes WHERE kind IN (%s) ORDER BY is_exported DESC, start_line ASC"
            % ",".join("?" * len(_SYMBOL_KINDS))
        )
        for r in conn.execute(q, _SYMBOL_KINDS):
            fp = r["file_path"]
            lst = top_syms.setdefault(fp, [])
            if len(lst) < 12:
                lst.append(
                    {
                        "name": r["name"],
                        "kind": r["kind"],
                        "signature": (r["signature"] or "")[:160],
                        "doc": _resolve_doc(
                            repo_root, fp, r["name"], r["start_line"],
                            r["docstring"] or "", py_doc_cache,
                        ),
                        "line": r["start_line"],
                    }
                )
            if r["is_exported"]:
                exported.setdefault(fp, []).append(r["name"])

        # ── Layer resolution ──────────────────────────────────────────────────
        # Files surviving the test filter, then layered (curated config or, when it
        # doesn't fit / --auto, derived from the directory structure).
        kept_list = sorted(p for p in files if include_tests or not _is_test_path(p))
        layers, assign, layer_mode, layer_depth = _resolve_layers(
            kept_list, layers_path, auto, layers_depth, allow_auto_fallback
        )
        kept = set(kept_list)

        # ── Nodes (files), layer-assigned ─────────────────────────────────────
        nodes = []
        for path in kept_list:
            info = files[path]
            nodes.append(
                {
                    "id": path,
                    "label": path.replace("\\", "/").rsplit("/", 1)[-1],
                    "file_path": path,
                    "layer": assign[path],
                    "language": info["language"],
                    "loc": int(loc.get(path, 0) or 0),
                    "n_symbols": int(info["n_symbols"] or 0),
                    "size": int(info["size"] or 0),
                    "exported": exported.get(path, [])[:24],
                    "top_symbols": top_syms.get(path, []),
                }
            )

        # ── Edges, aggregated to file level (imports / calls / references) ─────
        agg: dict[tuple, int] = {}
        for r in conn.execute("SELECT source, target, kind FROM edges"):
            kind = r["kind"]
            if kind == "contains":
                continue
            sf = node_file.get(r["source"])
            tf = node_file.get(r["target"])
            if not sf or not tf or sf == tf:
                continue
            if sf not in kept or tf not in kept:
                continue
            if kind in _PRIMARY_KINDS:
                cls = kind
            elif kind in _REFERENCE_KINDS:
                cls = "references"
            else:
                continue
            agg[(sf, tf, cls)] = agg.get((sf, tf, cls), 0) + 1

        edges = [
            {"source": sf, "target": tf, "kind": cls, "weight": w}
            for (sf, tf, cls), w in sorted(agg.items())
        ]

        used_layers = {n["layer"] for n in nodes}
        out_layers = [
            {"id": l["id"], "label": l["label"], "color": l["color"], "order": i}
            for i, l in enumerate(layers)
            if l["id"] in used_layers
        ]

        return {
            "meta": {
                "repo": repo_root.name,
                "repo_root": str(repo_root),  # for editor deep-links
                "generated_at": int(time.time()),
                "db_path": str(db_path),
                "n_files": len(nodes),
                "n_edges": len(edges),
                "include_tests": include_tests,
                "layer_mode": layer_mode,     # 'config' (curated) | 'auto' (directory-derived)
                "layer_depth": layer_depth,   # auto grouping depth (None in config mode)
            },
            "layers": out_layers,
            "nodes": sorted(nodes, key=lambda n: (n["layer"], n["id"])),
            "edges": edges,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent  # tools/architecture-explorer -> repo root
    bundled_layers = here / "layers.json"
    ap = argparse.ArgumentParser(description="Generate the architecture-explorer graph data.")
    ap.add_argument("--db", type=Path, default=repo_root / ".codegraph" / "codegraph.db")
    ap.add_argument("--layers", type=Path, default=None,
                    help="curated layer rules (JSON); always trusted. Default: the bundled "
                         "layers.json, used only if it fits the repo (else auto-derived).")
    ap.add_argument("--auto", action="store_true",
                    help="force directory-derived layers (ignore layers.json)")
    ap.add_argument("--layers-depth", type=int, default=None, dest="layers_depth",
                    help="auto-layer grouping depth (1=top-level dir, 2=two levels); default: auto-select")
    ap.add_argument("--out", type=Path, default=here / "graph-data.js")
    ap.add_argument("--include-tests", action="store_true", help="keep test files")
    ap.add_argument("--json", action="store_true", help="print a machine-readable summary")
    args = ap.parse_args(argv)

    # Resolve the layering mode:
    #   --auto            -> directory-derived (no config).
    #   --layers PATH     -> explicit curated config, always trusted.
    #   (neither)         -> the bundled layers.json, but allow auto-fallback if it
    #                        doesn't fit this repo (makes it work on external repos).
    if args.auto:
        layers_path, auto, allow_auto_fallback = None, True, False
    elif args.layers is not None:
        layers_path, auto, allow_auto_fallback = args.layers, False, False
    else:
        layers_path, auto, allow_auto_fallback = bundled_layers, False, True

    try:
        graph = build(
            args.db, layers_path, args.include_tests,
            auto=auto, layers_depth=args.layers_depth,
            allow_auto_fallback=allow_auto_fallback,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return NOT_FOUND
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return USAGE_ERROR
    except sqlite3.Error as e:
        print(f"sqlite error: {e}", file=sys.stderr)
        return INTERNAL_ERROR

    payload = "window.GRAPH = " + json.dumps(graph, separators=(",", ":")) + ";\n"
    args.out.write_text(payload, encoding="utf-8")

    mode = graph["meta"]["layer_mode"]
    depth = graph["meta"]["layer_depth"]
    summary = {
        "out": str(args.out),
        "n_files": graph["meta"]["n_files"],
        "n_edges": graph["meta"]["n_edges"],
        "layer_mode": mode,
        "layer_depth": depth,
        "layers": [l["id"] for l in graph["layers"]],
    }
    if args.json:
        print(json.dumps(summary))
    else:
        mode_note = f"auto (depth {depth})" if mode == "auto" else "config"
        print(f"wrote {args.out}")
        print(f"  files: {summary['n_files']}  edges: {summary['n_edges']}  layers: {mode_note}")
        print(f"  layers: {', '.join(summary['layers'])}")
        print("  open it:  python3 -m webbrowser " + str(here / "index.html"))
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
