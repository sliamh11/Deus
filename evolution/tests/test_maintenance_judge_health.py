"""Health recording for the batch judge (LIA-556 site 1).

Before this, judge_pending_interactions() returned 0 for "nothing pending",
"the judge would not construct", and "every row failed to score" alike, writing
nothing durable for any of them. These tests pin each outcome to a distinct
health record, and pin the one case the design deliberately cannot cover.
"""
import pathlib
import subprocess
import sys

import pytest

from evolution import health, maintenance

COMPONENT = maintenance._BATCH_COMPONENT
PREFIX = maintenance._JUDGE_HEALTH_PREFIX


class _Result:
    """Minimal stand-in for a judge result. Only the fields _score_single reads."""

    def __init__(self, score=0.8, is_parse_error=False, model="gemma4:e4b"):
        self.score = score
        self.quality = self.safety = self.tool_use = self.personalization = score
        self.rationale = "r"
        self.is_parse_error = is_parse_error
        self.schema_version = 1
        # LIA-558: _score_single now forwards the judge model to update_score.
        self.model = model
        # LIA-580: _score_single checks this before reading any score field.
        self.is_schema_error = False


def _row(rid="i1"):
    return {"id": rid, "prompt": "p", "response": "r", "group_folder": None,
            "tools_used": None}


@pytest.fixture
def wire(monkeypatch):
    """Wire the batch judge's collaborators so no network or real DB is touched.

    Returns a setter so each test states only what it varies.
    """
    def _wire(rows, evaluate=None, reflect=True):
        store = type("S", (), {"get_unjudged_interactions": lambda self, limit=50: list(rows)})()
        monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: store)
        judge = type("J", (), {"evaluate": staticmethod(evaluate or (lambda **kw: _Result()))})()
        monkeypatch.setattr("evolution.judge.make_runtime_judge", lambda *a, **k: judge)
        monkeypatch.setattr("evolution.ilog.interaction_log.update_score",
                            lambda *a, **k: None)
        monkeypatch.setattr("evolution.persona.digest_for_group", lambda *a, **k: "")
        monkeypatch.setattr(maintenance, "_reflect_single", lambda s, c: reflect)
        return store
    return _wire


# ── Failure is distinguishable from "nothing to do" ───────────────────────────


def test_judge_construction_failure_records_failed(test_db, wire, monkeypatch):
    wire([_row()])
    monkeypatch.setattr("evolution.judge.make_runtime_judge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))

    assert maintenance.judge_pending_interactions() == 0, "hot path must not raise"

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]
    assert "no model" in row["last_reason"]


def test_a_recorded_failure_reaches_has_failure(test_db, wire, monkeypatch):
    """The row needs a consumer, or it is just another thing nobody reads."""
    wire([_row()])
    monkeypatch.setattr("evolution.judge.make_runtime_judge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    maintenance.judge_pending_interactions()

    assert health.has_failure(PREFIX) is True
    assert health.has_failure() is True


def test_storage_read_failure_records_failed(test_db, wire, monkeypatch):
    """get_unjudged_interactions sat outside every guard: a locked DB escaped
    to a log.warning two frames up and wrote no health row at all."""
    broken = type("S", (), {
        "get_unjudged_interactions":
            lambda self, limit=50: (_ for _ in ()).throw(OSError("database is locked"))
    })()
    monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: broken)

    assert maintenance.judge_pending_interactions() == 0

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "database is locked" in row["last_reason"]


def test_empty_queue_records_a_skip_not_an_attempt(test_db, wire):
    wire([])
    assert maintenance.judge_pending_interactions() == 0

    row = health.get(COMPONENT)
    assert row["last_skipped_at"] is not None
    assert row["last_status"] is None, "a skip must not claim an attempt happened"


def test_an_idle_cycle_cannot_launder_a_live_failure(test_db, wire):
    """The property record_skip exists for: a quiet cycle after a real failure
    must leave the failure standing."""
    health.record_attempt(COMPONENT, health.STATUS_FAILED, "earlier breakage")

    wire([])
    maintenance.judge_pending_interactions()

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert row["last_reason"] == "earlier breakage"
    assert health.has_failure(PREFIX) is True
    # Without this the test passes vacuously: "the failure survived" is equally
    # true of "record_skip fired" and "the idle path did nothing at all".
    # Caught by mutation-testing the verification gate ran.
    assert row["last_skipped_at"] is not None, "the idle cycle must still record a skip"


# ── The false-OK paths, one test each ─────────────────────────────────────────


def test_every_row_raising_records_failed_not_ok(test_db, wire):
    """_score_single swallows per-row exceptions and returns None, so a judge
    that constructs fine but fails every call used to look like a quiet batch."""
    def explode(**kw):
        raise RuntimeError("inference failed")
    wire([_row("a"), _row("b")], evaluate=explode)

    assert maintenance.judge_pending_interactions() == 0

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "0 scored" in row["last_reason"]
    assert "2 failed" in row["last_reason"]


