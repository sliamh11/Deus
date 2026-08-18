"""Tests for the optimizer health records (LIA-551).

These exist because the optimizer was dead for ~5 months and nothing noticed.
Several cases below are direct regressions for defects found during plan
review — each is labelled with what it protects, because the whole point of
this module is that a silent failure must never be able to look healthy again.
"""
import pytest
from unittest.mock import MagicMock

import evolution.config as cfg
from evolution import health


OPT = "evolution.optimizer."
QA = OPT + "qa"
REGISTRY = OPT + "registry"
STORAGE = OPT + "storage"


@pytest.fixture
def store_above_threshold(monkeypatch):
    """Storage reporting enough scored interactions to trigger optimization."""
    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10)
    fake = MagicMock()
    fake.get_latest_artifact_timestamp.return_value = "1970-01-01"
    fake.count_scored_since.return_value = 20
    monkeypatch.setattr("evolution.storage.get_storage", lambda: fake)
    return fake


@pytest.fixture
def store_below_threshold(monkeypatch):
    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10)
    fake = MagicMock()
    fake.get_latest_artifact_timestamp.return_value = "1970-01-01"
    fake.count_scored_since.return_value = 3
    monkeypatch.setattr("evolution.storage.get_storage", lambda: fake)
    return fake


def _patch_optimize(monkeypatch, fn):
    monkeypatch.setattr("evolution.optimizer.dspy_optimizer.optimize", fn)


def _run(domain_presets=None):
    from evolution.cli import _maybe_auto_optimize
    _maybe_auto_optimize(domain_presets=domain_presets)


# ── the core failure path ────────────────────────────────────────────────────


def test_failure_is_recorded_and_not_propagated(test_db, store_above_threshold, monkeypatch):
    """A crashing optimizer must leave a durable FAILED record and must not
    break the caller — the batch-judge hot path has to keep working."""
    def boom(module="qa", **kw):
        raise RuntimeError("dspy exploded")
    _patch_optimize(monkeypatch, boom)

    _run()  # must not raise

    row = health.get(QA)
    assert row is not None
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]
    assert row["consecutive_failures"] == 1
    assert row["first_failed_at"] is not None


def test_consecutive_failures_accumulate(test_db, store_above_threshold, monkeypatch):
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()
    _run()
    _run()
    row = health.get(QA)
    assert row["consecutive_failures"] == 3
    # first_failed_at pins the START of the streak, not the latest failure.
    first = row["first_failed_at"]
    _run()
    assert health.get(QA)["first_failed_at"] == first


def test_success_clears_the_streak(test_db, store_above_threshold, monkeypatch):
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()
    assert health.get(QA)["consecutive_failures"] == 1

    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    row = health.get(QA)
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0
    assert row["first_failed_at"] is None
    assert row["last_ok_at"] is not None


def test_one_failing_module_does_not_block_the_others(test_db, store_above_threshold, monkeypatch):
    """Regression: the original code wrapped the whole MODULE_REGISTRY loop in
    a single try, so one module's exception aborted every remaining module."""
    def selective(module="qa", **kw):
        if module == "qa":
            raise RuntimeError("only qa fails")
    _patch_optimize(monkeypatch, selective)

    _run()

    assert health.get(QA)["last_status"] == health.STATUS_FAILED
    assert health.get(OPT + "tool_selection")["last_status"] == health.STATUS_OK
    assert health.get(OPT + "summarization")["last_status"] == health.STATUS_OK


# ── skip semantics: a skip must never mask a failure ─────────────────────────


def test_skip_touches_only_the_skip_column(test_db, store_below_threshold, monkeypatch):
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    row = health.get(QA)
    assert row is not None
    assert row["last_skipped_at"] is not None
    # A skip is not an attempt, so it must leave every attempt field alone.
    # last_reason is in this set deliberately: letting a skip write it is what
    # destroyed a real failure's diagnostic cause before this shape.
    assert row["last_status"] is None
    assert row["last_reason"] is None
    assert row["last_attempt_at"] is None
    assert row["last_ok_at"] is None
    assert row["consecutive_failures"] == 0


def test_first_ever_write_being_a_skip_leaves_status_sql_null(test_db, store_below_threshold, monkeypatch):
    """Round-5 review item: the UPSERT's INSERT branch must leave last_status
    as real SQL NULL, not '' or 0 — a never-attempted component must never be
    confusable with a healthy one."""
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    assert health.get(QA)["last_status"] is None
    assert health.rollup(OPT)["status"] is None
    # ...and NEVER-ATTEMPTED must not count as a failure for the exit code.
    assert health.has_failure(OPT) is False


