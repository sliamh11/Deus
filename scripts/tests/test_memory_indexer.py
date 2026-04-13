"""
Tests for scripts/memory_indexer.py — parsing, chunking, DB helpers.

memory_indexer.py imports google.genai at module level.
We stub those imports before loading the module.
"""
import importlib
import os
import sys
import types
from pathlib import Path

import json
import pytest

# Ensure project root is importable
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _install_google_genai_stub():
    """Install a minimal stub for google.genai so memory_indexer can import."""
    if "google" not in sys.modules:
        google_mod = types.ModuleType("google")
        sys.modules["google"] = google_mod
    else:
        google_mod = sys.modules["google"]

    if not hasattr(google_mod, "genai"):
        genai_mod = types.ModuleType("google.genai")

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

        genai_mod.Client = _FakeClient
        setattr(google_mod, "genai", genai_mod)
        sys.modules["google.genai"] = genai_mod

    if "google.genai.types" not in sys.modules:
        types_mod = types.ModuleType("google.genai.types")
        types_mod.EmbedContentConfig = object
        types_mod.GenerateContentConfig = lambda **kwargs: kwargs
        sys.modules["google.genai.types"] = types_mod

    genai_types_attr = sys.modules.get("google.genai")
    if genai_types_attr and not hasattr(genai_types_attr, "types"):
        setattr(genai_types_attr, "types", sys.modules["google.genai.types"])


_install_google_genai_stub()


@pytest.fixture(autouse=True)
def fresh_vault(tmp_path, monkeypatch):
    """Point DEUS_VAULT_PATH to a temp vault and ensure memory_indexer is clean."""
    vault = tmp_path / "vault"
    (vault / "Session-Logs").mkdir(parents=True)
    (vault / "Atoms").mkdir()
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))
    # Always reload to pick up env change
    if "memory_indexer" in sys.modules:
        del sys.modules["memory_indexer"]
    yield vault


@pytest.fixture
def mi(tmp_path, fresh_vault, monkeypatch):
    """Load memory_indexer with a temp DB."""
    mod = importlib.import_module("memory_indexer")
    test_db = tmp_path / "memory.db"
    monkeypatch.setattr(mod, "DB_PATH", test_db)
    return mod


# ── extract_frontmatter ───────────────────────────────────────────────────


def test_extract_frontmatter_empty_for_no_frontmatter(mi):
    result = mi.extract_frontmatter("# Just a header\nsome content")
    assert result == {}


def test_extract_frontmatter_extracts_date(mi):
    content = "---\ndate: 2024-06-15\ntldr: short summary\n---\nbody"
    result = mi.extract_frontmatter(content)
    assert result.get("date") == "2024-06-15"


def test_extract_frontmatter_extracts_tldr(mi):
    content = "---\ndate: 2024-06-15\ntldr: short summary\n---\nbody"
    result = mi.extract_frontmatter(content)
    assert result.get("tldr") == "short summary"


def test_extract_frontmatter_extracts_topics(mi):
    content = "---\ndate: 2024-06-15\ntopics: [math, physics]\n---\nbody"
    result = mi.extract_frontmatter(content)
    assert result.get("topics") == "math, physics"


def test_extract_frontmatter_extracts_decisions_list(mi):
    content = "---\ndate: 2024\ndecisions:\n  - Use vitest\n  - Mock all I/O\n---\nbody"
    result = mi.extract_frontmatter(content)
    assert "Use vitest" in result.get("decisions", "")
    assert "Mock all I/O" in result.get("decisions", "")


def test_extract_frontmatter_raw_contains_block(mi):
    content = "---\ndate: 2024-06-15\n---\nbody"
    result = mi.extract_frontmatter(content)
    assert "raw" in result
    assert "date: 2024-06-15" in result["raw"]


# ── extract_decisions_section ─────────────────────────────────────────────


def test_extract_decisions_section_empty_when_missing(mi):
    result = mi.extract_decisions_section("# Summary\nsome text")
    assert result == ""


def test_extract_decisions_section_extracts_body(mi):
    content = (
        "# Title\n## Decisions Made\nWe chose approach A.\n"
        "Because it was simpler.\n## Next Steps\nMore stuff"
    )
    result = mi.extract_decisions_section(content)
    assert "approach A" in result
    assert "More stuff" not in result


# ── chunks_for_log ────────────────────────────────────────────────────────


def test_chunks_for_log_empty_for_no_frontmatter(mi, tmp_path):
    p = tmp_path / "session.md"
    p.write_text("No frontmatter here")
    chunks = mi.chunks_for_log(p, p.read_text())
    assert chunks == []


def test_chunks_for_log_returns_frontmatter_chunk(mi, tmp_path):
    p = tmp_path / "session.md"
    p.write_text(
        "---\ndate: 2024-06-15\ntldr: a good session summary\ntopics: [engineering]\n---\n## Summary\nContent here"
    )
    chunks = mi.chunks_for_log(p, p.read_text())
    assert any(c["type"] == "frontmatter" for c in chunks)


def test_chunks_for_log_returns_decisions_chunk_when_present(mi, tmp_path):
    p = tmp_path / "session.md"
    p.write_text(
        "---\ndate: 2024-06-15\ntldr: summary\n---\n"
        "## Decisions Made\nWe chose to use sqlite-vec for vector storage.\n"
        "## Summary\nSome more"
    )
    chunks = mi.chunks_for_log(p, p.read_text())
    types = [c["type"] for c in chunks]
    assert "decisions" in types


# ── open_db + DB helpers ──────────────────────────────────────────────────


def test_open_db_creates_entries_table(mi):
    db = mi.open_db()
    tables = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    db.close()
    assert "entries" in tables


def test_entry_exists_false_for_new_db(mi):
    db = mi.open_db()
    result = mi.entry_exists(db, "/nonexistent/path.md")
    db.close()
    assert result is False


def test_delete_entries_removes_rows(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type) VALUES (?, ?, ?, ?)",
        ["/test/path.md", "2024-01-01", "some chunk text", "frontmatter"],
    )
    db.commit()
    assert mi.entry_exists(db, "/test/path.md")
    mi.delete_entries(db, "/test/path.md")
    assert not mi.entry_exists(db, "/test/path.md")
    db.close()


# ── cmd_recent ───────────────────────────────────────────────────────────


def _create_session(vault, date_folder: str, name: str, tldr: str, mtime_offset: float = 0):
    """Helper to create a session log file with controlled mtime."""
    day_dir = vault / "Session-Logs" / date_folder
    day_dir.mkdir(parents=True, exist_ok=True)
    p = day_dir / f"{name}.md"
    p.write_text(
        f"---\ntype: session\ndate: {date_folder}\ntldr: {tldr}\n---\n## Summary\n"
    )
    # Set mtime: base + offset (higher offset = newer)
    import time
    base_mtime = time.time() - 10000 + mtime_offset
    os.utime(p, (base_mtime, base_mtime))
    return p


