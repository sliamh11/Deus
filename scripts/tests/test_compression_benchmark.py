"""
Tests for scripts/compression_benchmark.py (v2 API)

Covers: compute_weighted_score, parse_json, save_golden, load_golden_pairs,
save_result, run_benchmark, run_auto, BEHAVIORAL_TESTS structure, CLI main().
All LLM calls are mocked — no Ollama or network needed.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# -- Import target module -----------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import compression_benchmark as cb


# == compute_weighted_score ===================================================


def test_compute_weighted_score_all_preserved():
    results = [
        {"fact": "A", "status": "preserved", "classification": "critical"},
        {"fact": "B", "status": "preserved", "classification": "critical"},
        {"fact": "C", "status": "preserved", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    assert s["score"] == pytest.approx(100.0)
    assert s["critical_coverage"] == pytest.approx(100.0)
    assert s["missing_critical"] == 0
    assert s["missing_supplementary"] == 0
    assert s["total"] == 3
    assert s["preserved"] == 3
    assert s["derivable"] == 0
    assert s["critical_total"] == 2
    assert s["supplementary_total"] == 1


def test_compute_weighted_score_all_derivable():
    results = [
        {"fact": "A", "status": "derivable", "classification": "critical"},
        {"fact": "B", "status": "derivable", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    # (0.8 + 0.8) / 2 * 100 = 80.0
    assert s["score"] == pytest.approx(80.0)
    assert s["critical_coverage"] == pytest.approx(100.0)
    assert s["preserved"] == 0
    assert s["derivable"] == 2


def test_compute_weighted_score_missing_supplementary():
    results = [
        {"fact": "A", "status": "preserved", "classification": "critical"},
        {"fact": "B", "status": "missing", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    # (1.0 + 0.5) / 2 * 100 = 75.0
    assert s["score"] == pytest.approx(75.0)
    assert s["critical_coverage"] == pytest.approx(100.0)
    assert s["missing_critical"] == 0
    assert s["missing_supplementary"] == 1


def test_compute_weighted_score_missing_critical():
    results = [
        {"fact": "A", "status": "missing", "classification": "critical"},
        {"fact": "B", "status": "preserved", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    # (0.0 + 1.0) / 2 * 100 = 50.0
    assert s["score"] == pytest.approx(50.0)
    assert s["critical_coverage"] == pytest.approx(0.0)
    assert s["missing_critical"] == 1
    assert s["missing_supplementary"] == 0


def test_compute_weighted_score_empty():
    s = cb.compute_weighted_score([])
    assert s == {"score": 0.0, "details": {}}


def test_compute_weighted_score_mixed():
    results = [
        {"fact": "A", "status": "preserved", "classification": "critical"},
        {"fact": "B", "status": "derivable", "classification": "critical"},
        {"fact": "C", "status": "missing", "classification": "critical"},
        {"fact": "D", "status": "preserved", "classification": "supplementary"},
        {"fact": "E", "status": "missing", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    # preserved=2 (1.0 each), derivable=1 (0.8), missing_critical=1 (0.0),
    # missing_supp=1 (0.5) => (2.0 + 0.8 + 0.0 + 0.5) / 5 * 100 = 66.0
    assert s["score"] == pytest.approx(66.0)
    # critical: 2 of 3 preserved/derivable => 66.67%
    assert s["critical_coverage"] == pytest.approx(200.0 / 3)
    assert s["missing_critical"] == 1
    assert s["missing_supplementary"] == 1
    assert s["total"] == 5
    assert s["critical_total"] == 3
    assert s["supplementary_total"] == 2


def test_compute_weighted_score_all_supplementary():
    results = [
        {"fact": "A", "status": "preserved", "classification": "supplementary"},
        {"fact": "B", "status": "missing", "classification": "supplementary"},
    ]
    s = cb.compute_weighted_score(results)
    # (1.0 + 0.5) / 2 * 100 = 75.0
    assert s["score"] == pytest.approx(75.0)
    # No critical facts => 100% critical coverage by default
    assert s["critical_coverage"] == pytest.approx(100.0)
    assert s["critical_total"] == 0
    assert s["supplementary_total"] == 2


# == parse_json ===============================================================


def test_parse_json_plain():
    raw = '[{"fact": "A", "status": "preserved"}]'
    result = cb.parse_json(raw)
    assert isinstance(result, list)
    assert result[0]["fact"] == "A"


def test_parse_json_fenced_with_lang():
    raw = '```json\n[{"fact": "B", "status": "missing"}]\n```'
    result = cb.parse_json(raw)
    assert isinstance(result, list)
    assert result[0]["status"] == "missing"


def test_parse_json_fenced_without_lang():
    raw = '```\n{"key": "value"}\n```'
    result = cb.parse_json(raw)
    assert isinstance(result, dict)
    assert result["key"] == "value"


# == save_golden ==============================================================


def test_save_golden_creates_files(tmp_path):
    golden_dir = tmp_path / "golden"
    # Create source files that save_golden will read
    orig_file = tmp_path / "orig.md"
    comp_file = tmp_path / "comp.md"
    orig_file.write_text("original content here", encoding="utf-8")
    comp_file.write_text("compressed content here", encoding="utf-8")

    with patch.object(cb, "GOLDEN_DIR", golden_dir):
        cb.save_golden(str(orig_file), str(comp_file), "test_label")

    assert (golden_dir / "test_label.original").exists()
    assert (golden_dir / "test_label.compressed").exists()
    assert (golden_dir / "test_label.meta.json").exists()

    assert (golden_dir / "test_label.original").read_text(encoding="utf-8") == "original content here"
    assert (golden_dir / "test_label.compressed").read_text(encoding="utf-8") == "compressed content here"

    meta = json.loads((golden_dir / "test_label.meta.json").read_text(encoding="utf-8"))
    assert meta["label"] == "test_label"
    assert meta["original_words"] == 3
    assert meta["compressed_words"] == 3
    assert "saved_at" in meta


# == load_golden_pairs ========================================================


def test_load_golden_pairs_with_pairs(tmp_path):
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "cv.original").write_text("orig text", encoding="utf-8")
    (golden_dir / "cv.compressed").write_text("comp text", encoding="utf-8")
    meta = {"label": "cv", "original_path": "/a", "compressed_path": "/b",
            "original_words": 2, "compressed_words": 2, "saved_at": "2026-01-01T00:00:00Z"}
    (golden_dir / "cv.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    with patch.object(cb, "GOLDEN_DIR", golden_dir):
        pairs = cb.load_golden_pairs()

    assert len(pairs) == 1
    assert pairs[0]["label"] == "cv"
    assert pairs[0]["original"] == "orig text"
    assert pairs[0]["compressed"] == "comp text"
    assert pairs[0]["meta"]["original_words"] == 2


def test_load_golden_pairs_empty_dir(tmp_path):
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    with patch.object(cb, "GOLDEN_DIR", golden_dir):
        assert cb.load_golden_pairs() == []


def test_load_golden_pairs_nonexistent_dir(tmp_path):
    golden_dir = tmp_path / "does_not_exist"
    with patch.object(cb, "GOLDEN_DIR", golden_dir):
        assert cb.load_golden_pairs() == []


# == save_result ==============================================================


def test_save_result_appends_jsonl(tmp_path):
    results_log = tmp_path / "compression.jsonl"
    with (
        patch.object(cb, "RESULTS_LOG", results_log),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        cb.save_result({"label": "cv", "pass": True})

    lines = results_log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["label"] == "cv"
    assert record["pass"] is True
    assert "timestamp" in record


def test_save_result_appends_multiple(tmp_path):
    results_log = tmp_path / "compression.jsonl"
    with (
        patch.object(cb, "RESULTS_LOG", results_log),
        patch.object(cb, "BENCHMARK_DIR", tmp_path),
    ):
        cb.save_result({"label": "first", "pass": True})
        cb.save_result({"label": "second", "pass": False})

    lines = results_log.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["label"] == "first"
    assert json.loads(lines[1])["label"] == "second"


# == run_benchmark (LLM mocked) ==============================================


def _mock_extract_facts(text):
    """Return a small set of classified facts."""
    return [
        {"fact": "User is Liam", "classification": "critical"},
        {"fact": "Located in Israel", "classification": "critical"},
        {"fact": "Uses Node.js", "classification": "supplementary"},
    ]


def _mock_verify_all_preserved(facts, compressed):
    """All facts preserved."""
    return [
        {"fact": f["fact"], "status": "preserved", "classification": f["classification"]}
        for f in facts
    ]


def _mock_behavioral_all_pass(compressed, test_set):
    """All behavioral tests pass."""
    tests = cb.BEHAVIORAL_TESTS.get(test_set, cb.BEHAVIORAL_TESTS["claude_vault"])
    return [{"query": q, "score": "PASS", "note": ""} for q, _ in tests]


def _mock_behavioral_half_fail(compressed, test_set):
    """Half of behavioral tests fail."""
    tests = cb.BEHAVIORAL_TESTS.get(test_set, cb.BEHAVIORAL_TESTS["claude_vault"])
    results = []
    for i, (q, _) in enumerate(tests):
        score = "PASS" if i % 2 == 0 else "FAIL"
        results.append({"query": q, "score": score, "note": ""})
    return results


def test_run_benchmark_pass(tmp_path):
    original = "This is the original document with many words for testing purposes."
    compressed = "Original doc many words testing."

    with (
        patch.object(cb, "extract_and_classify_facts", _mock_extract_facts),
        patch.object(cb, "verify_facts", _mock_verify_all_preserved),
        patch.object(cb, "run_behavioral", _mock_behavioral_all_pass),
    ):
        result = cb.run_benchmark(original, compressed, "claude_vault", quiet=True)

    assert result["label"] == "claude_vault"
    assert result["pass"] is True
    assert result["critical_coverage"] == 100.0
    assert result["behavioral_score"] == 100.0
    assert result["facts_total"] == 3
    assert result["facts_critical"] == 2
    assert result["missing_critical_facts"] == []
    assert result["failed_behavioral"] == []
    assert isinstance(result["original_words"], int)
    assert isinstance(result["compressed_words"], int)
    assert isinstance(result["reduction_pct"], float)
    assert isinstance(result["weighted_score"], float)


def test_run_benchmark_fail_behavioral(tmp_path):
    original = "This is the original document."
    compressed = "Compressed."

    with (
        patch.object(cb, "extract_and_classify_facts", _mock_extract_facts),
        patch.object(cb, "verify_facts", _mock_verify_all_preserved),
        patch.object(cb, "run_behavioral", _mock_behavioral_half_fail),
    ):
        result = cb.run_benchmark(original, compressed, "claude_vault", quiet=True)

    assert result["pass"] is False
    assert result["behavioral_score"] < 90.0
    assert len(result["failed_behavioral"]) > 0


# == run_auto =================================================================


def test_run_auto_no_pairs(tmp_path):
    golden_dir = tmp_path / "golden"
    with patch.object(cb, "GOLDEN_DIR", golden_dir):
        exit_code = cb.run_auto()
    assert exit_code == 1


def test_run_auto_with_passing_pairs(tmp_path):
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "cv.original").write_text("original words here", encoding="utf-8")
    (golden_dir / "cv.compressed").write_text("compressed text", encoding="utf-8")
    meta = {"label": "cv", "original_path": "/a", "compressed_path": "/b",
            "original_words": 3, "compressed_words": 2, "saved_at": "2026-01-01T00:00:00Z"}
    (golden_dir / "cv.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    passing_result = {
        "label": "cv", "pass": True, "original_words": 3, "compressed_words": 2,
        "reduction_pct": 33.3, "facts_total": 3, "facts_critical": 2,
        "critical_coverage": 100.0, "weighted_score": 100.0,
        "behavioral_passed": 25, "behavioral_total": 25, "behavioral_score": 100.0,
        "missing_critical_facts": [], "failed_behavioral": [],
    }

    with (
        patch.object(cb, "GOLDEN_DIR", golden_dir),
        patch.object(cb, "run_benchmark", return_value=passing_result),
        patch.object(cb, "save_result", MagicMock()),
    ):
        exit_code = cb.run_auto()

    assert exit_code == 0


def test_run_auto_with_failing_pair(tmp_path):
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "cv.original").write_text("original words", encoding="utf-8")
    (golden_dir / "cv.compressed").write_text("compressed", encoding="utf-8")
    meta = {"label": "cv", "original_path": "/a", "compressed_path": "/b",
            "original_words": 2, "compressed_words": 1, "saved_at": "2026-01-01T00:00:00Z"}
    (golden_dir / "cv.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    failing_result = {
        "label": "cv", "pass": False, "original_words": 2, "compressed_words": 1,
        "reduction_pct": 50.0, "facts_total": 3, "facts_critical": 2,
        "critical_coverage": 50.0, "weighted_score": 50.0,
        "behavioral_passed": 10, "behavioral_total": 25, "behavioral_score": 40.0,
        "missing_critical_facts": ["some fact"], "failed_behavioral": ["some query"],
    }

    with (
        patch.object(cb, "GOLDEN_DIR", golden_dir),
        patch.object(cb, "run_benchmark", return_value=failing_result),
        patch.object(cb, "save_result", MagicMock()),
    ):
        exit_code = cb.run_auto()

    assert exit_code == 1


# == BEHAVIORAL_TESTS structure ===============================================


def test_behavioral_tests_has_required_keys():
    assert "claude_vault" in cb.BEHAVIORAL_TESTS
    assert "memory_index" in cb.BEHAVIORAL_TESTS


def test_behavioral_tests_claude_vault_count():
    assert len(cb.BEHAVIORAL_TESTS["claude_vault"]) == 25


def test_behavioral_tests_memory_index_count():
    assert len(cb.BEHAVIORAL_TESTS["memory_index"]) == 25


def test_behavioral_tests_tuple_structure():
    for test_set_name, tests in cb.BEHAVIORAL_TESTS.items():
        for idx, entry in enumerate(tests):
            assert isinstance(entry, tuple), (
                f"{test_set_name}[{idx}] is {type(entry).__name__}, expected tuple"
            )
            assert len(entry) == 2, (
                f"{test_set_name}[{idx}] has {len(entry)} elements, expected 2"
            )
            question, expected_answer = entry
            assert isinstance(question, str) and len(question) > 0
            assert isinstance(expected_answer, str) and len(expected_answer) > 0


# == CLI main() ===============================================================


def test_cli_auto_mode(tmp_path):
    with (
        patch.object(cb, "run_auto", return_value=0) as mock_auto,
        patch("sys.argv", ["compression_benchmark.py", "--auto"]),
    ):
        exit_code = cb.main()

    assert exit_code == 0
    mock_auto.assert_called_once()


def test_cli_manual_mode(tmp_path):
    orig_file = tmp_path / "orig.md"
    comp_file = tmp_path / "comp.md"
    orig_file.write_text("original words here", encoding="utf-8")
    comp_file.write_text("compressed text", encoding="utf-8")

    manual_result = {
        "label": "claude_vault", "pass": True, "original_words": 3,
        "compressed_words": 2, "reduction_pct": 33.3, "facts_total": 3,
        "facts_critical": 2, "critical_coverage": 100.0, "weighted_score": 100.0,
        "behavioral_passed": 25, "behavioral_total": 25, "behavioral_score": 100.0,
        "missing_critical_facts": [], "failed_behavioral": [],
    }

    with (
        patch.object(cb, "run_benchmark", return_value=manual_result) as mock_bench,
        patch("sys.argv", [
            "compression_benchmark.py", str(orig_file), str(comp_file),
            "--label", "claude_vault",
        ]),
    ):
        exit_code = cb.main()

    assert exit_code == 0
    mock_bench.assert_called_once()
    call_args = mock_bench.call_args
    assert call_args[0][2] == "claude_vault"  # label argument


# == Module constants =========================================================


def test_module_constants_are_paths():
    assert isinstance(cb.BENCHMARK_DIR, Path)
    assert isinstance(cb.GOLDEN_DIR, Path)
    assert isinstance(cb.RESULTS_LOG, Path)
