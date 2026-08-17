"""Regression tests for the real-database guard in conftest.py (LIA-555).

The guard exists because the test suite really did write to the user's live
~/.deus/evolution.db. These tests prove it fires, prove it survives the specific
call that caused the original incident, and prove it does not fire on the
in-memory and tmp paths that ordinary tests use.

Every "must be blocked" case here asserts that the guard raises *before*
sqlite3 opens anything, so running this file never touches a real database.
"""
import os
import sqlite3
from pathlib import Path

import pytest

import evolution.config as config_mod
from evolution import health

from .conftest import (
    RealDatabaseAccess,
    _REAL_DBS,
    _REAL_EVOLUTION_DB,
    _REAL_LEGACY_DB,
    _is_real_db,
    _same_inode,
    _target_path,
)

# The path conftest actually guards. Not hardcoded: an ambient
# DEUS_EVOLUTION_DB in the developer's shell moves it, and these tests must
# still describe the real guard rather than a path it no longer covers.
REAL_EVOLUTION_DB = _REAL_EVOLUTION_DB


# ── Layer 1 fires ─────────────────────────────────────────────────────────────


def test_connecting_to_the_real_db_fails_the_test():
    with pytest.raises(RealDatabaseAccess) as exc:
        sqlite3.connect(str(REAL_EVOLUTION_DB))

    message = str(exc.value)
    assert str(REAL_EVOLUTION_DB) in message, "the offending path must be named"
    assert "test_connecting_to_the_real_db_fails_the_test" in message, (
        "the failing test must be named, so a suite-wide run points at the culprit"
    )


def test_guard_survives_monkeypatch_undo(monkeypatch):
    """The LIA-551 shape: undo() reverses every patch on this test's monkeypatch
    instance. If the guard were installed through that same fixture it would be
    torn down here, which is exactly when it is most needed."""
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", Path("/tmp/decoy.db"))
    monkeypatch.undo()

    with pytest.raises(RealDatabaseAccess):
        sqlite3.connect(str(REAL_EVOLUTION_DB))


def test_mid_test_unredirect_through_production_code_is_caught(test_db, monkeypatch):
    """The literal LIA-551 bug, end to end.

    undo() reverses test_db's redirect, so health.record_attempt resolves the
    real EVOLUTION_DB_PATH and would write an OK row into production -- clearing
    consecutive_failures and first_failed_at. The guard has to stop it inside
    the production call, not at fixture setup.
    """
    health.record_attempt("guard-probe", health.STATUS_OK)  # redirected: fine

    monkeypatch.undo()
    restored = Path(config_mod.EVOLUTION_DB_PATH).expanduser().resolve()
    assert restored == _REAL_EVOLUTION_DB, (
        "precondition: undo() really did restore the production path"
    )

    # record_attempt wraps its whole body in `except Exception` (health.py:150).
    # RealDatabaseAccess derives from BaseException precisely so that handler
    # cannot turn this into a green test with a log line nobody reads.
    with pytest.raises(RealDatabaseAccess, match="real database"):
        health.record_attempt("guard-probe", health.STATUS_OK)


# ── Alternate spellings of a real path ────────────────────────────────────────
#
# str() vs os.fsdecode, URI vs plain, and percent-encoding are all ways the same
# real path can arrive at sqlite3.connect. A guard that only recognises one of
# them is a guard with a hole.


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(lambda p: str(p), id="plain-str"),
        pytest.param(lambda p: p, id="path-object"),
        pytest.param(lambda p: "~/.deus/evolution.db", id="tilde"),
        pytest.param(lambda p: os.fsencode(str(p)), id="bytes"),
        pytest.param(lambda p: f"file:{p}?mode=ro", id="file-uri-with-query"),
        pytest.param(lambda p: f"file://{p}", id="file-uri-empty-authority"),
        pytest.param(lambda p: f"file://localhost{p}", id="file-uri-localhost"),
        pytest.param(
            lambda p: "file:" + str(p).replace(".db", "%2edb"),
            id="percent-encoded",
        ),
    ],
)
def test_every_spelling_of_the_real_path_is_blocked(spelling):
    assert _is_real_db(spelling(REAL_EVOLUTION_DB)) is True


