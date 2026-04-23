"""Tests for scripts/memory_tree.py — offline, stubbed embed()."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# Load the script as a module (it's a CLI tool, not installed as a package).
# Reuse the instance conftest pre-loaded so conftest's autouse path-isolation
# fixture applies to this file's tests too. See scripts/tests/conftest.py.
_ROOT = Path(__file__).resolve().parent.parent.parent
if "memory_tree" in sys.modules:
    mt = sys.modules["memory_tree"]
else:
    _SPEC = importlib.util.spec_from_file_location(
        "memory_tree", _ROOT / "scripts" / "memory_tree.py"
    )
    mt = importlib.util.module_from_spec(_SPEC)
    sys.modules["memory_tree"] = mt
    _SPEC.loader.exec_module(mt)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "tree.db"
    db = mt.open_db(db_path)
    yield db
    db.close()


@pytest.fixture
def fake_vault(tmp_path):
    """Create a small synthetic vault mirroring the real structure."""
    v = tmp_path / "vault"
    (v / "Persona" / "life").mkdir(parents=True)
    (v / "Persona" / "taste").mkdir()

    (v / "MEMORY_TREE.md").write_text(
        """---
id: root000000000000000000000000000001
type: memory-tree-root
title: Memory Navigation Tree
description: Root map — routes personal-fact queries to persona, projects, or infra branches.
level: 0
children:
  - Persona/INDEX.md
---
# Memory Tree
""",
        encoding="utf-8",
    )
    (v / "Persona" / "INDEX.md").write_text(
        """---
id: persona00000000000000000000000002
type: persona-index
title: Persona
description: Index for Liam's personal facts — household, career, taste, style.
level: 1
children:
  - Persona/life/household.md
  - Persona/taste/movies.md
---
""",
        encoding="utf-8",
    )
    (v / "Persona" / "life" / "household.md").write_text(
        """---
id: household000000000000000000000003
type: persona-node
title: Household
description: Who Liam lives with — Shani and Omer; Eden replaces Omer Aug 2026.
level: 2
see_also:
  - Persona/taste/movies.md
---
""",
        encoding="utf-8",
    )
    (v / "Persona" / "taste" / "movies.md").write_text(
        """---
id: movies000000000000000000000000004
type: persona-node
title: Movies
description: Liam's film taste — stylish crime, Nolan, Fincher; watches with roommates.
level: 2
see_also:
  - Persona/life/household.md
