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


@pytest.fixture
def stub_embed_batch(monkeypatch, stub_embed):
    """Stub embed_batch_text using the same StubEmbed instance."""
    def _batch(texts):
        return [stub_embed(t) for t in texts]
    monkeypatch.setattr(mt, "embed_batch_text", _batch)
    return stub_embed


@pytest.fixture
def stub_approach_gen(monkeypatch):
    """Deterministic approach-angle generator."""
    def _gen(description, body_excerpt):
        words = description.split()
        return [
            f"What about {words[0]}?" if words else "What about this?",
            f"Tell me regarding {words[-1]}" if words else "Tell me about this",
            f"How does {' '.join(words[:2])} work?" if len(words) >= 2 else "How does this work?",
        ]
    monkeypatch.setattr(mt, "generate_approach_angles", _gen)
    return _gen


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

class TestTreeAutomationEnabled:
    """LIA-243 follow-up: the freshness automation gates on the DB existing, with
    DEUS_MEMORY_TREE=0 as an explicit opt-out (DB_PATH is redirected to a tmp file
    by the autouse conftest; DEUS_MEMORY_TREE is cleared there)."""

    def test_off_when_no_db(self):
        assert not mt.DB_PATH.exists()
        assert mt.tree_automation_enabled() is False

    def test_on_when_db_present(self):
        mt.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        mt.DB_PATH.touch()
        assert mt.tree_automation_enabled() is True

    def test_off_on_explicit_opt_out_even_with_db(self, monkeypatch):
        mt.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        mt.DB_PATH.touch()
        monkeypatch.setenv("DEUS_MEMORY_TREE", "0")
        assert mt.tree_automation_enabled() is False

    def test_on_when_flag_non_zero_and_db_present(self, monkeypatch):
        # Any non-"0" value (incl. legacy "1") with a DB present → enabled.
        mt.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        mt.DB_PATH.touch()
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        assert mt.tree_automation_enabled() is True


class TestDb:
    def test_open_db_enables_wal_and_busy_timeout(self, tmp_db):
        """open_db must set WAL + busy_timeout so hook reads and indexer writes
        don't race on a single rollback-journal lock (LIA-242). Read form."""
        assert tmp_db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert tmp_db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000

    def test_open_db_default_path_enables_wal(self):
        """The production call form open_db() (db_path=None → DB_PATH) must also
        get WAL + busy_timeout. DB_PATH is redirected to a temp file by the
        autouse isolate_memory_tree_paths conftest fixture."""
        db = mt.open_db()
        try:
            assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        finally:
            db.close()

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

    def test_transient_walk_miss_does_not_orphan(
        self, tmp_db, fake_vault, stub_embed, monkeypatch
    ):
        """LIA-336: a file present on disk but missing from one walk (concurrent
        rewrite) must NOT be orphaned — _confirm_orphan re-reads it and aborts."""
        mt.build_tree(fake_vault, tmp_db)
        monkeypatch.setattr(mt, "ORPHAN_CONFIRM_DELAY_S", 0)
        movies = fake_vault / "Persona" / "taste" / "movies.md"
        assert movies.exists()  # still on disk — only the walk "missed" it

        real_iter = mt.iter_tree_files

        def _iter_minus_movies(vault):
            return [p for p in real_iter(vault) if p != movies]

        monkeypatch.setattr(mt, "iter_tree_files", _iter_minus_movies)
        counts = mt.build_tree(fake_vault, tmp_db)

        assert counts["orphaned"] == 0
        active = tmp_db.execute(
            "SELECT 1 FROM nodes WHERE path = ? AND orphaned_at IS NULL",
            ("Persona/taste/movies.md",),
        ).fetchone()
        assert active is not None  # node still live, not soft-deleted

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


# ── Confirm-orphan guard (LIA-336) ──────────────────────────────────────────────

class TestConfirmOrphan:
    """Frozen oracle for _confirm_orphan: confirm-before-orphan guard against
    false-orphaning a live file mid-rewrite. Returns True => safe to orphan."""

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(mt, "ORPHAN_CONFIRM_DELAY_S", 0)

    def test_present_with_id_not_orphaned(self, tmp_path):  # row a
        f = tmp_path / "n.md"
        f.write_text("---\nid: abc\ndescription: d\n---\n", encoding="utf-8")
        assert mt._confirm_orphan(f, require_id=True) is False

    def test_absent_orphaned_require_id(self, tmp_path):  # row b
        assert mt._confirm_orphan(tmp_path / "gone.md", require_id=True) is True

    def test_persistent_no_id_orphaned(self, tmp_path):  # row c — real de-list
        f = tmp_path / "n.md"
        f.write_text("plain text, no frontmatter id\n", encoding="utf-8")
        assert mt._confirm_orphan(f, require_id=True) is True

    def test_transient_truncation_not_orphaned(self):  # row c' — the bug case
        calls = {"n": 0}

        class _Flaky:
            def read_text(self, *a, **k):
                calls["n"] += 1
                # First read sees a truncated (mid-rewrite) file; retry sees id.
                return "" if calls["n"] == 1 else "---\nid: abc\n---\n"

        assert mt._confirm_orphan(_Flaky(), require_id=True) is False
        assert calls["n"] >= 2  # proves it retried rather than orphaning on miss 1

    def test_present_not_orphaned_existence(self, tmp_path):  # row d
        f = tmp_path / "n.md"
        f.write_text("x", encoding="utf-8")
        assert mt._confirm_orphan(f, require_id=False) is False

    def test_absent_orphaned_existence(self, tmp_path):  # row e
        assert mt._confirm_orphan(tmp_path / "gone.md", require_id=False) is True

    def test_large_frontmatter_present_not_orphaned(self, tmp_path):  # LIA-340
        # A frontmatter block whose closing `---` sits past a 4096-byte head:
        # a truncated read leaves the fence unterminated, parse_frontmatter
        # reports no id, and the node was falsely orphaned on every build
        # (this is exactly how the vault CLAUDE.md got orphaned).
        f = tmp_path / "big.md"
        padding = "x" * 5000  # pushes the closing fence well past byte 4096
        f.write_text(
            f"---\nid: bigfm\ndescription: d\npadding: {padding}\n---\nbody\n",
            encoding="utf-8",
        )
        fm_block = f.read_text(encoding="utf-8").split("---", 2)[1]
        assert len(fm_block.encode("utf-8")) > 4096  # guard the fixture's premise
        assert mt._confirm_orphan(f, require_id=True) is False


