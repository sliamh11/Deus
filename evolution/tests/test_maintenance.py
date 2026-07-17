"""
Tests for evolution/maintenance.py's process_human_feedback() (LIA-1011).

process_human_feedback() routes human-scored interactions into corrective/
positive reflections and archives contradicting reflections once a
human-verified zone is established. These tests cover the round-5/6/7
regressions: zone-alignment archival running on the dedup path (not just a
fresh save), the entire per-row body sharing one exception boundary
(including the direction-is-None early-skip), and counter increments
happening strictly after their row's write succeeds so a mid-row failure
can only ever land in `errored`.

Storage is faked in-memory (no real DB) so failure injection (raising from
update_interaction / archive_reflection_by_id) is simple and precise.
"""
import evolution.maintenance as maintenance_mod
import evolution.reflexion.generator as generator_mod
import evolution.reflexion.store as store_mod
import evolution.reflexion.validation as validation_mod
import evolution.storage as storage_mod
from evolution.maintenance import process_human_feedback


class _FakeHFStore:
    """Minimal in-memory storage double for process_human_feedback()."""

    def __init__(self, rows, reflections=None):
        # rows: list of dicts with at least id/prompt/human_score/judge_score/...
        self._rows = {r["id"]: dict(r) for r in rows}
        self._reflections = reflections or {}  # interaction_id -> list of {"id", "polarity"}
        self.update_interaction_calls: list[tuple[str, dict]] = []
        self.fail_update_interaction_for: set[str] = set()

    def get_unprocessed_human_feedback(self, limit: int = 50):
        return [
            dict(r) for r in self._rows.values()
            if r.get("human_score") is not None and r.get("processed_at") is None
        ]

    def update_interaction(self, interaction_id: str, **fields):
        self.update_interaction_calls.append((interaction_id, fields))
        if interaction_id in self.fail_update_interaction_for:
            raise RuntimeError(f"simulated update_interaction failure for {interaction_id}")
        self._rows[interaction_id].update(fields)

    def get_reflections_for_interaction(self, interaction_id: str):
        return list(self._reflections.get(interaction_id, []))


def _row(id_, human_score, judge_score=None, prompt="p", response="r",
         human_comment=None, metrics=None, group_folder="g", tools_used=None):
    return {
        "id": id_, "prompt": prompt, "response": response,
        "human_score": human_score, "judge_score": judge_score,
        "human_comment": human_comment, "metrics": metrics,
        "group_folder": group_folder, "tools_used": tools_used,
        "processed_at": None,
    }


def _patch_generation(monkeypatch, *, valid=True, reason="", save_result="ok"):
    """Patch generate_reflection/generate_positive_reflection/is_valid_reflection/
    save_reflection/archive_reflection_by_id with simple, deterministic fakes.

    save_result: "ok" -> save_reflection returns a fresh id; "dedup" -> returns
    None (simulating _is_duplicate); a callable -> called with kwargs, return value used.
    """
    calls = {"generate": [], "save": [], "archive": []}

    def _fake_generate(**kwargs):
        calls["generate"].append(("corrective", kwargs))
        return ("What went wrong: X\nNext time: Y\nCategory: reasoning", "reasoning")

    def _fake_generate_positive(**kwargs):
        calls["generate"].append(("positive", kwargs))
        return ("What worked: X\nPattern: Y\nCategory: positive_pattern", "positive_pattern")

    def _fake_is_valid(content):
        return (valid, reason)

    def _fake_save_reflection(**kwargs):
        calls["save"].append(kwargs)
        if callable(save_result):
            return save_result(**kwargs)
        if save_result == "dedup":
            return None
        return f"rid-{kwargs.get('interaction_id')}"

    def _fake_archive(reflection_id):
        calls["archive"].append(reflection_id)
        return True

    monkeypatch.setattr(generator_mod, "generate_reflection", _fake_generate)
    monkeypatch.setattr(generator_mod, "generate_positive_reflection", _fake_generate_positive)
    monkeypatch.setattr(validation_mod, "is_valid_reflection", _fake_is_valid)
    monkeypatch.setattr(store_mod, "save_reflection", _fake_save_reflection)
    monkeypatch.setattr(store_mod, "archive_reflection_by_id", _fake_archive)
    return calls


def _install_store(monkeypatch, store):
    monkeypatch.setattr(storage_mod, "get_storage", lambda *a, **kw: store)
    return store


# ── Basic routing ────────────────────────────────────────────────────────


