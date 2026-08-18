"""A skipped principles extraction must not be recorded as healthy.

`extract_principles` returns None on two entirely ordinary paths — too few new
scored interactions since the last run, and fewer than 3 usable examples once
empty responses are filtered out (`evolution/reflexion/principles.py`). The
caller used to discard the return value and record STATUS_OK unconditionally,
so a cycle that extracted nothing marked a never-attempted component healthy
and cleared any genuine FAILED streak with it — the exact "silent failure looks
healthy" shape LIA-551/LIA-556 exist to prevent. `health.record_attempt` has no
skipped status by design, so the fix records a genuine skip through
`health.record_skip` instead — it touches only `last_skipped_at`, leaving
`last_status` SQL NULL, so the component reads as never-attempted rather than
healthy while still proving the cycle ran. Recording nothing at all would make
a component that has legitimately skipped for months look identical to one that
silently stopped being called.
"""
import pytest

from evolution import cli, health

# Both imported for their side effect, before any fixture below patches
# `evolution.storage.get_storage`. Each binds a storage helper at MODULE level
# (`evolution/ilog/interaction_log.py:11`, `evolution/reflexion/principles.py:18`),
# so whichever test first triggers the import captures whatever is bound at that
# moment — and a stub captured there survives monkeypatch teardown for the rest
# of the process, breaking unrelated tests later in the run. Importing them here
# means the real functions are what get bound. Same reasoning as the
# interaction_log import in test_evolution_silent_swallows.py.
from evolution.ilog import interaction_log as _interaction_log  # noqa: F401
from evolution.reflexion import principles as _principles  # noqa: F401


CODING = cli._PRINCIPLES_HEALTH_PREFIX + "coding"


@pytest.fixture
def principles(monkeypatch):
    """Wire _maybe_auto_extract_principles so nothing real is touched.

    Returns a setter taking the extract callable; the store reports no prior
    extraction, so the cooldown never short-circuits the call under test.
    """
    def _wire(extract):
        store = type("S", (), {"get_last_extraction": lambda self, d: None})()
        monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: store)
        monkeypatch.setattr("evolution.reflexion.principles.extract_principles", extract)
    return _wire


def test_skip_records_no_attempt(test_db, principles):
    """The discriminating case: None back means nothing ran, so the component
    must stay never-attempted rather than being written as OK."""
    principles(lambda domain=None: None)

    cli._maybe_auto_extract_principles(["coding"])

    # Every domain in the preset list plus the implicit cross-domain pass.
    for component in (CODING, cli._PRINCIPLES_HEALTH_PREFIX + "cross-domain"):
        row = health.get(component)
        # A row exists because the cycle genuinely ran, but `last_status` stays
        # SQL NULL: never-attempted, which `_STATUS_RANK` sorts ABOVE OK in the
        # rollup precisely so it cannot hide behind healthy siblings.
        assert row is not None, component
        assert row["last_status"] is None, component
        assert row["last_attempt_at"] is None, component
        assert row["consecutive_failures"] == 0, component
        # The skip timestamp is the whole point: it distinguishes "ran and had
        # nothing to do" from "stopped being called at all".
        assert row["last_skipped_at"] is not None, component


def test_skip_does_not_clear_an_existing_failure_streak(test_db, principles):
    """A skip must not launder a real failure into health — OK is the only
    thing that resets `consecutive_failures`."""
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))
    cli._maybe_auto_extract_principles(["coding"])
    assert health.get(CODING)["consecutive_failures"] == 1

    principles(lambda domain=None: None)  # skipped, not recovered
    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(CODING)
    assert row["last_status"] == health.STATUS_FAILED
    assert row["consecutive_failures"] == 1
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is True


def test_real_extraction_records_ok(test_db, principles):
    principles(lambda domain=None: "1. Be concrete.")

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(CODING)
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0


def test_real_extraction_still_clears_a_failure_streak(test_db, principles):
    """The LIA-556 site-2 intent: the OK write happens on every real success,
    not only on recovery, which is what lets a resolved failure self-heal."""
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))
    cli._maybe_auto_extract_principles(["coding"])
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is True

    principles(lambda domain=None: "1. Be concrete.")
    cli._maybe_auto_extract_principles(["coding"])

    assert health.get(CODING)["last_status"] == health.STATUS_OK
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is False


def test_exception_still_records_failed(test_db, principles):
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))

    cli._maybe_auto_extract_principles(["coding"])  # must not raise

    row = health.get(CODING)
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]
    assert row["first_failed_at"] is not None


def test_empty_generation_is_a_failure_not_a_skip(test_db, principles):
    """Falsy-but-not-None is a real attempt that produced nothing.

    `extract_principles` returns the raw generation, and returns it AFTER
    `_record_extraction` has run — the cooldown is consumed and the tokens are
    spent. Filing that as a skip would hide a provider returning empty behind
    the same silence the None branch exists to remove.
    """
    principles(lambda domain=None: "   ")

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(CODING)
    assert row["last_status"] == health.STATUS_FAILED
    assert row["last_reason"] == "empty generation"
    assert row["consecutive_failures"] == 1
    # A failed attempt is an attempt: it must NOT be recorded as a skip.
    assert row["last_skipped_at"] is None


def test_empty_generation_does_not_clear_a_failure_streak(test_db, principles):
    """The streak keeps counting rather than resetting on an empty result."""
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))
    cli._maybe_auto_extract_principles(["coding"])
    assert health.get(CODING)["consecutive_failures"] == 1

    principles(lambda domain=None: "")
    cli._maybe_auto_extract_principles(["coding"])

    assert health.get(CODING)["consecutive_failures"] == 2
