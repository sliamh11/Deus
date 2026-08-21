"""Health recording for LIA-556 sites 2, 3 and 5, plus the rollup ambiguity.

Each of these sites swallowed an exception into a `log.warning` — or, at site 5,
into nothing at all — so a persistently broken subsystem was indistinguishable
from a quiet one. These tests pin each failure to a durable record, and pin the
self-heal, because `record_attempt(OK)` is the only thing that clears a streak:
a component that records FAILED and never OK is a one-way door, and
`has_failure()` feeds the gate the daily cockpit reads.
"""
import pytest

from evolution import cli, health, maintenance

# Imported for its side effect, before any fixture below patches
# `evolution.storage.get_storage`. `interaction_log` binds `get_storage` at
# MODULE level (`evolution/ilog/interaction_log.py:11`), so whichever test first
# triggers its import captures whatever `get_storage` is at that moment — and a
# stub captured there survives monkeypatch teardown for the rest of the process,
# breaking unrelated tests later in the run. Importing it here means the real
# function is what gets bound.
from evolution.ilog import interaction_log as _interaction_log  # noqa: F401


# ── Site 2: principles extraction, per domain ─────────────────────────────────


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


def test_principles_failure_records_per_domain(test_db, principles):
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]
    # The cross-domain pass runs too and fails the same way.
    assert health.get(cli._PRINCIPLES_HEALTH_PREFIX + "cross-domain")["last_status"] \
        == health.STATUS_FAILED


def test_principles_success_records_ok_and_self_heals(test_db, principles):
    principles(lambda domain=None: (_ for _ in ()).throw(RuntimeError("no key")))
    cli._maybe_auto_extract_principles(["coding"])
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is True

    # Recovered. The stub must return generated TEXT, not None: None is the
    # return of a skip that never reached the LLM, and a skip is precisely what
    # must NOT clear a streak (see the skip tests below).
    principles(lambda domain=None: _GENERATED)
    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is False


def test_one_broken_domain_does_not_taint_a_healthy_one(test_db, monkeypatch):
    """Per-domain components, so a dead domain is visible without reddening the
    domains that still work."""
    store = type("S", (), {"get_last_extraction": lambda self, d: None})()
    monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: store)

    def selective(domain=None):
        if domain == "coding":
            raise RuntimeError("boom")
        return _GENERATED

    monkeypatch.setattr("evolution.reflexion.principles.extract_principles", selective)
    cli._maybe_auto_extract_principles(["coding", "study"])

    assert health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")["last_status"] \
        == health.STATUS_FAILED
    assert health.get(cli._PRINCIPLES_HEALTH_PREFIX + "study")["last_status"] \
        == health.STATUS_OK


# ── Site 2, continued: a skip is not an attempt (#1248) ───────────────────────
#
# `extract_principles` returns None on two ordinary paths, BOTH of which return
# before the single `generate()` call: the `min_new` data-count gate
# (principles.py:71-76) and the "<3 usable examples once empty responses are
# filtered" gate (principles.py:94-95). Neither did any work, so neither is an
# attempt — and because record_attempt(OK) is the only write that zeroes
# `consecutive_failures` and nulls `first_failed_at`, recording one as OK
# laundered a live FAILED streak into a clean bill of health.
#
# The two gate tests drive the REAL `extract_principles` with only its data
# sources stubbed, so they are pinned to the actual early-return sites rather
# than to a hand-made stub that merely agrees with them.

_GENERATED = "1. Answer the question asked.\n2. Cite the file you changed."


@pytest.fixture
def real_extraction(monkeypatch):
    """Run the real `extract_principles`, with only its data sources stubbed.

    `principles.py` binds `get_storage` and `get_recent` at MODULE level, so
    these patch the names in that module rather than at their definition sites
    — patching `evolution.storage.get_storage` alone would not be seen here (it
    is seen in cli.py only because cli.py imports it inside the function body).

    Returns a setter taking `new_count` (what the min_new gate compares) and
    `rows` (what `get_recent` yields for both the best and worst queries).
    """
    def _wire(new_count, rows=(), generated=_GENERATED):
        store = type("S", (), {
            "get_last_extraction": lambda self, d: None,
            "count_new_scored": lambda self, since_timestamp=None, domain=None: new_count,
            "record_extraction": lambda self, **kw: None,
        })()
        monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: store)
        monkeypatch.setattr("evolution.reflexion.principles.get_storage",
                            lambda *a, **k: store)
        monkeypatch.setattr("evolution.reflexion.principles.get_recent",
                            lambda **kw: list(rows))
        monkeypatch.setattr("evolution.reflexion.principles.generate",
                            lambda *a, **k: generated)
        monkeypatch.setattr("evolution.reflexion.principles.save_reflection",
                            lambda **kw: None)
    return _wire


def _row(response="a real answer"):
    return {"prompt": "p", "response": response, "judge_score": 0.9}


