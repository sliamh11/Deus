"""
Unit tests for the architecture-explorer generator (build.py).

Covers the GENERATOR only — layer assignment, edge aggregation to file level,
test-file filtering, output schema, and determinism. The browser UI is verified
manually (open index.html after running build.py); see README.md.

Builds against a synthetic in-temp codegraph DB that mirrors the real schema
(files / nodes / edges), so no live codegraph DB is required.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

LAYERS = Path(__file__).resolve().parent.parent / "layers.json"


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "codegraph.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT, language TEXT,
                            size INTEGER, modified_at INTEGER, indexed_at INTEGER, node_count INTEGER);
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
                            file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
                            start_column INTEGER, end_column INTEGER, docstring TEXT, signature TEXT,
                            visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER,
                            is_abstract INTEGER, decorators TEXT, type_parameters TEXT, updated_at INTEGER);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT,
                            kind TEXT, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
        """
    )
    files = [
        ("src/channels/wa.ts", "ts", 100, 3),
        ("src/message-orchestrator.ts", "ts", 200, 4),
        ("evolution/judge/criteria.py", "py", 300, 2),
        ("src/foo.test.ts", "ts", 50, 1),       # test file -> excluded by default
        ("src/util-misc.ts", "ts", 40, 1),       # falls to "other"
    ]
    for path, lang, size, nc in files:
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (path, "h", lang, size, 0, 0, nc))
    nodes = [
        ("n_wa_fn", "function", "send", "src/channels/wa.ts", 10, 30, 1),
        ("n_wa_helper", "function", "fmt", "src/channels/wa.ts", 31, 40, 0),
        ("n_mo_fn", "function", "orchestrate", "src/message-orchestrator.ts", 5, 80, 1),
        ("n_crit", "class", "Rubric", "evolution/judge/criteria.py", 1, 120, 1),
        ("n_test_fn", "function", "t", "src/foo.test.ts", 1, 20, 0),
        ("n_util", "function", "misc", "src/util-misc.ts", 1, 25, 1),
    ]
    for nid, kind, name, fp, sl, el, exp in nodes:
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, start_line, "
            "end_line, start_column, end_column, is_exported, updated_at) VALUES (?,?,?,?,?,?,?,?,0,0,?,0)",
            (nid, kind, name, name, fp, "ts", sl, el, exp),
        )
    edges = [
        ("n_wa_fn", "n_mo_fn", "calls"),
        ("n_mo_fn", "n_crit", "calls"),
        ("n_mo_fn", "n_crit", "calls"),          # duplicate -> weight 2
        ("n_wa_helper", "n_wa_fn", "calls"),     # same file -> dropped
        ("n_mo_fn", "n_wa_fn", "contains"),      # contains -> dropped
        ("n_crit", "n_mo_fn", "extends"),        # -> references
        ("n_test_fn", "n_mo_fn", "calls"),       # from a test file -> dropped
    ]
    for s, t, k in edges:
        conn.execute("INSERT INTO edges (source, target, kind) VALUES (?,?,?)", (s, t, k))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def graph(tmp_path):
    return build.build(_make_db(tmp_path), LAYERS, include_tests=False)


class TestLayerAssignment:
    def test_first_match_wins(self):
        layers = json.loads(LAYERS.read_text())["layers"]
        assert build._assign_layer("src/channels/wa.ts", layers) == "channels"
        assert build._assign_layer("src/message-orchestrator.ts", layers) == "host"
        assert build._assign_layer("evolution/judge/criteria.py", layers) == "evolution"
        assert build._assign_layer("src/linear-dispatcher.ts", layers) == "linear"

    def test_unmatched_falls_to_other(self):
        layers = json.loads(LAYERS.read_text())["layers"]
        assert build._assign_layer("src/util-misc.ts", layers) == "other"


class TestTestFiltering:
    def test_test_files_excluded_by_default(self, graph):
        assert "src/foo.test.ts" not in {n["file_path"] for n in graph["nodes"]}

    def test_test_files_included_with_flag(self, tmp_path):
        g = build.build(_make_db(tmp_path), LAYERS, include_tests=True)
        assert "src/foo.test.ts" in {n["file_path"] for n in g["nodes"]}

    def test_is_test_path_helper(self):
        assert build._is_test_path("src/foo.test.ts")
        assert build._is_test_path("scripts/tests/test_x.py")
        assert build._is_test_path("a/__tests__/b.ts")
        assert not build._is_test_path("src/message-orchestrator.ts")


class TestEdgeAggregation:
    def _edge(self, graph, src, tgt, kind):
        return next(
            (e for e in graph["edges"] if e["source"] == src and e["target"] == tgt and e["kind"] == kind),
            None,
        )

    def test_calls_aggregated_with_weight(self, graph):
        e = self._edge(graph, "src/message-orchestrator.ts", "evolution/judge/criteria.py", "calls")
        assert e is not None and e["weight"] == 2

    def test_contains_dropped(self, graph):
        assert not any(e["kind"] == "contains" for e in graph["edges"])

    def test_self_file_edges_dropped(self, graph):
        assert not any(e["source"] == e["target"] for e in graph["edges"])

    def test_reference_kinds_rolled_up(self, graph):
        e = self._edge(graph, "evolution/judge/criteria.py", "src/message-orchestrator.ts", "references")
        assert e is not None

    def test_edges_from_test_files_excluded(self, graph):
        assert not any(e["source"] == "src/foo.test.ts" for e in graph["edges"])

    def test_only_known_edge_classes(self, graph):
        assert {e["kind"] for e in graph["edges"]} <= {"imports", "calls", "references"}


class TestSchema:
    def test_top_level_keys(self, graph):
        assert set(graph.keys()) == {"meta", "layers", "nodes", "edges"}

    def test_node_keys(self, graph):
        n = graph["nodes"][0]
        assert {"id", "label", "file_path", "layer", "loc", "n_symbols", "exported", "top_symbols"} <= set(n)

    def test_loc_from_max_end_line(self, graph):
        crit = next(n for n in graph["nodes"] if n["file_path"] == "evolution/judge/criteria.py")
        assert crit["loc"] == 120

    def test_only_used_layers_emitted(self, graph):
        used = {n["layer"] for n in graph["nodes"]}
        assert {l["id"] for l in graph["layers"]} == used

    def test_meta_counts_match(self, graph):
        assert graph["meta"]["n_files"] == len(graph["nodes"])
        assert graph["meta"]["n_edges"] == len(graph["edges"])

    def test_deterministic(self, tmp_path):
        db = _make_db(tmp_path)
        a = build.build(db, LAYERS, include_tests=False)
        b = build.build(db, LAYERS, include_tests=False)
        a["meta"]["generated_at"] = b["meta"]["generated_at"] = 0
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