def test_case_different_spelling_is_blocked_on_a_case_insensitive_filesystem():
    """~/.Deus and ~/.deus are one inode on macOS, so a case-mangled spelling
    would reach the real file while failing a literal path comparison.
    os.path.normcase does not help -- it is a no-op on POSIX -- so the guard
    compares (st_dev, st_ino) as well.

    Skipped where the filesystem really is case-sensitive (CI's ext4), because
    there the alias is a genuinely different file and must NOT be blocked.
    """
    aliased = Path(str(REAL_EVOLUTION_DB).replace("/.deus/", "/.Deus/"))
    if aliased == REAL_EVOLUTION_DB:
        pytest.skip("guarded path does not contain a '.deus' segment to re-case")
    if not aliased.exists():
        pytest.skip("case-sensitive filesystem: the alias is a different file")

    assert _is_real_db(str(aliased)) is True


def test_same_inode_matches_regardless_of_path_spelling(tmp_path, monkeypatch):
    """Filesystem-independent cover for the inode branch.

    The test above only exercises it on a case-insensitive filesystem, so it
    skips on CI's ext4. This drives _same_inode directly with two distinct paths
    reporting one identity, which is what an alias looks like from stat().
    """
    left, right = tmp_path / "a.db", tmp_path / "b.db"
    left.touch()
    right.touch()
    assert _same_inode(left, right) is False, "distinct files must not match"

    shared = os.stat_result((0o100644, 4242, 99, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "stat", lambda self, **kw: shared)
    assert _same_inode(left, right) is True


def test_same_inode_is_false_when_a_path_is_missing(tmp_path):
    """stat() raises on a path that does not exist -- the CI case, where
    ~/.deus is absent entirely. The literal comparison still covers it."""
    existing = tmp_path / "here.db"
    existing.touch()
    assert _same_inode(existing, tmp_path / "gone.db") is False


def test_the_whole_deus_directory_is_guarded():
    """Not just the two configured databases -- everything under ~/.deus is real
    user data."""
    assert _is_real_db(str(Path("~/.deus/memory_tree.db").expanduser())) is True


def test_a_sibling_directory_is_not_confused_for_the_real_one():
    """Guards against a naive string-prefix check: ~/.deusX is not ~/.deus."""
    assert _is_real_db(str(Path("~/.deusX/evolution.db").expanduser())) is False


# ── No false positives ────────────────────────────────────────────────────────


def test_tmp_paths_connect_normally(tmp_path):
    db = sqlite3.connect(tmp_path / "fine.db")
    db.execute("CREATE TABLE t (x)")
    db.close()
    assert (tmp_path / "fine.db").exists()


@pytest.mark.parametrize(
    "target", [":memory:", "file::memory:?cache=shared", "", None]
)
def test_non_filesystem_targets_are_ignored(target):
    assert _target_path(target) is None
    assert _is_real_db(target) is False


def test_in_memory_databases_still_work():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (x)")
    db.close()


# ── Layer 0 ───────────────────────────────────────────────────────────────────


def test_subprocess_env_points_away_from_the_real_databases():
    """A child interpreter re-reads these env vars, so they are what stands
    between a spawned `python -m evolution.cli ...` and production."""
    for var in ("DEUS_EVOLUTION_DB", "DEUS_DB"):
        value = os.environ.get(var)
        assert value, f"{var} must be set for the whole test session"
        assert not _is_real_db(value), f"{var} still points at real user data: {value}"


def test_guarded_paths_were_snapshotted_before_the_env_was_isolated():
    """Layer 0 rewrites DEUS_* at session start; the guarded set is captured at
    conftest import, which happens first. If that order ever inverted, the
    guarded set would hold the tmp paths and protect nothing."""
    session_tmp = {
        Path(os.environ["DEUS_EVOLUTION_DB"]).parent.resolve(),
        Path(os.environ["DEUS_DB"]).parent.resolve(),
    }
    assert _REAL_DBS == {_REAL_EVOLUTION_DB, _REAL_LEGACY_DB}
    for path in _REAL_DBS:
        assert path.parent.resolve() not in session_tmp, (
            f"{path} is a Layer 0 tmp path -- the snapshot ran too late"
        )
        assert _is_real_db(str(path)) is True