def test_min_new_gate_records_a_skip_not_ok(test_db, real_extraction):
    """AC1: below the min_new threshold, nothing ran — so nothing is recorded
    as having run."""
    real_extraction(new_count=0)  # default min_new is 5

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")
    assert row["last_status"] != health.STATUS_OK, \
        "a below-threshold skip must not be recorded as a successful extraction"
    assert row["last_status"] is None, "a skip must not invent a status at all"
    assert row["last_skipped_at"] is not None, "the skip itself must be recorded"


def test_usable_examples_gate_records_a_skip_not_ok(test_db, real_extraction):
    """AC2: past min_new, but the empty-response filter leaves <3 usable rows.

    Four rows in, three of them response-less: `response_supports_reflection`
    drops those, leaving 1 + 1 = 2 < 3. Still no LLM call, still not an attempt.
    """
    rows = [_row(), _row(""), _row(None), _row("   ")]
    real_extraction(new_count=99, rows=rows)

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")
    assert row["last_status"] != health.STATUS_OK, \
        "an insufficient-examples skip must not be recorded as a successful extraction"
    assert row["last_status"] is None
    assert row["last_skipped_at"] is not None


@pytest.mark.parametrize("gate,new_count,rows", [
    ("min_new", 0, ()),
    ("usable_examples", 99, (_row(), _row(""), _row(None))),
])
def test_a_skip_does_not_clear_a_live_failure_streak(
    test_db, real_extraction, gate, new_count, rows,
):
    """AC3: the actual bug. A real FAILED streak must survive both skip paths.

    Parametrised over both gates because they are separate `return None` sites
    and a fix that caught only one would still launder the other.
    """
    component = cli._PRINCIPLES_HEALTH_PREFIX + "coding"
    health.record_attempt(component, health.STATUS_FAILED, "no API key")
    health.record_attempt(component, health.STATUS_FAILED, "no API key")
    before = health.get(component)
    assert before["consecutive_failures"] == 2

    real_extraction(new_count=new_count, rows=rows)
    cli._maybe_auto_extract_principles(["coding"])

    after = health.get(component)
    assert after["last_status"] == health.STATUS_FAILED, f"{gate} skip cleared the status"
    assert after["consecutive_failures"] == 2, f"{gate} skip reset the streak counter"
    assert after["last_reason"] == "no API key", f"{gate} skip overwrote the diagnostic cause"
    assert after["first_failed_at"] == before["first_failed_at"], \
        f"{gate} skip moved the start of the streak"
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is True, \
        f"{gate} skip hid an unresolved failure from the cockpit gate"


def test_empty_generation_records_failed_not_skipped(test_db, principles):
    """AC4: the third case, and the easy one to miss.

    `extract_principles` returns the generation text AFTER `_record_extraction`
    has already run, so an empty string means the LLM call really happened and
    consumed both its cooldown and its tokens. That is a failed attempt — not a
    skip, and certainly not an OK.
    """
    principles(lambda domain=None: "   \n  ")

    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(cli._PRINCIPLES_HEALTH_PREFIX + "coding")
    assert row["last_status"] == health.STATUS_FAILED
    assert row["last_reason"] == "empty generation"
    assert row["consecutive_failures"] == 1


def test_a_real_extraction_still_clears_a_streak(test_db, real_extraction):
    """AC5: the LIA-556 intent is preserved — self-heal still works, but now
    only for an extraction that actually produced output."""
    component = cli._PRINCIPLES_HEALTH_PREFIX + "coding"
    health.record_attempt(component, health.STATUS_FAILED, "no API key")
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is True

    real_extraction(new_count=99, rows=[_row(), _row(), _row()])
    cli._maybe_auto_extract_principles(["coding"])

    row = health.get(component)
    assert row["last_status"] == health.STATUS_OK
    assert row["last_reason"] == "extraction completed"
    assert row["consecutive_failures"] == 0
    assert row["first_failed_at"] is None
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is False


def test_skip_only_history_is_never_attempted_not_healthy(test_db, real_extraction):
    """AC6: a component that has never extracted anything must not report as
    healthy — to `rollup()` or to anything reading it."""
    real_extraction(new_count=0)
    cli._maybe_auto_extract_principles(["coding"])

    roll = health.rollup(cli._PRINCIPLES_HEALTH_PREFIX)
    assert roll["status"] is None, "never-attempted must not surface as OK"
    assert "never attempted" in roll["reason"]
    # ...and it is still not a failure, so it must not trip the cockpit gate.
    assert health.has_failure(cli._PRINCIPLES_HEALTH_PREFIX) is False


# ── Site 3: the outermost dispatch guards ─────────────────────────────────────


def test_clear_dispatch_failure_writes_only_on_transition(test_db, monkeypatch):
    """This runs once per interaction, so the steady state must not write.

    Read first, write only on FAILED -> OK; otherwise every message would cost a
    health write.
    """
    component = cli._JUDGE_DISPATCH_COMPONENT
    writes = []
    real = health.record_attempt
    monkeypatch.setattr(
        health, "record_attempt",
        lambda c, s, r=None: (writes.append((c, s)), real(c, s, r))[1],
    )

    # No row at all: nothing to clear, so nothing written.
    cli._clear_dispatch_failure(component)
    assert writes == []

    real(component, health.STATUS_OK, "fine")
    writes.clear()
    # Already OK: still nothing written.
    cli._clear_dispatch_failure(component)
    assert writes == []

    real(component, health.STATUS_FAILED, "boom")
    writes.clear()
    # FAILED -> OK: exactly one write, and the streak is cleared.
    cli._clear_dispatch_failure(component)
    assert writes == [(component, health.STATUS_OK)]
    row = health.get(component)
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0