---
""",
        encoding="utf-8",
    )
    return v


class StubEmbed:
    """Deterministic sparse bag-of-words embedder for tests.

    Each token hashes to a small set of positive dimensions in the 768-dim
    space. Shared tokens across two texts → shared active dims → cosine
    correlates with token overlap. Good enough to validate retrieval logic
    without calling a real embedding model.
    """

    def __init__(self):
        self._cache = {}

    def __call__(self, text: str) -> list[float]:
        if text in self._cache:
            return self._cache[text]
        import hashlib
        import re as _re

        vec = [0.0] * mt.EMBED_DIM
        tokens = set(_re.findall(r"\w+", text.lower()))
        for tok in tokens:
            if len(tok) < 2:
                continue
            h = hashlib.sha256(tok.encode()).digest()
            for i in range(8):
                idx = int.from_bytes(h[i * 2 : i * 2 + 2], "big") % mt.EMBED_DIM
                vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        vec = [x / norm for x in vec]
        self._cache[text] = vec
        return vec


@pytest.fixture
def stub_embed(monkeypatch):
    s = StubEmbed()
    monkeypatch.setattr(mt, "embed_text", s)
    return s


# ── ID + hash helpers ─────────────────────────────────────────────────────────

class TestIds:
    def test_make_id_is_32_hex(self):
        nid = mt.make_id()
        assert len(nid) == 32
        int(nid, 16)  # raises if not hex

    def test_make_id_is_sortable_by_time(self):
        a = mt.make_id()
        time.sleep(0.002)
        b = mt.make_id()
        # Timestamps are first 12 hex chars (6 bytes); with 2ms sleep a<b.
        assert a[:12] <= b[:12]

    def test_make_id_unique(self):
        ids = {mt.make_id() for _ in range(500)}
        assert len(ids) == 500

    def test_content_hash_stable(self):
        assert mt.content_hash("hello") == mt.content_hash("hello")
        assert mt.content_hash("a") != mt.content_hash("b")


# ── Vector math ───────────────────────────────────────────────────────────────

class TestVectors:
    def test_cosine_identical_is_one(self):
        v = [1.0, 0.0, 0.0]
        assert abs(mt.cosine(v, v) - 1.0) < 1e-6

    def test_cosine_orthogonal_is_zero(self):
        assert abs(mt.cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6

    def test_cosine_handles_empty(self):
        assert mt.cosine([], [1.0]) == 0.0

    def test_serialize_roundtrip(self):
        v = [0.1, -0.2, 0.3, 0.4]
        assert mt.deserialize(mt.serialize(v)) == pytest.approx(v, abs=1e-6)


# ── Frontmatter parser ────────────────────────────────────────────────────────

class TestFrontmatter:
    def test_parses_scalars(self):
        fm = mt.parse_frontmatter(
            "---\nid: abc\ntitle: Hello\ndescription: a thing\n---\n"
        )
        assert fm["id"] == "abc"
        assert fm["title"] == "Hello"
        assert fm["description"] == "a thing"

    def test_parses_list_block(self):
        fm = mt.parse_frontmatter(
            "---\nchildren:\n  - a.md\n  - b.md\n---\n"
        )
        assert fm["children"] == ["a.md", "b.md"]

    def test_parses_list_inline(self):
        fm = mt.parse_frontmatter("---\nsee_also: [x.md, y.md]\n---\n")
        assert fm["see_also"] == ["x.md", "y.md"]

    def test_parses_level(self):
        assert mt.parse_frontmatter("---\nlevel: 3\n---\n")["level"] == 3

    def test_no_frontmatter_returns_empty(self):
        assert mt.parse_frontmatter("just body") == {}

    def test_handles_quoted_values(self):
        fm = mt.parse_frontmatter('---\ntitle: "With quotes"\n---\n')
        assert fm["title"] == "With quotes"

    def test_summary_falls_back_to_description(self):
        fm = mt.parse_frontmatter("---\nsummary: an existing vault field\n---\n")
        assert fm["description"] == "an existing vault field"

    def test_description_overrides_summary(self):
        fm = mt.parse_frontmatter(
            "---\nsummary: old\ndescription: new\n---\n"
        )
        assert fm["description"] == "new"


# ── DB schema ─────────────────────────────────────────────────────────────────

class TestDb:
    def test_creates_all_tables(self, tmp_db):
        tables = {
            r[0]
            for r in tmp_db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        assert {"nodes", "edges", "queries_log", "calibration"} <= tables

    def test_upsert_node_inserts(self, tmp_db):
        mt.upsert_node(
            tmp_db,
            node_id="n1",
            path="a.md",
            title="A",
            description="desc",
            level=0,
            node_type="root",
            embedding=None,
            content_hash_val="h1",
        )
        row = tmp_db.execute("SELECT id, title, content_hash FROM nodes").fetchone()
        assert row == ("n1", "A", "h1")

    def test_upsert_node_supersedes_by_path_on_id_change(self, tmp_db):
        mt.upsert_node(
            tmp_db, node_id="old", path="a.md", title="A",
            description="d", level=0, node_type="t", embedding=None, content_hash_val="h",
        )
        mt.upsert_node(
            tmp_db, node_id="new", path="a.md", title="A",
            description="d", level=0, node_type="t", embedding=None, content_hash_val="h",
        )
        old_row = tmp_db.execute(
            "SELECT orphaned_at, orphan_reason FROM nodes WHERE id = 'old'"
        ).fetchone()
        assert old_row[0] is not None
        assert old_row[1] == "superseded"

    def test_upsert_edge_idempotent(self, tmp_db):
        mt.upsert_edge(tmp_db, src="a", dst="b", kind="child")
        mt.upsert_edge(tmp_db, src="a", dst="b", kind="child", weight=2.0)
        rows = tmp_db.execute(
            "SELECT weight FROM edges WHERE src_id='a' AND dst_id='b'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2.0

    def test_edge_rejects_self_loop(self, tmp_db):
        with pytest.raises(sqlite3.IntegrityError):
            mt.upsert_edge(tmp_db, src="a", dst="a", kind="see_also")

    def test_expire_edges_missing(self, tmp_db):
        mt.upsert_edge(tmp_db, src="a", dst="b", kind="see_also")
        mt.upsert_edge(tmp_db, src="a", dst="c", kind="see_also")
        mt.expire_edges_missing(tmp_db, src="a", kind="see_also", keep_dst={"b"})
        active = tmp_db.execute(
            "SELECT dst_id FROM edges WHERE src_id='a' AND kind='see_also' AND expired_at IS NULL"
        ).fetchall()
        assert active == [("b",)]


# ── Build ─────────────────────────────────────────────────────────────────────

class TestBuild:
    def test_build_walks_vault(self, tmp_db, fake_vault, stub_embed):
        counts = mt.build_tree(fake_vault, tmp_db)
        assert counts["nodes"] == 4
        assert counts["embedded"] == 4
        assert counts["edges"] >= 5  # 1 root→persona + 2 persona→leaves + 2 see_also

    def test_build_is_idempotent_on_rerun(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        counts2 = mt.build_tree(fake_vault, tmp_db)
        assert counts2["embedded"] == 0  # content_hash unchanged

    def test_rebuild_orphans_all(self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch):
        # Redirect DB_PATH so _backup_db targets the temp file.
        monkeypatch.setattr(mt, "DB_PATH", Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        mt.build_tree(fake_vault, tmp_db)
        mt.build_tree(fake_vault, tmp_db, rebuild=True)
        # Active count still 4 (reinserted); original rows soft-deleted and regenerated new IDs.
        active = tmp_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0]
        assert active == 4

    def test_missing_file_gets_orphaned(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        (fake_vault / "Persona" / "taste" / "movies.md").unlink()
        counts = mt.build_tree(fake_vault, tmp_db)
        assert counts["orphaned"] == 1

    def test_rebuild_aborts_when_vault_shrinks(
        self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch
    ):
        """Rebuild with empty vault must abort — protects the 2026-04-15 wipe case."""
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
        monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "audit.jsonl")
        mt.build_tree(fake_vault, tmp_db)  # populate
        empty_vault = tmp_path / "empty-vault"
        empty_vault.mkdir()
        with pytest.raises(ValueError, match="Refusing rebuild"):
            mt.build_tree(empty_vault, tmp_db, rebuild=True)
        # Active nodes survive.
        active = tmp_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0]
        assert active == 4
        # Audit line was written.
        audit = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert any('"action": "rebuild_aborted"' in line for line in audit)

    def test_rebuild_force_bypasses_safety(
        self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch
    ):
        """force=True lets the rebuild proceed on an empty walk — explicit override."""
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
        monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "audit.jsonl")
        mt.build_tree(fake_vault, tmp_db)
        empty_vault = tmp_path / "empty-vault"
        empty_vault.mkdir()
        mt.build_tree(empty_vault, tmp_db, rebuild=True, force=True)
        # Everything orphaned, nothing reinserted.
        active = tmp_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0]
        assert active == 0

    def test_rebuild_first_run_has_no_active_no_abort(
        self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch
    ):
        """Fresh DB (0 active rows) must allow rebuild — no prior data to protect."""
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
        monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "audit.jsonl")
        counts = mt.build_tree(fake_vault, tmp_db, rebuild=True)
        assert counts["nodes"] == 4

    def test_rebuild_emits_audit_line(
        self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch
    ):
        """Successful rebuild appends a structured audit entry."""
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
        monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "audit.jsonl")
        mt.build_tree(fake_vault, tmp_db)
        mt.build_tree(fake_vault, tmp_db, rebuild=True)
        audit = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert any('"action": "rebuild"' in line for line in audit)


# ── Retrieve ──────────────────────────────────────────────────────────────────

class TestRetrieve:
    def test_query_finds_best_leaf(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        result = mt.retrieve(tmp_db, "shani omer household roommates", k=3)
        assert result["results"]
        assert result["results"][0]["path"] == "Persona/life/household.md"

    def test_query_expands_via_see_also(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        # A cross-branch query that should pull both household + movies via see_also.
        result = mt.retrieve(
            tmp_db, "watch movies with roommates",
            k=5, low_threshold=0.0, abstain_threshold=0.0,
        )
        paths = [r["path"] for r in result["results"]]
        assert "Persona/life/household.md" in paths
        assert "Persona/taste/movies.md" in paths

    def test_query_abstains_on_low_confidence(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        # Force a high abstain threshold — nothing clears it.
        result = mt.retrieve(tmp_db, "random unrelated query xyzzy", abstain_threshold=0.99)
        assert result["fell_back"] is True
        assert result["results"] == []

    def test_query_logs_to_db_and_file(self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "queries.jsonl")
        mt.build_tree(fake_vault, tmp_db)
        mt.retrieve(tmp_db, "household", k=1)
        db_rows = tmp_db.execute("SELECT query FROM queries_log").fetchall()
        assert len(db_rows) == 1
        assert (tmp_path / "queries.jsonl").exists()


# ── Reembed ───────────────────────────────────────────────────────────────────

class TestReembed:
    def test_unchanged_description_is_noop(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        status = mt.reembed_file(fake_vault, "Persona/life/household.md", tmp_db)
        assert status == "unchanged"

    def test_updated_description_triggers_reembed(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        p = fake_vault / "Persona" / "life" / "household.md"
        content = p.read_text()
        p.write_text(content.replace("Shani and Omer", "Shani and Bob"))
        status = mt.reembed_file(fake_vault, "Persona/life/household.md", tmp_db)
        assert status == "reembedded"

    def test_path_traversal_blocked(self, tmp_db, fake_vault, stub_embed, tmp_path, monkeypatch):
        mt.build_tree(fake_vault, tmp_db)
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        monkeypatch.setenv(mt.EXTERNAL_DIR_ENV, str(ext_dir))
        secret = tmp_path / "secret.md"
        secret.write_text("---\ndescription: leaked\n---\nsensitive data")
        status = mt.reembed_file(fake_vault, "auto-memory/../secret.md", tmp_db)
        assert status in ("missing", "not_in_tree")


# ── Check ─────────────────────────────────────────────────────────────────────

class TestCheck:
    def test_clean_tree_reports_ok(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        report = mt.check_tree(tmp_db, fake_vault)
        assert report["ok"] is True
        assert report["nodes_active"] == 4

    def test_missing_description_flagged(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        p = fake_vault / "Persona" / "life" / "household.md"
        p.write_text(
            "---\nid: household000000000000000000000003\ndescription: \n---\n"
        )
        report = mt.check_tree(tmp_db, fake_vault)
        assert report["ok"] is False
        assert any("missing description" in str(i) for i in report["issues"])

    def test_detects_cycle(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        # Manually add a cycle in child edges: movies → household → movies
        mt.upsert_edge(
            tmp_db, src="movies000000000000000000000000004",
            dst="household000000000000000000000003", kind="child",
        )
        mt.upsert_edge(
            tmp_db, src="household000000000000000000000003",
            dst="movies000000000000000000000000004", kind="child",
        )
        report = mt.check_tree(tmp_db, fake_vault)
        assert any("cycle" in str(i) for i in report["issues"])

    def test_token_budget_enforced(self, tmp_db, fake_vault, stub_embed, monkeypatch):
        monkeypatch.setattr(mt, "ROOT_TOKEN_BUDGET", 5)
        mt.build_tree(fake_vault, tmp_db)
        report = mt.check_tree(tmp_db, fake_vault)
        assert any("budget" in str(i) for i in report["issues"])


# ── Graph view ────────────────────────────────────────────────────────────────

class TestGraph:
    def test_emits_valid_dot(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        dot = mt.render_graph(tmp_db)
        assert dot.startswith("digraph memory_tree {")
        assert dot.endswith("}")
        assert "Persona/life/household.md" not in dot  # labels use title, not path
        assert "Household" in dot

    def test_highlight_dims_other_nodes(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        dot = mt.render_graph(tmp_db, highlight="Persona/life/household.md")
        assert "#d81b60" in dot  # focus color
        assert "#dddddd" in dot  # dim color on at least one edge


# ── Benchmark ─────────────────────────────────────────────────────────────────

class TestBenchmark:
    def test_benchmark_computes_metrics(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        dataset = [
            {
                "query": "shani omer household",
                "expected_path": "Persona/life/household.md",
                "tag": "single",
            },
            {
                "query": "watch movies with roommates",
                "expected_paths": [
                    "Persona/life/household.md",
                    "Persona/taste/movies.md",
                ],
                "tag": "cross-branch",
            },
            {
                "query": "something totally unrelated xyzzy",
                "expected_path": "Persona/life/household.md",
                "tag": "abstain",
                "abstain": True,
            },
        ]
        report = mt.benchmark(tmp_db, dataset, k=3)
        assert report["n"] == 3
        assert 0.0 <= report["recall_at_k"] <= 1.0
        assert report["latency_p50_ms"] >= 0


# ── External namespace / reindex-external tests ─────────────────────────────


class TestIsExternalNamespace:
    def test_auto_memory_prefix(self):
        assert mt.is_external_namespace("auto-memory/feedback_data_integrity.md")

    def test_vault_path(self):
        assert not mt.is_external_namespace("Persona/INDEX.md")

    def test_root(self):
        assert not mt.is_external_namespace("MEMORY_TREE.md")

    def test_empty(self):
        assert not mt.is_external_namespace("")


class TestWriteIdToFrontmatter:
    def test_injects_id(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("---\nname: Test\ndescription: Desc\ntype: feedback\n---\nBody\n")
        mt._write_id_to_frontmatter(p, "test_id_123")
        content = p.read_text()
        assert "id: test_id_123" in content
        assert content.startswith("---\n")
        assert "Body" in content

    def test_idempotent_skip(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("---\nname: Test\nid: existing_id\ndescription: Desc\n---\n")
        mt._write_id_to_frontmatter(p, "new_id")
        content = p.read_text()
        assert "id: existing_id" in content
        assert "id: new_id" not in content

    def test_no_frontmatter(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("No frontmatter here\n")
        mt._write_id_to_frontmatter(p, "test_id")
        assert p.read_text() == "No frontmatter here\n"


class TestReindexExternal:
    @pytest.fixture
    def ext_dir(self, tmp_path):
        d = tmp_path / "auto_memory"
        d.mkdir()
        (d / "feedback_test.md").write_text(
            "---\nname: Test rule\ndescription: A test rule for verification\ntype: feedback\n---\nBody\n"
        )
        (d / "project_test.md").write_text(
            "---\nname: Test project\ndescription: A test project entry\ntype: project\n---\nBody\n"
        )
        (d / "no_desc.md").write_text(
            "---\nname: No description\ntype: feedback\n---\nBody\n"
        )
        (d / "MEMORY.md").write_text("# Index\nShould be skipped\n")
        arc = d / "ARCHIVE"
        arc.mkdir()
        (arc / "old.md").write_text(
            "---\nname: Archived\ndescription: Old\ntype: feedback\n---\n"
        )
        return d

    def test_indexes_files(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external(tmp_db, ext_dir)
        assert counts["indexed"] == 2  # feedback_test + project_test
        assert counts["skipped"] == 1  # no_desc
        assert counts["id_written"] == 2

    def test_skips_archive_and_memory_md(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external(tmp_db, ext_dir)
        paths = [
            r[0] for r in tmp_db.execute(
                "SELECT path FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        ]
        assert not any("ARCHIVE" in p for p in paths)
        assert not any("MEMORY.md" in p for p in paths)

    def test_namespace_prefix(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
        paths = [
            r[0] for r in tmp_db.execute(
                "SELECT path FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        ]
        assert all(p.startswith("auto-memory/") for p in paths)

    def test_idempotent_rerun(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
            counts2 = mt.reindex_external(tmp_db, ext_dir)
        assert counts2["id_written"] == 0

    def test_orphans_deleted_files(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
        (ext_dir / "feedback_test.md").unlink()
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external(tmp_db, ext_dir)
        assert counts["orphaned"] == 1

    def test_reads_name_for_title(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
        row = tmp_db.execute(
            "SELECT title FROM nodes WHERE path = 'auto-memory/feedback_test.md' AND orphaned_at IS NULL"
        ).fetchone()
        assert row is not None
        assert row[0] == "Test rule"

    def test_missing_dir_raises(self, tmp_db, tmp_path):
        with pytest.raises(FileNotFoundError):
            mt.reindex_external(tmp_db, tmp_path / "nonexistent")


class TestBuildTreeExternalProtection:
    """Verify build_tree operations don't orphan external-namespace nodes."""

    @pytest.fixture
    def populated_db(self, tmp_db, fake_vault):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.build_tree(fake_vault, tmp_db)
        # Manually insert an external node.
        mt.upsert_node(
            tmp_db,
            node_id="ext_test_001",
            path="auto-memory/feedback_test.md",
            title="Test",
            description="test description",
            level=0,
            node_type="feedback",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="hash123",
        )
        tmp_db.commit()
        return tmp_db

    def test_build_doesnt_orphan_external(self, populated_db, fake_vault):
        before = populated_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE path = 'auto-memory/feedback_test.md' AND orphaned_at IS NULL"
        ).fetchone()[0]
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.build_tree(fake_vault, populated_db)
        after = populated_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE path = 'auto-memory/feedback_test.md' AND orphaned_at IS NULL"
        ).fetchone()[0]
        assert before == after == 1

    def test_rebuild_doesnt_orphan_external(self, populated_db, fake_vault):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.build_tree(fake_vault, populated_db, rebuild=True)
        after = populated_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE path = 'auto-memory/feedback_test.md' AND orphaned_at IS NULL"
        ).fetchone()[0]
        assert after == 1

    def test_autofix_doesnt_orphan_external(self, populated_db, fake_vault):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.autofix_tree(populated_db, fake_vault)
        after = populated_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE path = 'auto-memory/feedback_test.md' AND orphaned_at IS NULL"
        ).fetchone()[0]
        assert after == 1