def test_low_human_score_routes_corrective(monkeypatch):
    store = _install_store(monkeypatch, _FakeHFStore([_row("r1", human_score=0.2)]))
    calls = _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters == {"corrective": 1, "positive": 0, "skipped": 0, "errored": 0}
    assert calls["save"][0]["polarity"] == "corrective"
    assert store._rows["r1"]["processed_at"] is not None


def test_high_score_with_no_existing_positive_reflection_routes_positive(monkeypatch):
    store = _install_store(
        monkeypatch, _FakeHFStore([_row("r2", human_score=0.95, judge_score=0.5)]),
    )
    calls = _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 1, "skipped": 0, "errored": 0}
    assert calls["save"][0]["polarity"] == "positive"


def test_judge_score_is_not_a_reliable_redundancy_proxy(monkeypatch):
    """A high judge_score does NOT reliably imply a positive reflection was
    actually saved: judge_pending_interactions() persists judge_score via
    update_score() and generates the reflection in a SEPARATE pass
    (_reflect_single) that can independently fail (logged, not re-raised)
    without reverting judge_score. The redundancy check must be against
    ACTUAL reflection existence, not the score -- so a high judge_score with
    no existing positive reflection still proceeds as positive."""
    store = _install_store(
        monkeypatch,
        _FakeHFStore([_row("r3", human_score=0.9, judge_score=0.95)], reflections={}),
    )
    _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 1, "skipped": 0, "errored": 0}


def test_existing_positive_reflection_suppresses_redundant_generation(monkeypatch):
    """When a positive reflection genuinely already exists for this
    interaction (regardless of judge_score, or even with no judge_score at
    all), the human's corroborating high score is skipped rather than
    generating a redundant duplicate."""
    reflections = {"r4": [{"id": "existing-pos", "polarity": "positive"}]}
    store = _install_store(
        monkeypatch,
        _FakeHFStore([_row("r4", human_score=0.9, judge_score=None)], reflections=reflections),
    )
    _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 0, "skipped": 1, "errored": 0}
    assert store._rows["r4"]["processed_at"] is not None


def test_mid_range_score_is_skipped(monkeypatch):
    store = _install_store(monkeypatch, _FakeHFStore([_row("r5", human_score=0.7)]))
    _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 0, "skipped": 1, "errored": 0}


# ── Round-7: direction-is-None branch write failure ─────────────────────


def test_direction_none_write_failure_counts_errored_not_skipped(monkeypatch):
    """The direction-is-None early-skip branch's update_interaction call
    must be inside the try/except too. A failure there must land in
    `errored`, never `skipped`, and must not abort the rest of the batch."""
    store = _FakeHFStore([_row("mid", human_score=0.7), _row("clean", human_score=0.2)])
    store.fail_update_interaction_for.add("mid")
    _install_store(monkeypatch, store)
    _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert counters["errored"] == 1
    assert counters["skipped"] == 0
    assert counters["corrective"] == 1  # "clean" row still processed
    assert sum(counters.values()) == 2


# ── Round-5/6: validation vs. dedup distinguishing ──────────────────────


def test_validation_rejected_leaves_processed_at_unset(monkeypatch):
    store = _install_store(monkeypatch, _FakeHFStore([_row("bad", human_score=0.2)]))
    _patch_generation(monkeypatch, valid=False, reason="banned_token")

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 0, "skipped": 0, "errored": 1}
    assert store._rows["bad"]["processed_at"] is None  # retriable next cycle


def test_dedup_marks_processed_and_still_archives(monkeypatch):
    """Round-5 regression: a dedup (valid content, save_reflection returns
    None) must still run zone-alignment archival and mark processed_at --
    archival was previously gated on `reflection_id is not None`, which
    incorrectly skipped the dedup path."""
    reflections = {"dup1": [{"id": "existing-positive", "polarity": "positive"}]}
    store = _install_store(
        monkeypatch, _FakeHFStore([_row("dup1", human_score=0.2)], reflections=reflections),
    )
    calls = _patch_generation(monkeypatch, save_result="dedup")

    counters = process_human_feedback()

    assert counters == {"corrective": 0, "positive": 0, "skipped": 1, "errored": 0}
    assert calls["archive"] == ["existing-positive"]
    assert store._rows["dup1"]["processed_at"] is not None


def test_fresh_save_also_archives_contradicting_reflection(monkeypatch):
    reflections = {"fresh1": [{"id": "existing-positive", "polarity": "positive"}]}
    store = _install_store(
        monkeypatch, _FakeHFStore([_row("fresh1", human_score=0.2)], reflections=reflections),
    )
    calls = _patch_generation(monkeypatch, save_result="ok")

    counters = process_human_feedback()

    assert counters["corrective"] == 1
    assert calls["archive"] == ["existing-positive"]


