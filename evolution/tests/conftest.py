"""
Shared test fixtures for evolution tests.

test_db redirects EVOLUTION_DB_PATH for both evolution.db and evolution.config,
and DB_PATH so the legacy migration cannot reach the real memory.db.

Two autouse layers then keep the whole package off the real ~/.deus databases
(LIA-555): Layer 0 points the DEUS_* env vars at a session tmp dir so a
subprocess a test spawns inherits safe paths, and Layer 1 wraps sqlite3.connect
so an in-process open of a real database fails at the moment it happens rather
than at fixture setup. See the PR for the incident that motivated each.
"""
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

import evolution.config as config_mod
import evolution.db as db_mod

# ── Guarded real paths ────────────────────────────────────────────────────────
#
# Snapshotted at import, which happens before any fixture runs and so before
# Layer 0 rewrites the environment. That ordering is deliberate: these have to
# be the paths production would really use, including any DEUS_DB /
# DEUS_EVOLUTION_DB override already present in the environment.

_REAL_DEUS_DIR = Path("~/.deus").expanduser().resolve()
_REAL_EVOLUTION_DB = Path(config_mod.EVOLUTION_DB_PATH).expanduser().resolve()
_REAL_LEGACY_DB = Path(config_mod.DB_PATH).expanduser().resolve()
_REAL_DBS = frozenset({_REAL_EVOLUTION_DB, _REAL_LEGACY_DB})


class RealDatabaseAccess(BaseException):
    """Raised when a test tries to open a real ~/.deus database.

    BaseException, not Exception: non-test evolution/ code has 66
    `except Exception` handlers -- health.record_attempt (health.py:150) among
    them, on the exact path the LIA-551 incident took -- and any of them would
    catch this and let the test pass green. Same reasoning as KeyboardInterrupt.
    """


def _target_path(target):
    """Normalise a sqlite3.connect() target to a filesystem path string.

    Returns None when the target is not a filesystem path at all (in-memory,
    empty).

    os.fsdecode rather than str(): sqlite3.connect accepts a bytes path, and
    str(b"/x/y.db") is "b'/x/y.db'" -- a string that could never match a
    guarded path, so a bytes-spelled real path would slip straight through.
    """
    if not target:
        return None
    raw = os.fsdecode(target)
    if raw.startswith("file:"):
        # Drop the query string ("?mode=ro") and decode percent-escapes so a
        # URI spelling of a real path is checked like any other path. Note
        # "file::memory:?cache=shared" extracts to the literal ":memory:",
        # which is why the next check is a membership test rather than an
        # emptiness test.
        raw = unquote(urlsplit(raw).path)
    if raw in ("", ":memory:"):
        return None
    return raw


def _same_inode(a: Path, b: Path) -> bool:
    """True when two paths name the same file on disk, whatever they are called.

    Catches aliases string comparison misses: a case-different spelling on a
    case-insensitive filesystem (macOS resolves ~/.Deus and ~/.deus to one
    inode; os.path.normcase does not help, it is a no-op on POSIX), and hard
    links. Both paths must exist, so this supplements the literal check rather
    than replacing it -- on CI, ~/.deus does not exist at all.
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _is_real_db(target) -> bool:
    """True when a connect target resolves inside the real ~/.deus tree.

    resolve() normalises relative paths, "~", and symlinks; _same_inode covers
    the aliases it cannot see.
    """
    raw = _target_path(target)
    if raw is None:
        return False
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        # An unresolvable target is not a real-DB write. Let sqlite3 raise its
        # own error rather than replacing it with a confusing guard failure.
        return False
    if (
        resolved in _REAL_DBS
        or resolved == _REAL_DEUS_DIR
        or _REAL_DEUS_DIR in resolved.parents
    ):
        return True
    if any(_same_inode(resolved, db) for db in _REAL_DBS):
        return True
    return any(
        _same_inode(candidate, _REAL_DEUS_DIR)
        for candidate in (resolved, *resolved.parents)
    )


# ── Layer 0: keep subprocesses off the real databases ─────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _isolate_deus_env(tmp_path_factory):
    """Point the DEUS_* database env vars at a session tmp dir.

    A spawned `python -m evolution.cli ...` re-imports evolution.config and
    reads these itself; the parent's monkeypatched module attributes mean
    nothing to it. Deliberately does not rewrite this process's already-imported
    config.EVOLUTION_DB_PATH -- that would absolve in-process tests that forget
    the test_db fixture, and make them share one database. Layer 1 catches those.
    """
    env_dir = tmp_path_factory.mktemp("deus_env_isolation")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DEUS_EVOLUTION_DB", str(env_dir / "evolution.db"))
        mp.setenv("DEUS_DB", str(env_dir / "memory.db"))
        yield env_dir


# ── Layer 1: refuse a real-DB connect at the moment it happens ────────────────


@pytest.fixture(autouse=True)
def _forbid_real_db_connect(request):
    """Fail any test that opens a real ~/.deus database.

    Installed by hand, not via the monkeypatch fixture: monkeypatch is
    function-scoped and shared across a test's fixtures, so monkeypatch.undo()
    -- the LIA-551 bug itself -- would tear down this guard along with the
    redirect it backstops.
    """
    original = sqlite3.connect

    def guarded(database, *args, **kwargs):
        if _is_real_db(database):
            raise RealDatabaseAccess(
                f"{request.node.nodeid} tried to open the real database "
                f"{os.fsdecode(database)!r}.\n"
                "Evolution tests must never touch ~/.deus -- a write there can "
                "destroy real state (LIA-551 zeroed a genuine failure streak "
                "this way).\n"
                "Add the `test_db` fixture, or redirect the path yourself. If "
                "this fired right after monkeypatch.undo(), that call reversed "
                "the test_db redirect: use monkeypatch.context() instead."
            )
        return original(database, *args, **kwargs)

    sqlite3.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = original


# ── Per-test database redirect ────────────────────────────────────────────────


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Redirect EVOLUTION_DB_PATH to a temp file for both db.py and the storage provider."""
    test_db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db_path)
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db_path)
    # Prevent legacy migration from accessing the real memory.db
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")
    return test_db_path
