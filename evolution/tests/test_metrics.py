"""Tests for evolution.metrics — validation, post-hoc updates, and analysis."""
import json
import logging

import pytest

import evolution.config as config_mod
import evolution.db as db_mod
import evolution.metrics as metrics_mod
from evolution.metrics import (
    MAX_METRICS_BYTES,
    break_report,
    confidence_calibration,
    fetch_metrics_rows,
    metric_trend,
    parse_metrics,
    summarize_metrics,
    update_metrics,
    validate_metrics,
)
from evolution.storage.provider import StorageRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    """Point the default sqlite provider at a temp DB and return it."""
    test_db = tmp_path / "test_metrics.db"
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")
    from evolution.storage import get_storage
    return get_storage("sqlite")


def _row(metrics=None, judge_score=None, timestamp="2026-06-10T10:00:00Z",
         group_folder="g", iid="i1"):
    """Row shaped like get_metrics_rows() output, for the PURE analysis
    functions only — these never hit the DB time-window filter, so the fixed
    timestamp keeps trend-day assertions deterministic. DB-backed tests that
    go through get_metrics_rows must seed with datetime.now() timestamps."""
    return {
        "id": iid,
        "timestamp": timestamp,
        "group_folder": group_folder,
        "metrics": json.dumps(metrics) if metrics is not None else None,
        "judge_score": judge_score,
    }


# ── validate_metrics ──────────────────────────────────────────────────────────


class TestValidateMetrics:
    def test_accepts_scalars(self):
        m = {"tests_passed": 3, "confidence": 0.8, "task_type": "feature"}
        assert validate_metrics(m) is m

    def test_accepts_list_of_scalars(self):
        m = {"breaks": ["regression", "expected"]}
        assert validate_metrics(m) is m

    def test_accepts_empty_dict(self):
        assert validate_metrics({}) == {}

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            validate_metrics([1, 2])

    def test_rejects_non_string_key(self):
        with pytest.raises(ValueError, match="keys must be strings"):
            validate_metrics({3: "x"})

    def test_rejects_nested_object(self):
        with pytest.raises(ValueError, match="scalar"):
            validate_metrics({"tests_passed": {"unit": 3}})

    def test_rejects_list_of_objects(self):
        with pytest.raises(ValueError, match="lists may only contain scalars"):
            validate_metrics({"breaks": [{"category": "regression"}]})

    def test_rejects_none_value(self):
        with pytest.raises(ValueError, match="scalar"):
            validate_metrics({"tests_passed": None})

    def test_rejects_oversized_payload(self):
        m = {"task_type": "x" * MAX_METRICS_BYTES}
        with pytest.raises(ValueError, match="max is"):
            validate_metrics(m)

    def test_unknown_key_warns_but_accepts(self, caplog):
        with caplog.at_level(logging.WARNING, logger="evolution.metrics"):
            validate_metrics({"my_custom_metric": 1})
        assert "WELL_KNOWN_METRICS" in caplog.text

    def test_known_key_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="evolution.metrics"):
            validate_metrics({"tests_passed": 1})
        assert caplog.text == ""


# ── parse_metrics ─────────────────────────────────────────────────────────────


class TestParseMetrics:
    def test_parses_valid_json(self):
        assert parse_metrics('{"x": 1}') == {"x": 1}

    def test_none_and_empty_return_none(self):
        assert parse_metrics(None) is None
        assert parse_metrics("") is None

    def test_invalid_json_returns_none(self):
        assert parse_metrics("{not json") is None

    def test_non_object_json_returns_none(self):
        assert parse_metrics("[1, 2]") is None


# ── update_metrics (post-hoc path, sqlite-backed) ─────────────────────────────