class TestCheckTreeExternalExclusion:
    def test_external_nodes_not_unreachable(self, tmp_db, fake_vault):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.build_tree(fake_vault, tmp_db)
        mt.upsert_node(
            tmp_db,
            node_id="ext_check_001",
            path="auto-memory/feedback_check.md",
            title="Check test",
            description="test",
            level=0,
            node_type="feedback",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="hash456",
        )
        tmp_db.commit()
        report = mt.check_tree(tmp_db, fake_vault)
        assert report["ok"] is True
        for issue in report["issues"]:
            assert "auto-memory" not in issue


class TestGenerateManifest:
    def test_output_format(self, tmp_db, fake_vault):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.build_tree(fake_vault, tmp_db)
        manifest = mt.generate_manifest(tmp_db)
        assert manifest.startswith("# Memory Manifest")
        assert "Available Knowledge" in manifest
        assert "memory_tree.py query" in manifest

    def test_groups_by_type(self, tmp_db):
        mt.upsert_node(tmp_db, node_id="m1", path="auto-memory/a.md", title="Rule A",
                        description="desc", level=0, node_type="feedback",
                        embedding=None, content_hash_val="h1")
        mt.upsert_node(tmp_db, node_id="m2", path="auto-memory/b.md", title="Project B",
                        description="desc", level=0, node_type="project",
                        embedding=None, content_hash_val="h2")
        tmp_db.commit()
        manifest = mt.generate_manifest(tmp_db)
        assert "Behavioral rules" in manifest
        assert "Project state" in manifest