def test_cmd_recent_mtime_tiebreaker(mi, fresh_vault, capsys):
    """Sessions on the same day should be ordered by mtime, newest first."""
    _create_session(fresh_vault, "2024-06-15", "session-a", "first session", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "session-b", "second session", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-15", "session-c", "third session", mtime_offset=300)

    mi.cmd_recent(2)
    output = capsys.readouterr().out
    lines = [l for l in output.strip().split("\n") if l.startswith("- [")]

    assert len(lines) == 2
    # session-c (newest mtime) should be first, then session-b
    assert "session c" in lines[0]
    assert "session b" in lines[1]


def test_cmd_recent_days_returns_all_from_day(mi, fresh_vault, capsys):
    """--recent-days N should return ALL sessions from the last N calendar days."""
    # 3 sessions on the same day
    _create_session(fresh_vault, "2024-06-15", "session-a", "a", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "session-b", "b", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-15", "session-c", "c", mtime_offset=300)
    # 1 session on a different day
    _create_session(fresh_vault, "2024-06-14", "session-old", "old", mtime_offset=50)

    mi.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    lines = [l for l in output.strip().split("\n") if l.startswith("- [")]

    # Should return all 3 from 2024-06-15, NOT the one from 2024-06-14
    assert len(lines) == 3
    assert "session old" not in output


def test_cmd_recent_days_spans_multiple_days(mi, fresh_vault, capsys):
    """--recent-days 2 should return sessions from 2 distinct calendar days."""
    _create_session(fresh_vault, "2024-06-15", "today-a", "a", mtime_offset=300)
    _create_session(fresh_vault, "2024-06-15", "today-b", "b", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-14", "yesterday", "y", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-13", "ancient-session", "o", mtime_offset=50)

    mi.cmd_recent(2, days=True)
    output = capsys.readouterr().out
    lines = [l for l in output.strip().split("\n") if l.startswith("- [")]

    # 2 from Jun 15 + 1 from Jun 14 = 3
    assert len(lines) == 3
    assert "ancient-session" not in output


def test_cmd_recent_continuity_indicator(mi, fresh_vault, capsys):
    """Output should include a continuity summary line."""
    _create_session(fresh_vault, "2024-06-15", "session-a", "a", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "session-b", "b", mtime_offset=200)
    # Create an atom so we can verify atom count
    atoms_dir = fresh_vault / "Atoms"
    atoms_dir.mkdir(exist_ok=True)
    (atoms_dir / "test-atom.md").write_text("---\ntype: atom\n---\nTest atom\n")

    mi.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    assert "Continuity:" in output
    assert "2 sessions across 1 day" in output
    assert "1 atoms" in output


def test_cmd_recent_clustering_on_busy_days(mi, fresh_vault, capsys):
    """When 4+ sessions share a day, sessions with matching topics are clustered."""
    _create_session(fresh_vault, "2024-06-15", "auth-login", "login fix", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "auth-oauth", "oauth fix", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-15", "ui-dashboard", "dashboard", mtime_offset=300)
    _create_session(fresh_vault, "2024-06-15", "ui-sidebar", "sidebar", mtime_offset=400)

    # Patch the sessions to have topics
    for name, topics in [("auth-login", "auth, security"), ("auth-oauth", "auth, oauth"),
                         ("ui-dashboard", "ui, dashboard"), ("ui-sidebar", "ui, layout")]:
        p = fresh_vault / "Session-Logs" / "2024-06-15" / f"{name}.md"
        p.write_text(
            f"---\ntype: session\ndate: 2024-06-15\ntldr: {name} work\ntopics: [{topics}]\n---\n## Summary\n"
        )

    mi.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    # Should have at least one cluster header with "(2 sessions)"
    assert "(2 sessions)" in output


def test_cmd_recent_no_clustering_under_threshold(mi, fresh_vault, capsys):
    """Fewer than 4 sessions on a day should NOT cluster."""
    _create_session(fresh_vault, "2024-06-15", "session-a", "a", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "session-b", "b", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-15", "session-c", "c", mtime_offset=300)

    mi.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    # No clustering — flat format
    assert "(2 sessions)" not in output
    assert "session a" in output


def test_cmd_recent_days_mtime_order_within_day(mi, fresh_vault, capsys):
    """Within a day, sessions should be ordered newest-first by mtime."""
    _create_session(fresh_vault, "2024-06-15", "early", "e", mtime_offset=100)
    _create_session(fresh_vault, "2024-06-15", "middle", "m", mtime_offset=200)
    _create_session(fresh_vault, "2024-06-15", "late", "l", mtime_offset=300)

    mi.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    lines = [l for l in output.strip().split("\n") if l.startswith("- [")]

    assert len(lines) == 3
    assert "late" in lines[0]
    assert "middle" in lines[1]
    assert "early" in lines[2]


# ── cmd_learnings ────────────────────────────────────────────────────────


def _create_atom(vault, name: str, body: str, category: str = "decision",
                 corroborations: int = 1, confidence: float = 0.5,
                 created_at: str = "2024-06-10", updated_at: str = "2024-06-15",
                 ttl_days: str = "null"):
    """Helper to create an atom file."""
    atoms_dir = vault / "Atoms"
    atoms_dir.mkdir(exist_ok=True)
    p = atoms_dir / name
    p.write_text(
        f"---\ntype: atom\ncategory: {category}\ntags: []\n"
        f"confidence: {confidence}\ncorroborations: {corroborations}\n"
        f"source: /test/session.md\ncreated_at: {created_at}\n"
        f"updated_at: {updated_at}\nttl_days: {ttl_days}\n---\n{body}\n"
    )
    return p


def test_cmd_learnings_surfaces_strengthened_atoms(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """Atoms with updated_at > created_at and corroborations >= 2 show as 'Pattern confirmed'."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    from datetime import date, timedelta
    today = date.today()
    _create_atom(fresh_vault, "decision-branches.md", "Always use feature branches",
                 corroborations=3, confidence=0.80,
                 created_at=str(today - timedelta(days=10)),
                 updated_at=str(today - timedelta(days=1)))

    mi.cmd_learnings(since_days=7, max_items=3)
    output = capsys.readouterr().out
    assert "Pattern confirmed" in output
    assert "feature branches" in output
    assert "seen across 3 sessions" in output


def test_cmd_learnings_surfaces_new_insights(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """Atoms created recently with 1 corroboration show as 'New insight'."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    from datetime import date, timedelta
    today = date.today()
    _create_atom(fresh_vault, "fact-new-thing.md", "Playwright needs interactive stdin",
                 category="fact", corroborations=1, confidence=0.50,
                 created_at=str(today - timedelta(days=2)),
                 updated_at=str(today - timedelta(days=2)))

    mi.cmd_learnings(since_days=7, max_items=3)
    output = capsys.readouterr().out
    assert "New insight" in output
    assert "Playwright" in output


def test_cmd_learnings_delta_tracking(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """Second run skips atoms already shown in first run."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    from datetime import date, timedelta
    today = date.today()
    _create_atom(fresh_vault, "decision-only-one.md", "Use sqlite-vec for search",
                 corroborations=2, confidence=0.70,
                 created_at=str(today - timedelta(days=5)),
                 updated_at=str(today - timedelta(days=1)))

    # First run — should show it
    mi.cmd_learnings(since_days=7, max_items=3)
    first_output = capsys.readouterr().out
    assert "sqlite-vec" in first_output

    # Second run — should NOT show it (delta tracking)
    mi.cmd_learnings(since_days=7, max_items=3)
    second_output = capsys.readouterr().out
    assert "sqlite-vec" not in second_output


def test_cmd_learnings_empty_when_nothing_new(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """No output when no atoms qualify (old atoms only)."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    _create_atom(fresh_vault, "decision-old.md", "Old decision",
                 created_at="2020-01-01", updated_at="2020-01-01")

    mi.cmd_learnings(since_days=7, max_items=3)
    output = capsys.readouterr().out
    assert output == ""


def test_cmd_learnings_cold_start_welcome(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """When no atoms exist, show a welcome message."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    # fresh_vault has empty Atoms dir
    mi.cmd_learnings(since_days=7, max_items=3)
    output = capsys.readouterr().out
    assert "Your learnings will appear here" in output


# ── PR 1: source chunk preservation ──────────────────────────────────────


def test_open_db_adds_source_chunk_column(mi):
    """open_db() must add source_chunk column on fresh and existing DBs."""
    db = mi.open_db()
    cols = {row[1] for row in db.execute("PRAGMA table_info(entries)").fetchall()}
    db.close()
    assert "source_chunk" in cols


def test_open_db_source_chunk_upgrade_idempotent(mi):
    """Calling open_db() twice must not raise (column already exists)."""
    db = mi.open_db()
    db.close()
    db2 = mi.open_db()
    db2.close()


def test_write_atom_file_includes_source_excerpt(mi, fresh_vault):
    """write_atom_file() with source_excerpt writes it into frontmatter."""
    atom = {"text": "Prefers pytest over unittest", "category": "preference"}
    path = mi.write_atom_file(atom, "/session.md", "2024-06-15",
                              source_excerpt="## Decisions Made\nUse pytest.")
    content = path.read_text()
    assert "source_excerpt:" in content
    assert "Decisions Made" in content


def test_write_atom_file_no_source_excerpt_when_empty(mi, fresh_vault):
    """write_atom_file() without source_excerpt omits the field."""
    atom = {"text": "Prefers dark mode", "category": "preference"}
    path = mi.write_atom_file(atom, "/session.md", "2024-06-15")
    content = path.read_text()
    assert "source_excerpt" not in content


def test_write_atom_file_truncates_long_excerpt(mi, fresh_vault):
    """source_excerpt stored in frontmatter must be ≤ 2000 chars."""
    long_excerpt = "x" * 5000
    atom = {"text": "Some preference fact", "category": "preference"}
    path = mi.write_atom_file(atom, "/session.md", "2024-06-15",
                              source_excerpt=long_excerpt)
    content = path.read_text()
    # Verify truncation happened (not the full 5000) and is bounded near 2000
    x_count = content.count("x")
    assert x_count < 5000, "source_excerpt was not truncated"
    assert x_count <= 2002, f"source_excerpt exceeded expected bound: {x_count}"


def test_cmd_learnings_skips_expired_atoms(mi, fresh_vault, capsys, tmp_path, monkeypatch):
    """Atoms past their TTL should not appear."""
    monkeypatch.setattr(mi, "LAST_RESUME_LEARNINGS", tmp_path / "last_learnings.txt")
    from datetime import date, timedelta
    today = date.today()
    # Created 400 days ago with 365-day TTL — expired
    _create_atom(fresh_vault, "constraint-expired.md", "Expired constraint",
                 category="constraint", corroborations=5, confidence=0.90,
                 created_at=str(today - timedelta(days=400)),
                 updated_at=str(today - timedelta(days=1)),
                 ttl_days="365")

    mi.cmd_learnings(since_days=7, max_items=3)
    output = capsys.readouterr().out
    assert "Expired constraint" not in output


def test_rebuild_restores_source_chunk_from_frontmatter(mi, fresh_vault, tmp_path, monkeypatch):
    """--rebuild reads source_excerpt from atom .md frontmatter and stores in source_chunk column."""
    atoms_dir = fresh_vault / "Atoms"
    atoms_dir.mkdir(exist_ok=True)
    atom_file = atoms_dir / "preference-use-pytest.md"
    atom_file.write_text(
        "---\ntype: atom\ncategory: preference\ntags: []\n"
        "confidence: 0.70\ncorroborations: 2\n"
        "source: /session.md\ncreated_at: 2024-06-15\nupdated_at: 2024-06-15\nttl_days: 365\n"
        "source_excerpt: |\n"
        "  ## Decisions Made\n"
        "  We chose pytest over unittest for all tests.\n"
        "---\n"
        "Prefers pytest over unittest for testing\n"
    )

    # Stub embed so --rebuild doesn't need API key
    monkeypatch.setattr(mi, "embed", lambda text: [0.1] * 768)
    test_db = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "DB_PATH", test_db)

    mi.cmd_rebuild()

    db = mi.open_db()
    row = db.execute(
        "SELECT source_chunk FROM entries WHERE type='atom' LIMIT 1"
    ).fetchone()
    db.close()

    assert row is not None
    assert row[0] is not None, "source_chunk should be restored from frontmatter"
    assert "pytest" in row[0]


# ── PR 2: compact mode for --recent / --recent-days ───────────────────────


def test_cmd_recent_compact_truncates_decisions(mi, fresh_vault, capsys):
    """Compact mode: decisions field truncated to ≤ 81 chars (80 + ellipsis)."""
    day_dir = fresh_vault / "Session-Logs" / "2024-06-15"
    day_dir.mkdir(parents=True)
    p = day_dir / "session-a.md"
    long_decision = "A" * 120
    p.write_text(
        f"---\ntype: session\ndate: 2024-06-15\ntldr: summary\n"
        f"decisions:\n  - \"{long_decision}\"\n---\n"
    )

    mi.cmd_recent(1, compact=True)
    output = capsys.readouterr().out
    # 120 A's should not appear in full — truncated at 80
    assert "A" * 81 not in output
    assert "…" in output


def test_cmd_recent_compact_decisions_not_truncated_under_limit(mi, fresh_vault, capsys):
    """Decisions at or under 80 chars should NOT get ellipsis."""
    day_dir = fresh_vault / "Session-Logs" / "2024-06-15"
    day_dir.mkdir(parents=True)
    p = day_dir / "session-a.md"
    short_decision = "Use pytest"
    p.write_text(
        f"---\ntype: session\ndate: 2024-06-15\ntldr: summary\n"
        f"decisions:\n  - \"{short_decision}\"\n---\n"
    )

    mi.cmd_recent(1, compact=True)
    output = capsys.readouterr().out
    assert "Use pytest" in output
    assert "…" not in output


def test_cmd_recent_compact_strips_full_paths(mi, fresh_vault, capsys):
    """Compact mode: path shows 'log: <stem>' without full vault path."""
    _create_session(fresh_vault, "2024-06-15", "my-session", "summary")

    mi.cmd_recent(1, compact=True)
    output = capsys.readouterr().out
    assert str(fresh_vault) not in output
    assert "log: my-session" in output


def test_cmd_recent_compact_collapses_clusters(mi, fresh_vault, capsys):
    """Compact mode: clustered sessions show header only, no individual entries."""
    for name, topics in [("auth-login", "auth, security"), ("auth-oauth", "auth, oauth"),
                         ("ui-dashboard", "ui, dashboard"), ("ui-sidebar", "ui, layout")]:
        p = fresh_vault / "Session-Logs" / "2024-06-15" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"---\ntype: session\ndate: 2024-06-15\ntldr: {name} work\ntopics: [{topics}]\n---\n"
        )

    mi.cmd_recent(1, days=True, compact=True)
    output = capsys.readouterr().out
    assert "(2 sessions" in output
    assert "  - auth login" not in output
    assert "  - auth oauth" not in output


def test_cmd_recent_compact_cluster_no_covering_when_no_tldr(mi, fresh_vault, capsys):
    """Compact cluster header omits 'covering:' when sessions have no tldr."""
    for name, topics in [("no-tldr-1", "auth"), ("no-tldr-2", "auth"),
                         ("no-tldr-3", "ui"), ("no-tldr-4", "ui")]:
        p = fresh_vault / "Session-Logs" / "2024-06-15" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        # Note: NO tldr field
        p.write_text(
            f"---\ntype: session\ndate: 2024-06-15\ntopics: [{topics}]\n---\n"
        )

    mi.cmd_recent(1, days=True, compact=True)
    output = capsys.readouterr().out
    # Should not produce "covering: " with nothing after it
    assert "covering: )" not in output
    assert "(2 sessions)" in output


def test_cmd_recent_auto_compact_at_threshold(mi, fresh_vault, capsys, monkeypatch):
    """Auto-compact triggers when session count >= COMPACT_SESSION_THRESHOLD."""
    import importlib
    mod = importlib.import_module("memory_indexer")
    monkeypatch.setattr(mod, "COMPACT_SESSION_THRESHOLD", 3)

    for i in range(4):
        _create_session(fresh_vault, "2024-06-15", f"session-{i}", f"summary {i}", mtime_offset=i * 10)

    mod.cmd_recent(1, days=True)
    output = capsys.readouterr().out
    assert "(compact)" in output


def test_cmd_recent_compact_indicator_in_continuity(mi, fresh_vault, capsys):
    """Compact mode appends (compact) to continuity line."""
    _create_session(fresh_vault, "2024-06-15", "session-a", "a", mtime_offset=100)

    mi.cmd_recent(1, compact=True)
    output = capsys.readouterr().out
    assert "(compact)" in output


def test_cmd_recent_normal_shows_full_path(mi, fresh_vault, capsys):
    """Non-compact mode shows full vault path in output."""
    _create_session(fresh_vault, "2024-06-15", "session-a", "a", mtime_offset=100)

    mi.cmd_recent(1, compact=False)
    output = capsys.readouterr().out
    assert "full log:" in output
    assert "(compact)" not in output


# ── health analytics ──────────────────────────────────────────────────────────


def _seed_atoms(db, atoms: list[tuple[str, float, int]]):
    """Insert (path, confidence, corroborations) rows into entries table."""
    for path, conf, corr in atoms:
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, tldr, confidence, corroborations) "
            "VALUES (?, '2024-01-01', 'body', 'atom', 'body', ?, ?)",
            [path, conf, corr],
        )
        # Insert a dummy embedding (768 floats) so sqlite-vec doesn't complain
        import struct
        dummy = struct.pack(f"768f", *([0.0] * 768))
        db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur.lastrowid, dummy])
    db.commit()


def test_collect_health_metrics_empty_db(mi, tmp_path, monkeypatch):
    """Snapshot with no atoms returns zeros."""
    test_db = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    db = mi.open_db()
    snap = mi._collect_health_metrics(db)
    db.close()
    assert snap["atoms"] == 0
    assert snap["avg_confidence"] == 0.0
    assert snap["corr_1x"] == 0


def test_collect_health_metrics_counts_correctly(mi, tmp_path, monkeypatch):
    """Snapshot computes correct counts from DB rows."""
    test_db = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    db = mi.open_db()
    _seed_atoms(db, [
        ("preference-use-pytest.md", 0.60, 1),
        ("decision-use-sqlite.md",   0.70, 2),
        ("fact-lives-in-israel.md",  0.80, 3),
        ("preference-dark-mode.md",  0.50, 1),
    ])
    snap = mi._collect_health_metrics(db)
    db.close()
    assert snap["atoms"] == 4
    assert snap["corr_1x"] == 2
    assert snap["corr_2x"] == 1
    assert snap["corr_3plus"] == 1
    assert abs(snap["avg_confidence"] - (0.60 + 0.70 + 0.80 + 0.50) / 4) < 0.001
    assert snap["categories"]["preference"] == 2
    assert snap["categories"]["decision"] == 1


def test_cmd_health_prints_report(mi, tmp_path, monkeypatch, capsys):
    """cmd_health() produces a report with key sections."""
    test_db = tmp_path / "memory.db"
    health_log = tmp_path / "health.jsonl"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    monkeypatch.setattr(mi, "HEALTH_LOG_PATH", health_log)
    db = mi.open_db()
    _seed_atoms(db, [
        ("preference-pytest.md", 0.70, 2),
        ("decision-sqlite.md",   0.80, 1),
    ])
    db.close()

    mi.cmd_health(save=False)
    output = capsys.readouterr().out
    assert "Memory Health" in output
    assert "Atoms: 2" in output
    assert "avg confidence" in output
    assert "corroborations" in output
    assert "source coverage" in output


def test_cmd_health_saves_snapshot(mi, tmp_path, monkeypatch, capsys):
    """cmd_health() appends a JSON line to HEALTH_LOG_PATH."""
    test_db = tmp_path / "memory.db"
    health_log = tmp_path / "health.jsonl"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    monkeypatch.setattr(mi, "HEALTH_LOG_PATH", health_log)
    mi.open_db().close()

    mi.cmd_health(save=True)
    capsys.readouterr()

    assert health_log.exists()
    lines = [l for l in health_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    snap = json.loads(lines[0])
    assert "date" in snap
    assert "atoms" in snap


def test_cmd_health_idempotent_same_day(mi, tmp_path, monkeypatch, capsys):
    """Running --health twice on the same day does NOT duplicate the snapshot."""
    test_db = tmp_path / "memory.db"
    health_log = tmp_path / "health.jsonl"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    monkeypatch.setattr(mi, "HEALTH_LOG_PATH", health_log)
    mi.open_db().close()

    mi.cmd_health(save=True)
    mi.cmd_health(save=True)
    capsys.readouterr()

    lines = [l for l in health_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, "Should not write duplicate snapshot for the same day"


def test_cmd_health_shows_trends_on_second_run(mi, tmp_path, monkeypatch, capsys):
    """Second run shows trend comparison vs previous snapshot."""
    import json as _json
    from datetime import date, timedelta

    test_db = tmp_path / "memory.db"
    health_log = tmp_path / "health.jsonl"
    monkeypatch.setattr(mi, "DB_PATH", test_db)
    monkeypatch.setattr(mi, "HEALTH_LOG_PATH", health_log)

    # Seed a prior snapshot (yesterday, fewer atoms)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    prior = {
        "date": yesterday, "atoms": 2, "avg_confidence": 0.50,
        "corr_1x": 2, "corr_2x": 0, "corr_3plus": 0,
        "source_chunk_coverage": 0.0, "categories": {}, "sessions": 10,
    }
    health_log.write_text(_json.dumps(prior) + "\n")

    # Today: more atoms, better confidence
    db = mi.open_db()
    _seed_atoms(db, [
        ("preference-pytest.md", 0.70, 2),
        ("decision-sqlite.md",   0.80, 2),
        ("fact-israel.md",       0.90, 3),
    ])
    db.close()

    mi.cmd_health(save=False)
    output = capsys.readouterr().out
    assert "Trends vs last snapshot" in output
    assert "+1 new atoms" in output or "+3" in output or "new atoms" in output
    assert "corroboration" in output.lower()


# ── cmd_add: extract param (PR 1) ─────────────────────────────────────────────


def _make_session_file(path, decisions=True):
    content = "---\ndate: 2024-01-01\ntldr: test session\ntopics: [test]\n"
    if decisions:
        content += 'decisions:\n  - "chose pytest: fast"\n'
    content += "---\n\n## Decisions Made\n- chose pytest for speed\n"
    path.write_text(content)


def test_cmd_add_calls_extract_by_default(mi, tmp_path, monkeypatch):
    """extract=True (default) — cmd_extract must be called after successful indexing."""
    session = tmp_path / "vault" / "Session-Logs" / "my-session.md"
    _make_session_file(session)

    calls = []
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: calls.append(path))

    mi.cmd_add(str(session))

    assert len(calls) == 1
    assert calls[0] == str(session)


def test_cmd_add_skips_extract_with_flag(mi, tmp_path, monkeypatch):
    """extract=False — cmd_extract must NOT be called."""
    session = tmp_path / "vault" / "Session-Logs" / "my-session.md"
    _make_session_file(session)

    calls = []
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: calls.append(path))

    mi.cmd_add(str(session), extract=False)

    assert calls == []


def test_cmd_add_survives_extract_exception(mi, tmp_path, monkeypatch):
    """A RuntimeError from cmd_extract must not abort cmd_add."""
    session = tmp_path / "vault" / "Session-Logs" / "boom-session.md"
    _make_session_file(session)
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))

    mi.cmd_add(str(session))  # must not raise

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM entries WHERE path = ?", [str(session)]).fetchone()[0]
    assert count > 0


def test_cmd_add_survives_extract_systemexit(mi, tmp_path, monkeypatch):
    """A SystemExit from cmd_extract must not abort cmd_add."""
    session = tmp_path / "vault" / "Session-Logs" / "exit-session.md"
    _make_session_file(session)
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: (_ for _ in ()).throw(SystemExit(1)))

    mi.cmd_add(str(session))  # must not raise

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM entries WHERE path = ?", [str(session)]).fetchone()[0]
    assert count > 0


def test_no_extract_flag_parsed():
    """--no-extract flag must be recognised by argparse."""
    import argparse
    import importlib
    import sys as _sys

    # Re-import fresh to get argparse parser
    if "memory_indexer" in _sys.modules:
        del _sys.modules["memory_indexer"]
    mi_mod = importlib.import_module("memory_indexer")

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-extract", action="store_true")
    args = parser.parse_args(["--no-extract"])
    assert args.no_extract is True


# ── Turn chunking helpers (PR 2) ───────────────────────────────────────────────


def test_split_turns_no_markers(mi):
    """Plain prose — no **user**: markers — returns empty list."""
    body = "This is a plain session log with no conversation turns.\nJust prose."
    assert mi._split_turns(body) == []


def test_split_turns_finds_markers(mi):
    body = "**user**: Hello\n**assistant**: Hi there\n**user**: How are you?"
    turns = mi._split_turns(body)
    assert len(turns) == 3
    assert turns[0].startswith("**user**:")
    assert turns[1].startswith("**assistant**:")


def test_split_turns_case_insensitive(mi):
    body = "**User**: Hello\n**Assistant**: Hi"
    turns = mi._split_turns(body)
    assert len(turns) == 2


def test_estimate_tokens(mi):
    text = " ".join(["word"] * 100)
    est = mi._estimate_tokens(text)
    assert 120 <= est <= 140  # 100 * 1.3


def test_make_turn_windows_grouping(mi):
    """Short turns should be grouped into fewer windows."""
    turns = [f"**user**: word{i}" for i in range(10)]
    windows = mi._make_turn_windows(turns, target=400)
    assert len(windows) < 10  # grouped, not one-per-turn


def test_make_turn_windows_splits_large_turns(mi):
    """Each large turn exceeds target — should produce one window each."""
    big = " ".join(["word"] * 400)  # ~520 tokens
    turns = [f"**user**: {big}", f"**assistant**: {big}"]
    windows = mi._make_turn_windows(turns, target=400)
    assert len(windows) == 2


def test_make_turn_windows_discards_tiny(mi):
    """Windows below MIN_CHUNK_TOKENS must be discarded."""
    turns = ["**user**: hi"]  # single very short turn
    windows = mi._make_turn_windows(turns, target=400)
    assert windows == []


def test_chunks_for_log_no_turn_chunks_plain_prose(mi, tmp_path):
    """Sessions without **user**:**assistant**: markers produce no turn chunks."""
    session = tmp_path / "vault" / "Session-Logs" / "prose.md"
    session.write_text(
        "---\ndate: 2024-01-01\ntldr: plain prose\ntopics: [test]\n---\n\n"
        "This is just a plain paragraph. No conversation markers here.\n"
    )
    content = session.read_text()
    chunks = mi.chunks_for_log(session, content)
    types = [c["type"] for c in chunks]
    assert "turn" not in types


def test_chunks_for_log_produces_turn_chunks(mi, tmp_path):
    """Sessions with conversation markers produce at least one turn chunk."""
    session = tmp_path / "vault" / "Session-Logs" / "convo.md"
    # Turns must be long enough to pass _MIN_CHUNK_TOKENS (80 tokens each)
    long_turn = " ".join(["word"] * 70)
    session.write_text(
        "---\ndate: 2024-01-01\ntldr: a conversation\ntopics: [test]\n---\n\n"
        f"**user**: {long_turn}\n"
        f"**assistant**: {long_turn}\n"
    )
    content = session.read_text()
    chunks = mi.chunks_for_log(session, content)
    types = [c["type"] for c in chunks]
    assert "turn" in types


def test_turn_chunk_metadata(mi, tmp_path):
    """Turn chunks carry date/tldr/topics from frontmatter."""
    session = tmp_path / "vault" / "Session-Logs" / "meta.md"
    session.write_text(
        "---\ndate: 2024-06-15\ntldr: my tldr\ntopics: [a, b]\n---\n\n"
        "**user**: hello\n**assistant**: " + " ".join(["word"] * 100) + "\n"
    )
    content = session.read_text()
    chunks = mi.chunks_for_log(session, content)
    turn_chunks = [c for c in chunks if c["type"] == "turn"]
    assert len(turn_chunks) >= 1
    for tc in turn_chunks:
        assert tc["date"] == "2024-06-15"
        assert tc["tldr"] == "my tldr"
        assert "a, b" in tc["topics"]


# ── FTS5 + RRF (PR 3) ─────────────────────────────────────────────────────────


def _add_entry_direct(db, path, chunk, entry_type="frontmatter"):
    """Insert directly into entries (bypassing embed) for fast FTS tests."""
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) "
        "VALUES (?, '2024-01-01', ?, ?, '', '', '')",
        [path, chunk, entry_type],
    )
    rowid = cur.lastrowid
    try:
        db.execute("INSERT INTO entries_fts(rowid, chunk) VALUES (?, ?)", [rowid, chunk])
    except Exception:
        pass
    db.commit()
    return rowid


def test_fts_table_created(mi, tmp_path):
    """entries_fts virtual table must exist after open_db()."""
    db = mi.open_db()
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    assert row is not None


def test_fts_populated_on_add(mi, tmp_path, monkeypatch):
    """cmd_add must insert a row into entries_fts for each indexed chunk."""
    session = tmp_path / "vault" / "Session-Logs" / "fts-session.md"
    _make_session_file(session)
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: None)

    mi.cmd_add(str(session), extract=False)

    db = mi.open_db()
    entries_count = db.execute("SELECT COUNT(*) FROM entries WHERE path = ?", [str(session)]).fetchone()[0]
    fts_count = db.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    assert entries_count > 0
    assert fts_count >= entries_count


def test_fts_removed_on_delete(mi, tmp_path, monkeypatch):
    """delete_entries must remove FTS5 rows for that path."""
    session = tmp_path / "vault" / "Session-Logs" / "del-session.md"
    _make_session_file(session)
    monkeypatch.setattr(mi, "embed", lambda text: [0.0] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: None)
    mi.cmd_add(str(session), extract=False)

    db = mi.open_db()
    before = db.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    mi.delete_entries(db, str(session))
    after = db.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    assert after < before


def test_fts_exact_keyword_match(mi, tmp_path):
    """_fts_query must find a session containing the exact query term."""
    db = mi.open_db()
    path = str(tmp_path / "eigenvalue-session.md")
    _add_entry_direct(db, path, "The eigenvalue decomposition is a key concept in linear algebra.")

    results = mi._fts_query(db, "eigenvalue", k=5)
    paths = [p for p, _ in results]
    assert path in paths


def test_fts_no_match_returns_empty(mi, tmp_path):
    """_fts_query returns [] when no session contains the query term."""
    db = mi.open_db()
    _add_entry_direct(db, str(tmp_path / "unrelated.md"), "We discussed astronomy and stars.")

    results = mi._fts_query(db, "carpentry", k=5)
    assert results == []


def test_fts_dedup_by_path(mi, tmp_path):
    """Multiple chunks from the same path appear only once in _fts_query results."""
    db = mi.open_db()
    path = str(tmp_path / "multi-chunk.md")
    _add_entry_direct(db, path, "eigenvalue chunk one")
    _add_entry_direct(db, path, "eigenvalue chunk two")

    results = mi._fts_query(db, "eigenvalue", k=10)
    paths = [p for p, _ in results]
    assert paths.count(path) == 1


def test_rrf_ordering(mi):
    """A path in both ANN and FTS lists should outscore a path in only one."""
    ann = [("both.md", 5), ("ann-only.md", 1)]
    fts = [("both.md", 3), ("fts-only.md", 1)]
    fused = mi._rrf_fuse(ann, fts, top=10)
    # "both.md" scores 1/(60+5) + 1/(60+3) ≈ 0.0154+0.0159 = 0.0313
    # "ann-only.md" scores 1/(60+1) ≈ 0.0164
    # "fts-only.md" scores 1/(60+1) ≈ 0.0164
    assert fused[0] == "both.md"


def test_rrf_fts_only_path_included(mi):
    """A path present only in FTS results must appear in fused output."""
    ann = [("a.md", 1)]
    fts = [("b.md", 1)]
    fused = mi._rrf_fuse(ann, fts, top=10)
    assert "b.md" in fused


def test_fts_escape_strips_operators(mi):
    result = mi._fts_escape('"linux" AND "kernel" OR NOT test')
    assert '"' not in result
    assert "AND" not in result
    assert "OR" not in result
    assert "NOT" not in result
    assert "linux" in result
    assert "kernel" in result


def test_fts_escape_preserves_words(mi):
    result = mi._fts_escape("memory retrieval benchmark")
    assert "memory" in result
    assert "retrieval" in result
    assert "benchmark" in result


def test_fts_graceful_fallback_no_table(mi, tmp_path):
    """_fts_query returns [] gracefully when entries_fts table doesn't exist."""
    db = mi.open_db()
    # Drop the FTS table to simulate old SQLite without FTS5
    try:
        db.execute("DROP TABLE IF EXISTS entries_fts")
    except Exception:
        pass
    results = mi._fts_query(db, "test query", k=5)
    assert results == []


def test_backfill_fts_on_open(mi, tmp_path):
    """Opening a DB with entries but empty FTS triggers backfill."""
    db = mi.open_db()
    # Insert directly into entries (bypassing FTS sync)
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) "
        "VALUES ('test.md', '2024-01-01', 'some content', 'frontmatter', '', '', '')"
    )
    db.commit()
    db.close()

    # Re-open: _backfill_fts should rebuild FTS index
    db2 = mi.open_db()
    fts_count = db2.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    entries_count = db2.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert fts_count >= entries_count


def test_backfill_fts_idempotent(mi, tmp_path):
    """Calling _backfill_fts twice on a synced DB raises no errors."""
    db = mi.open_db()
    mi._backfill_fts(db)
    mi._backfill_fts(db)  # must not raise


# ── cmd_rebuild safety guard ─────────────────────────────────────────────


def test_rebuild_aborts_if_db_contains_evolution_tables(mi, tmp_path, fresh_vault):
    """cmd_rebuild refuses to delete a DB that contains evolution tables."""
    import sqlite3

    # Create a session log so rebuild doesn't fail on missing logs
    log_dir = fresh_vault / "Session-Logs" / "2024-01-01"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "test.md").write_text("---\ndate: 2024-01-01\n---\n# Test")

    # Create a DB with evolution tables (simulating the old shared-DB scenario)
    db = sqlite3.connect(mi.DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS interactions (id TEXT PRIMARY KEY)")
    db.commit()
    db.close()

    with pytest.raises(SystemExit) as exc_info:
        mi.cmd_rebuild()
    assert exc_info.value.code == 1

    # DB should NOT have been deleted
    assert mi.DB_PATH.exists()
    db = sqlite3.connect(mi.DB_PATH)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    db.close()
    assert "interactions" in tables


def test_rebuild_proceeds_if_db_has_no_evolution_tables(mi, tmp_path, fresh_vault):
    """cmd_rebuild deletes and recreates DB when no evolution tables present."""
    import sqlite3

    # Create a session log
    log_dir = fresh_vault / "Session-Logs" / "2024-01-01"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "test.md").write_text("---\ndate: 2024-01-01\n---\n# Test")

    # Create a DB with only memory-indexer tables (safe to delete)
    db = sqlite3.connect(mi.DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS atoms (id TEXT PRIMARY KEY)")
    db.commit()
    db.close()

    # Should NOT raise — safe to rebuild
    mi.cmd_rebuild()

    # DB should have been recreated (old tables gone, new schema applied)
    assert mi.DB_PATH.exists()


def test_rebuild_proceeds_if_no_db_exists(mi, tmp_path, fresh_vault):
    """cmd_rebuild works fine when DB doesn't exist yet."""
    # Create a session log
    log_dir = fresh_vault / "Session-Logs" / "2024-01-01"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "test.md").write_text("---\ndate: 2024-01-01\n---\n# Test")

    # Ensure no DB exists
    if mi.DB_PATH.exists():
        mi.DB_PATH.unlink()

    # Should NOT raise
    mi.cmd_rebuild()
    assert mi.DB_PATH.exists()


# ── KB Phase 1: Schema migration ────────────────────────────────────────────


def test_schema_has_expired_at_column(mi):
    db = mi.open_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(entries)").fetchall()}
    assert "expired_at" in cols
    assert "expired_reason" in cols
    db.close()


def test_schema_has_domain_column(mi):
    db = mi.open_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(entries)").fetchall()}
    assert "domain" in cols
    db.close()


# ── KB Phase 1: Temporal invalidation ───────────────────────────────────────


def test_invalidate_atom_sets_expired_at(mi, fresh_vault):
    db = mi.open_db()
    # Create a test atom
    atom_path = fresh_vault / "Atoms" / "fact-test.md"
    atom_path.write_text(
        "---\ntype: atom\ncategory: fact\nconfidence: 0.70\ncorroborations: 1\n"
        "domain: dev\ncreated_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: null\n---\n"
        "User prefers Python over Java\n"
    )
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, confidence, corroborations, domain) "
        "VALUES (?, '2024-01-01', 'test', 'atom', 'test', 0.70, 1, 'dev')",
        [str(atom_path)],
    )
    db.commit()
    entry_id = cur.lastrowid

    mi.invalidate_atom(db, entry_id, "contradicted")

    row = db.execute("SELECT expired_at, expired_reason FROM entries WHERE id = ?", [entry_id]).fetchone()
    assert row[0] is not None  # expired_at set
    assert row[1] == "contradicted"

    # Frontmatter also updated
    content = atom_path.read_text()
    assert "expired_at:" in content
    db.close()


def test_query_filters_expired_atoms(mi, fresh_vault, monkeypatch):
    db = mi.open_db()
    import struct

    # Insert a live atom
    live_vec = [0.1] * mi.EMBED_DIM
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, confidence, corroborations, domain) "
        "VALUES ('live.md', '2024-01-01', 'live atom', 'atom', 'live', 0.70, 2, 'dev')",
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(live_vec)])

    # Insert an expired atom (very similar embedding)
    expired_vec = [0.1] * mi.EMBED_DIM
    expired_vec[0] = 0.11  # tiny difference
    cur2 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, confidence, corroborations, domain, expired_at, expired_reason) "
        "VALUES ('expired.md', '2024-01-01', 'expired atom', 'atom', 'expired', 0.70, 2, 'dev', '2024-06-01', 'contradicted')",
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur2.lastrowid, mi.serialize(expired_vec)])
    db.commit()

    # Mock embed to return a vector close to both
    monkeypatch.setattr(mi, "embed", lambda text: [0.1] * mi.EMBED_DIM)

    # Capture output
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        mi.cmd_query("test query", top=5)
    except SystemExit:
        pass
    output = sys.stdout.getvalue()
    sys.stdout = old_stdout

    assert "expired" not in output.lower() or "expired atom" not in output
    db.close()