class TestFrontmatterId:
    """LIA-340: id extraction must read the whole frontmatter, not a 4096 head."""

    def test_large_frontmatter_id_found(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text(
            f"---\nid: bigfm\ndescription: d\npadding: {'x' * 5000}\n---\nbody\n",
            encoding="utf-8",
        )
        assert mt._frontmatter_id(f) == "bigfm"

    def test_no_id_returns_none(self, tmp_path):
        f = tmp_path / "n.md"
        f.write_text("plain text, no frontmatter\n", encoding="utf-8")
        assert mt._frontmatter_id(f) is None

    def test_iter_tree_files_admits_large_frontmatter(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "MEMORY_TREE.md").write_text("---\nid: root\n---\n", encoding="utf-8")
        big = vault / "big.md"
        big.write_text(
            f"---\nid: bigfm\ndescription: d\npadding: {'x' * 5000}\n---\nbody\n",
            encoding="utf-8",
        )
        assert big in mt.iter_tree_files(vault)


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

    @pytest.mark.skipif(mt.sqlite_vec is None, reason="sqlite_vec not available")
    def test_reembeds_when_embedding_row_missing(self, tmp_db, ext_dir):
        """Nodes with a matching content_hash but no embedding row must be re-embedded."""
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)

        # Manually delete all embedding rows to simulate a failed embed run.
        node_ids = [
            r[0] for r in tmp_db.execute(
                "SELECT id FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        ]
        for nid in node_ids:
            rowid = mt._rowid_for(nid)
            tmp_db.execute("DELETE FROM embeddings WHERE rowid = ?", (rowid,))
        tmp_db.commit()

        # Second run: content_hash unchanged, but embedding row is gone.
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM) as mock_embed:
            counts = mt.reindex_external(tmp_db, ext_dir)

        assert counts["embedded"] > 0, "Expected re-embedding when embedding rows were deleted"

    def test_orphans_deleted_files(self, tmp_db, ext_dir):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
        (ext_dir / "feedback_test.md").unlink()
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external(tmp_db, ext_dir)
        assert counts["orphaned"] == 1

    def test_reindex_external_one_adds_without_orphaning(self, tmp_db, ext_dir, tmp_path):
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)
        before = {
            r[0] for r in tmp_db.execute(
                "SELECT path FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        }
        assert len(before) == 2  # feedback_test + project_test

        (ext_dir / "procedure_new.md").write_text(
            "---\nname: New proc\ndescription: A newly captured procedure\ntype: procedure\n---\nSteps\n"
        )
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external_one(tmp_db, ext_dir, "procedure_new.md")
        assert counts["indexed"] == 1
        after = {
            r[0] for r in tmp_db.execute(
                "SELECT path FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        }
        assert before <= after and len(after) == 3  # originals untouched

        # Persistence: a FRESH connection sees the committed node (proves the
        # explicit db.commit() — a single-connection assert would pass without it).
        db2 = mt.open_db(tmp_path / "tree.db")
        try:
            active = {
                r[0] for r in db2.execute(
                    "SELECT path FROM nodes WHERE orphaned_at IS NULL"
                ).fetchall()
            }
        finally:
            db2.close()
        assert "auto-memory/procedure_new.md" in active
        assert len(active) == 3

    def test_reindex_external_one_rejects_escape(self, tmp_db, ext_dir, tmp_path):
        # A ../ path that escapes external_dir is rejected (resolved containment),
        # even though the target file exists.
        (tmp_path / "outside.md").write_text(
            "---\nname: x\ndescription: y\ntype: feedback\n---\n"
        )
        with pytest.raises(ValueError):
            mt.reindex_external_one(tmp_db, ext_dir, "../outside.md")
        assert tmp_db.execute(
            "SELECT count(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0] == 0

    def test_reindex_external_one_missing_file(self, tmp_db, ext_dir):
        with pytest.raises(FileNotFoundError):
            mt.reindex_external_one(tmp_db, ext_dir, "nonexistent.md")

    def test_cli_add_dispatch(self, tmp_path, ext_dir, monkeypatch):
        # Exercises the `reindex-external --add` CLI branch end to end: resolver
        # via DEUS_AUTO_MEMORY_DIR, SUCCESS exit + --json, and the USAGE_ERROR path.
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "cli.db")
        monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(ext_dir))
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            rc = mt.main(["reindex-external", "--add", "feedback_test.md", "--json"])
        assert rc == mt.SUCCESS
        # A ../ escape returns USAGE_ERROR, not a crash.
        rc_escape = mt.main(["reindex-external", "--add", "../escape.md"])
        assert rc_escape == mt.USAGE_ERROR

    def test_transient_rglob_miss_does_not_orphan(self, tmp_db, ext_dir, monkeypatch):
        """LIA-336: a present file that rglob misses during one sweep (a rename
        gap) must NOT be orphaned — _confirm_orphan re-checks it on disk. Mirrors
        test_orphans_deleted_files but the file stays on disk."""
        import pathlib

        monkeypatch.setattr(mt, "ORPHAN_CONFIRM_DELAY_S", 0)
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            mt.reindex_external(tmp_db, ext_dir)

        target = ext_dir / "feedback_test.md"
        assert target.exists()  # still on disk; only the walk "misses" it

        real_rglob = pathlib.Path.rglob

        def _rglob_minus_target(self, pattern):
            return (p for p in real_rglob(self, pattern) if p.name != "feedback_test.md")

        monkeypatch.setattr(pathlib.Path, "rglob", _rglob_minus_target)
        with patch.object(mt, "embed_text", return_value=[0.1] * mt.EMBED_DIM):
            counts = mt.reindex_external(tmp_db, ext_dir)

        assert counts["orphaned"] == 0
        row = tmp_db.execute(
            "SELECT 1 FROM nodes WHERE path = ? AND orphaned_at IS NULL",
            ("auto-memory/feedback_test.md",),
        ).fetchone()
        assert row is not None  # live node preserved, not soft-deleted

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


# ── Coherence gate ──────────────────────────────────────────────────────────

class TestCoherenceGate:
    """Verify coherence rescue signals prevent false gap-abstains."""

    @pytest.fixture
    def coherent_db(self, tmp_db):
        """Two nodes with shared entities + near-identical embeddings (small gap)."""
        base = [0.5] * mt.EMBED_DIM
        vec_a = list(base); vec_a[0] = 0.502
        vec_b = list(base); vec_b[0] = 0.501
        mt.upsert_node(
            tmp_db, node_id="coh_a", path="auto-memory/python_django.md",
            title="Python Django framework", description="Python Django web framework setup",
            level=0, node_type="feedback", embedding=vec_a, content_hash_val="coh_a_h",
        )
        mt.upsert_node(
            tmp_db, node_id="coh_b", path="auto-memory/python_flask.md",
            title="Python Flask framework", description="Python Flask web framework setup",
            level=0, node_type="feedback", embedding=vec_b, content_hash_val="coh_b_h",
        )
        mt._upsert_entities(tmp_db, "coh_a", "Python Django framework", "Python Django web framework setup")
        mt._upsert_entities(tmp_db, "coh_b", "Python Flask framework", "Python Flask web framework setup")
        tmp_db.commit()
        return tmp_db

    def test_coherence_entities_rescue(self, coherent_db):
        """Top-2 share entities (python, framework) → rescued from gap abstain."""
        query_vec = [0.5] * mt.EMBED_DIM
        query_vec[0] = 0.503
        result = mt.retrieve(
            coherent_db, "python web", k=5, query_vec=query_vec,
            abstain_threshold=0.01, low_threshold=0.55,
            gap_threshold=0.99, use_coherence_gate=True, min_entity_overlap=2,
            use_fts=False,
        )
        assert result["fell_back"] is False
        trace = " ".join(result["trace"])
        assert "coherence:entities=" in trace
        assert "rescued" in trace

    def test_coherence_allows_genuine_abstain(self, tmp_db):
        """Two unrelated nodes (no shared entities, no edges) → genuine abstain."""
        base = [0.5] * mt.EMBED_DIM
        vec_a = list(base); vec_a[0] = 0.502
        vec_b = list(base); vec_b[0] = 0.501
        mt.upsert_node(
            tmp_db, node_id="inc_a", path="auto-memory/cooking_recipes.md",
            title="Cooking recipes collection", description="Italian cooking recipes and techniques",
            level=0, node_type="feedback", embedding=vec_a, content_hash_val="inc_a_h",
        )
        mt.upsert_node(
            tmp_db, node_id="inc_b", path="auto-memory/git_workflow.md",
            title="Git branching workflow", description="Git branching strategy and merge policies",
            level=0, node_type="feedback", embedding=vec_b, content_hash_val="inc_b_h",
        )
        mt._upsert_entities(tmp_db, "inc_a", "Cooking recipes collection", "Italian cooking recipes and techniques")
        mt._upsert_entities(tmp_db, "inc_b", "Git branching workflow", "Git branching strategy and merge policies")
        tmp_db.commit()
        query_vec = [0.5] * mt.EMBED_DIM
        query_vec[0] = 0.503
        result = mt.retrieve(
            tmp_db, "random unrelated", k=5, query_vec=query_vec,
            abstain_threshold=0.01, low_threshold=0.55,
            gap_threshold=0.99, use_coherence_gate=True, min_entity_overlap=2,
        )
        assert result["fell_back"] is True
        trace = " ".join(result["trace"])
        assert "coherence:none" in trace

    def test_coherence_edge_rescue(self, tmp_db):
        """Two nodes linked by edge but no entity overlap → rescued."""
        base = [0.5] * mt.EMBED_DIM
        vec_a = list(base); vec_a[0] = 0.502
        vec_b = list(base); vec_b[0] = 0.501
        mt.upsert_node(
            tmp_db, node_id="edge_a", path="auto-memory/edge_a.md",
            title="Xylophone percussion", description="Xylophone percussion instruments wooden",
            level=0, node_type="feedback", embedding=vec_a, content_hash_val="edge_a_h",
        )
        mt.upsert_node(
            tmp_db, node_id="edge_b", path="auto-memory/edge_b.md",
            title="Astronomy telescope", description="Astronomy telescope observation celestial",
            level=0, node_type="feedback", embedding=vec_b, content_hash_val="edge_b_h",
        )
        mt._upsert_entities(tmp_db, "edge_a", "Xylophone percussion", "Xylophone percussion instruments wooden")
        mt._upsert_entities(tmp_db, "edge_b", "Astronomy telescope", "Astronomy telescope observation celestial")
        mt.upsert_edge(tmp_db, src="edge_a", dst="edge_b", kind="see_also")
        tmp_db.commit()
        query_vec = [0.5] * mt.EMBED_DIM
        query_vec[0] = 0.503
        result = mt.retrieve(
            tmp_db, "edge test", k=5, query_vec=query_vec,
            abstain_threshold=0.01, low_threshold=0.55,
            gap_threshold=0.99, use_coherence_gate=True, min_entity_overlap=2,
        )
        assert result["fell_back"] is False
        trace = " ".join(result["trace"])
        assert "coherence:edge" in trace

    def test_rrf_agreement_rescue(self, tmp_db):
        """BM25 and vector agree on top-1 → rescued via RRF agreement."""
        spike_vec = [0.0] * mt.EMBED_DIM
        spike_vec[0] = 1.0
        noise_vec = [0.0] * mt.EMBED_DIM
        noise_vec[1] = 1.0
        mt.upsert_node(
            tmp_db, node_id="rrf_spike", path="auto-memory/deus_models.md",
            title="Deus models", description="Models and providers used by deus system",
            level=0, node_type="feedback", embedding=spike_vec, content_hash_val="rrf_spike_h",
            body_text="Models and providers used by deus system",
        )
        mt.upsert_node(
            tmp_db, node_id="rrf_noise", path="auto-memory/deus_config.md",
            title="Deus config", description="Configuration and settings for deus",
            level=0, node_type="feedback", embedding=noise_vec, content_hash_val="rrf_noise_h",
            body_text="Configuration and settings for deus",
        )
        mt._upsert_entities(tmp_db, "rrf_spike", "Deus models", "Models and providers used by deus system")
        mt._upsert_entities(tmp_db, "rrf_noise", "Deus config", "Configuration and settings for deus")
        tmp_db.commit()
        query_vec = list(spike_vec)
        query_vec[0] = 0.99
        query_vec[1] = 0.98
        result = mt.retrieve(
            tmp_db, "deus models", k=5, query_vec=query_vec,
            abstain_threshold=0.01, low_threshold=0.55,
            gap_threshold=0.99, use_coherence_gate=False, use_fts=True,
        )
        assert result["fell_back"] is False
        trace = " ".join(result["trace"])
        assert "rrf_agree:keep" in trace


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


class TestConceptExpansion:
    """Tests for concept-expanded FTS5 retrieval (Phase F)."""

    def test_concepts_expand_fts_query(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="c_001", path="auto-memory/simd_research.md",
            title="SIMD Research", description="Generic unrelated description",
            level=0, node_type="research",
            embedding=stub_embed("Generic unrelated description"),
            content_hash_val="h_c1",
            body_text="SIMD data layout optimization for vector search speedup.",
        )
        mt.upsert_node(
            tmp_db, node_id="c_002", path="auto-memory/cooking.md",
            title="Cooking", description="Another unrelated description",
            level=0, node_type="feedback",
            embedding=stub_embed("Another unrelated description"),
            content_hash_val="h_c2",
            body_text="Recipes for pasta and pizza.",
        )
        tmp_db.commit()

        result_no_concepts = mt.retrieve(
            tmp_db, "what about the data layout", k=5,
            use_abstain=False, use_fts=True, concepts=None,
        )
        result_with_concepts = mt.retrieve(
            tmp_db, "what about the data layout", k=5,
            use_abstain=False, use_fts=True, concepts=["SIMD", "vector", "optimization"],
        )

        with_paths = [r["path"] for r in result_with_concepts["results"]]
        assert "auto-memory/simd_research.md" in with_paths[:2], \
            f"Concepts should boost SIMD research, got {with_paths}"

    def test_concepts_in_trace(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="t_001", path="auto-memory/t.md",
            title="Test", description="Test node",
            level=0, node_type="feedback",
            embedding=stub_embed("Test node"),
            content_hash_val="h_t",
        )
        tmp_db.commit()
        result = mt.retrieve(
            tmp_db, "test", k=3,
            use_abstain=False, use_fts=True,
            concepts=["alpha", "beta"],
        )
        assert any("concepts=2" in t for t in result["trace"]), \
            f"Trace should contain concepts count, got {result['trace']}"

    def test_concepts_none_is_baseline(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="b_001", path="auto-memory/base.md",
            title="Base", description="Baseline test node",
            level=0, node_type="feedback",
            embedding=stub_embed("Baseline test node"),
            content_hash_val="h_b",
        )
        tmp_db.commit()
        r1 = mt.retrieve(
            tmp_db, "baseline test", k=3,
            use_abstain=False, use_fts=True, concepts=None,
        )
        r2 = mt.retrieve(
            tmp_db, "baseline test", k=3,
            use_abstain=False, use_fts=True,
        )
        assert r1["results"] == r2["results"]
        assert r1["confidence"] == r2["confidence"]

    def test_embed_text_receives_original_prompt_only(self, tmp_db, monkeypatch):
        calls = []
        original_embed = mt.embed_text

        def spy(text):
            calls.append(text)
            return original_embed(text)

        monkeypatch.setattr(mt, "embed_text", spy)

        mt.upsert_node(
            tmp_db, node_id="e_001", path="auto-memory/e.md",
            title="Embed", description="Embed test",
            level=0, node_type="feedback",
            embedding=original_embed("Embed test"),
            content_hash_val="h_e",
        )
        tmp_db.commit()

        prompt = "original query only"
        mt.retrieve(
            tmp_db, prompt, k=3,
            use_abstain=False, use_fts=True,
            concepts=["extra", "concept", "terms"],
        )
        assert len(calls) == 1
        assert calls[0] == prompt, \
            f"embed_text should receive original prompt, got {calls[0]}"


class TestApproachAngles:
    """Tests for approach-angle embedding retrieval (Phase 1.5)."""

    def _insert_with_angles(self, db, stub_embed, node_id, path, desc, chash, angle_queries):
        """Helper: insert a node and its approach-angle embeddings."""
        mt.upsert_node(
            db, node_id=node_id, path=path,
            title=path, description=desc,
            level=0, node_type="feedback",
            embedding=stub_embed(desc),
            content_hash_val=chash,
        )
        now = mt._utc_iso()
        for idx, q in enumerate(angle_queries):
            vec = stub_embed(q)
            db.execute(
                """INSERT OR REPLACE INTO approach_angles
                   (node_id, angle_idx, query_text, embedding, source_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (node_id, idx, q, mt.serialize(vec), chash, now),
            )
        db.commit()

    def test_approach_off_is_noop(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="aa_001", path="auto-memory/aa.md",
            title="AA", description="Test approach node",
            level=0, node_type="feedback",
            embedding=stub_embed("Test approach node"),
            content_hash_val="h_aa",
        )
        tmp_db.commit()
        result = mt.retrieve(
            tmp_db, "anything", k=5,
            use_abstain=False, use_approach_angles=False,
        )
        assert not any("approach_max" in t for t in result["trace"]), \
            f"Should not have approach trace when off, got {result['trace']}"

    def test_approach_empty_is_noop(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="ae_001", path="auto-memory/ae.md",
            title="AE", description="Test empty approach",
            level=0, node_type="feedback",
            embedding=stub_embed("Test empty approach"),
            content_hash_val="h_ae",
        )
        tmp_db.commit()
        result = mt.retrieve(
            tmp_db, "anything", k=5,
            use_abstain=False, use_approach_angles=True,
        )
        assert any("approach_empty" in t for t in result["trace"]), \
            f"Should have approach_empty trace, got {result['trace']}"

    def test_approach_boost_score(self, tmp_db, stub_embed):
        self._insert_with_angles(
            tmp_db, stub_embed,
            node_id="ab_001", path="Persona/food.md",
            desc="Roommate dietary preferences vegetarian Mediterranean",
            chash="h_food",
            angle_queries=[
                "What should I order for dinner tonight",
                "Does my roommate have food restrictions",
                "What restaurants work for everyone at home",
            ],
        )
        mt.upsert_node(
            tmp_db, node_id="ab_002", path="auto-memory/unrelated.md",
            title="Unrelated", description="Git workflow and CI pipeline rules",
            level=0, node_type="feedback",
            embedding=stub_embed("Git workflow and CI pipeline rules"),
            content_hash_val="h_unrel",
        )
        tmp_db.commit()

        result = mt.retrieve(
            tmp_db, "What should I order for dinner tonight", k=5,
            use_abstain=False, use_approach_angles=True, use_fts=False,
        )
        paths = [r["path"] for r in result["results"]]
        assert paths[0] == "Persona/food.md", \
            f"Approach-angle boosted node should rank first, got {paths}"
        assert result["results"][0]["route"] == "approach"

    def test_approach_max_not_avg(self, tmp_db, stub_embed):
        self._insert_with_angles(
            tmp_db, stub_embed,
            node_id="am_001", path="auto-memory/max.md",
            desc="Completely unrelated description for max test",
            chash="h_max",
            angle_queries=[
                "Exact match query for testing max",
                "Weak unrelated angle query one",
                "Weak unrelated angle query two",
            ],
        )
        tmp_db.commit()

        result = mt.retrieve(
            tmp_db, "Exact match query for testing max", k=5,
            use_abstain=False, use_approach_angles=True, use_fts=False,
        )
        top = result["results"][0]
        assert top["path"] == "auto-memory/max.md"
        assert top["score"] > 0.5, "Max approach score should be high (exact match)"

    def test_approach_rescues_from_abstain(self, tmp_db, stub_embed):
        self._insert_with_angles(
            tmp_db, stub_embed,
            node_id="ar_001", path="Persona/rescue.md",
            desc="Very specific obscure description unlikely to match",
            chash="h_rescue",
            angle_queries=[
                "Rescue query that exactly matches user prompt",
            ],
        )
        tmp_db.commit()

        result_no_angles = mt.retrieve(
            tmp_db, "Rescue query that exactly matches user prompt", k=5,
            use_abstain=True, use_approach_angles=False, use_fts=False,
        )
        result_with_angles = mt.retrieve(
            tmp_db, "Rescue query that exactly matches user prompt", k=5,
            use_abstain=True, use_approach_angles=True, use_fts=False,
        )
        assert result_no_angles["fell_back"] or len(result_no_angles["results"]) == 0 or \
            result_no_angles["confidence"] < result_with_angles["confidence"], \
            "Approach angles should improve confidence vs content-only"
        if result_with_angles["results"]:
            assert result_with_angles["results"][0]["path"] == "Persona/rescue.md"

    def test_backfill_skips_existing(self, tmp_db, stub_embed_batch, stub_approach_gen, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "test.md").write_text(
            "---\nid: bf_001\ntitle: Test\ndescription: Backfill test node\nlevel: 1\n---\nBody content."
        )
        mt.upsert_node(
            tmp_db, node_id="bf_001", path="test.md",
            title="Test", description="Backfill test node",
            level=1, node_type="feedback",
            embedding=stub_embed_batch("Backfill test node"),
            content_hash_val="h_bf",
        )
        tmp_db.commit()

        counts1 = mt.backfill_approach_angles(tmp_db, vault)
        assert counts1["generated"] == 1
        assert counts1["skipped"] == 0

        counts2 = mt.backfill_approach_angles(tmp_db, vault)
        assert counts2["generated"] == 0
        assert counts2["skipped"] == 1


class TestCoverageGate:
    """Tests for approach-angle coverage abstain gate."""

    def _insert_with_angles(self, db, stub_embed, node_id, path, desc, chash, angle_queries):
        mt.upsert_node(
            db, node_id=node_id, path=path,
            title=path, description=desc,
            level=0, node_type="feedback",
            embedding=stub_embed(desc),
            content_hash_val=chash,
        )
        now = mt._utc_iso()
        for idx, q in enumerate(angle_queries):
            vec = stub_embed(q)
            db.execute(
                """INSERT OR REPLACE INTO approach_angles
                   (node_id, angle_idx, query_text, embedding, source_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (node_id, idx, q, mt.serialize(vec), chash, now),
            )
        db.commit()

    def test_coverage_gate_forces_abstain(self, tmp_db, stub_embed):
        self._insert_with_angles(
            tmp_db, stub_embed,
            node_id="cg_001", path="Persona/household.md",
            desc="lives with two roommates cooking pasta",
            chash="h_cg",
            angle_queries=["who are my flatmates", "roommate preferences"],
        )

        result = mt.retrieve(
            tmp_db, "what car do I drive", k=5,
            use_abstain=True, use_approach_angles=True, use_fts=False,
        )
        has_coverage = any("abstain:coverage" in t for t in result["trace"])
        assert has_coverage, f"Expected coverage abstain, got trace: {result['trace']}"
        assert result["fell_back"]

    def test_coverage_gate_no_false_abstain(self, tmp_db, stub_embed):
        self._insert_with_angles(
            tmp_db, stub_embed,
            node_id="cn_001", path="Persona/household.md",
            desc="lives with two roommates cooking pasta",
            chash="h_cn",
            angle_queries=["who are my roommates", "people I live with"],
        )

        result = mt.retrieve(
            tmp_db, "who are my roommates", k=5,
            use_abstain=True, use_approach_angles=True, use_fts=False,
        )
        has_coverage = any("abstain:coverage" in t for t in result["trace"])
        assert not has_coverage, f"Should not coverage-abstain when angle matches, got trace: {result['trace']}"

    def test_coverage_gate_off_when_no_angles(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="co_001", path="Persona/test.md",
            title="Test", description="some test node description",
            level=0, node_type="feedback",
            embedding=stub_embed("some test node description"),
            content_hash_val="h_co",
        )
        tmp_db.commit()

        result = mt.retrieve(
            tmp_db, "what car do I drive", k=5,
            use_abstain=True, use_approach_angles=True, use_fts=False,
        )
        has_coverage = any("abstain:coverage" in t for t in result["trace"])
        assert not has_coverage, f"Coverage gate should not fire when no angles exist, got trace: {result['trace']}"

    def test_extract_entities_basic(self):
        ents = mt.extract_entities("university student enrolled in math physics program")
        assert "student" in ents
        assert "university" in ents
        assert "math" in ents

    def test_backfill_entities(self, tmp_db, stub_embed):
        mt._upsert_entities(tmp_db, "be_001", "Test Entity", "university math student enrolled")
        tmp_db.commit()
        rows = tmp_db.execute(
            "SELECT entity FROM node_entities WHERE node_id = 'be_001'"
        ).fetchall()
        entities = {r[0] for r in rows}
        assert "math" in entities
        assert "student" in entities


class TestFTSAngleInjection:
    """Tests for FTS5 angle text injection."""

    def test_fts_includes_angle_text(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="fi_001", path="Persona/food.md",
            title="Food Prefs", description="Dietary preferences vegetarian",
            level=0, node_type="persona-node",
            embedding=stub_embed("Dietary preferences vegetarian"),
            content_hash_val="h_fi",
        )
        now = mt._utc_iso()
        for idx, q in enumerate(["dinner ideas", "restaurant picks", "meal planning"]):
            tmp_db.execute(
                """INSERT OR REPLACE INTO approach_angles
                   (node_id, angle_idx, query_text, embedding, source_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("fi_001", idx, q, mt.serialize(stub_embed(q)), "h_fi", now),
            )
        tmp_db.commit()

        mt.upsert_node(
            tmp_db, node_id="fi_001", path="Persona/food.md",
            title="Food Prefs", description="Dietary preferences vegetarian",
            level=0, node_type="persona-node",
            embedding=stub_embed("Dietary preferences vegetarian"),
            content_hash_val="h_fi",
        )
        tmp_db.commit()

        fts_row = tmp_db.execute(
            "SELECT body FROM nodes_fts WHERE rowid = ?",
            (mt._rowid_for("fi_001"),),
        ).fetchone()
        assert fts_row is not None
        assert "dinner" in fts_row[0], f"Angle text should be in FTS body: {fts_row[0]}"
        assert "restaurant" in fts_row[0]


class TestConvexBlend:
    """Tests for convex combination fusion (alpha blend)."""

    def test_convex_blend_produces_moderate_score(self, tmp_db, stub_embed):
        TestApproachAngles()._insert_with_angles(
            tmp_db, stub_embed,
            node_id="cb_001", path="Persona/blend.md",
            desc="Completely unrelated description for blend test",
            chash="h_cb",
            angle_queries=[
                "Exact match query for testing blend",
            ],
        )
        tmp_db.commit()

        result_max = mt.retrieve(
            tmp_db, "Exact match query for testing blend", k=5,
            use_abstain=False, use_approach_angles=True, use_fts=False,
        )
        top = result_max["results"][0]
        assert top["path"] == "Persona/blend.md"
        content_score = mt.cosine(
            stub_embed("Completely unrelated description for blend test"),
            stub_embed("Exact match query for testing blend"),
        )
        approach_score = mt.cosine(
            stub_embed("Exact match query for testing blend"),
            stub_embed("Exact match query for testing blend"),
        )
        expected_blend = mt.APPROACH_ALPHA * content_score + (1 - mt.APPROACH_ALPHA) * approach_score
        assert abs(top["score"] - expected_blend) < 0.05, \
            f"Score {top['score']:.3f} should be near blend {expected_blend:.3f}"

    def test_convex_blend_never_penalizes(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db, node_id="cn_001", path="Persona/nopen.md",
            title="NoPen", description="High confidence exact match query",
            level=0, node_type="persona-node",
            embedding=stub_embed("High confidence exact match query"),
            content_hash_val="h_cn",
        )
        now = mt._utc_iso()
        tmp_db.execute(
            """INSERT OR REPLACE INTO approach_angles
               (node_id, angle_idx, query_text, embedding, source_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("cn_001", 0, "weak unrelated angle", mt.serialize(stub_embed("weak unrelated angle")), "h_cn", now),
        )
        tmp_db.commit()

        result_off = mt.retrieve(
            tmp_db, "High confidence exact match query", k=5,
            use_abstain=False, use_approach_angles=False, use_fts=False,
        )
        result_on = mt.retrieve(
            tmp_db, "High confidence exact match query", k=5,
            use_abstain=False, use_approach_angles=True, use_fts=False,
        )
        assert result_on["results"][0]["score"] >= result_off["results"][0]["score"], \
            f"Blend should never penalize: on={result_on['results'][0]['score']:.3f} off={result_off['results'][0]['score']:.3f}"


# ── Benchmark Tiered ────────────────────────────────────────────────────────


class TestBenchmarkTiered:
    """Tests for the tiered atom system benchmark function."""

    def _make_dataset(self):
        """Minimal dataset with one entry per role."""
        return [
            {
                "query": "commit these changes to the branch",
                "expected_path": "auto-memory/feedback_one_concern.md",
                "tag": "methodology",
                "tier1_should_cover": True,
            },
            {
                "query": "deploy to production",
                "expected_path": "auto-memory/feedback_security_audit.md",
                "tag": "methodology",
                "tier1_should_cover": True,
            },
            {
                "query": "shani omer household",
                "expected_path": "Persona/life/household.md",
                "expected_paths": ["Persona/life/household.md"],
                "tag": "knowledge",
                "tier1_should_cover": False,
            },
            {
                "query": "watch movies with roommates",
                "expected_path": "Persona/taste/movies.md",
                "expected_paths": ["Persona/taste/movies.md"],
                "tag": "knowledge",
                "tier1_should_cover": False,
            },
            {
                "query": "how to cook a chocolate souffle",
                "abstain": True,
                "tag": "abstain",
                "tier1_should_cover": False,
            },
        ]

    def test_benchmark_tiered_returns_expected_keys(self, tmp_db, fake_vault, stub_embed):
        mt.build_tree(fake_vault, tmp_db)
        dataset = self._make_dataset()
        standards = {
            "auto-memory/feedback_one_concern.md",
            "auto-memory/feedback_security_audit.md",
        }
        report = mt.benchmark_tiered(tmp_db, dataset, standards, k=3)

        expected_keys = {
            "tier1_coverage",
            "tier2_recall",
            "combined_recall",
            "tier1_token_cost",
            "tier2_token_cost_avg",
            "baseline_recall",
        }
        assert expected_keys <= set(report.keys()), \
            f"Missing keys: {expected_keys - set(report.keys())}"
        # Additional informational keys should be present too.
        assert "n" in report
        assert "n_methodology" in report
        assert "n_knowledge" in report
        assert "n_abstain" in report
        assert "abstain_accuracy" in report

    def test_tier1_coverage_with_standards(self, tmp_db, fake_vault, stub_embed):
        """tier1_coverage is 1.0 when all methodology paths are in standards_paths.

        This is a pure set-membership check — no DB rows needed for the
        methodology atoms themselves.
        """
        mt.build_tree(fake_vault, tmp_db)
        dataset = self._make_dataset()
        # All methodology expected_paths are in standards_paths.
        standards = {
            "auto-memory/feedback_one_concern.md",
            "auto-memory/feedback_security_audit.md",
        }
        report = mt.benchmark_tiered(tmp_db, dataset, standards, k=3)
        assert report["tier1_coverage"] == 1.0, \
            f"Expected tier1_coverage=1.0 when all methodology paths in standards, got {report['tier1_coverage']}"

    def test_tier1_coverage_partial(self, tmp_db, fake_vault, stub_embed):
        """tier1_coverage is 0.5 when only half of methodology paths are in standards."""
        mt.build_tree(fake_vault, tmp_db)
        dataset = self._make_dataset()
        # Only one of two methodology expected_paths is in standards.
        standards = {"auto-memory/feedback_one_concern.md"}
        report = mt.benchmark_tiered(tmp_db, dataset, standards, k=3)
        assert report["tier1_coverage"] == 0.5

    def test_empty_dataset_returns_error(self, tmp_db):
        report = mt.benchmark_tiered(tmp_db, [], set())
        assert "error" in report


# ── Atom Kind (tiered atom system) ───────────────────────────────────────────

class TestAtomKind:
    def test_upsert_node_stores_atom_kind(self, tmp_db):
        mt.upsert_node(
            tmp_db,
            node_id="ak1",
            path="atoms/standard-test.md",
            title="Standard Atom",
            description="A standard-tier atom for testing",
            level=0,
            node_type="atom",
            embedding=None,
            content_hash_val="h_ak1",
            atom_kind="standard",
        )
        row = tmp_db.execute(
            "SELECT atom_kind FROM nodes WHERE id = 'ak1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "standard"

    def test_retrieve_excludes_standard_atoms(self, tmp_db, stub_embed):
        mt.upsert_node(
            tmp_db,
            node_id="ek_std",
            path="atoms/std.md",
            title="Standard Node",
            description="standard tier atom about cooking preferences",
            level=0,
            node_type="atom",
            embedding=stub_embed("standard tier atom about cooking preferences"),
            content_hash_val="h_ek_std",
            atom_kind="standard",
        )
        mt.upsert_node(
            tmp_db,
            node_id="ek_know",
            path="atoms/know.md",
            title="Knowledge Node",
            description="knowledge tier atom about cooking preferences",
            level=0,
            node_type="atom",
            embedding=stub_embed("knowledge tier atom about cooking preferences"),
            content_hash_val="h_ek_know",
            atom_kind="knowledge",
        )
        result = mt.retrieve(
            tmp_db, "cooking preferences", k=10,
            use_abstain=False, use_fts=False, use_approach_angles=False,
            exclude_kinds={"standard"},
        )
        returned_ids = {r["id"] for r in result["results"]}
        assert "ek_know" in returned_ids
        assert "ek_std" not in returned_ids

    def test_retrieve_excludes_standard_atoms_through_see_also_expansion(
        self, tmp_db, stub_embed,
    ):
        """Graph expansion must honor `exclude_kinds`.

        Regression test for the M4-prereq #417 finding: a methodology-kind
        seed atom with a `see_also` edge to a `kind: standard` neighbor
        would silently re-inject the standard atom into top-K results when
        callers passed `exclude_kinds={"standard"}`. The cosine filter at
        line 1236 protected Phase 1, but Phase 2 graph expansion
        (memory_tree.py:1326-1369) was unfiltered.
        """
        # Methodology-kind seed atom — the cosine match target.
        mt.upsert_node(
            tmp_db,
            node_id="seek_meth",
            path="atoms/methodology.md",
            title="Methodology Seed",
            description="rules about workflow methodology for development tasks",
            level=0,
            node_type="atom",
            embedding=stub_embed("rules about workflow methodology for development tasks"),
            content_hash_val="h_seek_meth",
            atom_kind="knowledge",  # non-standard, eligible under default filter
        )
        # Standard-kind neighbor reachable via `see_also` — must NOT leak.
        mt.upsert_node(
            tmp_db,
            node_id="seek_std",
            path="atoms/standard.md",
            title="Standard Neighbor",
            description="unrelated cooking text that would not cosine-match the query",
            level=0,
            node_type="atom",
            embedding=stub_embed("unrelated cooking text that would not cosine-match the query"),
            content_hash_val="h_seek_std",
            atom_kind="standard",
        )
        mt.upsert_edge(tmp_db, src="seek_meth", dst="seek_std", kind="see_also")
        # `use_see_also=True` is the retrieve() default. Set explicitly here
        # so the regression intent is visible: this test fails without the
        # graph-expansion guard. `low_threshold=0.0` forces Phase 2 to run.
        result = mt.retrieve(
            tmp_db, "rules about workflow methodology",
            k=5,
            use_see_also=True,
            low_threshold=0.0,
            use_abstain=False,
            use_fts=False,
            use_approach_angles=False,
            exclude_kinds={"standard"},
        )
        returned_ids = {r["id"] for r in result["results"]}
        assert "seek_meth" in returned_ids, (
            "methodology seed should pass the cosine filter and surface"
        )
        assert "seek_std" not in returned_ids, (
            "kind: standard neighbor must not leak through see_also expansion "
            "when exclude_kinds={'standard'}"
        )

    def test_parse_frontmatter_maps_kind_to_atom_kind(self):
        fm = mt.parse_frontmatter(
            "---\nid: xyz\nkind: standard\ndescription: test\n---\n"
        )
        assert "atom_kind" in fm
        assert fm["atom_kind"] == "standard"
        assert "kind" not in fm


class TestSyncAtomKinds:
    """Tests for `sync_atom_kinds` — DB-to-frontmatter atom_kind reconciliation."""

    @staticmethod
    def _write_atom(
        ext_dir: Path, name: str, kind: str | None, *, description: str = "test"
    ) -> None:
        """Write an atom file with optional `kind:` in frontmatter."""
        kind_line = f"kind: {kind}\n" if kind else ""
        (ext_dir / name).write_text(
            f"---\n"
            f"id: id_{name.replace('.md', '')}\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"{kind_line}"
            f"type: feedback\n"
            f"---\n"
            f"body\n",
            encoding="utf-8",
        )

    def test_sync_atom_kinds_fixes_stale_db_value(self, tmp_db, tmp_path):
        ext = tmp_path / "auto-memory"
        ext.mkdir()
        self._write_atom(ext, "atom_a.md", "standard")
        # DB row reflects pre-promotion state.
        mt.upsert_node(
            tmp_db,
            node_id="id_atom_a",
            path=f"{mt.EXTERNAL_NAMESPACE}atom_a.md",
            title="atom_a",
            description="test",
            level=0,
            node_type="atom",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="h_a",
            atom_kind="knowledge",
        )
        result = mt.sync_atom_kinds(tmp_db, ext, apply=True)
        assert len(result["fixed"]) == 1
        assert result["fixed"][0] == ("atom_a.md", "knowledge", "standard")
        # DB now reflects the on-disk frontmatter.
        row = tmp_db.execute(
            "SELECT atom_kind FROM nodes WHERE path = ?",
            (f"{mt.EXTERNAL_NAMESPACE}atom_a.md",),
        ).fetchone()
        assert row[0] == "standard"

    def test_sync_atom_kinds_dry_run_does_not_write(self, tmp_db, tmp_path):
        ext = tmp_path / "auto-memory"
        ext.mkdir()
        self._write_atom(ext, "atom_b.md", "standard")
        mt.upsert_node(
            tmp_db,
            node_id="id_atom_b",
            path=f"{mt.EXTERNAL_NAMESPACE}atom_b.md",
            title="atom_b",
            description="test",
            level=0,
            node_type="atom",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="h_b",
            atom_kind="knowledge",
        )
        result = mt.sync_atom_kinds(tmp_db, ext, apply=False)
        # `fixed` still records the would-be change.
        assert len(result["fixed"]) == 1
        assert result["fixed"][0] == ("atom_b.md", "knowledge", "standard")
        # But the DB is unchanged.
        row = tmp_db.execute(
            "SELECT atom_kind FROM nodes WHERE path = ?",
            (f"{mt.EXTERNAL_NAMESPACE}atom_b.md",),
        ).fetchone()
        assert row[0] == "knowledge"

    def test_sync_atom_kinds_skips_already_synced(self, tmp_db, tmp_path):
        ext = tmp_path / "auto-memory"
        ext.mkdir()
        self._write_atom(ext, "atom_c.md", "knowledge")
        mt.upsert_node(
            tmp_db,
            node_id="id_atom_c",
            path=f"{mt.EXTERNAL_NAMESPACE}atom_c.md",
            title="atom_c",
            description="test",
            level=0,
            node_type="atom",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="h_c",
            atom_kind="knowledge",
        )
        result = mt.sync_atom_kinds(tmp_db, ext, apply=True)
        assert result["fixed"] == []
        assert result["unchanged"] == 1

    def test_sync_atom_kinds_skips_files_without_kind_field(self, tmp_db, tmp_path):
        ext = tmp_path / "auto-memory"
        ext.mkdir()
        self._write_atom(ext, "atom_d.md", None)  # no kind: line
        mt.upsert_node(
            tmp_db,
            node_id="id_atom_d",
            path=f"{mt.EXTERNAL_NAMESPACE}atom_d.md",
            title="atom_d",
            description="test",
            level=0,
            node_type="atom",
            embedding=[0.1] * mt.EMBED_DIM,
            content_hash_val="h_d",
            atom_kind="knowledge",
        )
        result = mt.sync_atom_kinds(tmp_db, ext, apply=True)
        assert "atom_d.md" in result["no_kind_in_file"]
        assert result["fixed"] == []
        # DB unchanged.
        row = tmp_db.execute(
            "SELECT atom_kind FROM nodes WHERE path = ?",
            (f"{mt.EXTERNAL_NAMESPACE}atom_d.md",),
        ).fetchone()
        assert row[0] == "knowledge"


class TestStandardsPack:
    """Tests for scripts/standards_pack.py."""

    def test_load_standards_empty_dir(self, tmp_path: Path):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        result = sp.load_standards(tmp_path)
        assert result == ""

    def test_load_standards_finds_standard_atoms(self, tmp_path: Path):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        (tmp_path / "atom1.md").write_text(
            "---\nname: Rule One\ndescription: Do thing one\nkind: standard\n---\nBody text.\n"
        )
        (tmp_path / "atom2.md").write_text(
            "---\nname: Rule Two\ndescription: Do thing two\nkind: knowledge\n---\nBody text.\n"
        )
        result = sp.load_standards(tmp_path, token_budget=800)
        assert "Rule One" in result
        assert "Rule Two" not in result
        assert "Working Standards" in result

    def test_load_standards_respects_token_budget(self, tmp_path: Path):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        for i in range(20):
            (tmp_path / f"atom{i:02d}.md").write_text(
                f"---\nname: Rule {i}\ndescription: {'word ' * 50}\nkind: standard\n---\n"
            )
        result = sp.load_standards(tmp_path, token_budget=100)
        assert result != ""
        lines = [l for l in result.split("\n") if l.startswith("- ")]
        assert len(lines) < 20

    def test_load_standards_warns_and_records_on_truncation(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """Fail-loud on budget overrun. Cache records dropped atoms + over-by tokens."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        # Isolated cache path so we don't clobber the user's real cache.
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        for i in range(20):
            (tmp_path / f"atom{i:02d}.md").write_text(
                f"---\nname: Rule {i}\ndescription: {'word ' * 50}\nkind: standard\n---\n"
            )
        sp.load_standards(tmp_path, token_budget=100)

        captured = capsys.readouterr()
        assert "budget exceeded" in captured.err
        assert "dropped" in captured.err.lower()
        assert "totalling" in captured.err

        cache = json.loads(cache_file.read_text())
        assert isinstance(cache.get("dropped"), list)
        assert len(cache["dropped"]) > 0
        assert cache["dropped_tokens"] > 0

    def test_load_standards_no_warn_when_under_budget(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """No stderr noise + empty `dropped` field when everything fits."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        (tmp_path / "atom1.md").write_text(
            "---\nname: Rule One\ndescription: short\nkind: standard\n---\n"
        )
        sp.load_standards(tmp_path, token_budget=10_000)

        captured = capsys.readouterr()
        assert "budget exceeded" not in captured.err

        cache = json.loads(cache_file.read_text())
        assert cache["dropped"] == []
        assert cache["dropped_tokens"] == 0

    def test_load_standards_cache_invalidates_on_content_edit(
        self, tmp_path: Path, monkeypatch
    ):
        """Content edit (no create/delete) must invalidate the cache.

        Regression test for the macOS/ext4 mtime bug surfaced post-PR #413:
        directory `st_mtime` does not update on content changes of existing
        files, so a cache key based on `_dir_mtime` returned stale results
        when atoms were edited in place (e.g., adding a `priority:` field
        via `sed -i` for M2).
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        atom = tmp_path / "atom1.md"
        atom.write_text(
            "---\nname: Rule One\ndescription: First version\nkind: standard\n---\n"
        )
        first = sp.load_standards(tmp_path, token_budget=10_000)
        assert "First version" in first

        # Edit the same file in place — no create/delete in the directory.
        atom.write_text(
            "---\nname: Rule One\ndescription: Second version\nkind: standard\n---\n"
        )
        second = sp.load_standards(tmp_path, token_budget=10_000)
        assert "Second version" in second
        assert "First version" not in second

    def test_priority_high_wins_over_alphabetical_under_budget_pressure(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """`priority: high` atom is packed first even when its filename sorts last.

        Under budget pressure that admits only one atom, the high-priority
        atom must win over the med-priority one regardless of alphabetical
        order. This verifies priority sort overrides filename sort within
        the budget pass.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        # a_med.md sorts first alphabetically; z_high.md sorts last.
        # With alphabetical-only ordering, a_med would win.
        # Each oneliner is roughly 12 tokens (~9 words × 1.3) so budget=15
        # admits exactly one and forces the other to drop.
        (tmp_path / "a_med.md").write_text(
            "---\nname: MedRule\ndescription: medium priority rule with extra words\n"
            "kind: standard\n---\n"
        )
        (tmp_path / "z_high.md").write_text(
            "---\nname: HighRule\ndescription: high priority rule with extra words\n"
            "kind: standard\npriority: high\n---\n"
        )

        result = sp.load_standards(tmp_path, token_budget=15)

        assert "HighRule" in result, "high-priority atom must be included"
        assert "MedRule" not in result, "med-priority atom must drop under budget"

        cache = json.loads(cache_file.read_text())
        # High atom went in; med atom got dropped. No high-priority loss.
        assert cache["dropped_high"] == [], (
            f"no high-priority drops expected, got {cache['dropped_high']}"
        )
        assert "MedRule" in cache["dropped"]

    def test_priority_high_overflow_critical_warn(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """CRITICAL stderr + dropped_high cache field when high atom exceeds budget alone.

        Inclusion of `priority: high` is NOT unconditionally guaranteed by
        design — see docs/decisions/standards-pack-priority.md. When a high
        atom can't fit, the loudness contract is: ordinary WARN (existing)
        PLUS additive CRITICAL message naming the lost atoms.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        # Two high-priority atoms. The first fits (~10 tokens), the second
        # exceeds the remaining budget — exercises the dropped-high path
        # while keeping `lines` non-empty so cache writes.
        (tmp_path / "a_high.md").write_text(
            "---\nname: SmallHigh\ndescription: small high priority rule\n"
            "kind: standard\npriority: high\n---\n"
        )
        (tmp_path / "z_huge_high.md").write_text(
            f"---\nname: HugeHigh\ndescription: {'word ' * 50}\n"
            "kind: standard\npriority: high\n---\n"
        )
        sp.load_standards(tmp_path, token_budget=15)

        captured = capsys.readouterr()
        assert "budget exceeded" in captured.err, "ordinary WARN must fire"
        assert "CRITICAL" in captured.err, "CRITICAL must fire on high drop"
        assert "non-negotiable rules lost" in captured.err
        assert "HugeHigh" in captured.err

        cache = json.loads(cache_file.read_text())
        assert cache["dropped_high"] == ["HugeHigh"]
        assert "HugeHigh" in cache["dropped"]
        # SmallHigh fit; only HugeHigh dropped.
        assert "SmallHigh" not in cache["dropped"]

    def test_priority_default_is_med(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """Atoms without `priority:` field behave as before (filename sort).

        Backward-compatibility test: all atoms in the wild lack the new field
        until the host-local tagging step runs. Without `priority:`, every
        atom defaults to med (rank 1) so `(rank, filename)` sort collapses
        to pure filename order — production parity with pre-M2 behavior.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import standards_pack as sp

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(sp, "CACHE_PATH", cache_file)

        # Three atoms, no priority field anywhere.
        (tmp_path / "atom_b.md").write_text(
            "---\nname: RuleB\ndescription: rule b\nkind: standard\n---\n"
        )
        (tmp_path / "atom_a.md").write_text(
            "---\nname: RuleA\ndescription: rule a\nkind: standard\n---\n"
        )
        (tmp_path / "atom_c.md").write_text(
            "---\nname: RuleC\ndescription: rule c\nkind: standard\n---\n"
        )
        result = sp.load_standards(tmp_path, token_budget=10_000)

        captured = capsys.readouterr()
        # No unknown-priority WARN should fire (the field is absent, not invalid).
        assert "unknown priority" not in captured.err

        # Order must be alphabetical (atom_a, atom_b, atom_c → RuleA, RuleB, RuleC).
        # Find the positions of each name in the result.
        pos_a = result.find("RuleA")
        pos_b = result.find("RuleB")
        pos_c = result.find("RuleC")
        assert pos_a >= 0 and pos_b >= 0 and pos_c >= 0
        assert pos_a < pos_b < pos_c, (
            f"expected alphabetical order; positions: A={pos_a} B={pos_b} C={pos_c}"
        )


# ── resolve_vault_path — env var → config.json → legacy fallback ───────────────


class TestResolveVaultPath:
    """Cover resolve_vault_path's three tiers; redirect only the config.json
    expanduser to a tmp file (pattern from test_memory_tree_phase3.py).
    Fallback assertions are structural to stay user-agnostic for the public repo.
    """

    @staticmethod
    def _redirect_config(monkeypatch, cfg_path):
        original_expanduser = Path.expanduser

        def fake_expand(self):
            if str(self) == "~/.config/deus/config.json":
                return cfg_path
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", fake_expand)

    @staticmethod
    def _write_config(tmp_path, body):
        cfg_dir = tmp_path / ".config" / "deus"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "config.json"
        cfg_path.write_text(body)
        return cfg_path

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        env_vault = tmp_path / "env_vault"
        monkeypatch.setenv("DEUS_VAULT_PATH", str(env_vault))
        # Config present but must be ignored when the env var is set.
        cfg_path = self._write_config(
            tmp_path, json.dumps({"vault_path": str(tmp_path / "cfg_vault")})
        )
        self._redirect_config(monkeypatch, cfg_path)
        assert mt.resolve_vault_path() == Path(str(env_vault))

    def test_config_json_used_when_env_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_vault = tmp_path / "cfg_vault"
        cfg_path = self._write_config(
            tmp_path, json.dumps({"vault_path": str(cfg_vault)})
        )
        self._redirect_config(monkeypatch, cfg_path)
        assert mt.resolve_vault_path() == Path(str(cfg_vault))

    def test_missing_config_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        # Point at a config path that does not exist (file never written).
        cfg_path = tmp_path / ".config" / "deus" / "config.json"
        self._redirect_config(monkeypatch, cfg_path)
        result = mt.resolve_vault_path()
        assert isinstance(result, Path)
        assert str(result).endswith("Deus")  # legacy fallback (user-agnostic)

    def test_malformed_config_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_path = self._write_config(tmp_path, "{not valid json")
        self._redirect_config(monkeypatch, cfg_path)
        result = mt.resolve_vault_path()  # must not raise
        assert isinstance(result, Path)
        assert result != cfg_path
        assert str(result).endswith("Deus")

    def test_empty_vault_path_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_path = self._write_config(tmp_path, json.dumps({"vault_path": ""}))
        self._redirect_config(monkeypatch, cfg_path)
        result = mt.resolve_vault_path()
        assert isinstance(result, Path)
        assert str(result).endswith("Deus")

    def test_non_string_vault_path_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_path = self._write_config(tmp_path, json.dumps({"vault_path": 42}))
        self._redirect_config(monkeypatch, cfg_path)
        result = mt.resolve_vault_path()
        assert isinstance(result, Path)
        assert str(result).endswith("Deus")