class TestParseFrontmatterName:
    def test_parses_name_field(self):
        fm = mt.parse_frontmatter("---\nname: Test Name\ndescription: Desc\n---\n")
        assert fm.get("name") == "Test Name"

    def test_name_absent(self):
        fm = mt.parse_frontmatter("---\ndescription: Desc\n---\n")
        assert "name" not in fm


class TestEmbeddingSource:
    def test_appends_body(self):
        content = "---\ndescription: Short desc\n---\nThis is the body with more detail."
        result = mt.embedding_source("Short desc", content)
        assert result.startswith("Short desc")
        assert "body with more detail" in result

    def test_no_body(self):
        content = "---\ndescription: Short desc\n---\n"
        result = mt.embedding_source("Short desc", content)
        assert result == "Short desc"

    def test_truncates_long_body(self):
        body = " ".join(f"word{i}" for i in range(500))
        content = f"---\ndescription: Desc\n---\n{body}"
        result = mt.embedding_source("Desc", content)
        words_after_dash = result.split(" — ", 1)[1].split()
        assert len(words_after_dash) == mt.EMBED_BODY_WORDS


class TestScoreGapAbstain:
    """Verify score-gap abstain catches flat distributions."""

    @pytest.fixture
    def flat_db(self, tmp_db):
        """DB with nodes that all have nearly identical embeddings (flat scores)."""
        base_vec = [0.5] * mt.EMBED_DIM
        for i in range(5):
            vec = list(base_vec)
            vec[i] = 0.501 + i * 0.001
            mt.upsert_node(
                tmp_db,
                node_id=f"flat_{i:03d}",
                path=f"auto-memory/flat_{i}.md",
                title=f"Flat node {i}",
                description=f"Generic description {i}",
                level=0,
                node_type="feedback",
                embedding=vec,
                content_hash_val=f"hash_flat_{i}",
            )
        tmp_db.commit()
        return tmp_db

    def test_flat_distribution_abstains(self, flat_db):
        # Query vector orthogonal to the flat cluster — produces low, flat scores.
        query_vec = [0.0] * mt.EMBED_DIM
        query_vec[mt.EMBED_DIM - 1] = 1.0
        result = mt.retrieve(
            flat_db, "anything", k=5,
            query_vec=query_vec,
            abstain_threshold=0.01,
            low_threshold=0.55,
        )
        assert result["fell_back"] is True
        gap_traces = [t for t in result["trace"] if "abstain" in t]
        assert len(gap_traces) > 0

    def test_spike_distribution_passes(self, tmp_db):
        # Spike aligned with query; noise vectors orthogonal.
        spike_vec = [0.0] * mt.EMBED_DIM
        spike_vec[0] = 1.0
        query_vec = list(spike_vec)
        mt.upsert_node(
            tmp_db, node_id="spike_001", path="auto-memory/spike.md",
            title="Spike", description="Very relevant",
            level=0, node_type="feedback",
            embedding=spike_vec, content_hash_val="spike_h",
        )
        for i in range(3):
            noise = [0.0] * mt.EMBED_DIM
            noise[i + 1] = 1.0  # orthogonal to query
            mt.upsert_node(
                tmp_db, node_id=f"noise_{i:03d}", path=f"auto-memory/noise_{i}.md",
                title=f"Noise {i}", description=f"Irrelevant {i}",
                level=0, node_type="feedback",
                embedding=noise, content_hash_val=f"noise_h_{i}",
            )
        tmp_db.commit()
        result = mt.retrieve(
            tmp_db, "relevant", k=5,
            query_vec=query_vec,
            abstain_threshold=0.30,
            low_threshold=0.55,
        )
        assert result["fell_back"] is False
        assert result["results"][0]["path"] == "auto-memory/spike.md"