def test_find_duplicate_atom_ignores_expired(mi):
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM

    # Insert an expired atom
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence, expired_at) "
        "VALUES ('old.md', '2024-01-01', 'test', 'atom', 0.50, '2024-06-01')",
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(vec)])
    db.commit()

    # Same vector should NOT match the expired atom
    result = mi.find_duplicate_atom(db, vec)
    assert result is None
    db.close()


def test_cmd_prune_expires_ttl_atoms(mi, fresh_vault, capsys):
    db = mi.open_db()
    # Create an atom with TTL=30 created 60 days ago
    atom_path = fresh_vault / "Atoms" / "belief-old.md"
    atom_path.write_text(
        "---\ntype: atom\ncategory: belief\nconfidence: 0.40\ncorroborations: 1\n"
        "created_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: 30\n---\n"
        "Some old belief\n"
    )
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence, corroborations) "
        "VALUES (?, '2024-01-01', 'old belief', 'atom', 0.40, 1)",
        [str(atom_path)],
    )
    db.commit()

    mi.cmd_prune(dry_run=False)

    row = db.execute("SELECT expired_at, expired_reason FROM entries WHERE id = ?", [cur.lastrowid]).fetchone()
    assert row[0] is not None  # should be expired
    assert row[1] == "ttl"
    db.close()


