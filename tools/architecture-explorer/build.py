#!/usr/bin/env python3
"""
Architecture Explorer — graph generator.

Reads a codegraph SQLite database and a layer-rule config, aggregates the
symbol-level graph up to FILE/module level, assigns each file to an
architectural layer, and emits `graph-data.js` (a `window.GRAPH = {...}` global)
that the static `index.html` renders with no server and no network.

Self-contained: standard library only (sqlite3, json, argparse, fnmatch,
pathlib). Portable — copy this directory into any codegraph-indexed repo and
run it; edit `layers.json` to re-layer.

Usage:
  python3 build.py                       # uses ../../.codegraph/codegraph.db
  python3 build.py --db path/to.db       # explicit DB
  python3 build.py --include-tests       # keep *.test.* / test_* / tests/ files
  python3 build.py --json                # print a machine-readable summary

Then open index.html (python3 -m webbrowser index.html).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sqlite3
import sys
import time
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


def build(db_path: Path, layers_path: Path, include_tests: bool) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"codegraph DB not found: {db_path}")
    layers = _load_layers(layers_path)
    layer_ids = {l["id"] for l in layers}

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
                        "doc": (r["docstring"] or "")[:600],
                        "line": r["start_line"],
                    }
                )
            if r["is_exported"]:
                exported.setdefault(fp, []).append(r["name"])

        # ── Nodes (files), filtered + layer-assigned ──────────────────────────
        nodes = []
        kept: set[str] = set()
        for path, info in files.items():
            if not include_tests and _is_test_path(path):
                continue
            layer = _assign_layer(path, layers)
            kept.add(path)
            nodes.append(
                {
                    "id": path,
                    "label": path.replace("\\", "/").rsplit("/", 1)[-1],
                    "file_path": path,
                    "layer": layer,
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
                "repo": db_path.resolve().parent.parent.name,
                "repo_root": str(db_path.resolve().parent.parent),  # for editor deep-links
                "generated_at": int(time.time()),
                "db_path": str(db_path),
                "n_files": len(nodes),
                "n_edges": len(edges),
                "include_tests": include_tests,
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
    ap = argparse.ArgumentParser(description="Generate the architecture-explorer graph data.")
    ap.add_argument("--db", type=Path, default=repo_root / ".codegraph" / "codegraph.db")
    ap.add_argument("--layers", type=Path, default=here / "layers.json")
    ap.add_argument("--out", type=Path, default=here / "graph-data.js")
    ap.add_argument("--include-tests", action="store_true", help="keep test files")
    ap.add_argument("--json", action="store_true", help="print a machine-readable summary")
    args = ap.parse_args(argv)

    try:
        graph = build(args.db, args.layers, args.include_tests)
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

    summary = {
        "out": str(args.out),
        "n_files": graph["meta"]["n_files"],
        "n_edges": graph["meta"]["n_edges"],
        "layers": [l["id"] for l in graph["layers"]],
    }
    if args.json:
        print(json.dumps(summary))
    else:
        print(f"wrote {args.out}")
        print(f"  files: {summary['n_files']}  edges: {summary['n_edges']}")
        print(f"  layers: {', '.join(summary['layers'])}")
        print("  open it:  python3 -m webbrowser " + str(here / "index.html"))
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