class TestUpdateMetrics:
    def test_merge_over_existing(self, sqlite_store):
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2026-06-10T10:00:00Z", interaction_id="u1",
            metrics='{"tests_passed": 3}',
        )
        final = update_metrics("u1", {"warden_rounds": 2})
        assert final == {"tests_passed": 3, "warden_rounds": 2}
        row = sqlite_store.get_interaction("u1")
        assert json.loads(row["metrics"]) == final

    def test_merge_new_key_wins(self, sqlite_store):
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2026-06-10T10:00:00Z", interaction_id="u2",
            metrics='{"confidence": 0.5}',
        )
        final = update_metrics("u2", {"confidence": 0.9})
        assert final == {"confidence": 0.9}

    def test_replace_mode(self, sqlite_store):
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2026-06-10T10:00:00Z", interaction_id="u3",
            metrics='{"tests_passed": 3}',
        )
        final = update_metrics("u3", {"warden_rounds": 1}, merge=False)
        assert final == {"warden_rounds": 1}
        row = sqlite_store.get_interaction("u3")
        assert json.loads(row["metrics"]) == {"warden_rounds": 1}

    def test_on_interaction_without_metrics(self, sqlite_store):
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2026-06-10T10:00:00Z", interaction_id="u4",
        )
        final = update_metrics("u4", {"tests_passed": 1})
        assert final == {"tests_passed": 1}

    def test_missing_interaction_raises(self, sqlite_store):
        with pytest.raises(ValueError, match="not found"):
            update_metrics("nope", {"tests_passed": 1})

    def test_invalid_metrics_raise_before_write(self, sqlite_store):
        with pytest.raises(ValueError):
            update_metrics("irrelevant", {"nested": {"a": 1}})

    def test_merged_payload_size_enforced(self, sqlite_store):
        big = "x" * (MAX_METRICS_BYTES // 2)
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2026-06-10T10:00:00Z", interaction_id="u5",
            metrics=json.dumps({"task_type": big}),
        )
        with pytest.raises(ValueError, match="max is"):
            update_metrics("u5", {"my_other_blob": "y" * (MAX_METRICS_BYTES // 2 + 100)})

    def test_fetch_metrics_rows_roundtrip(self, sqlite_store):
        from datetime import datetime, timezone
        sqlite_store.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interaction_id="u6", metrics='{"tests_passed": 5}',
        )
        rows = fetch_metrics_rows(days=7)
        assert [r["id"] for r in rows] == ["u6"]


# ── summarize_metrics ─────────────────────────────────────────────────────────


class TestSummarizeMetrics:
    def test_numeric_summary(self):
        rows = [
            _row({"tests_passed": 2}, iid="a"),
            _row({"tests_passed": 4}, iid="b"),
        ]
        result = summarize_metrics(rows)
        assert result["interactions"] == 2
        tp = result["keys"]["tests_passed"]
        assert tp["type"] == "numeric"
        assert tp["count"] == 2
        assert tp["mean"] == 3
        assert tp["min"] == 2
        assert tp["max"] == 4
        assert tp["sum"] == 6

    def test_categorical_and_list_values(self):
        rows = [
            _row({"task_type": "feature", "breaks": ["regression", "expected"]}, iid="a"),
            _row({"task_type": "feature", "breaks": ["regression"]}, iid="b"),
        ]
        result = summarize_metrics(rows)
        assert result["keys"]["task_type"]["values"] == {"feature": 2}
        assert result["keys"]["breaks"]["values"] == {"regression": 2, "expected": 1}

    def test_bool_is_categorical(self):
        result = summarize_metrics([_row({"my_flag": True})])
        assert result["keys"]["my_flag"]["type"] == "categorical"

    def test_key_filter(self):
        rows = [_row({"tests_passed": 1, "confidence": 0.5})]
        result = summarize_metrics(rows, key="confidence")
        assert list(result["keys"]) == ["confidence"]

    def test_unparseable_rows_skipped(self):
        rows = [{"id": "x", "timestamp": "t", "group_folder": "g",
                 "metrics": "{broken", "judge_score": None}]
        result = summarize_metrics(rows)
        assert result["interactions"] == 0
        assert result["keys"] == {}


# ── metric_trend ──────────────────────────────────────────────────────────────


class TestMetricTrend:
    def test_daily_averages_sorted(self):
        rows = [
            _row({"tests_passed": 2}, timestamp="2026-06-09T08:00:00Z", iid="a"),
            _row({"tests_passed": 4}, timestamp="2026-06-09T18:00:00Z", iid="b"),
            _row({"tests_passed": 6}, timestamp="2026-06-10T08:00:00Z", iid="c"),
        ]
        trend = metric_trend(rows, "tests_passed")
        assert trend == [
            {"day": "2026-06-09", "avg": 3, "count": 2},
            {"day": "2026-06-10", "avg": 6, "count": 1},
        ]

    def test_non_numeric_values_skipped(self):
        rows = [_row({"tests_passed": "lots"})]
        assert metric_trend(rows, "tests_passed") == []

    def test_missing_key_yields_empty(self):
        rows = [_row({"confidence": 0.5})]
        assert metric_trend(rows, "tests_passed") == []


# ── confidence_calibration ────────────────────────────────────────────────────


class TestConfidenceCalibration:
    def test_bands_and_gap(self):
        rows = [
            _row({"confidence": 0.9}, judge_score=0.7, iid="a"),
            _row({"confidence": 1.0}, judge_score=0.8, iid="b"),
            _row({"confidence": 0.3}, judge_score=0.6, iid="c"),
        ]
        result = confidence_calibration(rows)
        assert result["n"] == 3
        bands = {b["band"]: b for b in result["buckets"]}
        assert bands["high"]["n"] == 2  # 1.0 lands in "high" via epsilon
        assert bands["high"]["gap"] == pytest.approx(0.95 - 0.75)
        assert bands["low"]["n"] == 1
        assert bands["low"]["gap"] == pytest.approx(0.3 - 0.6)
        assert result["overall_gap"] == pytest.approx((0.2 + 0.2 - 0.3) / 3)

    def test_rows_without_judge_score_excluded(self):
        rows = [_row({"confidence": 0.9}, judge_score=None)]
        result = confidence_calibration(rows)
        assert result["n"] == 0
        assert result["overall_gap"] is None
        assert result["buckets"] == []


# ── break_report ──────────────────────────────────────────────────────────────


class TestBreakReport:
    def test_counts_by_category(self):
        rows = [
            _row({"breaks": ["regression", "expected"]}, iid="a"),
            _row({"breaks": ["regression"]}, iid="b"),
            _row({"tests_passed": 3}, iid="c"),
        ]
        result = break_report(rows)
        assert result["interactions"] == 3
        assert result["interactions_with_breaks"] == 2
        assert result["total_breaks"] == 3
        assert result["by_category"] == {"regression": 2, "expected": 1}

    def test_bare_string_treated_as_single_category(self):
        result = break_report([_row({"breaks": "suspicious"})])
        assert result["by_category"] == {"suspicious": 1}

    def test_unknown_categories_still_counted(self):
        result = break_report([_row({"breaks": ["flaky-env"]})])
        assert result["by_category"] == {"flaky-env": 1}

    def test_empty_breaks_list_not_counted(self):
        result = break_report([_row({"breaks": []})])
        assert result["interactions_with_breaks"] == 0