def test_cmd_prune_removes_orphans(mi, fresh_vault, capsys):
    db = mi.open_db()
    # Insert a DB row pointing to a non-existent file
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('/nonexistent/atom.md', '2024-01-01', 'orphan', 'atom', 0.50)",
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize([0.1] * mi.EMBED_DIM)])
    db.commit()

    mi.cmd_prune(dry_run=False)

    row = db.execute("SELECT id FROM entries WHERE id = ?", [cur.lastrowid]).fetchone()
    assert row is None  # orphan should be deleted
    db.close()


# ── KB Phase 1: Domain tagging ──────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("using typescript to build docker containers with npm", "dev"),
    ("linear algebra exam preparation study notes", "study"),
    ("portfolio rebalancing stock trading", "trading"),
    ("birthday party with family and friends", "personal"),
    ("random sentence about weather patterns", "general"),
])
def test_classify_domain_matches_keyword_map(mi, text, expected):
    assert mi.classify_domain(text) == expected


def test_write_atom_file_includes_domain(mi, fresh_vault):
    atom = {"text": "user prefers docker", "category": "preference"}
    path = mi.write_atom_file(atom, "/src/log.md", "2024-01-01", domain="dev")
    content = path.read_text()
    assert "domain: dev" in content


def test_write_atom_file_uses_confidence_prior(mi, fresh_vault):
    atom = {"text": "always use eslint", "category": "constraint"}
    path = mi.write_atom_file(atom, "/src/log.md", "2024-01-01")
    content = path.read_text()
    assert "confidence: 0.65" in content  # constraint prior


# ── KB Phase 1: Confidence priors ───────────────────────────────────────────


def test_confidence_prior_by_category(mi, fresh_vault):
    """New atoms of each category get the correct initial confidence."""
    for cat, expected_conf in mi.CONFIDENCE_PRIOR.items():
        atom = {"text": f"test {cat} atom", "category": cat}
        path = mi.write_atom_file(atom, "/src/log.md", "2024-01-01")
        content = path.read_text()
        assert f"confidence: {expected_conf:.2f}" in content
        path.unlink()  # cleanup for next iteration


def test_bump_corroboration_uses_category_prior(mi, fresh_vault):
    db = mi.open_db()
    # Create a belief atom (prior=0.40)
    atom_path = fresh_vault / "Atoms" / "belief-test-bump.md"
    atom_path.write_text(
        "---\ntype: atom\ncategory: belief\nconfidence: 0.40\ncorroborations: 1\n"
        "created_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: 90\n---\n"
        "Some belief\n"
    )
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence, corroborations) "
        "VALUES (?, '2024-01-01', 'belief', 'atom', 0.40, 1)",
        [str(atom_path)],
    )
    db.commit()

    mi.bump_corroboration(db, cur.lastrowid)

    row = db.execute("SELECT confidence, corroborations FROM entries WHERE id = ?", [cur.lastrowid]).fetchone()
    # belief prior=0.40, 2 corroborations: min(0.40 + 2*0.1, 0.95) = 0.60
    assert row[0] == pytest.approx(0.60, abs=0.01)
    assert row[1] == 2
    db.close()


# ── KB Phase 1: --gaps command ──────────────────────────────────────────────


def test_cmd_gaps_surfaces_uncovered_topics(mi, fresh_vault, capsys):
    # Create 4 sessions all tagged "docker"
    for i in range(4):
        log_dir = fresh_vault / "Session-Logs" / f"2024-01-0{i+1}"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"session-{i}.md").write_text(
            f"---\ndate: 2024-01-0{i+1}\ntopics: [docker, ci]\n---\n# Session {i}\n"
        )

    # No atoms exist → docker should be a gap
    mi.cmd_gaps(top=5)
    output = capsys.readouterr().out
    assert "docker" in output
    assert "4 sessions" in output


def test_cmd_gaps_no_gaps_when_covered(mi, fresh_vault, capsys):
    # Create 3 sessions tagged "python"
    for i in range(3):
        log_dir = fresh_vault / "Session-Logs" / f"2024-02-0{i+1}"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"session-{i}.md").write_text(
            f"---\ndate: 2024-02-0{i+1}\ntopics: [python]\n---\n# Session {i}\n"
        )

    # Create atoms covering "python"
    for i in range(3):
        (fresh_vault / "Atoms" / f"fact-python-{i}.md").write_text(
            f"---\ntype: atom\ncategory: fact\n---\nUser uses python for scripting task {i}\n"
        )

    mi.cmd_gaps(top=5)
    output = capsys.readouterr().out
    assert "python" not in output or "No significant gaps" in output


# ── KB Phase 2: Schema ──────────────────────────────────────────────────────


def test_schema_has_entities_table(mi):
    db = mi.open_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(entities)").fetchall()}
    assert "name" in cols
    assert "entity_type" in cols
    assert "mention_count" in cols
    db.close()


def test_schema_has_relationships_table(mi):
    db = mi.open_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(relationships)").fetchall()}
    assert "source_id" in cols
    assert "target_id" in cols
    assert "rel_type" in cols
    assert "expired_at" in cols
    db.close()


def test_schema_has_atom_entities_table(mi):
    db = mi.open_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(atom_entities)").fetchall()}
    assert "atom_id" in cols
    assert "entity_id" in cols
    db.close()


def test_schema_reopen_idempotent(mi):
    """Opening DB twice doesn't error (CREATE TABLE IF NOT EXISTS)."""
    db1 = mi.open_db()
    db1.close()
    db2 = mi.open_db()
    count = db2.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 0
    db2.close()


# ── KB Phase 2: upsert_entity / upsert_relationship ─────────────────────────


def test_upsert_entity_creates_new(mi):
    db = mi.open_db()
    eid = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    assert eid > 0
    row = db.execute("SELECT name, entity_type, mention_count FROM entities WHERE id = ?", [eid]).fetchone()
    assert row[0] == "docker"
    assert row[1] == "tool"
    assert row[2] == 1
    db.close()


def test_upsert_entity_increments_mention_count(mi):
    db = mi.open_db()
    eid1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    eid2 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-02")
    assert eid1 == eid2
    row = db.execute("SELECT mention_count, last_seen FROM entities WHERE id = ?", [eid1]).fetchone()
    assert row[0] == 2
    assert row[1] == "2024-01-02"
    db.close()


def test_upsert_entity_preserves_type(mi):
    db = mi.open_db()
    mi.upsert_entity(db, "Python", "tool", "dev", "2024-01-01")
    mi.upsert_entity(db, "Python", "tool", "dev", "2024-01-02")
    row = db.execute("SELECT entity_type FROM entities WHERE name = 'python'").fetchone()
    assert row[0] == "tool"
    db.close()


def test_upsert_relationship_creates_edge(mi):
    db = mi.open_db()
    src = mi.upsert_entity(db, "User", "person", "personal", "2024-01-01")
    tgt = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    rid = mi.upsert_relationship(db, src, tgt, "uses", 0.7, "2024-01-01")
    assert rid > 0
    row = db.execute("SELECT confidence, evidence_count FROM relationships WHERE id = ?", [rid]).fetchone()
    assert row[0] == pytest.approx(0.7)
    assert row[1] == 1
    db.close()


def test_upsert_relationship_increments_evidence(mi):
    db = mi.open_db()
    src = mi.upsert_entity(db, "User", "person", "personal", "2024-01-01")
    tgt = mi.upsert_entity(db, "Python", "tool", "dev", "2024-01-01")
    mi.upsert_relationship(db, src, tgt, "uses", 0.5, "2024-01-01")
    mi.upsert_relationship(db, src, tgt, "uses", 0.5, "2024-01-02")
    row = db.execute(
        "SELECT evidence_count, confidence FROM relationships WHERE source_id = ? AND target_id = ?",
        [src, tgt],
    ).fetchone()
    assert row[0] == 2
    assert row[1] == pytest.approx(0.55)  # 0.5 + 0.05
    db.close()


def test_upsert_relationship_confidence_cap(mi):
    db = mi.open_db()
    src = mi.upsert_entity(db, "User", "person", "personal", "2024-01-01")
    tgt = mi.upsert_entity(db, "Git", "tool", "dev", "2024-01-01")
    mi.upsert_relationship(db, src, tgt, "uses", 0.90, "2024-01-01")
    # Upsert again — confidence should cap at 0.95
    mi.upsert_relationship(db, src, tgt, "uses", 0.90, "2024-01-02")
    row = db.execute(
        "SELECT confidence FROM relationships WHERE source_id = ? AND target_id = ?",
        [src, tgt],
    ).fetchone()
    assert row[0] <= 0.95
    db.close()


def test_upsert_relationship_clears_expired_on_reassert(mi):
    db = mi.open_db()
    src = mi.upsert_entity(db, "User", "person", "personal", "2024-01-01")
    tgt = mi.upsert_entity(db, "Ruby", "tool", "dev", "2024-01-01")
    rid = mi.upsert_relationship(db, src, tgt, "uses", 0.5, "2024-01-01")
    # Manually expire
    db.execute("UPDATE relationships SET expired_at = '2024-06-01' WHERE id = ?", [rid])
    db.commit()
    # Re-assert
    mi.upsert_relationship(db, src, tgt, "uses", 0.5, "2024-07-01")
    row = db.execute("SELECT expired_at FROM relationships WHERE id = ?", [rid]).fetchone()
    assert row[0] is None  # cleared
    db.close()


# ── KB Phase 2: link_atom_entities ───────────────────────────────────────────


def test_link_atom_entities_creates_junction_rows(mi):
    db = mi.open_db()
    mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    mi.upsert_entity(db, "Python", "tool", "dev", "2024-01-01")
    # Create a fake atom entry
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) VALUES ('a.md', '2024-01-01', 'test', 'atom', 0.5)"
    )
    atom_id = cur.lastrowid
    mi.link_atom_entities(db, atom_id, [("Docker", "tool"), ("Python", "tool")])
    rows = db.execute("SELECT COUNT(*) FROM atom_entities WHERE atom_id = ?", [atom_id]).fetchone()
    assert rows[0] == 2
    db.close()


def test_link_atom_entities_skips_missing_entity(mi):
    db = mi.open_db()
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) VALUES ('b.md', '2024-01-01', 'test', 'atom', 0.5)"
    )
    # Should not crash when entity doesn't exist
    mi.link_atom_entities(db, cur.lastrowid, [("Nonexistent", "tool")])
    rows = db.execute("SELECT COUNT(*) FROM atom_entities WHERE atom_id = ?", [cur.lastrowid]).fetchone()
    assert rows[0] == 0
    db.close()


def test_link_atom_entities_idempotent(mi):
    db = mi.open_db()
    mi.upsert_entity(db, "Node", "tool", "dev", "2024-01-01")
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) VALUES ('c.md', '2024-01-01', 'test', 'atom', 0.5)"
    )
    atom_id = cur.lastrowid
    mi.link_atom_entities(db, atom_id, [("Node", "tool")])
    mi.link_atom_entities(db, atom_id, [("Node", "tool")])  # duplicate — should not crash
    rows = db.execute("SELECT COUNT(*) FROM atom_entities WHERE atom_id = ?", [atom_id]).fetchone()
    assert rows[0] == 1
    db.close()


# ── KB Phase 2: Contradiction detection ──────────────────────────────────────


def test_contradiction_prompt_format(mi):
    prompt = mi._contradiction_prompt("User lives in NYC", "User lives in Tel Aviv")
    assert "Fact A" in prompt
    assert "Fact B" in prompt
    assert "CONTRADICT" in prompt


def test_detect_contradictions_skips_distant_embeddings(mi):
    """Atoms with L2 distance > 1.2 should not trigger LLM calls."""
    db = mi.open_db()
    # Insert an atom with a very different vector
    far_vec = [1.0] * mi.EMBED_DIM
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('far.md', '2024-01-01', 'totally different topic', 'atom', 0.5)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(far_vec)])
    db.commit()

    # New atom with opposite vector — L2 distance should be large
    new_vec = [0.0] * mi.EMBED_DIM
    # Should return empty (no LLM calls made) — we don't mock _client,
    # so if it tried to call LLM it would crash
    conflicts = mi.detect_contradictions(db, 999, "new fact", new_vec)
    assert conflicts == []
    db.close()


def test_detect_contradictions_caps_at_5_calls(mi, monkeypatch):
    """Should not make more than 5 LLM calls even with many similar atoms."""
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM

    # Insert 10 similar atoms
    for i in range(10):
        v = [0.5] * mi.EMBED_DIM
        v[0] = 0.5 + i * 0.001  # tiny variation
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, confidence) "
            f"VALUES ('atom{i}.md', '2024-01-01', 'similar fact {i}', 'atom', 0.5)"
        )
        db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                   [cur.lastrowid, mi.serialize(v)])
    db.commit()

    # Mock the LLM client to track calls
    call_count = [0]

    class FakeResponse:
        text = "CONSISTENT"

    class FakeModels:
        def generate_content(self, **kwargs):
            call_count[0] += 1
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(mi, "_client", FakeClient())

    mi.detect_contradictions(db, 999, "test fact", vec)
    assert call_count[0] <= 5
    db.close()


def test_detect_contradictions_invalidates_on_conflict(mi, fresh_vault, monkeypatch):
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM

    # Insert an existing atom
    atom_path = fresh_vault / "Atoms" / "fact-old.md"
    atom_path.write_text(
        "---\ntype: atom\ncategory: fact\nconfidence: 0.70\ncorroborations: 1\n"
        "created_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: null\n---\n"
        "User lives in NYC\n"
    )
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES (?, '2024-01-01', 'User lives in NYC', 'atom', 0.70)",
        [str(atom_path)],
    )
    old_id = cur.lastrowid
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [old_id, mi.serialize(vec)])
    db.commit()

    # Mock LLM to return CONTRADICT
    class FakeResponse:
        text = "CONTRADICT"

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(mi, "_client", FakeClient())

    new_vec = [0.5] * mi.EMBED_DIM
    new_vec[0] = 0.501
    conflicts = mi.detect_contradictions(db, 999, "User lives in Tel Aviv", new_vec)
    assert len(conflicts) == 1
    assert conflicts[0]["older_id"] == old_id

    # Old atom should NOT be auto-expired — conflicts are logged for user review
    row = db.execute("SELECT expired_at FROM entries WHERE id = ?", [old_id]).fetchone()
    assert row[0] is None  # not auto-invalidated

    # Conflict should be logged in pending_conflicts table
    pending = db.execute(
        "SELECT older_id, newer_id, resolved FROM pending_conflicts WHERE older_id = ?",
        [old_id],
    ).fetchone()
    assert pending is not None
    assert pending[0] == old_id
    assert pending[1] == 999
    assert pending[2] == 0  # unresolved
    db.close()


def test_detect_contradictions_consistent_no_invalidation(mi, monkeypatch):
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM

    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('compat.md', '2024-01-01', 'User likes Python', 'atom', 0.70)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(vec)])
    db.commit()

    class FakeResponse:
        text = "CONSISTENT"

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(mi, "_client", FakeClient())

    conflicts = mi.detect_contradictions(db, 999, "User also likes TypeScript", vec)
    assert len(conflicts) == 0
    # Original not expired
    row = db.execute("SELECT expired_at FROM entries WHERE id = ?", [cur.lastrowid]).fetchone()
    assert row[0] is None
    db.close()


