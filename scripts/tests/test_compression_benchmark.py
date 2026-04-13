"""
Tests for scripts/compression_benchmark.py

Covers: scoring logic, fact classification, golden file management, results logging.
LLM calls are mocked throughout -- no Ollama needed.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# -- Import target module -----------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import compression_benchmark as cb


# -- compute_weighted_score ---------------------------------------------------


def test_weighted_score_all_preserved():
    results = [
        {"status": "preserved", "classification": "critical", "fact": "a"},
        {"status": "preserved", "classification": "critical", "fact": "b"},
        {"status": "preserved", "classification": "supplementary", "fact": "c"},
    ]
    scores = cb.compute_weighted_score(results)
    assert scores["weighted_score"] == pytest.approx(1.0)
    assert scores["critical_coverage"] == pytest.approx(1.0)
    assert scores["missing_critical"] == 0
    assert scores["missing_supplementary"] == 0


def test_weighted_score_derivable_counts():
    results = [
        {"status": "derivable", "classification": "critical", "fact": "a"},
        {"status": "derivable", "classification": "supplementary", "fact": "b"},
    ]
    scores = cb.compute_weighted_score(results)
    # derivable * 0.8 each -> 1.6 / 2 = 0.8
    assert scores["weighted_score"] == pytest.approx(0.8)
    assert scores["critical_coverage"] == pytest.approx(1.0)


def test_weighted_score_missing_supplementary():
    results = [
        {"status": "preserved", "classification": "critical", "fact": "a"},
        {"status": "missing", "classification": "supplementary", "fact": "b"},
    ]
    scores = cb.compute_weighted_score(results)
    # (1.0 + 0.5) / 2 = 0.75
    assert scores["weighted_score"] == pytest.approx(0.75)
    assert scores["critical_coverage"] == pytest.approx(1.0)
    assert scores["missing_supplementary"] == 1


def test_weighted_score_missing_critical():
    results = [
        {"status": "preserved", "classification": "critical", "fact": "a"},
        {"status": "missing", "classification": "critical", "fact": "b"},
    ]
    scores = cb.compute_weighted_score(results)
    assert scores["critical_coverage"] == pytest.approx(0.5)
    assert scores["missing_critical"] == 1


def test_weighted_score_empty():
    scores = cb.compute_weighted_score([])
    assert scores["weighted_score"] == pytest.approx(0.0)
    assert scores["critical_coverage"] == pytest.approx(0.0)
    assert scores["total"] == 0


def test_weighted_score_mixed():
    results = [
        {"status": "preserved", "classification": "critical", "fact": "a"},
        {"status": "derivable", "classification": "critical", "fact": "b"},
        {"status": "missing", "classification": "critical", "fact": "c"},
        {"status": "preserved", "classification": "supplementary", "fact": "d"},
        {"status": "missing", "classification": "supplementary", "fact": "e"},
    ]
    scores = cb.compute_weighted_score(results)
    # critical: 2/3 covered
    assert scores["critical_coverage"] == pytest.approx(2.0 / 3.0)
    # weighted: (1.0 + 0.8 + 0 + 1.0 + 0.5) / 5 = 3.3/5 = 0.66
    assert scores["weighted_score"] == pytest.approx(3.3 / 5.0)
    assert scores["missing_critical"] == 1
    assert scores["missing_supplementary"] == 1


def test_weighted_score_all_supplementary():
    """No critical facts -> critical_coverage defaults to 1.0."""
    results = [
        {"status": "preserved", "classification": "supplementary", "fact": "a"},
        {"status": "missing", "classification": "supplementary", "fact": "b"},
    ]
    scores = cb.compute_weighted_score(results)
    assert scores["critical_coverage"] == pytest.approx(1.0)


# -- parse_json ---------------------------------------------------------------


def test_parse_json_plain():
    assert cb.parse_json('[1, 2, 3]') == [1, 2, 3]


def test_parse_json_fenced():
    text = '```json\n[1, 2]\n```'
    assert cb.parse_json(text) == [1, 2]


def test_parse_json_fenced_no_lang():
    text = '```\n{"a": 1}\n```'
    assert cb.parse_json(text) == {"a": 1}


# -- golden file management ---------------------------------------------------


def test_save_golden_creates_files(tmp_path):
    with patch.object(cb, "GOLDEN_DIR", tmp_path):
        cb.save_golden("test_label", "original text", "compressed text")

    assert (tmp_path / "test_label.original").read_text() == "original text"
    assert (tmp_path / "test_label.compressed").read_text() == "compressed text"


def test_list_golden_pairs(tmp_path):
    (tmp_path / "claude_vault.original").write_text("orig")
    (tmp_path / "claude_vault.compressed").write_text("comp")
    (tmp_path / "memory_index.original").write_text("orig2")
    (tmp_path / "memory_index.compressed").write_text("comp2")
    # Orphan file with no pair
    (tmp_path / "orphan.original").write_text("no pair")

    with patch.object(cb, "GOLDEN_DIR", tmp_path):
        pairs = cb.list_golden_pairs()

    labels = [p[0] for p in pairs]
    assert "claude_vault" in labels
    assert "memory_index" in labels
    assert "orphan" not in labels


def test_list_golden_pairs_empty(tmp_path):
    with patch.object(cb, "GOLDEN_DIR", tmp_path / "nonexistent"):
        pairs = cb.list_golden_pairs()
    assert pairs == []


# -- save_results -------------------------------------------------------------


def test_save_results_appends_jsonl(tmp_path):
    log = tmp_path / "compression.jsonl"
    with (
        patch.object(cb, "RESULTS_LOG", log),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        cb.save_results({"label": "test", "pass": True})

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["label"] == "test"
    assert "timestamp" in record


def test_save_results_multiple(tmp_path):
    log = tmp_path / "compression.jsonl"
    with (
        patch.object(cb, "RESULTS_LOG", log),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        cb.save_results({"label": "run1"})
        cb.save_results({"label": "run2"})

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2


# -- run_benchmark (LLM mocked) ----------------------------------------------


def _mock_extract_and_classify(text):
    return [
        {"fact": "User name is Liam", "classification": "critical"},
        {"fact": "File at /some/path.py", "classification": "supplementary"},
    ]


def _mock_verify_facts(facts, compressed, batch_size=20):
    return [
        {"fact": "User name is Liam", "status": "preserved", "classification": "critical", "note": ""},
        {"fact": "File at /some/path.py", "status": "missing", "classification": "supplementary", "note": ""},
    ]


def _mock_run_behavioral(compressed, test_set):
    tests = cb.BEHAVIORAL_TESTS.get(test_set, cb.BEHAVIORAL_TESTS["claude_vault"])
    return [{"query": q, "score": "PASS", "note": ""} for q, _ in tests]


def test_run_benchmark_pass(tmp_path):
    with (
        patch.object(cb, "extract_and_classify_facts", _mock_extract_and_classify),
        patch.object(cb, "verify_facts", _mock_verify_facts),
        patch.object(cb, "run_behavioral", _mock_run_behavioral),
        patch.object(cb, "RESULTS_LOG", tmp_path / "results.jsonl"),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        result = cb.run_benchmark(
            "original " * 100, "compressed " * 60, "claude_vault", save=True, quiet=True
        )

    assert result["label"] == "claude_vault"
    assert result["critical_coverage"] == pytest.approx(1.0)
    assert result["behavioral_score"] == pytest.approx(1.0)
    assert result["pass"] is True


def test_run_benchmark_fail_behavioral(tmp_path):
    def mock_behavioral_fail(compressed, test_set):
        tests = cb.BEHAVIORAL_TESTS.get(test_set, cb.BEHAVIORAL_TESTS["claude_vault"])
        # Fail most tests
        return [{"query": q, "score": "FAIL", "note": ""} for q, _ in tests]

    with (
        patch.object(cb, "extract_and_classify_facts", _mock_extract_and_classify),
        patch.object(cb, "verify_facts", _mock_verify_facts),
        patch.object(cb, "run_behavioral", mock_behavioral_fail),
        patch.object(cb, "RESULTS_LOG", tmp_path / "results.jsonl"),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        result = cb.run_benchmark(
            "original " * 100, "compressed " * 60, "claude_vault", save=True, quiet=True
        )

    assert result["pass"] is False
    assert result["behavioral_score"] == pytest.approx(0.0)


# -- run_auto -----------------------------------------------------------------


def test_run_auto_no_pairs(tmp_path):
    with patch.object(cb, "GOLDEN_DIR", tmp_path / "nonexistent"):
        assert cb.run_auto() is True  # no pairs = nothing to fail


def test_run_auto_with_pairs(tmp_path):
    (tmp_path / "test.original").write_text("original content here")
    (tmp_path / "test.compressed").write_text("compressed content here")

    mock_result = {
        "label": "test",
        "pass": True,
        "critical_coverage": 1.0,
        "behavioral_score": 1.0,
    }

    with (
        patch.object(cb, "GOLDEN_DIR", tmp_path),
        patch.object(cb, "run_benchmark", return_value=mock_result),
    ):
        assert cb.run_auto() is True


def test_run_auto_fails_if_any_pair_fails(tmp_path):
    (tmp_path / "good.original").write_text("orig")
    (tmp_path / "good.compressed").write_text("comp")
    (tmp_path / "bad.original").write_text("orig")
    (tmp_path / "bad.compressed").write_text("comp")

    def mock_benchmark(original, compressed, label, save=True, quiet=False):
        return {"label": label, "pass": label == "good"}

    with (
        patch.object(cb, "GOLDEN_DIR", tmp_path),
        patch.object(cb, "run_benchmark", mock_benchmark),
    ):
        assert cb.run_auto() is False


# -- behavioral test coverage -------------------------------------------------


def test_behavioral_tests_claude_vault_count():
    """Verify we have 18+ behavioral tests for claude_vault."""
    assert len(cb.BEHAVIORAL_TESTS["claude_vault"]) >= 18


def test_behavioral_tests_memory_index_count():
    """Verify we have 16+ behavioral tests for memory_index."""
    assert len(cb.BEHAVIORAL_TESTS["memory_index"]) >= 16


def test_behavioral_tests_have_expected_answer():
    """Every test tuple must have (question, expected_answer)."""
    for test_set, tests in cb.BEHAVIORAL_TESTS.items():
        for i, t in enumerate(tests):
            assert len(t) == 2, f"{test_set}[{i}] must be (question, answer) tuple"
            assert len(t[0]) > 10, f"{test_set}[{i}] question too short"
            assert len(t[1]) > 0, f"{test_set}[{i}] expected answer empty"


# -- CLI arg parsing ----------------------------------------------------------


def test_main_auto_mode(tmp_path):
    with (
        patch("sys.argv", ["compression_benchmark.py", "--auto"]),
        patch.object(cb, "run_auto", return_value=True) as mock_auto,
    ):
        assert cb.main() == 0
        mock_auto.assert_called_once()


def test_main_manual_mode(tmp_path):
    orig = tmp_path / "orig.md"
    comp = tmp_path / "comp.md"
    orig.write_text("original")
    comp.write_text("compressed")

    mock_result = {"pass": True}
    with (
        patch("sys.argv", ["compression_benchmark.py", str(orig), str(comp), "--label", "claude_vault"]),
        patch.object(cb, "run_benchmark", return_value=mock_result),
    ):
        assert cb.main() == 0