def test_null_polarity_reflection_never_archived(monkeypatch):
    """Legacy reflections with polarity=None are never targeted by
    zone-alignment archival (deliberate; docs/KNOWN_LIMITATIONS.md)."""
    reflections = {"legacy1": [{"id": "legacy-ref", "polarity": None}]}
    store = _install_store(
        monkeypatch, _FakeHFStore([_row("legacy1", human_score=0.2)], reflections=reflections),
    )
    calls = _patch_generation(monkeypatch, save_result="ok")

    process_human_feedback()

    assert calls["archive"] == []


def test_same_polarity_reflection_not_archived(monkeypatch):
    """Only the CONTRADICTING polarity is archived, never the matching one."""
    reflections = {"same1": [{"id": "existing-corrective", "polarity": "corrective"}]}
    store = _install_store(
        monkeypatch, _FakeHFStore([_row("same1", human_score=0.2)], reflections=reflections),
    )
    calls = _patch_generation(monkeypatch, save_result="ok")

    process_human_feedback()

    assert calls["archive"] == []


# ── Round-7: archival failure must not double-count / must not abort batch ─


def test_archival_failure_does_not_abort_batch_frozen_counters(monkeypatch):
    """If archive_reflection_by_id raises after a successful save, the row
    must land in `errored` ONLY -- NOT also increment its direction bucket
    -- and the rest of the batch must still process."""
    reflections = {"row1": [{"id": "existing-positive", "polarity": "positive"}]}
    store = _FakeHFStore(
        [_row("row1", human_score=0.2), _row("row2", human_score=0.2)],
        reflections=reflections,
    )
    _install_store(monkeypatch, store)
    calls = _patch_generation(monkeypatch, save_result="ok")

    def _raising_archive(reflection_id):
        raise RuntimeError("simulated archival failure")

    monkeypatch.setattr(store_mod, "archive_reflection_by_id", _raising_archive)

    counters = process_human_feedback()

    assert counters["errored"] == 1
    assert counters["corrective"] == 1  # only row2, NOT row1 double-counted
    assert store._rows["row1"]["processed_at"] is None  # never reached update_interaction
    assert store._rows["row2"]["processed_at"] is not None
    assert sum(counters.values()) == 2


# ── Partition invariant ──────────────────────────────────────────────────


def test_partition_invariant_sum_equals_len_rows(monkeypatch):
    rows = [
        _row("clean_corrective", human_score=0.1),
        _row("clean_positive", human_score=0.95, judge_score=0.1),
        _row("skipped_mid", human_score=0.7),
        _row("will_fail", human_score=0.2),
    ]
    store = _FakeHFStore(rows)
    store.fail_update_interaction_for.add("will_fail")
    _install_store(monkeypatch, store)
    _patch_generation(monkeypatch)

    counters = process_human_feedback()

    assert sum(counters.values()) == len(rows)


# ── run_maintenance wiring ────────────────────────────────────────────────


def test_run_maintenance_wires_in_human_feedback_step(monkeypatch):
    """process_human_feedback() runs as step 1.5, between judge_pending_interactions()
    and archive_stale_reflections(), and its counters surface in run_maintenance()'s result."""
    monkeypatch.setattr(maintenance_mod, "is_maintenance_due", lambda **kw: True)
    monkeypatch.setattr(maintenance_mod, "judge_pending_interactions", lambda: 0)
    monkeypatch.setattr(maintenance_mod, "compact_old_interactions", lambda: 0)

    calls_order = []

    def _fake_process_human_feedback(**kw):
        calls_order.append("human_feedback")
        return {"corrective": 1, "positive": 0, "skipped": 0, "errored": 0}

    def _fake_archive_stale(**kw):
        calls_order.append("archive_stale")
        return 0

    monkeypatch.setattr(maintenance_mod, "process_human_feedback", _fake_process_human_feedback)
    monkeypatch.setattr(store_mod, "archive_stale_reflections", _fake_archive_stale)

    class _Store:
        def count_interactions(self):
            return 0

        def get_interaction(self, _id):
            return None

        def log_interaction(self, **kw):
            return kw.get("interaction_id")

        def update_interaction(self, *a, **kw):
            pass

    monkeypatch.setattr(storage_mod, "get_storage", lambda *a, **kw: _Store())

    result = maintenance_mod.run_maintenance(force=True)

    assert calls_order == ["human_feedback", "archive_stale"]
    assert result["human_feedback_processed"] == {"corrective": 1, "positive": 0, "skipped": 0, "errored": 0}