def test_skip_after_failure_keeps_reporting_failed(test_db, monkeypatch):
    """THE regression the GPT co-gate caught (round 4).

    The earlier design made SKIPPED a third status that preserved the counters
    but still overwrote last_status. Since the rollup and the health exit code
    both read last_status, one quiet cycle after a real failure flipped the
    system back to healthy with the failure unresolved.
    """
    # 1. a genuine failure
    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10)
    fake = MagicMock()
    fake.get_latest_artifact_timestamp.return_value = "1970-01-01"
    fake.count_scored_since.return_value = 20
    monkeypatch.setattr("evolution.storage.get_storage", lambda: fake)
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()
    assert health.rollup(OPT)["status"] == health.STATUS_FAILED

    # 2. a quiet cycle: volume drops below the threshold
    fake.count_scored_since.return_value = 1
    _run()

    # 3. the failure must survive it, at BOTH surfaces that decide "healthy"
    assert health.get(QA)["last_status"] == health.STATUS_FAILED
    assert health.get(QA)["consecutive_failures"] == 1
    assert health.rollup(OPT)["status"] == health.STATUS_FAILED
    assert health.has_failure(OPT) is True

    # 4. and the DIAGNOSTIC CAUSE must survive too, not just the status.
    #    The code-review co-gate caught this: an earlier record_skip wrote
    #    last_reason, so a quiet cycle rewrote "RuntimeError: x" to
    #    "below threshold" — an unresolved failure reported with the wrong
    #    cause and the evidence gone. The original assertions above all passed
    #    while that was broken, which is exactly why this one exists.
    assert "RuntimeError" in health.get(QA)["last_reason"]
    assert "below threshold" not in (health.get(QA)["last_reason"] or "")


def test_raising_the_threshold_cannot_silence_a_failure(test_db, monkeypatch):
    """The operator-silences-noise path, end to end: bumping the threshold
    above the available count after a failure must not clear or hide it."""
    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10)
    fake = MagicMock()
    fake.get_latest_artifact_timestamp.return_value = "1970-01-01"
    fake.count_scored_since.return_value = 20
    monkeypatch.setattr("evolution.storage.get_storage", lambda: fake)
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()

    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10_000)
    _run()
    _run()

    assert health.has_failure(OPT) is True
    assert health.get(QA)["consecutive_failures"] == 1


# ── domain-variant aggregation ───────────────────────────────────────────────


def test_domain_success_cannot_erase_a_cross_domain_failure(test_db, store_above_threshold, monkeypatch):
    """Round-3 blocking issue: cross-domain runs first, then each domain, all
    writing the same row. Writing per call let a later domain success clear a
    genuine cross-domain failure — every batch, whenever presets are set."""
    def cross_domain_only_fails(module="qa", **kw):
        if kw.get("domain") is None:
            raise RuntimeError("cross-domain broke")
    _patch_optimize(monkeypatch, cross_domain_only_fails)

    _run(domain_presets=["marketing", "engineering"])

    row = health.get(QA)
    assert row["last_status"] == health.STATUS_FAILED
    assert "cross-domain" in row["last_reason"]
    assert row["consecutive_failures"] == 1


def test_exactly_one_health_write_per_module_per_invocation(test_db, store_above_threshold, monkeypatch):
    """Aggregate-then-write, regardless of how many domain presets exist."""
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)

    calls = []
    real = health.record_attempt
    def counting(component, status, reason=None):
        calls.append(component)
        return real(component, status, reason)
    monkeypatch.setattr("evolution.health.record_attempt", counting)

    _run(domain_presets=["a", "b", "c"])

    # 3 modules + the registry and storage components, and nothing more.
    assert sorted(calls) == sorted(
        [REGISTRY, STORAGE, QA, OPT + "tool_selection", OPT + "summarization"]
    )


# ── enumeration health ───────────────────────────────────────────────────────


def test_registry_import_failure_is_failed_not_silent(test_db, store_above_threshold, monkeypatch):
    """If we cannot even enumerate the work, that is an infrastructure failure
    and must surface — not look like an idle cycle."""
    class Exploding:
        def __iter__(self):
            raise ImportError("no dspy for you")
    monkeypatch.setattr("evolution.optimizer.modules.MODULE_REGISTRY", Exploding())

    _run()

    row = health.get(REGISTRY)
    assert row["last_status"] == health.STATUS_FAILED
    assert "ImportError" in row["last_reason"]
    # and it reaches the derived rollup, so it has a real consumer
    assert health.rollup(OPT)["status"] == health.STATUS_FAILED
    assert health.has_failure(OPT) is True