# ── FTS5 helpers ─────────────────────────────────────────────────────────────

class TestFTSHelpers:
    def test_fts_escape_strips_operators_and_joins_or(self):
        result = mt._fts_escape('hello AND "world" NOT (foo)')
        assert result == "hello OR world OR foo"

    def test_fts_escape_strips_punctuation(self):
        result = mt._fts_escape("phone/ID goes on my Hebrew?")
        assert "/" not in result
        assert "?" not in result

    def test_fts_escape_removes_stop_words(self):
        assert mt._fts_escape("What is my name?") == "name"
        assert mt._fts_escape("Where am I located?") == "located"

    def test_fts_escape_empty(self):
        assert mt._fts_escape("") == ""

    def test_fts_escape_short_tokens_filtered(self):
        assert mt._fts_escape("a b cd ef") == "cd OR ef"

    def test_body_from_content_strips_frontmatter(self):
        content = "---\nid: abc\ndescription: test\n---\nBody text here."
        assert mt._body_from_content(content) == "Body text here."

    def test_body_from_content_no_frontmatter(self):
        assert mt._body_from_content("Just plain text") == "Just plain text"

    def test_fts_available(self, tmp_db):
        assert mt._fts_available(tmp_db) is True

    def test_fts_upsert_and_delete(self, tmp_db):
        nid = "fts_test_001"
        mt._fts_upsert(tmp_db, nid, "My Title", "desc", "body text with keywords")
        rowid = mt._rowid_for(nid)
        row = tmp_db.execute("SELECT title FROM nodes_fts WHERE rowid = ?", (rowid,)).fetchone()
        assert row is not None
        assert row[0] == "My Title"
        mt._fts_delete(tmp_db, nid)
        row = tmp_db.execute("SELECT title FROM nodes_fts WHERE rowid = ?", (rowid,)).fetchone()
        assert row is None