def test_dispatch_components_are_siblings_not_the_batch_row(test_db):
    """The dispatch guards fire when the inner wrapper could not run at all, so
    they must not clobber the inner component's row.

    Both prefixes are asserted against `maintenance`'s own constants rather than
    re-stated as literals. `cli.py` declares its component names independently —
    importing `maintenance` at cli's module level would load it on a path
    Node.js calls once per interaction — so this test is what keeps the two
    declarations from silently drifting apart on a rename.
    """
    assert cli._JUDGE_DISPATCH_COMPONENT != maintenance._BATCH_COMPONENT
    assert cli._JUDGE_DISPATCH_COMPONENT.startswith(maintenance._JUDGE_HEALTH_PREFIX)
    assert cli._MAINTENANCE_DISPATCH_COMPONENT.startswith(
        maintenance._MAINTENANCE_HEALTH_PREFIX
    )
    assert cli._MAINTENANCE_DISPATCH_COMPONENT != maintenance._COMPACTION_COMPONENT


# ── Site 5: the compaction generative probe ───────────────────────────────────


@pytest.fixture
def compactable(monkeypatch):
    """One compactable row, so compact_old_interactions reaches the probe."""
    store = type("S", (), {
        "get_compactable_interactions": lambda self, days=0, limit=50: [
            {"id": "i1", "prompt": "p", "response": "r"}
        ],
        "update_interaction": lambda self, *a, **k: None,
    })()
    monkeypatch.setattr("evolution.storage.get_storage", lambda *a, **k: store)


def test_probe_exception_is_logged_and_recorded(test_db, compactable, monkeypatch):
    """Was a bare `except: pass` — compaction silently degraded to truncation
    forever, with no log and no record."""
    monkeypatch.setattr(
        "evolution.generative.provider.GenerativeRegistry.default",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no provider"))),
    )

    maintenance.compact_old_interactions()

    row = health.get(maintenance._COMPACTION_COMPONENT)
    assert row["last_status"] == health.STATUS_FAILED
    assert "RuntimeError" in row["last_reason"]


def test_unconfigured_provider_is_not_a_failure(test_db, compactable, monkeypatch):
    """`is_available() == False` is a legitimate "no backend configured" state.
    Recording it FAILED would cry wolf on every install without one."""
    provider = type("P", (), {"is_available": lambda self: False})()
    monkeypatch.setattr(
        "evolution.generative.provider.GenerativeRegistry.default",
        staticmethod(lambda: type("R", (), {"resolve": lambda self: provider})()),
    )

    maintenance.compact_old_interactions()

    row = health.get(maintenance._COMPACTION_COMPONENT)
    assert row["last_status"] == health.STATUS_OK
    assert "not configured" in row["last_reason"]


def test_compaction_component_self_heals(test_db, compactable, monkeypatch):
    monkeypatch.setattr(
        "evolution.generative.provider.GenerativeRegistry.default",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    maintenance.compact_old_interactions()
    assert health.get(maintenance._COMPACTION_COMPONENT)["last_status"] \
        == health.STATUS_FAILED

    provider = type("P", (), {"is_available": lambda self: True})()
    monkeypatch.setattr(
        "evolution.generative.provider.GenerativeRegistry.default",
        staticmethod(lambda: type("R", (), {"resolve": lambda self: provider})()),
    )
    maintenance.compact_old_interactions()

    row = health.get(maintenance._COMPACTION_COMPONENT)
    assert row["last_status"] == health.STATUS_OK
    assert row["consecutive_failures"] == 0


# ── rollup(): zero rows vs recorded-but-never-attempted ───────────────────────


def test_rollup_distinguishes_no_records_from_never_attempted(test_db):
    """Both return status None, so only the reason can tell them apart — and
    cmd_status printed one fixed string for both."""
    empty = health.rollup("evolution.nothing.")
    assert empty["status"] is None
    assert empty["reason"] == "no health records"

    health.record_skip("evolution.skipped.alpha")
    skipped = health.rollup("evolution.skipped.")

    assert skipped["status"] is None, "a skip must not invent a status"
    assert skipped["reason"] != empty["reason"], "the two cases must be distinguishable"
    assert "never attempted" in skipped["reason"]


def test_rollup_reason_survives_a_real_failure(test_db):
    """The new branch must not shadow a genuine FAILED reason."""
    health.record_skip("evolution.mixed.alpha")
    health.record_attempt("evolution.mixed.beta", health.STATUS_FAILED, "real cause")

    roll = health.rollup("evolution.mixed.")
    assert roll["status"] == health.STATUS_FAILED
    assert roll["reason"] == "real cause"
