"""Tests for LIA-200 warden-backup pruning (scripts/maintenance/prune_warden_backups.py).

Hermetic: every test builds a throwaway `.claude` tree under pytest's tmp_path,
so nothing touches the real repo. Backup filenames use the production stamp
format `{name}.bak-<14 digits>`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "prune_warden_backups.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("prune_warden_backups", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prune_warden_backups"] = mod
    spec.loader.exec_module(mod)
    return mod


pwb = _load()


def _mk_backups(dir_: Path, store: str, stamps: list[str]) -> list[Path]:
    """Create `{store}.bak-<stamp>` files and return them."""
    dir_.mkdir(parents=True, exist_ok=True)
    made = []
    for s in stamps:
        p = dir_ / f"{store}.bak-{s}"
        p.write_text("verdict-snapshot")
        made.append(p)
    return made


def test_keep_newest_n_lexicographic_is_chronological(tmp_path: Path):
    claude = tmp_path / ".claude"
    # 5 backups; lexicographic order on the fixed-width stamp == chronological.
    stamps = [
        "20260601000000", "20260605120000", "20260609083702",
        "20260609103834", "20260609165549",
    ]
    _mk_backups(claude, ".warden-verdicts.json", stamps)

    deleted, kept = pwb.prune(claude, keep=2, dry_run=False, verbose=False)
    assert (deleted, kept) == (3, 2)
    survivors = sorted(p.name for p in claude.glob("*.bak-*"))
    # The two newest stamps survive.
    assert survivors == [
        ".warden-verdicts.json.bak-20260609103834",
        ".warden-verdicts.json.bak-20260609165549",
    ]


def test_groups_by_store_path_not_just_basename(tmp_path: Path):
    claude = tmp_path / ".claude"
    # Same basename (.warden-verdicts.json) in three different dirs = three groups.
    _mk_backups(claude, ".warden-verdicts.json",
                ["20260609000001", "20260609000002", "20260609000003"])
    _mk_backups(claude / "worktree-markers" / "abc123def456",
                ".warden-verdicts.json",
                ["20260609000001", "20260609000002", "20260609000003"])
    _mk_backups(claude / "worktrees" / "feat-x" / ".claude",
                ".warden-verdicts.json",
                ["20260609000001", "20260609000002", "20260609000003"])

    deleted, kept = pwb.prune(claude, keep=1, dry_run=False, verbose=False)
    # keep=1 PER group across 3 groups → 3 kept, 6 deleted.
    assert (deleted, kept) == (6, 3)
    assert len(list(claude.glob("*.bak-*"))) == 1
    assert len(list((claude / "worktree-markers" / "abc123def456").glob("*.bak-*"))) == 1
    assert len(list(
        (claude / "worktrees" / "feat-x" / ".claude").glob("*.bak-*"))) == 1


def test_all_three_families_discovered(tmp_path: Path):
    claude = tmp_path / ".claude"
    flat = _mk_backups(claude, ".warden-verdicts.json", ["20260609000001"])
    marker = _mk_backups(claude / "worktree-markers" / "deadbeef0000",
                         "hooks.json", ["20260609000001"])
    wt = _mk_backups(claude / "worktrees" / "br" / ".claude",
                     ".warden-verdicts.json", ["20260609000001"])
    found = {p.resolve() for p in pwb.find_backups(claude)}
    assert found == {p.resolve() for p in (flat + marker + wt)}


def test_never_touches_live_store_or_tempfiles(tmp_path: Path):
    claude = tmp_path / ".claude"
    _mk_backups(claude, ".warden-verdicts.json",
                ["20260609000001", "20260609000002", "20260609000003"])
    live = claude / ".warden-verdicts.json"
    live.write_text("LIVE")
    log = claude / ".warden-log"
    log.write_text("audit")
    # A tempfile left by an interrupted _write_atomic (random name, no stamp).
    tmpf = claude / "tmp_abc123"
    tmpf.write_text("partial")
    # A near-miss: ".bak-" with a non-14-digit suffix must NOT match.
    near = claude / ".warden-verdicts.json.bak-2026"
    near.write_text("not a real backup")

    pwb.prune(claude, keep=0, dry_run=False, verbose=False)
    assert live.read_text() == "LIVE"  # live store untouched
    assert log.exists()  # unrelated state file untouched
    assert tmpf.exists()  # in-flight tempfile untouched
    assert near.exists()  # ".bak-2026" (non-14-digit) is NOT a backup → kept
    # keep=0 means every REAL (14-digit-stamped) backup is gone.
    assert list(claude.glob("*.warden-verdicts.json.bak-2026[0-9]*")) == []


def test_dry_run_deletes_nothing(tmp_path: Path):
    claude = tmp_path / ".claude"
    made = _mk_backups(claude, ".warden-verdicts.json",
                       ["20260609000001", "20260609000002", "20260609000003"])
    deleted, kept = pwb.prune(claude, keep=1, dry_run=True, verbose=False)
    assert deleted == 2 and kept == 1
    assert all(p.exists() for p in made)  # nothing actually removed


def test_concurrent_removal_is_tolerated(tmp_path: Path, monkeypatch):
    claude = tmp_path / ".claude"
    _mk_backups(claude, ".warden-verdicts.json",
                ["20260609000001", "20260609000002", "20260609000003"])
    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError(self)  # simulate a racing writer
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    # Must not raise; counts the survivable unlink, swallows the FileNotFoundError.
    deleted, kept = pwb.prune(claude, keep=1, dry_run=False, verbose=False)
    assert kept == 1
    assert deleted == 1  # one real removal; the racing one was swallowed


def test_missing_claude_dir_is_success(tmp_path: Path):
    rc = pwb.main(["--repo", str(tmp_path), "--keep", "5"])
    assert rc == 0


def test_main_negative_keep_rejected(tmp_path: Path):
    rc = pwb.main(["--repo", str(tmp_path), "--keep", "-1"])
    assert rc == 1


def test_main_end_to_end_real_layout(tmp_path: Path):
    claude = tmp_path / ".claude"
    _mk_backups(claude, ".warden-verdicts.json",
                [f"202606090000{i:02d}" for i in range(15)])
    rc = pwb.main(["--repo", str(tmp_path), "--keep", "10"])
    assert rc == 0
    assert len(list(claude.glob("*.bak-*"))) == 10