class TestRRFFuse:
    def test_fuse_combines_rankings(self):
        vec = [("a", 1), ("b", 2), ("c", 3)]
        fts = [("b", 1), ("d", 2), ("a", 3)]
        fused = mt._rrf_fuse(vec, fts, k_rrf=60, top=5)
        assert "a" in fused[:2]
        assert "b" in fused[:2]

    def test_fuse_single_list(self):
        vec = [("a", 1), ("b", 2)]
        fts = []
        fused = mt._rrf_fuse(vec, fts, k_rrf=60, top=5)
        assert fused == ["a", "b"]


class TestFTSQuery:
    def test_query_matches_body_keyword(self, tmp_db):
        nid = "fts_q_001"
        mt.upsert_node(
            tmp_db, node_id=nid, path="auto-memory/profile.md",
            title="User Profile", description="Identity facts",
            level=0, node_type="user",
            embedding=[0.0] * mt.EMBED_DIM, content_hash_val="h1",
            body_text="Name: Liam. Location: Israel. Phone: 972527391393",
        )
        tmp_db.commit()
        rowid_map = {mt._rowid_for(nid): nid}
        results = mt._fts_query(tmp_db, "Liam name", k=5, _rowid_to_id=rowid_map)
        assert len(results) >= 1
        assert results[0][0] == nid

    def test_query_no_match_returns_empty(self, tmp_db):
        results = mt._fts_query(tmp_db, "xyznonexistent", k=5, _rowid_to_id={})
        assert results == []