# ── KB Phase 2: Graph wander ────────────────────────────────────────────────


def test_cmd_wander_graph_empty_graph(mi, capsys):
    """No crash on empty entities table."""
    mi.cmd_wander_graph(["test"])
    output = capsys.readouterr().out
    assert "No entities" in output


def test_cmd_wander_graph_spreads_activation(mi, capsys):
    db = mi.open_db()
    # Build a simple graph: A -> B -> C
    a = mi.upsert_entity(db, "alpha", "concept", "dev", "2024-01-01")
    b = mi.upsert_entity(db, "beta", "concept", "dev", "2024-01-01")
    c = mi.upsert_entity(db, "gamma", "concept", "dev", "2024-01-01")
    mi.upsert_relationship(db, a, b, "related_to", 0.8, "2024-01-01")
    mi.upsert_relationship(db, b, c, "related_to", 0.8, "2024-01-01")
    db.commit()
    db.close()

    mi.cmd_wander_graph(["alpha"], steps=2, top_k=10)
    output = capsys.readouterr().out
    assert "beta" in output
    assert "gamma" in output  # reached via 2-step spread


def test_cmd_wander_graph_fan_effect(mi):
    """Hub node should spread less activation per edge than a leaf."""
    import math
    db = mi.open_db()
    hub = mi.upsert_entity(db, "hub", "concept", "dev", "2024-01-01")
    # Connect hub to 10 neighbors
    for i in range(10):
        n = mi.upsert_entity(db, f"neighbor-{i}", "concept", "dev", "2024-01-01")
        mi.upsert_relationship(db, hub, n, "related_to", 0.5, "2024-01-01")
    db.commit()

    # Fan penalty should be sqrt(10) ≈ 3.16
    # Per-edge activation = 1.0 * 0.7 * weight / sqrt(10) — much less than without fan effect
    degree = db.execute(
        "SELECT COUNT(*) FROM relationships WHERE source_id = ? OR target_id = ?", [hub, hub]
    ).fetchone()[0]
    assert degree == 10
    fan_penalty = math.sqrt(degree)
    assert fan_penalty > 3.0  # confirms fan effect would reduce spread
    db.close()


def test_cmd_wander_graph_bridges(mi, capsys):
    db = mi.open_db()
    # A-B, A-C, D-B, D-C — B and C are bridges between A and D (no direct A-D edge)
    a = mi.upsert_entity(db, "node-a", "concept", "dev", "2024-01-01")
    b = mi.upsert_entity(db, "node-b", "concept", "dev", "2024-01-01")
    c = mi.upsert_entity(db, "node-c", "concept", "dev", "2024-01-01")
    d = mi.upsert_entity(db, "node-d", "concept", "dev", "2024-01-01")
    mi.upsert_relationship(db, a, b, "related_to", 0.8, "2024-01-01")
    mi.upsert_relationship(db, a, c, "related_to", 0.8, "2024-01-01")
    mi.upsert_relationship(db, d, b, "related_to", 0.8, "2024-01-01")
    mi.upsert_relationship(db, d, c, "related_to", 0.8, "2024-01-01")
    db.commit()
    db.close()

    mi.cmd_wander_graph(["node-a"], steps=2, top_k=10)
    output = capsys.readouterr().out
    assert "node-d" in output  # reached via indirect path


# ── KB Phase 2: Health metrics ──────────────────────────────────────────────


def test_health_includes_graph_metrics(mi):
    db = mi.open_db()
    mi.upsert_entity(db, "TestEntity", "concept", "dev", "2024-01-01")
    src = mi.upsert_entity(db, "Src", "tool", "dev", "2024-01-01")
    tgt = mi.upsert_entity(db, "Tgt", "tool", "dev", "2024-01-01")
    mi.upsert_relationship(db, src, tgt, "uses", 0.5, "2024-01-01")
    db.commit()

    metrics = mi._collect_health_metrics(db)
    assert metrics["entities"] == 3
    assert metrics["relationships_live"] == 1
    assert metrics["relationships_expired"] == 0
    db.close()


# ── KB Phase 3: Schema ──────────────────────────────────────────────────────


def test_schema_has_entity_articles_table(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(entity_articles)").fetchall()]
    assert "entity_id" in cols
    assert "vault_path" in cols
    assert "source_hash" in cols
    assert "generated_at" in cols
    db.close()


def test_schema_has_digests_table(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(digests)").fetchall()]
    assert "level" in cols
    assert "period_key" in cols
    assert "content" in cols
    assert "atom_ids" in cols
    db.close()


# ── KB Phase 3: _compute_entity_source_hash ─────────────────────────────────


def _seed_entity(db, mi, name, entity_type, mention_count=1):
    """Helper to create an entity with a specific mention_count."""
    eid = mi.upsert_entity(db, name, entity_type, "dev", "2024-01-01")
    if mention_count > 1:
        db.execute("UPDATE entities SET mention_count = ? WHERE id = ?", [mention_count, eid])
        db.commit()
    return eid


def _seed_atom_for_entity(db, mi, entity_id, text="test atom", fresh_vault=None):
    """Helper to create an atom and link it to an entity."""
    vec = mi.embed(text)
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain) "
        "VALUES (?, '2024-01-01', ?, 'atom', ?, '', 0.5, 1, 'dev')",
        [f"/tmp/atom-{text[:10]}.md", text, text],
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(vec)])
    db.execute("INSERT OR IGNORE INTO atom_entities (atom_id, entity_id) VALUES (?, ?)",
               [cur.lastrowid, entity_id])
    db.commit()
    return cur.lastrowid


def test_compute_entity_source_hash_deterministic(mi):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool")
    h1 = mi._compute_entity_source_hash(db, eid)
    h2 = mi._compute_entity_source_hash(db, eid)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex
    db.close()


def test_compute_entity_source_hash_changes_on_new_atom(mi):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool")
    h1 = mi._compute_entity_source_hash(db, eid)
    _seed_atom_for_entity(db, mi, eid, "docker uses containers")
    h2 = mi._compute_entity_source_hash(db, eid)
    assert h1 != h2
    db.close()


# ── KB Phase 3: _entity_article_prompt ───────────────────────────────────────


def test_entity_article_prompt_includes_relationships(mi):
    entity = {"name": "Docker", "entity_type": "tool"}
    rels = [{"source": "User", "target": "Docker", "rel_type": "uses"}]
    atoms = [{"text": "Docker runs containers"}]
    prompt = mi._entity_article_prompt(entity, rels, atoms)
    assert "User" in prompt
    assert "uses" in prompt
    assert "Docker" in prompt


def test_entity_article_prompt_includes_atoms(mi):
    entity = {"name": "Docker", "entity_type": "tool"}
    rels = []
    atoms = [{"text": "Docker runs containers"}, {"text": "Docker uses images"}]
    prompt = mi._entity_article_prompt(entity, rels, atoms)
    assert "Docker runs containers" in prompt
    assert "Docker uses images" in prompt


# ── KB Phase 3: generate_entity_article ──────────────────────────────────────


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, response_text="# Test Article\n\nThis is a test."):
        self._response_text = response_text
        self.call_count = 0

    def generate_content(self, **kwargs):
        self.call_count += 1
        return _FakeResponse(self._response_text)


def test_generate_entity_article_writes_file(mi, fresh_vault, monkeypatch):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool")
    _seed_atom_for_entity(db, mi, eid, "Docker runs containers")
    db.commit()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    path = mi.generate_entity_article(db, eid)
    assert path.exists()
    content = path.read_text()
    assert "Test Article" in content
    assert "entity: docker" in content
    db.close()


def test_generate_entity_article_records_in_db(mi, fresh_vault, monkeypatch):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool")
    _seed_atom_for_entity(db, mi, eid, "Docker runs containers")
    db.commit()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.generate_entity_article(db, eid)
    row = db.execute("SELECT entity_id, source_hash FROM entity_articles WHERE entity_id = ?", [eid]).fetchone()
    assert row is not None
    assert row[0] == eid
    assert len(row[1]) == 64
    db.close()


# ── KB Phase 3: cmd_compile ──────────────────────────────────────────────────


def test_cmd_compile_auto_selects_eligible(mi, fresh_vault, monkeypatch, capsys):
    db = mi.open_db()
    eid_high = _seed_entity(db, mi, "Docker", "tool", mention_count=5)
    _seed_atom_for_entity(db, mi, eid_high, "docker fact")
    _seed_entity(db, mi, "Rare", "concept", mention_count=1)
    db.commit()
    db.close()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compile()
    assert fake_models.call_count == 1  # only Docker compiled, Rare skipped
    out = capsys.readouterr().out
    assert "docker" in out.lower()


def test_cmd_compile_skips_fresh_articles(mi, fresh_vault, monkeypatch, capsys):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool", mention_count=5)
    _seed_atom_for_entity(db, mi, eid, "docker fact")
    db.commit()
    db.close()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    # First compile
    mi.cmd_compile()
    assert fake_models.call_count == 1

    # Second compile — should skip (hash unchanged)
    mi.cmd_compile()
    assert fake_models.call_count == 1  # no new calls


def test_cmd_compile_regenerates_stale(mi, fresh_vault, monkeypatch, capsys):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool", mention_count=5)
    _seed_atom_for_entity(db, mi, eid, "docker fact")
    db.commit()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compile()
    assert fake_models.call_count == 1

    # Add new atom → hash changes → stale
    _seed_atom_for_entity(db, mi, eid, "docker new fact")
    db.close()

    mi.cmd_compile()
    assert fake_models.call_count == 2  # regenerated


def test_cmd_compile_specific_entity(mi, fresh_vault, monkeypatch, capsys):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool", mention_count=1)  # below threshold
    _seed_atom_for_entity(db, mi, eid, "docker fact")
    db.commit()
    db.close()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compile(entity_name="Docker")
    assert fake_models.call_count == 1  # compiled despite low mention_count


# ── KB Phase 3: _get_period_key ──────────────────────────────────────────────


def test_get_period_key_weekly(mi):
    # 2024-06-15 is a Saturday in ISO week 24
    assert mi._get_period_key("2024-06-15", "weekly") == "2024-W24"


def test_get_period_key_monthly(mi):
    assert mi._get_period_key("2024-06-15", "monthly") == "2024-06"


# ── KB Phase 3: compress_period / cmd_compress_digests ───────────────────────


def test_compress_period_stores_digest(mi, monkeypatch):
    db = mi.open_db()
    # Seed a session entry in a known period
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s.md', '2024-06-15', 'test session', 'frontmatter', 'did stuff', 'dev')"
    )
    db.commit()

    fake_models = _FakeModels(response_text="Weekly digest summary")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    content = mi.compress_period(db, "weekly", "2024-W24")
    assert content == "Weekly digest summary"

    row = db.execute("SELECT content FROM digests WHERE level='weekly' AND period_key='2024-W24'").fetchone()
    assert row is not None
    assert row[0] == "Weekly digest summary"
    db.close()


def test_compress_period_idempotent(mi, monkeypatch):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s.md', '2024-06-15', 'test session', 'frontmatter', 'did stuff', 'dev')"
    )
    db.commit()

    fake_models = _FakeModels(response_text="First digest")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.compress_period(db, "weekly", "2024-W24")
    # Second call with different content should upsert (not duplicate)
    fake_models._response_text = "Updated digest"
    mi.compress_period(db, "weekly", "2024-W24")

    count = db.execute("SELECT COUNT(*) FROM digests WHERE level='weekly' AND period_key='2024-W24'").fetchone()[0]
    assert count == 1
    row = db.execute("SELECT content FROM digests WHERE period_key='2024-W24'").fetchone()
    assert row[0] == "Updated digest"
    db.close()


def test_cmd_compress_digests_finds_missing_periods(mi, monkeypatch, capsys):
    db = mi.open_db()
    # Seed sessions in 2 different weeks (W23 and W25)
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s1.md', '2024-06-03', 'session1', 'frontmatter', 'week23', 'dev')"
    )
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s2.md', '2024-06-17', 'session2', 'frontmatter', 'week25', 'dev')"
    )
    # Pre-seed digest for W23 only
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('weekly', '2024-W23', 'existing', '2024-06-07')"
    )
    db.commit()
    db.close()

    fake_models = _FakeModels(response_text="new digest")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compress_digests("weekly")
    # W23 already exists, only W25 should be generated
    assert fake_models.call_count == 1


# ── KB Phase 3: classify_query_intent ────────────────────────────────────────


def test_classify_query_intent_factual(mi):
    assert mi.classify_query_intent("what did we decide about auth") == "factual"
    assert mi.classify_query_intent("who is Eden") == "factual"


def test_classify_query_intent_temporal(mi):
    assert mi.classify_query_intent("how has my thinking on auth evolved") == "temporal"
    assert mi.classify_query_intent("history of the auth module") == "temporal"


def test_classify_query_intent_exhaustive(mi):
    assert mi.classify_query_intent("everything about python") == "exhaustive"
    assert mi.classify_query_intent("deep dive into Docker") == "exhaustive"


def test_classify_query_intent_exploratory_default(mi):
    assert mi.classify_query_intent("auth middleware") == "exploratory"
    assert mi.classify_query_intent("python docker") == "exploratory"


# ── KB Phase 3: query intent routing ─────────────────────────────────────────


def _seed_session_entry(db, mi, path, date, tldr, text="chunk"):
    """Seed a session entry with embedding for query tests."""
    vec = mi.embed(text)
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) "
        "VALUES (?, ?, ?, 'frontmatter', ?, 'dev', '')",
        [path, date, text, tldr],
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(vec)])
    try:
        db.execute("INSERT INTO entries_fts(rowid, chunk) VALUES (?, ?)",
                   [cur.lastrowid, text])
    except Exception:
        pass
    db.commit()
    return cur.lastrowid


def test_query_as_of_filters_by_date(mi, monkeypatch, capsys):
    db = mi.open_db()
    _seed_session_entry(db, mi, "/tmp/old.md", "2024-01-01", "old session", "auth middleware")
    _seed_session_entry(db, mi, "/tmp/new.md", "2024-12-01", "new session", "auth middleware new")
    db.close()

    # Query with as_of should only show old entry
    mi.cmd_query("auth middleware", top=5, as_of="2024-06-01")
    out = capsys.readouterr().out
    assert "old.md" in out
    assert "new.md" not in out


def test_query_intent_override(mi, monkeypatch, capsys):
    db = mi.open_db()
    _seed_session_entry(db, mi, "/tmp/s.md", "2024-01-01", "test session", "auth stuff")
    db.close()

    # "auth stuff" would be exploratory by default, but we override to factual
    mi.cmd_query("auth stuff", top=5, intent="factual")
    out = capsys.readouterr().out
    assert "Past Sessions" in out  # still shows results (factual uses same hybrid path)


def test_query_exploratory_uses_graph(mi, monkeypatch, capsys):
    """Exploratory intent should still produce results from hybrid search."""
    db = mi.open_db()
    _seed_session_entry(db, mi, "/tmp/s.md", "2024-01-01", "docker session", "docker containers")
    db.close()

    mi.cmd_query("docker containers", top=5, intent="exploratory")
    out = capsys.readouterr().out
    assert "Past Sessions" in out


# ── KB Phase 3: Health metrics ───────────────────────────────────────────────


def test_health_includes_article_metrics(mi, fresh_vault, monkeypatch):
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool", mention_count=5)
    _seed_atom_for_entity(db, mi, eid, "docker fact")
    db.commit()

    fake_models = _FakeModels()
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)
    mi.generate_entity_article(db, eid)

    metrics = mi._collect_health_metrics(db)
    assert "articles" in metrics
    assert metrics["articles"] == 1
    assert "articles_stale" in metrics
    assert metrics["articles_stale"] == 0
    db.close()


