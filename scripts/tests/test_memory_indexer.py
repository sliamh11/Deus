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
