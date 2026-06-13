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


# Source whose docstrings differ from the leading comments codegraph would store.
# Line numbers matter — they drive the by_line match in _resolve_doc.
#  2 def greet         3 "Return a friendly greeting."
#  9 def run          10 "Run the thing."
# 15 def bare         (no docstring)
# 19 @deco / 20 def decorated / 21 "Decorated doc."
_PY_SRC = '''# this is a leading comment, NOT the docstring
def greet(name):
    """Return a friendly greeting."""
    return "hi " + name


class Thing:
    """A thing with behavior."""
    def run(self):
        """Run the thing."""
        return 1


# misleading comment
def bare():
    return 0


@deco
def decorated():
    """Decorated doc."""
    return 2
'''


def _make_py_db(tmp_path: Path) -> Path:
    """A codegraph DB at <tmp>/.codegraph/codegraph.db (so repo_root == <tmp>),
    plus a real sample.py on disk, with codegraph's WRONG leading-comment values
    in the docstring column — exercises the Python ast-docstring override."""
    (tmp_path / "sample.py").write_text(_PY_SRC, encoding="utf-8")
    cg = tmp_path / ".codegraph"
    cg.mkdir(parents=True, exist_ok=True)
    db = cg / "codegraph.db"
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
    for path, lang, nc in [("sample.py", "py", 4), ("sample.ts", "ts", 1)]:
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (path, "h", lang, 100, 0, 0, nc))
    # (id, kind, name, file, start_line, end_line, docstring[=codegraph's WRONG value])
    nodes = [
        ("greet", "function", "greet", "sample.py", 2, 4, "this is a leading comment, NOT the docstring"),
        ("run", "method", "run", "sample.py", 9, 11, "A thing with behavior."),
        ("bare", "function", "bare", "sample.py", 15, 16, "misleading comment"),
        # start_line points at the @deco line (19), not the def (20): forces the by_name fallback
        ("decorated", "function", "decorated", "sample.py", 19, 22, "stale comment"),
        ("tsfn", "function", "tsFn", "sample.ts", 1, 10, "JSDoc summary."),
    ]
    for nid, kind, name, fp, sl, el, doc in nodes:
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, start_line, "
            "end_line, start_column, end_column, docstring, is_exported, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,1,0)",
            (nid, kind, name, name, fp, ("py" if fp.endswith(".py") else "ts"), sl, el, doc),
        )
    conn.commit()
    conn.close()
    return db


class TestPythonDocstring:
    """Fix: codegraph stores the leading comment as a Python "docstring"; build.py
    recovers the real PEP-257 docstring via ast and overrides it (Python only)."""

    def _sym(self, graph, fp, name):
        node = next(n for n in graph["nodes"] if n["file_path"] == fp)
        return next(s for s in node["top_symbols"] if s["name"] == name)

    @pytest.fixture
    def graph(self, tmp_path):
        return build.build(_make_py_db(tmp_path), LAYERS, include_tests=False)

    def test_real_docstring_beats_leading_comment(self, graph):
        # by_line match: codegraph start_line (2) == ast def lineno
        assert self._sym(graph, "sample.py", "greet")["doc"] == "Return a friendly greeting."

    def test_method_docstring_recovered(self, graph):
        assert self._sym(graph, "sample.py", "run")["doc"] == "Run the thing."

    def test_no_docstring_yields_empty_not_comment(self, graph):
        # matched but no real docstring -> "" (NOT codegraph's misleading comment)
        assert self._sym(graph, "sample.py", "bare")["doc"] == ""

    def test_by_name_fallback_when_start_line_offset(self, graph):
        # start_line (19, the decorator) misses by_line; name match recovers it
        assert self._sym(graph, "sample.py", "decorated")["doc"] == "Decorated doc."

    def test_non_python_docstring_preserved(self, graph):
        # TS/JS keep codegraph's value (its JSDoc handling is fine)
        assert self._sym(graph, "sample.ts", "tsFn")["doc"] == "JSDoc summary."

    def test_unreadable_py_falls_back_to_codegraph(self, tmp_path):
        # DB references a .py file that doesn't exist on disk -> parse fails ->
        # _py_doc_maps empty -> codegraph's value kept (graceful, no crash)
        cg = tmp_path / ".codegraph"
        cg.mkdir(parents=True, exist_ok=True)
        db = cg / "codegraph.db"
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
        conn.execute("INSERT INTO files VALUES ('ghost.py','h','py',10,0,0,1)")
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, start_line, "
            "end_line, start_column, end_column, docstring, is_exported, updated_at) "
            "VALUES ('g','function','gone','gone','ghost.py','py',1,5,0,0,'fallback doc',1,0)"
        )
        conn.commit()
        conn.close()
        graph = build.build(db, LAYERS, include_tests=False)
        node = next(n for n in graph["nodes"] if n["file_path"] == "ghost.py")
        assert node["top_symbols"][0]["doc"] == "fallback doc"