def test_registry_self_heals_on_the_next_good_run(test_db, store_above_threshold, monkeypatch):
    class Exploding:
        def __iter__(self):
            raise ImportError("boom")

    # Scoped context, NOT monkeypatch.undo(). undo() reverses EVERY patch
    # registered on this monkeypatch instance — including the test_db
    # fixture's redirect of EVOLUTION_DB_PATH — so the _run() after it wrote
    # OK rows straight into the real ~/.deus/evolution.db. Not merely untidy:
    # an OK write zeroes consecutive_failures and clears first_failed_at, so
    # running the suite could erase a genuine production failure streak —
    # exactly the state this module exists to protect. Caught by the
    # code-review co-gate; it also explains four stray OK rows that appeared
    # in the live DB while this ticket was being developed.
    with monkeypatch.context() as scoped:
        scoped.setattr("evolution.optimizer.modules.MODULE_REGISTRY", Exploding())
        _run()
        assert health.get(REGISTRY)["last_status"] == health.STATUS_FAILED

    # Outside the context MODULE_REGISTRY is real again, while test_db's
    # redirect — and every other fixture patch — remains in force.
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    assert health.get(REGISTRY)["last_status"] == health.STATUS_OK


def test_storage_query_failure_is_recorded_not_swallowed(test_db, monkeypatch):
    """Round-3 code review: the storage queries that decide whether to run at
    all were unguarded. If they raised, no health row was written AND the
    exception escaped to an outer log.warning — this ticket's own bug, one
    frame up, on a path the docstring claimed was covered."""
    monkeypatch.setattr(cfg, "AUTO_OPTIMIZE_THRESHOLD", 10)

    def exploding_storage():
        raise RuntimeError("evolution.db is locked")
    monkeypatch.setattr("evolution.storage.get_storage", exploding_storage)

    _run()  # must not raise

    row = health.get(STORAGE)
    assert row is not None, "a storage failure left no health record"
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]
    assert health.rollup(OPT)["status"] == health.STATUS_FAILED
    assert health.has_failure() is True


def test_storage_component_self_heals(test_db, store_above_threshold, monkeypatch):
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()
    assert health.get(STORAGE)["last_status"] == health.STATUS_OK


# ── the escalation surface ───────────────────────────────────────────────────


def test_health_command_exits_nonzero_on_failure(test_db, store_above_threshold, monkeypatch):
    from evolution.cli import cmd_health
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()

    with pytest.raises(SystemExit) as exc:
        cmd_health()
    assert exc.value.code == 1


def test_health_command_exits_zero_when_clean(test_db, store_above_threshold, monkeypatch):
    from evolution.cli import cmd_health
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    cmd_health()  # must not raise SystemExit


def test_health_json_is_machine_readable(test_db, store_above_threshold, monkeypatch, capsys):
    import json
    from evolution.cli import cmd_health
    _patch_optimize(monkeypatch, lambda module="qa", **kw: None)
    _run()

    cmd_health(as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert "components" in payload and "rollups" in payload


def test_status_names_the_failure_instead_of_suggesting_optimize(
    test_db, store_above_threshold, monkeypatch, capsys
):
    """The old message read 'Run `optimize` to generate one.' while the
    optimizer was crashing on every cycle. An empty artifact list is only good
    news if the optimizer is healthy."""
    from evolution.cli import cmd_status
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _run()

    cmd_status()
    out = capsys.readouterr().out
    assert "FAILING, not idle" in out
    assert "Run `optimize` to generate one." not in out


# ── anti-regression ──────────────────────────────────────────────────────────


def test_a_failure_always_leaves_a_record(test_db, store_above_threshold, monkeypatch):
    """Guard against anyone reinstating a bare log-and-continue. If the except
    path stops recording, this fails."""
    _patch_optimize(monkeypatch, lambda module="qa", **kw: (_ for _ in ()).throw(ValueError("nope")))

    _run()

    rows = health.list_all(OPT)
    assert rows, "a failing optimizer left no health record at all"
    assert any(r["last_status"] == health.STATUS_FAILED for r in rows)