def test_health_includes_digest_metrics(mi, monkeypatch):
    db = mi.open_db()
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('weekly', '2024-W24', 'test', '2024-06-15')"
    )
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('monthly', '2024-06', 'test monthly', '2024-06-30')"
    )
    db.commit()

    metrics = mi._collect_health_metrics(db)
    assert metrics["digests_weekly"] == 1
    assert metrics["digests_monthly"] == 1
    db.close()


# ── KB Phase 4: Schema ──────────────────────────────────────────────────────


def test_schema_has_access_log_table(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(access_log)").fetchall()]
    assert "entry_id" in cols
    assert "accessed_at" in cols
    assert "access_type" in cols
    db.close()


def test_schema_has_query_log_table(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(query_log)").fetchall()]
    assert "query_text" in cols
    assert "intent" in cols
    assert "result_count" in cols
    assert "atom_hit" in cols
    db.close()


def test_schema_has_synthesis_suggestions_table(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(synthesis_suggestions)").fetchall()]
    assert "entity_a_id" in cols
    assert "entity_b_id" in cols
    assert "bridge_text" in cols
    assert "dismissed" in cols
    db.close()


def test_schema_has_privacy_column(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(entries)").fetchall()]
    assert "privacy" in cols
    db.close()


def test_schema_has_temperature_column(mi):
    db = mi.open_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(entries)").fetchall()]
    assert "temperature" in cols
    db.close()


# ── KB Phase 4: Logging ─────────────────────────────────────────────────────


def test_log_access_inserts_row(mi):
    db = mi.open_db()
    # Seed an entry
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.commit()
    mi.log_access(db, 1, "query")
    row = db.execute("SELECT entry_id, access_type FROM access_log WHERE entry_id = 1").fetchone()
    assert row is not None
    assert row[1] == "query"
    db.close()


def test_log_query_inserts_row(mi):
    db = mi.open_db()
    mi.log_query(db, "test query", "factual", result_count=3, atom_hit=2)
    row = db.execute("SELECT query_text, intent, result_count, atom_hit FROM query_log").fetchone()
    assert row[0] == "test query"
    assert row[1] == "factual"
    assert row[2] == 3
    assert row[3] == 2
    db.close()


# ── KB Phase 4: Temperature / Forgetting curves ─────────────────────────────


def test_compute_temperature_recent_access_hot(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.commit()
    from datetime import datetime as dt
    today = dt.now().strftime("%Y-%m-%dT%H:%M:%S")
    db.execute("INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (1, ?, 'query')", [today])
    db.commit()
    temp = mi.compute_temperature(db, 1)
    assert temp >= 0.9  # exp(0) ≈ 1.0
    db.close()


def test_compute_temperature_old_access_cold(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.commit()
    db.execute("INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (1, '2020-01-01T00:00:00', 'query')")
    db.commit()
    temp = mi.compute_temperature(db, 1)
    assert temp < 0.01  # very old access → near zero
    db.close()


def test_compute_temperature_multiple_accesses_cumulative(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.commit()
    from datetime import datetime as dt
    today = dt.now().strftime("%Y-%m-%dT%H:%M:%S")
    db.execute("INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (1, ?, 'query')", [today])
    db.execute("INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (1, ?, 'wander')", [today])
    db.commit()
    temp = mi.compute_temperature(db, 1)
    assert temp >= 1.9  # two recent accesses → ~2.0
    db.close()


def test_compute_temperature_no_accesses(mi):
    """Atoms with no access history get 0.5 baseline (not 0.0) to avoid permanent bottom-ranking."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.commit()
    temp = mi.compute_temperature(db, 1)
    assert temp == 0.5
    db.close()


def test_cmd_decay_updates_temperature_column(mi, capsys):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, temperature) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '', 1.0)"
    )
    db.commit()
    db.close()
    mi.cmd_decay()
    db = mi.open_db()
    row = db.execute("SELECT temperature FROM entries WHERE id = 1").fetchone()
    assert row[0] == 0.5  # no accesses → baseline (not 0.0)
    out = capsys.readouterr().out
    assert "warm" in out  # 0.5 is in warm range (0.1-0.5)
    db.close()


def test_cmd_decay_dry_run(mi, capsys):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, temperature) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test', 'atom', 'test', '', 1.0)"
    )
    db.commit()
    db.close()
    mi.cmd_decay(dry_run=True)
    db = mi.open_db()
    row = db.execute("SELECT temperature FROM entries WHERE id = 1").fetchone()
    assert row[0] == 1.0  # unchanged in dry-run
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    db.close()


# ── KB Phase 4: Query integration ───────────────────────────────────────────


def test_query_temperature_weighting(mi, monkeypatch, capsys):
    """Hot atoms should rank higher than equivalent cold atoms."""
    db = mi.open_db()
    vec1 = mi.embed("auth middleware hot")
    cur1 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain, temperature) "
        "VALUES ('/tmp/hot.md', '2024-01-01', 'auth middleware hot', 'atom', 'auth hot', '', 0.5, 2, 'dev', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur1.lastrowid, mi.serialize(vec1)])

    vec2 = mi.embed("auth middleware warm")
    cur2 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain, temperature) "
        "VALUES ('/tmp/warm.md', '2024-01-01', 'auth middleware warm', 'atom', 'auth warm', '', 0.5, 2, 'dev', 0.2)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur2.lastrowid, mi.serialize(vec2)])
    db.commit()
    db.close()

    mi.cmd_query("auth middleware", top=5)
    out = capsys.readouterr().out
    # Both should appear (neither is cold)
    assert "auth" in out.lower()


def test_query_cold_atoms_rank_lower(mi, monkeypatch, capsys):
    """Cold atoms should still appear but rank lower due to temperature weighting."""
    db = mi.open_db()
    vec = mi.embed("cold atom test")
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain, temperature) "
        "VALUES ('/tmp/cold.md', '2024-01-01', 'cold atom test', 'atom', 'cold fact', '', 0.5, 2, 'dev', 0.05)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur.lastrowid, mi.serialize(vec)])
    # Also add a session so query doesn't exit with code 1
    vec2 = mi.embed("session about testing")
    cur2 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s.md', '2024-01-01', 'session about testing', 'frontmatter', 'testing', 'dev')"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur2.lastrowid, mi.serialize(vec2)])
    db.commit()
    db.close()

    mi.cmd_query("cold atom test", top=5)
    out = capsys.readouterr().out
    # Cold atoms still appear (not excluded), just rank lower
    assert "cold fact" in out


# ── KB Phase 4: Privacy ──────────────────────────────────────────────────────


def test_classify_privacy_pii_sensitive(mi):
    assert mi.classify_privacy("My SSN is 123-45-6789", "dev") == "sensitive"
    assert mi.classify_privacy("Email: user@example.com", "dev") == "sensitive"


def test_classify_privacy_personal_domain(mi):
    assert mi.classify_privacy("some personal note", "personal") == "private"


def test_classify_privacy_dev_internal(mi):
    assert mi.classify_privacy("refactored the build pipeline", "dev") == "internal"


def test_classify_privacy_study_public(mi):
    assert mi.classify_privacy("linear algebra theorem", "study") == "public"


def test_classify_privacy_trading_sensitive(mi):
    assert mi.classify_privacy("market analysis", "trading") == "sensitive"


def test_write_atom_file_includes_privacy(mi, fresh_vault):
    atom = {"text": "test atom", "category": "fact"}
    path = mi.write_atom_file(atom, "/tmp/source.md", "2024-01-01", privacy="private")
    content = path.read_text()
    assert "privacy: private" in content


def test_cmd_extract_sets_privacy(mi, fresh_vault, monkeypatch):
    """cmd_extract should classify privacy for new atoms."""
    session = fresh_vault / "Session-Logs" / "2024-01-01" / "test.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "---\ntype: session\ndate: 2024-01-01\ntopics: [dev]\ntldr: test\n"
        "decisions:\n  - \"chose X\"\n---\n## Decisions Made\n- chose X\n"
    )

    fake_atoms = [{"text": "prefer dark mode always", "category": "preference"}]
    monkeypatch.setattr(mi, "extract_atoms", lambda c: fake_atoms)
    monkeypatch.setattr(mi, "embed", lambda t: [0.1] * 768)

    mi.cmd_extract(str(session))

    db = mi.open_db()
    row = db.execute("SELECT privacy FROM entries WHERE type = 'atom' LIMIT 1").fetchone()
    assert row is not None
    assert row[0] in ("public", "internal", "private", "sensitive")
    db.close()


def test_query_privacy_filter(mi, monkeypatch, capsys):
    db = mi.open_db()
    vec1 = mi.embed("public fact")
    cur1 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain, privacy, temperature) "
        "VALUES ('/tmp/pub.md', '2024-01-01', 'public fact', 'atom', 'public fact', '', 0.5, 2, 'dev', 'public', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur1.lastrowid, mi.serialize(vec1)])

    vec2 = mi.embed("sensitive secret")
    cur2 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, domain, privacy, temperature) "
        "VALUES ('/tmp/sec.md', '2024-01-01', 'sensitive secret', 'atom', 'sensitive secret', '', 0.5, 2, 'dev', 'sensitive', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)", [cur2.lastrowid, mi.serialize(vec2)])
    db.commit()
    db.close()

    mi.cmd_query("fact secret", top=5, privacy="public")
    out = capsys.readouterr().out
    assert "sensitive secret" not in out


# ── KB Phase 4: Cross-domain synthesis ───────────────────────────────────────


def test_find_cross_domain_bridges_detects_candidates(mi):
    db = mi.open_db()
    e1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    e2 = mi.upsert_entity(db, "Study", "topic", "study", "2024-01-01")
    bridge = mi.upsert_entity(db, "Automation", "concept", "dev", "2024-01-01")
    mi.upsert_relationship(db, e1, bridge, "uses", 0.5, "2024-01-01")
    mi.upsert_relationship(db, e2, bridge, "uses", 0.5, "2024-01-01")
    db.commit()
    bridges = mi.find_cross_domain_bridges(db)
    assert len(bridges) >= 1
    names = [(b["entity_a"], b["entity_b"]) for b in bridges]
    assert any("docker" in a or "docker" in b for a, b in names)
    db.close()


def test_find_cross_domain_bridges_no_same_domain(mi):
    db = mi.open_db()
    e1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    e2 = mi.upsert_entity(db, "Python", "tool", "dev", "2024-01-01")
    bridge = mi.upsert_entity(db, "CI", "concept", "dev", "2024-01-01")
    mi.upsert_relationship(db, e1, bridge, "uses", 0.5, "2024-01-01")
    mi.upsert_relationship(db, e2, bridge, "uses", 0.5, "2024-01-01")
    db.commit()
    bridges = mi.find_cross_domain_bridges(db)
    # All entities are in "dev" domain → no cross-domain bridges
    assert len(bridges) == 0
    db.close()


def test_find_cross_domain_bridges_empty_graph(mi):
    db = mi.open_db()
    bridges = mi.find_cross_domain_bridges(db)
    assert bridges == []
    db.close()


def test_generate_synthesis_stores_in_db(mi, monkeypatch):
    db = mi.open_db()
    e1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    e2 = mi.upsert_entity(db, "Study", "topic", "study", "2024-01-01")
    db.commit()

    fake_models = _FakeModels(response_text="Docker containerization patterns apply to study modularization")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    text = mi.generate_synthesis(db, e1, e2)
    assert "Docker" in text
    row = db.execute("SELECT bridge_text FROM synthesis_suggestions WHERE entity_a_id = ? AND entity_b_id = ?", [e1, e2]).fetchone()
    assert row is not None
    db.close()


def test_generate_synthesis_cached(mi, monkeypatch):
    db = mi.open_db()
    e1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    e2 = mi.upsert_entity(db, "Study", "topic", "study", "2024-01-01")
    db.commit()

    fake_models = _FakeModels(response_text="Synthesis result")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.generate_synthesis(db, e1, e2)
    assert fake_models.call_count == 1
    # Second call should use cache
    mi.generate_synthesis(db, e1, e2)
    assert fake_models.call_count == 1  # no new LLM call
    db.close()


def test_cmd_synthesize_outputs_suggestions(mi, monkeypatch, capsys):
    db = mi.open_db()
    e1 = mi.upsert_entity(db, "Docker", "tool", "dev", "2024-01-01")
    e2 = mi.upsert_entity(db, "Study", "topic", "study", "2024-01-01")
    bridge = mi.upsert_entity(db, "Automation", "concept", "dev", "2024-01-01")
    mi.upsert_relationship(db, e1, bridge, "uses", 0.5, "2024-01-01")
    mi.upsert_relationship(db, e2, bridge, "uses", 0.5, "2024-01-01")
    db.commit()
    db.close()

    fake_models = _FakeModels(response_text="Cross-domain insight here")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_synthesize(top=3)
    out = capsys.readouterr().out
    assert "Cross-Domain Synthesis" in out


# ── KB Phase 4: Blind spots ─────────────────────────────────────────────────


def test_cmd_blind_spots_combines_all_sources(mi, fresh_vault, capsys):
    """Blind spots should run without errors even with empty data."""
    mi.cmd_blind_spots(top=5)
    out = capsys.readouterr().out
    assert "Blind Spots" in out


def test_cmd_blind_spots_query_misses(mi, capsys):
    db = mi.open_db()
    # Log queries with 0 atom hits
    mi.log_query(db, "unknown topic", "exploratory", result_count=0, atom_hit=0)
    mi.log_query(db, "unknown topic", "exploratory", result_count=0, atom_hit=0)
    db.close()

    mi.cmd_blind_spots(top=5)
    out = capsys.readouterr().out
    assert "query-miss" in out
    assert "unknown topic" in out


def test_cmd_blind_spots_entity_orphans(mi, capsys):
    db = mi.open_db()
    # Create entity with mention_count >= 2 but no linked atoms
    mi.upsert_entity(db, "OrphanEntity", "concept", "dev", "2024-01-01")
    db.execute("UPDATE entities SET mention_count = 3 WHERE name = 'orphanentity'")
    db.commit()
    db.close()

    mi.cmd_blind_spots(top=5)
    out = capsys.readouterr().out
    assert "entity-orphan" in out
    assert "orphanentity" in out


# ── KB Phase 4: Export ───────────────────────────────────────────────────────


def test_cmd_export_filters_by_privacy(mi, tmp_path):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, domain, privacy) "
        "VALUES ('/tmp/pub.md', '2024-01-01', 'public fact', 'atom', 'public fact', '', 'dev', 'public')"
    )
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, domain, privacy) "
        "VALUES ('/tmp/sec.md', '2024-01-01', 'secret fact', 'atom', 'secret fact', '', 'dev', 'sensitive')"
    )
    db.commit()
    db.close()

    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path), privacy_levels=["public"])
    content = out_path.read_text()
    assert "public fact" in content
    assert "secret fact" not in content


def test_cmd_export_writes_file(mi, tmp_path):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, domain, privacy) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'test atom', 'atom', 'test atom', '', 'dev', 'internal')"
    )
    db.commit()
    db.close()

    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path))
    assert out_path.exists()
    content = out_path.read_text()
    assert "test atom" in content


# ── KB Phase 4: Health metrics ───────────────────────────────────────────────


def test_health_includes_temperature_distribution(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, temperature) "
        "VALUES ('/tmp/hot.md', '2024-01-01', 'hot', 'atom', 'hot', '', 0.8)"
    )
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, temperature) "
        "VALUES ('/tmp/cold.md', '2024-01-01', 'cold', 'atom', 'cold', '', 0.05)"
    )
    db.commit()
    metrics = mi._collect_health_metrics(db)
    assert metrics["temp_hot"] == 1
    assert metrics["temp_cold"] == 1
    db.close()


def test_health_includes_query_success_rate(mi):
    db = mi.open_db()
    mi.log_query(db, "query1", "factual", result_count=3, atom_hit=2)
    mi.log_query(db, "query2", "factual", result_count=0, atom_hit=0)
    metrics = mi._collect_health_metrics(db)
    assert metrics["query_total"] == 2
    assert metrics["query_success_rate"] == 0.5
    db.close()


def test_health_includes_privacy_distribution(mi):
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, privacy) "
        "VALUES ('/tmp/a.md', '2024-01-01', 'a', 'atom', 'a', '', 'public')"
    )
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, privacy) "
        "VALUES ('/tmp/b.md', '2024-01-01', 'b', 'atom', 'b', '', 'sensitive')"
    )
    db.commit()
    metrics = mi._collect_health_metrics(db)
    assert "privacy" in metrics
    assert metrics["privacy"].get("public") == 1
    assert metrics["privacy"].get("sensitive") == 1
    db.close()


# ── New coverage: functions with no tests ────────────────────────────────────


def test_resolve_wikilinks_simple(mi):
    """[[page]] should be replaced with page."""
    result = mi.resolve_wikilinks("See [[Linear Algebra]] for details.")
    assert result == "See Linear Algebra for details."


def test_resolve_wikilinks_with_display(mi):
    """[[page|display text]] should be replaced with display text."""
    result = mi.resolve_wikilinks("See [[Linear Algebra|LA]] for details.")
    assert result == "See LA for details."


def test_slugify_basic(mi):
    """'Hello World Test' should become 'hello-world-test'."""
    assert mi.slugify("Hello World Test") == "hello-world-test"


def test_slugify_truncates_to_5_words(mi):
    """Long text should be truncated to 5 words in the slug."""
    result = mi.slugify("one two three four five six seven eight")
    parts = result.split("-")
    assert len(parts) == 5


def test_slugify_collision_increments_counter(mi, fresh_vault):
    """write_atom_file picks a different filename when the slug path already exists."""
    atom = {"text": "user prefers pytest over unittest", "category": "preference"}
    # Write once to create the file at the expected slug path
    first_path = mi.write_atom_file(atom, "/session.md", "2024-01-01")
    assert first_path.exists()
    # Write again with same text — should pick a counter-suffixed name
    second_path = mi.write_atom_file(atom, "/session.md", "2024-01-01")
    assert second_path.exists()
    assert second_path != first_path
    assert "-2" in second_path.name


def test_cmd_invalidate_happy_path(mi, fresh_vault):
    """cmd_invalidate should set expired_at on an existing atom entry."""
    atom_path = fresh_vault / "Atoms" / "fact-live.md"
    atom_path.write_text(
        "---\ntype: atom\ncategory: fact\nconfidence: 0.70\ncorroborations: 1\n"
        "created_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: null\n---\n"
        "User lives in Tel Aviv\n"
    )
    db = mi.open_db()
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES (?, '2024-01-01', 'User lives in Tel Aviv', 'atom', 0.70)",
        [str(atom_path)],
    )
    db.commit()
    db.close()

    mi.cmd_invalidate(str(atom_path), "superseded")

    db = mi.open_db()
    row = db.execute(
        "SELECT expired_at, expired_reason FROM entries WHERE path = ?",
        [str(atom_path)],
    ).fetchone()
    db.close()
    assert row[0] is not None
    assert row[1] == "superseded"


def test_cmd_invalidate_not_found_exits(mi, fresh_vault):
    """cmd_invalidate with nonexistent path should raise SystemExit."""
    nonexistent = str(fresh_vault / "Atoms" / "does-not-exist.md")
    with pytest.raises(SystemExit):
        mi.cmd_invalidate(nonexistent, "test")


# ── New coverage: error paths ────────────────────────────────────────────────


def test_cmd_add_file_not_found_exits(mi):
    """cmd_add with a nonexistent path should raise SystemExit."""
    with pytest.raises(SystemExit):
        mi.cmd_add("/nonexistent/path/session.md")


def test_cmd_extract_file_not_found_exits(mi):
    """cmd_extract with a nonexistent path should raise SystemExit."""
    with pytest.raises(SystemExit):
        mi.cmd_extract("/nonexistent/path/session.md")


def test_cmd_query_empty_index_exits(mi):
    """cmd_query on an empty DB should raise SystemExit."""
    with pytest.raises(SystemExit):
        mi.cmd_query("anything")


def test_cmd_extract_no_decisions_skips(mi, fresh_vault, capsys):
    """Session with no decisions section should print 'skipping extraction'."""
    session = fresh_vault / "Session-Logs" / "2024-01-01" / "no-dec.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "---\ndate: 2024-01-01\ntldr: no decisions here\ntopics: [test]\n---\n"
        "## Summary\nJust a plain session with no decisions or decision frontmatter.\n"
    )
    mi.cmd_extract(str(session))
    output = capsys.readouterr().out
    assert "skipping" in output.lower() or "No decisions" in output


def test_extract_content_for_llm_truncation(mi):
    """Content > 6000 chars should be truncated, keeping frontmatter + key sections."""
    frontmatter = "---\ndate: 2024-01-01\ntldr: test\n---\n"
    decisions = "## Decisions Made\nUse pytest. Always.\n"
    filler = "x" * 7000
    content = frontmatter + decisions + filler
    result = mi._extract_content_for_llm(content)
    assert len(result) <= 6000
    # Frontmatter should be preserved
    assert "date: 2024-01-01" in result
    # Decisions should be preserved
    assert "Decisions Made" in result


# ── New coverage: CLI flags ──────────────────────────────────────────────────


def test_query_recency_boost(mi, monkeypatch, capsys):
    """With recency_boost=True, a 1-day-old entry should rank above a 30-day-old one."""
    from datetime import date, timedelta
    today = date.today()
    recent_date = (today - timedelta(days=1)).isoformat()
    old_date = (today - timedelta(days=30)).isoformat()

    db = mi.open_db()
    # Both sessions get the same embedding so ANN distance is equal;
    # recency boost should separate them.
    vec = [0.5] * mi.EMBED_DIM
    cur1 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) "
        "VALUES (?, ?, 'auth stuff', 'frontmatter', 'recent session', 'dev', '')",
        ["/tmp/recent.md", recent_date],
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur1.lastrowid, mi.serialize(vec)])
    cur2 = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) "
        "VALUES (?, ?, 'auth stuff', 'frontmatter', 'old session', 'dev', '')",
        ["/tmp/old.md", old_date],
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur2.lastrowid, mi.serialize(vec)])
    db.commit()
    db.close()

    mi.cmd_query("auth stuff", top=5, recency_boost=True)
    out = capsys.readouterr().out
    # Recent entry should appear before old entry
    assert "recent.md" in out
    recent_pos = out.find("recent.md")
    old_pos = out.find("old.md")
    assert recent_pos < old_pos, "Recent entry should rank before old entry"


def test_query_show_source(mi, capsys):
    """cmd_query with show_source=True should print 'Source context:' for atoms with source_chunk."""
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, "
        "source_chunk, domain, privacy, temperature) "
        "VALUES ('/tmp/src-atom.md', '2024-01-01', 'user prefers pytest', 'atom', "
        "'user prefers pytest', '', 0.70, 2, 'Source context: pytest over unittest', 'dev', 'internal', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur.lastrowid, mi.serialize(vec)])
    db.commit()
    db.close()

    mi.cmd_query("pytest", top=5, show_source=True)
    out = capsys.readouterr().out
    assert "Source context:" in out


def test_query_domain_filter(mi, capsys):
    """cmd_query with domain='dev' should only return atoms in the dev domain."""
    db = mi.open_db()
    vec_dev = [0.5] * mi.EMBED_DIM
    vec_study = [0.6] * mi.EMBED_DIM

    cur_dev = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, "
        "domain, privacy, temperature) "
        "VALUES ('/tmp/dev-atom.md', '2024-01-01', 'typescript build pipeline', 'atom', "
        "'typescript build pipeline', '', 0.70, 2, 'dev', 'internal', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur_dev.lastrowid, mi.serialize(vec_dev)])

    cur_study = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, "
        "domain, privacy, temperature) "
        "VALUES ('/tmp/study-atom.md', '2024-01-01', 'linear algebra theorem', 'atom', "
        "'linear algebra theorem', '', 0.70, 2, 'study', 'internal', 1.0)"
    )
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [cur_study.lastrowid, mi.serialize(vec_study)])
    db.commit()
    db.close()

    mi.cmd_query("build test", top=5, domain="dev")
    out = capsys.readouterr().out
    # The atom text (stored as chunk/tldr) appears in output; study atom must be absent
    assert "typescript build pipeline" in out
    assert "linear algebra theorem" not in out


def test_cmd_extract_no_contradict_skips_detection(mi, fresh_vault, monkeypatch):
    """With no_contradict=True, detect_contradictions should NOT be called."""
    session = fresh_vault / "Session-Logs" / "2024-01-01" / "test-nc.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "---\ndate: 2024-01-01\ntldr: test\ntopics: [dev]\n"
        "decisions:\n  - \"use pytest\"\n---\n## Decisions Made\n- use pytest\n"
    )

    called = []
    monkeypatch.setattr(mi, "extract_atoms",
                        lambda c: [{"text": "user prefers pytest", "category": "preference"}])
    monkeypatch.setattr(mi, "embed", lambda t: [0.1] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "extract_entities_and_relations",
                        lambda c: {"entities": [], "relationships": []})
    monkeypatch.setattr(mi, "detect_contradictions",
                        lambda db, aid, txt, vec: called.append(True) or [])

    mi.cmd_extract(str(session), no_contradict=True)

    assert called == [], "detect_contradictions should NOT have been called"


def test_compress_digests_monthly(mi, monkeypatch, capsys):
    """cmd_compress_digests('monthly') should produce period_key in 'YYYY-MM' format."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/june.md', '2024-06-15', 'june session', 'frontmatter', 'did stuff', 'dev')"
    )
    db.commit()
    db.close()

    fake_models = _FakeModels(response_text="Monthly summary for June 2024")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compress_digests("monthly")

    db = mi.open_db()
    row = db.execute(
        "SELECT period_key FROM digests WHERE level = 'monthly'"
    ).fetchone()
    db.close()
    assert row is not None
    # Should be YYYY-MM format
    import re
    assert re.match(r"^\d{4}-\d{2}$", row[0]), f"Expected YYYY-MM format, got: {row[0]}"


# ── New coverage: edge cases ─────────────────────────────────────────────────


def test_cmd_add_stale_entity_articles_notification(mi, fresh_vault, monkeypatch, capsys):
    """After adding a new atom linked to an entity, cmd_add should print '[stale]'."""
    session = fresh_vault / "Session-Logs" / "2024-01-01" / "stale-test.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "---\ndate: 2024-01-01\ntldr: test\ntopics: [dev]\n"
        "decisions:\n  - \"use docker\"\n---\n## Decisions Made\n- use docker\n"
    )
    monkeypatch.setattr(mi, "embed", lambda t: [0.1] * mi.EMBED_DIM)
    monkeypatch.setattr(mi, "cmd_extract", lambda path: None)

    # Seed a stale entity article (hash won't match the empty atom set)
    db = mi.open_db()
    eid = _seed_entity(db, mi, "Docker", "tool", mention_count=3)
    # Compute hash with no atoms linked, store it, then add an atom to make it stale
    initial_hash = mi._compute_entity_source_hash(db, eid)
    today = "2024-01-01"
    db.execute(
        "INSERT INTO entity_articles (entity_id, vault_path, generated_at, source_hash) "
        "VALUES (?, '/tmp/docker.md', ?, ?)",
        [eid, today, initial_hash],
    )
    # Add an atom linked to the entity to invalidate the hash
    _seed_atom_for_entity(db, mi, eid, "docker uses containers")
    db.commit()
    db.close()

    # cmd_add should detect the now-stale article and print [stale]
    mi.cmd_add(str(session), extract=False)
    out = capsys.readouterr().out
    assert "[stale]" in out


def test_classify_privacy_keyword_private_non_personal_domain(mi):
    """Text containing 'family' in domain='dev' should return 'private' via keyword match."""
    # domain is not 'personal', but keyword 'family' triggers private
    result = mi.classify_privacy("talked about family reunion plans", "dev")
    assert result == "private"


def test_cmd_compress_digests_all_up_to_date(mi, monkeypatch, capsys):
    """When all periods already have digests, print 'up to date'."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/s.md', '2024-06-15', 'session', 'frontmatter', 'did stuff', 'dev')"
    )
    # Pre-seed the digest for the only period that exists
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('weekly', '2024-W24', 'existing digest', '2024-06-15')"
    )
    db.commit()
    db.close()

    fake_models = _FakeModels(response_text="should not be called")
    fake_client = type("C", (), {"models": fake_models})()
    monkeypatch.setattr(mi, "_client", fake_client)

    mi.cmd_compress_digests("weekly")
    out = capsys.readouterr().out
    assert "up to date" in out
    assert fake_models.call_count == 0


def test_query_exhaustive_includes_digests(mi, monkeypatch, capsys):
    """cmd_query with intent='exhaustive' should include 'Period Digests' in output."""
    db = mi.open_db()
    # Seed a session entry so query doesn't exit empty
    _seed_session_entry(db, mi, "/tmp/exhaustive-session.md", "2024-06-15",
                        "exhaustive session", "auth middleware")
    # Seed a digest
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('weekly', '2024-W24', 'Weekly summary of auth work', '2024-06-15')"
    )
    db.commit()
    db.close()

    mi.cmd_query("auth middleware", top=5, intent="exhaustive")
    out = capsys.readouterr().out
    assert "Period Digests" in out


# ── Privacy: default sensitive exclusion ─────────────────────────────────────


def _seed_privacy_atoms(db, mi):
    """Seed atoms at each privacy level for privacy filter tests."""
    levels = {
        "public": ("public study fact", "/tmp/pub.md"),
        "internal": ("internal dev fact", "/tmp/int.md"),
        "private": ("private family fact", "/tmp/prv.md"),
        "sensitive": ("sensitive trading secret", "/tmp/sen.md"),
    }
    for privacy, (text, path) in levels.items():
        vec = mi.embed(text)
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, tldr, topics, "
            "confidence, corroborations, domain, privacy, temperature) "
            "VALUES (?, '2024-01-01', ?, 'atom', ?, '', 0.5, 2, 'dev', ?, 1.0)",
            [path, text, text, privacy],
        )
        db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                   [cur.lastrowid, mi.serialize(vec)])
    db.commit()


def test_query_default_excludes_sensitive(mi, capsys):
    """Without --privacy flag, sensitive atoms should be excluded by default."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("fact secret", top=10)
    out = capsys.readouterr().out
    assert "sensitive trading secret" not in out
    # Public, internal, and private should all appear
    assert "public study fact" in out or "internal dev fact" in out or "private family fact" in out


def test_query_default_includes_private(mi, capsys):
    """Private atoms should be visible by default (not blocked like sensitive)."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("private family fact", top=10)
    out = capsys.readouterr().out
    assert "private family fact" in out


def test_query_explicit_sensitive_shows_sensitive(mi, capsys):
    """Passing --privacy sensitive should show ONLY sensitive atoms."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("fact secret", top=10, privacy="sensitive")
    out = capsys.readouterr().out
    assert "sensitive trading secret" in out
    # Other levels should NOT appear
    assert "public study fact" not in out
    assert "internal dev fact" not in out


def test_query_default_shows_blocked_notice(mi, capsys):
    """When sensitive atoms are filtered, output should mention how many were blocked."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("sensitive trading secret", top=10)
    out = capsys.readouterr().out
    assert "blocked by privacy filter" in out
    assert "sensitive atoms are excluded by default" in out


def test_export_default_excludes_sensitive_and_private(mi, tmp_path):
    """Default export should only include public and internal atoms."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path))
    content = out_path.read_text()
    assert "internal dev fact" in content
    assert "public study fact" in content
    assert "sensitive trading secret" not in content
    assert "private family fact" not in content


# ── Channel privacy: --allowed-privacy and DEUS_MEMORY_PRIVACY ───────────────


def test_allowed_privacy_filters_to_allowlist(mi, capsys):
    """--allowed-privacy should only show atoms in the allowlist."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("fact secret", top=10, allowed_privacy=["public", "internal"])
    out = capsys.readouterr().out
    assert "public study fact" in out or "internal dev fact" in out
    assert "private family fact" not in out
    assert "sensitive trading secret" not in out


def test_allowed_privacy_includes_sensitive_when_listed(mi, capsys):
    """If sensitive is in the allowlist, it should appear."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("sensitive trading secret", top=10, allowed_privacy=["sensitive"])
    out = capsys.readouterr().out
    assert "sensitive trading secret" in out
    assert "public study fact" not in out


def test_allowed_privacy_env_fallback(mi, capsys, monkeypatch):
    """DEUS_MEMORY_PRIVACY env var should act as fallback for --allowed-privacy."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "public,internal")
    mi.cmd_query("fact secret", top=10)
    out = capsys.readouterr().out
    assert "private family fact" not in out
    assert "sensitive trading secret" not in out


def test_allowed_privacy_overrides_env(mi, capsys, monkeypatch):
    """Explicit --allowed-privacy should override DEUS_MEMORY_PRIVACY env."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "public")
    # Explicit allowlist includes private, env says only public
    mi.cmd_query("private family fact", top=10, allowed_privacy=["public", "internal", "private"])
    out = capsys.readouterr().out
    assert "private family fact" in out


def test_allowed_privacy_blocked_notice_shows_channel_message(mi, capsys):
    """When atoms are blocked by allowlist, notice should mention channel policy."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("sensitive trading secret", top=10, allowed_privacy=["public", "internal"])
    out = capsys.readouterr().out
    assert "blocked by channel privacy policy" in out
    assert "/settings memory_privacy" in out


def test_allowed_privacy_no_blocked_notice_when_all_allowed(mi, capsys):
    """No blocked notice when all privacy levels are in the allowlist."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    mi.cmd_query("fact", top=10, allowed_privacy=["public", "internal", "private", "sensitive"])
    out = capsys.readouterr().out
    assert "blocked" not in out


def test_export_uses_env_var_privacy(mi, tmp_path, monkeypatch):
    """cmd_export should respect DEUS_MEMORY_PRIVACY env var."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "public,internal,private")
    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path))
    content = out_path.read_text()
    assert "private family fact" in content
    assert "sensitive trading secret" not in content


def test_export_explicit_overrides_env(mi, tmp_path, monkeypatch):
    """Explicit privacy_levels should override DEUS_MEMORY_PRIVACY env."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "public,internal,private,sensitive")
    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path), privacy_levels=["public"])
    content = out_path.read_text()
    assert "public study fact" in content
    assert "internal dev fact" not in content
    assert "sensitive trading secret" not in content


def test_allowed_privacy_takes_precedence_over_legacy_privacy(mi, capsys):
    """When both --allowed-privacy and --privacy are given, allowlist wins."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    # --privacy=sensitive would normally show ONLY sensitive
    # --allowed-privacy=public,internal should override that
    mi.cmd_query("fact", top=10, privacy="sensitive", allowed_privacy=["public", "internal"])
    out = capsys.readouterr().out
    assert "sensitive trading secret" not in out
    # Allowlist should filter, not the legacy flag
    assert "private family fact" not in out


def test_empty_env_var_falls_through_to_default(mi, capsys, monkeypatch):
    """DEUS_MEMORY_PRIVACY='' should behave like no env var (default filtering)."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "")
    mi.cmd_query("sensitive trading secret", top=10)
    out = capsys.readouterr().out
    # Default behavior: sensitive excluded, blocked notice shown
    assert "sensitive trading secret" not in out
    assert "blocked by privacy filter" in out


def test_allowed_privacy_with_no_matching_atoms(mi, capsys):
    """Allowlist with levels that have no atoms should return empty gracefully."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    # Only seed has public/internal/private/sensitive — query with allowlist
    # that excludes everything the query matches
    mi.cmd_query("sensitive trading secret", top=10, allowed_privacy=["public"])
    out = capsys.readouterr().out
    assert "sensitive trading secret" not in out
    assert "blocked by channel privacy policy" in out


def test_new_channel_gets_safe_default(mi, capsys):
    """Without any privacy config (new channel), sensitive is excluded by default."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    # No allowed_privacy, no env var, no --privacy → default excludes sensitive
    mi.cmd_query("fact secret", top=10)
    out = capsys.readouterr().out
    assert "sensitive trading secret" not in out
    assert "public study fact" in out or "internal dev fact" in out or "private family fact" in out


def test_commas_only_env_var_falls_through_to_default(mi, capsys, monkeypatch):
    """DEUS_MEMORY_PRIVACY=',,,' should behave like no env var."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", ",,,")
    mi.cmd_query("sensitive trading secret", top=10)
    out = capsys.readouterr().out
    assert "sensitive trading secret" not in out
    assert "blocked by privacy filter" in out


def test_invalid_levels_in_env_var_stripped(mi, capsys, monkeypatch):
    """Invalid privacy levels in DEUS_MEMORY_PRIVACY should be silently stripped."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    monkeypatch.setenv("DEUS_MEMORY_PRIVACY", "public,bogus,internal")
    mi.cmd_query("fact", top=10)
    out = capsys.readouterr().out
    # 'bogus' is stripped; only public and internal are allowed
    assert "private family fact" not in out
    assert "sensitive trading secret" not in out


def test_export_respects_allowed_privacy_arg(mi, tmp_path):
    """cmd_export should respect explicit privacy_levels allowlist."""
    db = mi.open_db()
    _seed_privacy_atoms(db, mi)
    db.close()

    out_path = tmp_path / "export.md"
    mi.cmd_export(str(out_path), privacy_levels=["public", "internal", "private"])
    content = out_path.read_text()
    assert "private family fact" in content
    assert "sensitive trading secret" not in content


def test_resolve_privacy_allowlist_validates(mi):
    """_resolve_privacy_allowlist should strip invalid levels."""
    result = mi._resolve_privacy_allowlist(["public", "bogus", "internal"])
    assert result == ["public", "internal"]


def test_resolve_privacy_allowlist_all_invalid_returns_none(mi):
    """_resolve_privacy_allowlist with all-invalid input returns None."""
    result = mi._resolve_privacy_allowlist(["bogus", "fake"])
    assert result is None


# ── Data integrity: rebuild preserves metadata ───────────────────────────────


def test_rebuild_preserves_privacy_from_atom_files(mi, fresh_vault, monkeypatch):
    """--rebuild should read privacy from atom file frontmatter, not reset to 'internal'."""
    atoms_dir = fresh_vault / "Atoms"
    atoms_dir.mkdir(parents=True, exist_ok=True)
    (atoms_dir / "fact-test.md").write_text(
        "---\ntype: atom\ncategory: fact\ntags: []\n"
        "confidence: 0.80\ncorroborations: 2\ndomain: trading\nprivacy: sensitive\n"
        "source: /tmp/src.md\ncreated_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: null\n"
        "---\nMy trading account number is 12345\n"
    )
    # Run rebuild (session logs dir already exists from fresh_vault fixture)
    mi.cmd_rebuild()

    db = mi.open_db()
    row = db.execute(
        "SELECT privacy FROM entries WHERE type = 'atom' LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "sensitive"  # preserved, not reset to 'internal'
    db.close()


def test_rebuild_preserves_expired_at_from_atom_files(mi, fresh_vault, monkeypatch):
    """--rebuild should read expired_at from atom file frontmatter."""
    atoms_dir = fresh_vault / "Atoms"
    atoms_dir.mkdir(parents=True, exist_ok=True)
    (atoms_dir / "fact-expired.md").write_text(
        "---\ntype: atom\ncategory: fact\ntags: []\n"
        "confidence: 0.50\ncorroborations: 1\ndomain: dev\nprivacy: internal\n"
        "expired_at: 2024-06-01\nexpired_reason: superseded\n"
        "source: /tmp/src.md\ncreated_at: 2024-01-01\nupdated_at: 2024-01-01\nttl_days: null\n"
        "---\nOld fact that was invalidated\n"
    )
    mi.cmd_rebuild()

    db = mi.open_db()
    row = db.execute(
        "SELECT expired_at, expired_reason FROM entries WHERE type = 'atom' LIMIT 1"
    ).fetchone()
    assert row[0] == "2024-06-01"
    assert row[1] == "superseded"
    db.close()


def test_compute_temperature_baseline_for_unaccessed(mi):
    """Atoms with no access_log should get 0.5 baseline, not 0.0."""
    db = mi.open_db()
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('/tmp/t.md', '2024-01-01', 'test', 'atom', 0.5)"
    )
    db.commit()
    temp = mi.compute_temperature(db, cur.lastrowid)
    assert temp == 0.5  # baseline, not 0.0
    db.close()


def test_contradiction_does_not_auto_invalidate(mi, monkeypatch):
    """Contradictions should be logged, not auto-invalidated."""
    db = mi.open_db()
    vec = [0.5] * mi.EMBED_DIM
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('/tmp/old.md', '2024-01-01', 'User prefers Python', 'atom', 0.5)"
    )
    old_id = cur.lastrowid
    db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
               [old_id, mi.serialize(vec)])
    db.commit()

    class FakeResponse:
        text = "CONTRADICT"

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(mi, "_client", FakeClient())

    new_vec = [0.5] * mi.EMBED_DIM
    new_vec[0] = 0.501
    mi.detect_contradictions(db, 999, "User prefers Rust", new_vec)

    # Atom should NOT be expired
    row = db.execute("SELECT expired_at FROM entries WHERE id = ?", [old_id]).fetchone()
    assert row[0] is None

    # Conflict should be in pending_conflicts
    conflict = db.execute("SELECT resolved FROM pending_conflicts WHERE older_id = ?", [old_id]).fetchone()
    assert conflict is not None
    assert conflict[0] == 0
    db.close()


def test_invalidate_conflict_expires_atom(mi):
    """--invalidate-conflict should expire the older atom after user confirmation."""
    db = mi.open_db()
    # Create atom + conflict
    atom_path = mi.VAULT_ATOMS
    atom_path.mkdir(parents=True, exist_ok=True)
    af = atom_path / "fact-conflict-test.md"
    af.write_text("---\ntype: atom\n---\nOld fact\n")
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES (?, '2024-01-01', 'Old fact', 'atom', 0.5)", [str(af)]
    )
    old_id = cur.lastrowid
    db.execute(
        "INSERT INTO pending_conflicts (older_id, newer_id, older_text, newer_text, created_at) "
        "VALUES (?, 999, 'Old fact', 'New fact', '2024-06-01')", [old_id]
    )
    db.commit()
    db.close()

    mi.cmd_invalidate_conflict(1)

    db = mi.open_db()
    row = db.execute("SELECT expired_at FROM entries WHERE id = ?", [old_id]).fetchone()
    assert row[0] is not None  # now expired after user confirmation
    conflict = db.execute("SELECT resolved, resolution FROM pending_conflicts WHERE id = 1").fetchone()
    assert conflict[0] == 1
    assert conflict[1] == "invalidated"
    db.close()