class TestHybridRetrieve:
    """Acceptance tests for FTS5 hybrid retrieval."""

    def test_identity_query_via_fts(self, tmp_db, stub_embed):
        """The motivating use case: 'what is my name?' matches body keyword."""
        mt.upsert_node(
            tmp_db, node_id="id_001", path="auto-memory/user_profile.md",
            title="User Profile", description="Generic identity information",
            level=0, node_type="user",
            embedding=stub_embed("Generic identity information"),
            content_hash_val="h_id",
            body_text="Name: Liam. Location: Israel. Phone: 0527391393.",
        )
        mt.upsert_node(
            tmp_db, node_id="id_002", path="auto-memory/feedback_style.md",
            title="Style Prefs", description="Response style and communication preferences",
            level=0, node_type="feedback",
            embedding=stub_embed("Response style and communication preferences"),
            content_hash_val="h_style",
            body_text="Prefers simplicity over features. Concise. Direct.",
        )
        tmp_db.commit()
        result_hybrid = mt.retrieve(
            tmp_db, "what is my name", k=5,
            use_abstain=False, use_fts=True,
        )
        result_vector = mt.retrieve(
            tmp_db, "what is my name", k=5,
            use_abstain=False, use_fts=False,
        )
        hybrid_paths = [r["path"] for r in result_hybrid["results"]]
        assert "auto-memory/user_profile.md" in hybrid_paths[:2], \
            f"Hybrid should rank user_profile in top 2, got {hybrid_paths}"
        assert any("fts" in t for t in result_hybrid["trace"]), \
            f"Should have FTS trace entry, got {result_hybrid['trace']}"

    def test_use_fts_false_skips_fts(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        result = mt.retrieve(
            tmp_db, "anything", k=5,
            use_abstain=False, use_fts=False,
        )
        assert any("fts_off" in t for t in result["trace"])

    def test_fts_promotes_keyword_match(self, tmp_db, stub_embed):
        """FTS5 BM25 promotes results with keyword matches in body text."""
        mt.upsert_node(
            tmp_db, node_id="rrf_001", path="auto-memory/a.md",
            title="Node A", description="Machine learning deep networks",
            level=0, node_type="feedback",
            embedding=stub_embed("Machine learning deep networks"),
            content_hash_val="h_a",
            body_text="This document is about trading stocks and IBKR.",
        )
        mt.upsert_node(
            tmp_db, node_id="rrf_002", path="auto-memory/b.md",
            title="Node B", description="Other unrelated topic",
            level=0, node_type="feedback",
            embedding=stub_embed("Other unrelated topic"),
            content_hash_val="h_b",
            body_text="Trading diary for IBKR interactive brokers stocks.",
        )
        tmp_db.commit()
        result_hybrid = mt.retrieve(
            tmp_db, "trading IBKR stocks", k=5,
            use_abstain=False, use_fts=True,
        )
        result_vector = mt.retrieve(
            tmp_db, "trading IBKR stocks", k=5,
            use_abstain=False, use_fts=False,
        )
        h_paths = [r["path"] for r in result_hybrid["results"]]
        v_paths = [r["path"] for r in result_vector["results"]]
        assert len(h_paths) >= 2
        assert any("rrf" in r["route"] for r in result_hybrid["results"]), \
            "At least one result should be FTS-promoted"