def test_every_row_a_parse_error_records_failed_despite_nonzero_return(test_db, wire):
    """The status must key on scored_results, never the return value: this
    batch produced nothing usable yet returns a nonzero count."""
    wire([_row("a"), _row("b")], evaluate=lambda **kw: _Result(is_parse_error=True))

    returned = maintenance.judge_pending_interactions()
    assert returned == 2, "precondition: the return value looks productive here"

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "2 parse errors" in row["last_reason"]


def test_partial_failure_stays_ok_with_the_count_recorded(test_db, wire):
    """No invented degradation threshold -- but the damage is on the record."""
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Result(score=0.5)
        raise RuntimeError("inference failed")

    wire([_row("a"), _row("b"), _row("c")], evaluate=flaky)
    maintenance.judge_pending_interactions()

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_OK
    assert "1 scored" in row["last_reason"]
    assert "2 failed" in row["last_reason"]


def test_a_successful_batch_records_ok_and_clears_the_streak(test_db, wire):
    health.record_attempt(COMPONENT, health.STATUS_FAILED, "earlier breakage")
    wire([_row("a")], evaluate=lambda **kw: _Result(score=0.5))

    maintenance.judge_pending_interactions()

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0
    assert health.has_failure(PREFIX) is False


def test_failed_and_nothing_to_do_are_distinguishable(test_db, wire, monkeypatch):
    """LIA-556's acceptance criterion, stated directly: both return 0, and the
    health rows must not agree."""
    wire([])
    maintenance.judge_pending_interactions()
    idle = dict(health.get(COMPONENT))

    wire([_row()])  # must precede the override: wire() sets a working judge
    monkeypatch.setattr("evolution.judge.make_runtime_judge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    maintenance.judge_pending_interactions()
    broken = dict(health.get(COMPONENT))

    assert idle["last_status"] != broken["last_status"]
    assert broken["last_status"] == health.STATUS_FAILED


def test_a_wholly_failed_reflection_pass_is_visible_but_does_not_flip_status(
    test_db, wire
):
    """OK attests to scoring, not reflection -- but pass 2's booleans are no
    longer discarded, so a dead reflection pass shows up on the row."""
    wire([_row("a")], evaluate=lambda **kw: _Result(score=0.1), reflect=False)

    maintenance.judge_pending_interactions()

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_OK
    assert "0/1 reflections" in row["last_reason"]


# ── The guarantee, and its limit ──────────────────────────────────────────────


def test_the_wrapper_covers_a_path_no_test_enumerates(test_db, wire, monkeypatch):
    """The point of the wrapper is paths nobody thought of, so test that rather
    than one more specific branch."""
    monkeypatch.setattr(
        maintenance, "_judge_pending_interactions",
        lambda: (_ for _ in ()).throw(ValueError("something nobody predicted")),
    )

    assert maintenance.judge_pending_interactions() == 0

    row = health.get(COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "something nobody predicted" in row["last_reason"]


def test_a_health_db_that_is_also_down_records_nothing_by_design(
    test_db, wire, monkeypatch
):
    """Documents the limit rather than asserting a guarantee the code cannot
    deliver: health writes go to the same evolution.db, and record_attempt
    swallows its own errors, so the failure that takes out the DB takes out its
    own record too. Detecting that needs an out-of-process staleness probe
    (LIA-552)."""
    broken = type("S", (), {
        "get_unjudged_interactions":
            lambda self, limit=50: (_ for _ in ()).throw(OSError("database is locked"))
    })()
    monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: broken)
    # Model what a locked health DB actually does. record_attempt wraps its own
    # body in `except Exception: log.error(...)` (health.py:150), so it returns
    # normally having written nothing -- it does not raise. An earlier draft of
    # this test made it raise instead, which no real failure produces and which
    # only proved the wrapper propagates a thing that cannot happen.
    monkeypatch.setattr(health, "record_attempt", lambda *a, **k: None)

    assert maintenance.judge_pending_interactions() == 0
    assert health.get(COMPONENT) is None, (
        "no durable record survives when the health DB is the thing that failed"
    )


def test_the_direct_script_entry_point_still_works():
    """maintenance.py supports `python evolution/maintenance.py`, via a
    __package__ bootstrap its module docstring advertises.

    A relative import placed above that bootstrap breaks it with
    "attempted relative import with no known parent package" — which is exactly
    what adding the health import did on the first attempt. No existing test
    caught it, because `python -m evolution.maintenance` and a plain
    `import evolution.maintenance` both keep working. This runs the form that
    actually breaks.
    """
    script = pathlib.Path(maintenance.__file__)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"direct-script entry point broken:\n{result.stderr}"
    )
    assert "attempted relative import" not in result.stderr