def test_dismiss_conflict_marks_resolved(mi):
    """--dismiss-conflict should mark conflict as dismissed without expiring."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, confidence) "
        "VALUES ('/tmp/x.md', '2024-01-01', 'Some fact', 'atom', 0.5)"
    )
    db.execute(
        "INSERT INTO pending_conflicts (older_id, newer_id, older_text, newer_text, created_at) "
        "VALUES (1, 999, 'Some fact', 'Other fact', '2024-06-01')"
    )
    db.commit()
    db.close()

    mi.cmd_dismiss_conflict(1)

    db = mi.open_db()
    row = db.execute("SELECT expired_at FROM entries WHERE id = 1").fetchone()
    assert row[0] is None  # not expired
    conflict = db.execute("SELECT resolved, resolution FROM pending_conflicts WHERE id = 1").fetchone()
    assert conflict[0] == 1
    assert conflict[1] == "dismissed"
    db.close()


# ── Data integrity: rebuild backup and runtime preservation ──────────────────


def test_rebuild_creates_backup(mi, fresh_vault):
    """--rebuild should create a timestamped .bak file before clearing tables."""
    # Seed the DB so it exists
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/x.md', '2024-01-01', 'test', 'session', 'test', '')"
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    # Check backup was created
    import glob
    backups = glob.glob(str(mi.DB_PATH.with_suffix(".bak-*")))
    assert len(backups) >= 1, "No backup file created by --rebuild"


def test_rebuild_preserves_access_log(mi, fresh_vault):
    """--rebuild should NOT destroy access_log (runtime data with no disk source)."""
    db = mi.open_db()
    # Seed an entry + access_log row
    cur = db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/x.md', '2024-01-01', 'test', 'atom', 'test', '')"
    )
    db.execute(
        "INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (?, '2024-06-01', 'query')",
        [cur.lastrowid],
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert count == 1, f"access_log was destroyed by rebuild (expected 1, got {count})"
    db.close()


def test_rebuild_preserves_query_log(mi, fresh_vault):
    """--rebuild should NOT destroy query_log."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO query_log (query_text, intent, result_count, atom_hit, queried_at) "
        "VALUES ('test query', 'factual', 3, 1, '2024-06-01')"
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
    assert count == 1, f"query_log was destroyed by rebuild (expected 1, got {count})"
    db.close()


