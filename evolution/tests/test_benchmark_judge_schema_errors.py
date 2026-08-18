"""Schema-invalid records must affect ranking and reach the JSON output (LIA-580).

Before this, schema errors were counted and printed but fed nothing else: the
composite ranking used only the parse-error rate, so a model that omitted required
dimensions was graded on the favourable subset it did answer, and --json-out emitted
neither schema_errors nor total, so a consumer could not tell a complete benchmark
from one computed on a reduced subset.
"""
import json
import sys

import pytest

from evolution import benchmark_judge as bj


def _result(model: str, *, n: int = 10, parse_errors: int = 0,
            schema_errors: int = 0, total: int | None = None) -> bj.ModelResult:
    """A ModelResult with a fixed, perfectly-correlated score/truth pair.

    Correlation and MAE are therefore identical across every result built here,
    which isolates the error component of composite() as the only thing that varies.
    """
    scores = [i / (n - 1) for i in range(n)]
    return bj.ModelResult(
        model=model,
        scores=list(scores),
        ground_truth=list(scores),
        parse_errors=parse_errors,
        schema_errors=schema_errors,
        total=n if total is None else total,
    )


# ── schema_error_rate ────────────────────────────────────────────────────────

def test_schema_error_rate():
    assert _result("m", n=10, schema_errors=3).schema_error_rate == pytest.approx(0.3)
    assert _result("m", n=10, schema_errors=0).schema_error_rate == 0.0


def test_schema_error_rate_zero_total_does_not_divide_by_zero():
    # Mirrors parse_error_rate: no records benchmarked -> 0.0, not ZeroDivisionError.
    r = bj.ModelResult(model="m", total=0, schema_errors=0)
    assert r.schema_error_rate == 0.0
    r_with_errors = bj.ModelResult(model="m", total=0, schema_errors=5)
    assert r_with_errors.schema_error_rate == 0.0


# ── composite ranking ────────────────────────────────────────────────────────
#
# composite() is a closure inside print_comparison, so it is exercised through
# the ranked table print_comparison emits rather than called directly.

def _ranked_models(capsys, results: list[bj.ModelResult]) -> list[str]:
    bj.print_comparison(results)
    out = capsys.readouterr().out
    body = out.split("BENCHMARK RESULTS", 1)[1]
    return [line.split()[1] for line in body.splitlines() if line[:1].isdigit()]


def test_schema_errors_rank_below_otherwise_equal_clean_model(capsys):
    # Identical correlation and MAE; the only difference is schema errors.
    dirty = _result("dirty", n=10, schema_errors=7)
    clean = _result("clean", n=10)
    order = _ranked_models(capsys, [dirty, clean])
    assert order == ["clean", "dirty"]


def test_error_score_clamps_at_zero(capsys):
    # parse 0.6 + schema 0.6 = 1.2 > 1 -> the error component clamps at 0, so the
    # composite is the correlation + MAE terms alone (0.4 * 1 + 0.3 * 1 = 0.700)
    # rather than going negative.
    saturated = _result("saturated", n=10, parse_errors=6, schema_errors=6)
    bj.print_comparison([saturated])
    row = [line for line in capsys.readouterr().out.splitlines()
           if line.startswith("1 ")][0]
    assert "0.700" in row


# ── --json-out payload ───────────────────────────────────────────────────────

def test_json_out_carries_schema_errors_and_total(tmp_path, monkeypatch, capsys):
    result = _result("m", n=10, parse_errors=1, schema_errors=2, total=13)
    out_path = tmp_path / "bench.json"

    monkeypatch.setattr(bj, "is_ollama_available", lambda: True)
    monkeypatch.setattr(bj, "_get_scored_interactions",
                        lambda limit, clean=False: [{"id": 1}])
    monkeypatch.setattr(bj, "compute_trivial_baselines", lambda interactions: {})
    monkeypatch.setattr(bj, "benchmark",
                        lambda models, interactions, provider="ollama", verbose=True: [result])
    monkeypatch.setattr(sys, "argv",
                        ["benchmark_judge", "--models", "m", "--json-out", str(out_path)])

    bj.main()

    payload = json.loads(out_path.read_text())["results"][0]
    assert payload["parse_errors"] == 1
    assert payload["schema_errors"] == 2
    assert payload["total"] == 13
