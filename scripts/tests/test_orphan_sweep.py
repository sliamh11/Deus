"""Tests for the LIA-143 Layer 3 hybrid orphan-sweep (scripts/maintenance/orphan_sweep.py).

Hermetic: each test builds a temp git repo (so `git grep` is isolated) plus a temp
SQLite DB with a codegraph-compatible {nodes, edges} schema. The two signals are
exercised independently to prove the dual-evidence AND-logic:
  - a codegraph caller alone suppresses a flag (even when grep would miss it),
  - a grep cross-file hit alone suppresses a flag (even when codegraph misses the edge —
    the createMessageOrchestrator case that motivated the hybrid),
  - only BOTH-negative yields an orphan.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_MOD_PATH = _ROOT / "scripts" / "maintenance" / "orphan_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("orphan_sweep", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orphan_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


sweep = _load()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    for path, content in files.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_db(repo: Path, nodes: list[dict], edges: list[tuple]) -> Path:
    db = repo / ".codegraph" / "codegraph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, file_path TEXT, is_exported INTEGER)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, kind TEXT)")
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path, is_exported) VALUES (?,?,?,?,?)",
            (n["id"], n["kind"], n["name"], n["file_path"], n.get("is_exported", 1)),
        )
    for src, tgt, kind in edges:
        conn.execute("INSERT INTO edges (source, target, kind) VALUES (?,?,?)", (src, tgt, kind))
    conn.commit()
    conn.close()
    return db


def _run(repo: Path, modules: list[str]):
    db = repo / ".codegraph" / "codegraph.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return sweep.find_orphans(conn, modules, str(repo))
    finally:
        conn.close()


# --- dual-evidence truth table -------------------------------------------


def test_codegraph_caller_suppresses_even_when_grep_misses(tmp_path):
    # foo has a codegraph caller from another non-test file, but its name does NOT
    # textually appear there → codegraph signal alone must suppress the flag.
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function foo() {}\n", "src/caller.ts": "doStuff();\n"})
    _make_db(
        repo,
        nodes=[
            {"id": "fn:foo", "kind": "function", "name": "foo", "file_path": "src/mod.ts"},
            {"id": "fn:caller", "kind": "function", "name": "doStuff", "file_path": "src/caller.ts"},
        ],
        edges=[("fn:caller", "fn:foo", "calls")],
    )
    assert _run(repo, ["src/mod.ts"]) == []


def test_grep_suppresses_when_codegraph_edge_missing(tmp_path):
    # The createMessageOrchestrator case: no codegraph caller edge, but the name IS
    # used cross-file → grep signal must suppress the flag.
    repo = _make_repo(
        tmp_path,
        {"src/mod.ts": "export function makeThing() {}\n", "src/index.ts": "const t = makeThing();\n"},
    )
    _make_db(repo, nodes=[{"id": "fn:mk", "kind": "function", "name": "makeThing", "file_path": "src/mod.ts"}], edges=[])
    assert _run(repo, ["src/mod.ts"]) == []


def test_both_negative_is_orphan(tmp_path):
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function lonelyExport() {}\n", "src/index.ts": "noop();\n"})
    _make_db(repo, nodes=[{"id": "fn:lo", "kind": "function", "name": "lonelyExport", "file_path": "src/mod.ts"}], edges=[])
    orphans = _run(repo, ["src/mod.ts"])
    assert len(orphans) == 1
    assert orphans[0].name == "lonelyExport"
    assert orphans[0].file_path == "src/mod.ts"


# --- exclusions ----------------------------------------------------------


def test_type_export_not_flagged(tmp_path):
    # An unused exported interface is dead-type cleanup, not a runtime facade.
    repo = _make_repo(tmp_path, {"src/mod.ts": "export interface Unused {}\n", "src/index.ts": "noop();\n"})
    _make_db(repo, nodes=[{"id": "if:u", "kind": "interface", "name": "Unused", "file_path": "src/mod.ts"}], edges=[])
    assert _run(repo, ["src/mod.ts"]) == []


def test_self_and_test_callers_do_not_count(tmp_path):
    # Used only within its own file + a test file → still an orphan (no runtime caller).
    repo = _make_repo(
        tmp_path,
        {
            "src/mod.ts": "export function selfUsed() {}\nselfUsed();\n",
            "src/mod.test.ts": "selfUsed();\n",
        },
    )
    _make_db(
        repo,
        nodes=[
            {"id": "fn:su", "kind": "function", "name": "selfUsed", "file_path": "src/mod.ts"},
            {"id": "fn:t", "kind": "function", "name": "tcase", "file_path": "src/mod.test.ts"},
        ],
        edges=[("fn:su", "fn:su", "calls"), ("fn:t", "fn:su", "calls")],
    )
    orphans = _run(repo, ["src/mod.ts"])
    assert len(orphans) == 1
    assert orphans[0].name == "selfUsed"


# --- stale-index warning -------------------------------------------------


def test_index_stale_detection(tmp_path):
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function foo() {}\n"})
    db = _make_db(repo, nodes=[], edges=[])
    # db is newer than source right after creation → not stale.
    assert sweep.index_looks_stale(db, ["src/mod.ts"], str(repo)) is False
    # touch the source to be newer than the db → stale.
    import os, time

    future = db.stat().st_mtime + 100
    os.utime(repo / "src/mod.ts", (future, future))
    assert sweep.index_looks_stale(db, ["src/mod.ts"], str(repo)) is True


def test_missing_db_reports_setup_error(tmp_path, capsys):
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function foo() {}\n"})
    rc = sweep.main(["--repo", str(repo), "--modules", str(_MOD_PATH.with_name("orphan_sweep_modules.txt"))])
    assert rc == 2
    assert "codegraph index not found" in capsys.readouterr().err


def test_bad_modules_file_returns_setup_error(tmp_path, capsys):
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function foo() {}\n"})
    _make_db(repo, nodes=[], edges=[])  # DB must exist so the db-not-found guard doesn't fire first
    rc = sweep.main(["--repo", str(repo), "--modules", str(repo / "does-not-exist.txt")])
    assert rc == 2
    assert "cannot read modules file" in capsys.readouterr().err


def test_print_launchd_emits_plist(tmp_path, capsys):
    rc = sweep.main(["--print-launchd", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "com.deus.orphan-sweep" in out
    assert "orphan_sweep.py" in out


def test_strict_exit_code_on_orphan(tmp_path):
    repo = _make_repo(tmp_path, {"src/mod.ts": "export function lonelyExport() {}\n"})
    _make_db(repo, nodes=[{"id": "fn:lo", "kind": "function", "name": "lonelyExport", "file_path": "src/mod.ts"}], edges=[])
    modules_file = repo / "mods.txt"
    modules_file.write_text("src/mod.ts\n")
    rc = sweep.main(["--repo", str(repo), "--modules", str(modules_file), "--strict"])
    assert rc == 1
