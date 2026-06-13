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


# ── External-project support: directory-derived (auto) layers ─────────────────

_SCHEMA = """
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


def _make_simple_db(tmp_path: Path, paths: list[str]) -> Path:
    """Minimal codegraph DB at <tmp>/.codegraph/codegraph.db: one `files` row + one
    `nodes` row per path (paths must be DISTINCT — `path` is PRIMARY KEY). Enough to
    exercise layer resolution; no edges."""
    cg = tmp_path / ".codegraph"
    cg.mkdir(parents=True, exist_ok=True)
    db = cg / "codegraph.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for i, p in enumerate(paths):
        lang = "py" if p.endswith(".py") else "ts"
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (p, "h", lang, 50, 0, 0, 1))
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, start_line, "
            "end_line, start_column, end_column, is_exported, updated_at) VALUES (?,?,?,?,?,?,?,?,0,0,1,0)",
            (f"n{i}", "function", f"fn{i}", f"fn{i}", p, lang, 1, 10),
        )
    conn.commit()
    conn.close()
    return db


def _grp(prefix: str, n: int) -> list[str]:
    """n distinct file paths under `prefix`."""
    return [f"{prefix}/f{i}.ts" for i in range(n)]


class TestPalette:
    def test_hex_format(self):
        for h in (0, 90, 180, 270, 359):
            c = build._hsl_to_hex(h)
            assert len(c) == 7 and c[0] == "#"
            int(c[1:], 16)  # parses as hex (raises otherwise)

    def test_known_hue_red_dominant(self):
        # H=0 (red), L=0.5, S=0.7 -> red channel clearly dominant. Guards the
        # colorsys HLS argument order (lightness before saturation): a swap here
        # silently desaturates and this assertion fails.
        c = build._hsl_to_hex(0, sat=0.7, light=0.5)
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        assert r > g and r > b and r >= 178  # 0.7*255 ~= 178

    def test_distinct_colors(self):
        cols = [build._hsl_to_hex(360.0 * i / 12) for i in range(12)]
        assert len(set(cols)) == 12


class TestSanitizeAndPrefix:
    def test_sanitize_whitespace_to_underscore(self):
        assert build._sanitize_layer_id("my dir") == "my_dir"
        assert build._sanitize_layer_id("a\tb  c") == "a_b_c"

    def test_sanitize_keeps_slash(self):
        # '/' is NOT the app.js edge-key delimiter (space is) -> kept for depth-2 ids
        assert build._sanitize_layer_id("src/memory") == "src/memory"

    def test_sanitize_blank_falls_to_other(self):
        assert build._sanitize_layer_id("   ") == build._OTHER_ID

    def test_dir_prefix_depths(self):
        assert build._dir_prefix("src/memory/foo.ts", 1) == "src"
        assert build._dir_prefix("src/memory/foo.ts", 2) == "src/memory"
        assert build._dir_prefix("src/foo.ts", 2) == "src"   # fewer levels than depth
        assert build._dir_prefix("README.md", 1) is None     # root file (no directory)


class TestAutoLayers:
    def test_depth1_groups_by_top_dir(self):
        paths = _grp("src", 5) + _grp("lib", 5) + _grp("docs", 5) + _grp("test_x", 5)
        layers, assign, depth = build._auto_layers(paths)
        assert depth == 1
        assert {"src", "lib", "docs", "test_x"} <= {l["id"] for l in layers}
        assert set(assign) == set(paths)

    def test_descend_to_depth2_when_one_dir_dominant(self):
        paths = _grp("src/api", 8) + _grp("src/web", 8) + _grp("lib", 2)  # src = 16/18 > 70%
        layers, _, depth = build._auto_layers(paths)
        assert depth == 2
        assert "src/api" in {l["label"] for l in layers}

    def test_descend_when_too_few_top_dirs(self):
        paths = _grp("src/api", 3) + _grp("src/web", 3)  # only 1 top-level dir (<4)
        _, _, depth = build._auto_layers(paths)
        assert depth == 2

    def test_root_files_go_to_other(self):
        paths = _grp("src", 5) + _grp("lib", 5) + _grp("docs", 5) + _grp("x", 5) + ["README.md", "setup.py"]
        _, assign, _ = build._auto_layers(paths, depth=1)
        assert assign["README.md"] == build._OTHER_ID
        assert assign["setup.py"] == build._OTHER_ID

    def test_cap_and_long_tail_merge_into_other(self):
        # 20 top-level dirs (d00 biggest .. d19 smallest) -> capped at 16 + "other"
        paths = [p for i in range(20) for p in _grp(f"d{i:02d}", 20 - i)]
        layers, assign, _ = build._auto_layers(paths, depth=1)
        non_other = [l for l in layers if l["id"] != build._OTHER_ID]
        assert len(non_other) == build._AUTO_MAX_LAYERS  # 16
        assert any(l["id"] == build._OTHER_ID for l in layers)
        assert assign["d19/f0.ts"] == build._OTHER_ID    # smallest -> tail -> other
        assert assign["d00/f0.ts"] != build._OTHER_ID    # biggest -> kept

    def test_layers_ordered_by_file_count(self):
        paths = _grp("big", 10) + _grp("mid", 5) + _grp("small", 2)
        layers, _, _ = build._auto_layers(paths, depth=1)
        non_other = [l["id"] for l in layers if l["id"] != build._OTHER_ID]
        assert non_other[:3] == ["big", "mid", "small"]

    def test_every_path_assigned_exactly_once(self):
        paths = ["src/a.ts", "lib/b.ts", "c.md", "src/d/e.ts"]
        _, assign, _ = build._auto_layers(paths)
        assert set(assign) == set(paths)

    def test_whitespace_dir_id_sanitized_label_preserved(self):
        paths = _grp("my src", 5) + _grp("lib", 5) + _grp("x", 5) + _grp("y", 5)
        layers, assign, _ = build._auto_layers(paths, depth=1)
        msrc = next(l for l in layers if l["label"] == "my src")
        assert msrc["id"] == "my_src" and " " not in msrc["id"]   # protects app.js space-delim edge keys
        assert assign["my src/f0.ts"] == "my_src"

    def test_deterministic(self):
        paths = _grp("src", 5) + _grp("lib", 3) + _grp("x", 4) + _grp("y", 2)
        assert build._auto_layers(paths, depth=1) == build._auto_layers(paths, depth=1)

    def test_deterministic_with_ties(self):
        # equal counts -> ties broken by first-occurrence order in `paths` (stable)
        paths = _grp("x", 4) + _grp("y", 4) + _grp("z", 4)
        a = build._auto_layers(paths, depth=1)
        assert a == build._auto_layers(paths, depth=1)
        assert [l["id"] for l in a[0]] == ["x", "y", "z"]

    def test_distinct_colors_per_layer_including_other(self):
        # root files force an 'other' layer too; its grey must not collide with the palette
        paths = _grp("a", 5) + _grp("b", 4) + _grp("c", 3) + _grp("d", 2) + ["README.md", "x.py"]
        layers, _, _ = build._auto_layers(paths, depth=1)
        assert any(l["id"] == build._OTHER_ID for l in layers)
        colors = [l["color"] for l in layers]
        assert len(set(colors)) == len(colors)


class TestLayerResolution:
    def test_build_auto_mode(self, tmp_path):
        paths = _grp("src", 4) + _grp("lib", 4) + _grp("api", 4) + _grp("web", 4)
        g = build.build(_make_simple_db(tmp_path, paths), None, include_tests=False, auto=True)
        assert g["meta"]["layer_mode"] == "auto" and g["meta"]["layer_depth"] == 1
        assert g["meta"]["n_files"] == 16
        assert {"src", "lib", "api", "web"} <= {n["layer"] for n in g["nodes"]}

    def test_none_layers_without_auto_raises(self, tmp_path):
        db = _make_simple_db(tmp_path, ["src/a.ts"])
        with pytest.raises(ValueError):
            build.build(db, None, include_tests=False)  # auto=False + no config

    def test_foreign_repo_auto_fallback(self, tmp_path):
        # none of Deus's globs match -> with fallback allowed, auto-derive (not 1 blob)
        paths = _grp("app/api", 5) + _grp("app/web", 5) + _grp("server", 5) + _grp("client", 5)
        g = build.build(_make_simple_db(tmp_path, paths), LAYERS, include_tests=False,
                        allow_auto_fallback=True)
        assert g["meta"]["layer_mode"] == "auto"
        used = {n["layer"] for n in g["nodes"]}
        assert "channels" not in used and len(used) > 1

    def test_fitting_repo_keeps_curated_config(self, tmp_path):
        # Deus-matching paths -> curated config kept even with fallback allowed
        paths = _grp("src/channels", 5) + _grp("evolution", 5) + _grp("container", 5) + _grp("tui", 5)
        g = build.build(_make_simple_db(tmp_path, paths), LAYERS, include_tests=False,
                        allow_auto_fallback=True)
        assert g["meta"]["layer_mode"] == "config"

    def test_explicit_layers_never_auto_fallback(self, tmp_path):
        # allow_auto_fallback=False (an explicit --layers) -> config even on a poor fit
        paths = _grp("app", 5) + _grp("web", 5)
        g = build.build(_make_simple_db(tmp_path, paths), LAYERS, include_tests=False,
                        allow_auto_fallback=False)
        assert g["meta"]["layer_mode"] == "config"

    def test_invariant_same_files_and_edges_across_modes(self, tmp_path):
        # config vs auto must layer the SAME file set (none lost/duplicated) + same edges
        paths = _grp("src/channels", 4) + _grp("evolution", 4) + _grp("tui", 4)
        db = _make_simple_db(tmp_path, paths)
        cfg = build.build(db, LAYERS, include_tests=False)
        aut = build.build(db, None, include_tests=False, auto=True)
        assert cfg["meta"]["n_files"] == aut["meta"]["n_files"]
        assert cfg["meta"]["n_edges"] == aut["meta"]["n_edges"]
        assert {n["id"] for n in cfg["nodes"]} == {n["id"] for n in aut["nodes"]}

    def test_auto_layers_all_present_in_out(self, tmp_path):
        # every auto layer with files is emitted (out_layers filters to used)
        paths = _grp("src", 4) + _grp("lib", 4) + _grp("docs", 4) + _grp("api", 4)
        g = build.build(_make_simple_db(tmp_path, paths), None, include_tests=False, auto=True)
        assert {n["layer"] for n in g["nodes"]} == {l["id"] for l in g["layers"]}