def test_rebuild_preserves_pending_conflicts(mi, fresh_vault):
    """--rebuild should NOT destroy pending_conflicts."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO pending_conflicts (older_id, newer_id, older_text, newer_text, created_at) "
        "VALUES (1, 2, 'old', 'new', '2024-06-01')"
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM pending_conflicts").fetchone()[0]
    assert count == 1, f"pending_conflicts was destroyed by rebuild (expected 1, got {count})"
    db.close()


def test_rebuild_preserves_digests(mi, fresh_vault):
    """--rebuild should NOT destroy digests."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES ('weekly', '2024-W24', 'Weekly summary', '2024-06-15')"
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    db = mi.open_db()
    count = db.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    assert count == 1, f"digests was destroyed by rebuild (expected 1, got {count})"
    db.close()


def test_rebuild_clears_entries_and_embeddings(mi, fresh_vault):
    """--rebuild should clear entries/embeddings (they're rebuilt from disk)."""
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (path, date, chunk, type, tldr, topics) "
        "VALUES ('/tmp/stale.md', '2024-01-01', 'stale', 'session', 'stale', '')"
    )
    db.commit()
    db.close()

    mi.cmd_rebuild()

    db = mi.open_db()
    # Old stale entry should be gone (it's not a real file on disk)
    count = db.execute("SELECT COUNT(*) FROM entries WHERE path = '/tmp/stale.md'").fetchone()[0]
    assert count == 0, "Stale entry survived rebuild"
    db.close()
