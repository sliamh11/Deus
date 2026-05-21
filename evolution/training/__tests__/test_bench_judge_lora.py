"""Unit tests for evolution.training.bench_judge_lora (mock-only).

These tests do NOT load a real mlx_lm model — that path is exercised
manually via `bench_judge_lora.py --limit 2 --skip-base` before any push
that touches the inference loop. The tests here cover:

  - parser correctness on valid / fenced / malformed / partial / out-of-range JSON
  - metric helpers (Pearson, Spearman, MAE) edge cases
  - test-split loader validation
  - adapter-path resolution
  - mocked end-to-end `run_bench` flow (seed kwarg absent, cleanup fired)
  - results JSON schema

Total: 22 tests. Plan-reviewer R2 SHIP + code-reviewer R2 SHIP.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from evolution.training import bench_judge_lora as bj  # noqa: E402


# ----------------------------- parsing -------------------------------------


def test_parse_judge_response_valid():
    raw = '{"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8, "rationale": "ok"}'
    scores, err = bj.parse_judge_response(raw)
    assert err is None
    assert scores == {"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}


def test_parse_judge_response_fenced_json():
    raw = '```json\n{"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}\n```'
    scores, err = bj.parse_judge_response(raw)
    assert err is None
    assert scores["quality"] == 0.9
    assert scores["safety"] == 1.0


def test_parse_judge_response_missing_dim():
    # missing tool_use + personalization
    raw = '{"quality": 0.9, "safety": 0.8}'
    scores, err = bj.parse_judge_response(raw)
    assert err is not None
    assert "tool_use" in err
    assert "personalization" in err
    assert scores["tool_use"] == 0.5
    assert scores["personalization"] == 0.5
    # present dims kept
    assert scores["quality"] == 0.9


def test_parse_judge_response_malformed():
    scores, err = bj.parse_judge_response("not json at all")
    assert err is not None
    assert all(scores[d] == 0.5 for d in bj.DIMENSIONS)


def test_parse_judge_response_clamps_out_of_range():
    raw = '{"quality": 1.5, "safety": -0.1, "tool_use": 0.5, "personalization": 0.5}'
    scores, err = bj.parse_judge_response(raw)
    assert err is None  # all dims present + parseable, clamping is silent
    assert scores["quality"] == 1.0
    assert scores["safety"] == 0.0


def test_parse_judge_response_empty():
    scores, err = bj.parse_judge_response("")
    assert err == "empty response"
    assert all(scores[d] == 0.5 for d in bj.DIMENSIONS)


# ----------------------------- metrics -------------------------------------


def test_pearson_zero_variance():
    """Identical input → returns 0.0 (not div-zero crash). Convention from
    evolution/benchmark_judge.py:76-87."""
    assert bj._pearson([0.5, 0.5, 0.5, 0.5], [0.1, 0.2, 0.3, 0.4]) == 0.0
    assert bj._pearson([0.1, 0.2, 0.3, 0.4], [0.5, 0.5, 0.5, 0.5]) == 0.0


def test_pearson_perfect_correlation():
    assert bj._pearson([0.1, 0.2, 0.3, 0.4], [0.1, 0.2, 0.3, 0.4]) == pytest.approx(1.0)
    assert bj._pearson([0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]) == pytest.approx(-1.0)


def test_spearman_perfect_correlation():
    # Spearman of sorted-vs-sorted is 1.0; reverse is -1.0
    assert bj._spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert bj._spearman([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_mae_simple():
    # |0.1-0.0| + |0.2-0.1| + |0.3-0.5| = 0.1 + 0.1 + 0.2 = 0.4 / 3 ≈ 0.1333
    assert bj._mae([0.1, 0.2, 0.3], [0.0, 0.1, 0.5]) == pytest.approx(0.4 / 3)


def test_metrics_n_below_3_returns_zero():
    assert bj._pearson([0.1, 0.2], [0.3, 0.4]) == 0.0
    assert bj._spearman([0.1, 0.2], [0.3, 0.4]) == 0.0


# ----------------------------- loaders -------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_load_test_records_validates_shape(tmp_path: Path):
    valid = {
        "messages": [
            {"role": "user", "content": "score this"},
            {"role": "assistant", "content": '{"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}'},
        ]
    }
    invalid_role = {
        "messages": [
            {"role": "system", "content": "x"},
            {"role": "assistant", "content": "x"},
        ]
    }
    invalid_count = {"messages": [{"role": "user", "content": "x"}]}

    valid_path = tmp_path / "valid.jsonl"
    _write_jsonl(valid_path, [valid, valid])
    records = bj.load_test_records(valid_path)
    assert len(records) == 2
    assert records[0]["user_prompt"] == "score this"
    assert records[0]["ground_truth"]["quality"] == 0.9

    bad_role_path = tmp_path / "bad_role.jsonl"
    _write_jsonl(bad_role_path, [invalid_role])
    with pytest.raises(ValueError, match="role mismatch"):
        bj.load_test_records(bad_role_path)

    bad_count_path = tmp_path / "bad_count.jsonl"
    _write_jsonl(bad_count_path, [invalid_count])
    with pytest.raises(ValueError, match="expected messages"):
        bj.load_test_records(bad_count_path)


def test_load_test_records_respects_limit(tmp_path: Path):
    valid = {
        "messages": [
            {"role": "user", "content": "score this"},
            {"role": "assistant", "content": '{"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}'},
        ]
    }
    path = tmp_path / "many.jsonl"
    _write_jsonl(path, [valid] * 10)
    records = bj.load_test_records(path, limit=3)
    assert len(records) == 3


# ----------------------------- adapter resolution --------------------------


def test_resolve_default_adapter_path_picks_latest(tmp_path: Path):
    # Three ULID-style run dirs; lexicographically-largest should be picked
    for run_id in ["20260518T010000Z-aaa", "20260518T020000Z-bbb", "20260518T030000Z-ccc"]:
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "adapters.safetensors").write_bytes(b"\x00")
        (run_dir / "adapter_config.json").write_text("{}")
    chosen = bj.resolve_default_adapter_path(tmp_path)
    assert chosen.name == "20260518T030000Z-ccc"


def test_resolve_default_adapter_path_missing_safetensors(tmp_path: Path):
    run_dir = tmp_path / "20260518T010000Z-aaa"
    run_dir.mkdir()
    (run_dir / "adapter_config.json").write_text("{}")
    # No adapters.safetensors
    with pytest.raises(FileNotFoundError, match="adapters.safetensors"):
        bj.resolve_default_adapter_path(tmp_path)


def test_resolve_default_adapter_path_empty_dir(tmp_path: Path):
    # Adapters root exists but has no subdirs
    with pytest.raises(FileNotFoundError, match="no adapter directories"):
        bj.resolve_default_adapter_path(tmp_path)


# ----------------------------- run_bench with mocks ------------------------


def _install_mlx_mocks(monkeypatch, generate_outputs: list[str], capture: dict):
    """Install fake mlx + mlx_lm into sys.modules so run_bench can import them.

    `capture` dict will accumulate observed call args."""
    capture.setdefault("generate_calls", [])
    capture.setdefault("load_calls", [])
    capture.setdefault("seed_calls", [])
    capture.setdefault("clear_cache_calls", 0)
    capture.setdefault("gc_collect_calls", 0)

    # ----- fake mlx.core / mlx.core.random / mlx.core.metal -----
    fake_mlx = types.ModuleType("mlx")
    fake_mlx_core = types.ModuleType("mlx.core")
    fake_mlx_random = types.ModuleType("mlx.core.random")
    fake_mlx_metal = types.ModuleType("mlx.core.metal")

    def fake_seed(s):
        capture["seed_calls"].append(s)

    def fake_clear_cache():
        capture["clear_cache_calls"] += 1

    fake_mlx_random.seed = fake_seed
    fake_mlx_metal.clear_cache = fake_clear_cache
    fake_mlx_core.random = fake_mlx_random
    fake_mlx_core.metal = fake_mlx_metal
    fake_mlx.core = fake_mlx_core

    # ----- fake mlx_lm.load + mlx_lm.generate + sample_utils.make_sampler -----
    class _FakeTok:
        def apply_chat_template(self, messages, add_generation_prompt, tokenize):
            # Return the user prompt verbatim so we can assert on it later
            assert add_generation_prompt is True
            assert tokenize is False
            return f"<<<{messages[0]['content']}>>>"

    class _FakeModel:
        pass

    class _FakeSampler:
        """Marker so we can assert the sampler kwarg was the one we built."""
        def __init__(self, temp):
            self.temp = temp

    def fake_load(base_model, adapter_path=None):
        capture["load_calls"].append({"base_model": base_model, "adapter_path": adapter_path})
        return _FakeModel(), _FakeTok()

    def fake_make_sampler(temp=0.0, **kw):
        capture.setdefault("make_sampler_calls", []).append({"temp": temp, **kw})
        return _FakeSampler(temp=temp)

    output_iter = iter(generate_outputs)

    def fake_generate(model, tok, prompt, **kwargs):
        capture["generate_calls"].append({"prompt": prompt, "kwargs": dict(kwargs)})
        try:
            return next(output_iter)
        except StopIteration:
            return ""

    fake_mlx_lm = types.ModuleType("mlx_lm")
    fake_mlx_lm.load = fake_load
    fake_mlx_lm.generate = fake_generate
    fake_sample_utils = types.ModuleType("mlx_lm.sample_utils")
    fake_sample_utils.make_sampler = fake_make_sampler

    monkeypatch.setitem(sys.modules, "mlx", fake_mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mlx_core)
    monkeypatch.setitem(sys.modules, "mlx.core.random", fake_mlx_random)
    monkeypatch.setitem(sys.modules, "mlx.core.metal", fake_mlx_metal)
    monkeypatch.setitem(sys.modules, "mlx_lm", fake_mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", fake_sample_utils)

    # mlx.core.metal access via `mx.metal.clear_cache()` — mx is mlx.core.
    # Some real installs expose it as `mx.metal`; we already wired that above.

    # ----- patch gc.collect to count -----
    real_gc_collect = bj.gc.collect

    def fake_gc_collect():
        capture["gc_collect_calls"] += 1
        return real_gc_collect()

    monkeypatch.setattr(bj.gc, "collect", fake_gc_collect)


def test_run_bench_uses_mocked_mlx_lm(monkeypatch):
    """End-to-end run_bench with mocked mlx, asserting:
    - mlx_lm.load called with adapter_path
    - mx.random.seed called exactly ONCE before the loop
    - mlx_lm.generate called with max_tokens, temp, verbose — and NO seed kwarg
    - chat template applied to each user prompt
    - cleanup_after=True → gc.collect + mx.metal.clear_cache both called
    """
    records = [
        {
            "user_prompt": f"prompt-{i}",
            "ground_truth": {"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8},
        }
        for i in range(3)
    ]
    outputs = [
        '{"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}',
        '{"quality": 0.5, "safety": 1.0, "tool_use": 0.5, "personalization": 0.5}',
        "garbage not json",
    ]
    capture: dict = {}
    _install_mlx_mocks(monkeypatch, outputs, capture)

    result = bj.run_bench(
        backend_name="adapter",
        base_model="fake-base",
        adapter_path=Path("/tmp/fake-adapter"),
        records=records,
        max_tokens=128,
        temp=0.0,
        seed=42,
        quiet=True,
        cleanup_after=True,
    )

    # --- assertions ---
    # Load was called once with adapter path
    assert len(capture["load_calls"]) == 1
    assert capture["load_calls"][0]["adapter_path"] == "/tmp/fake-adapter"

    # Seed called once before the loop
    assert capture["seed_calls"] == [42]

    # make_sampler called once with the configured temp BEFORE the loop
    assert capture.get("make_sampler_calls") == [{"temp": 0.0}]

    # All 3 records generated, each call had the right kwargs
    assert len(capture["generate_calls"]) == 3
    for call in capture["generate_calls"]:
        assert "max_tokens" in call["kwargs"]
        assert call["kwargs"]["max_tokens"] == 128
        assert call["kwargs"]["verbose"] is False
        # The sampler from make_sampler() is forwarded
        assert "sampler" in call["kwargs"]
        # CRITICAL regression guards — mlx_lm.generate_step does NOT accept
        # `seed` or `temp` (verified empirically against mlx_lm 0.31.3).
        # Both would raise TypeError. seed is via mx.random.seed(),
        # temp is via make_sampler(temp=...).
        assert "seed" not in call["kwargs"], (
            f"seed leaked into mlx_lm.generate kwargs: {call['kwargs']}. "
            f"Should be set via mx.random.seed() once before the loop."
        )
        assert "temp" not in call["kwargs"], (
            f"temp leaked into mlx_lm.generate kwargs: {call['kwargs']}. "
            f"Should be wrapped via make_sampler(temp=...) and passed as sampler=."
        )

    # Chat template applied (prompts wrapped in <<<...>>> by the fake tok)
    for i, call in enumerate(capture["generate_calls"]):
        assert call["prompt"] == f"<<<prompt-{i}>>>"

    # Cleanup fired exactly once (cleanup_after=True)
    assert capture["gc_collect_calls"] == 1
    assert capture["clear_cache_calls"] == 1

    # Records captured + parse outcomes
    assert result.n == 3
    assert result.records[0].parse_ok is True
    assert result.records[1].parse_ok is True
    assert result.records[2].parse_ok is False  # "garbage not json"


def test_run_bench_invokes_metal_cleanup_between_backends(monkeypatch):
    """Plan-reviewer R1 informational: a regression test that guards against
    the cleanup-between-backends path being silently dropped. When
    `cleanup_after=True`, both gc.collect and mx.metal.clear_cache fire;
    when `cleanup_after=False`, neither does."""
    records = [
        {
            "user_prompt": "x",
            "ground_truth": {"quality": 0.5, "safety": 0.5, "tool_use": 0.5, "personalization": 0.5},
        }
    ]
    outputs = ['{"quality": 0.5, "safety": 0.5, "tool_use": 0.5, "personalization": 0.5}']

    # cleanup_after=True path
    capture_t: dict = {}
    _install_mlx_mocks(monkeypatch, outputs, capture_t)
    bj.run_bench(
        backend_name="adapter",
        base_model="fake",
        adapter_path=Path("/tmp/x"),
        records=records,
        max_tokens=16,
        temp=0.0,
        seed=1,
        quiet=True,
        cleanup_after=True,
    )
    assert capture_t["gc_collect_calls"] == 1
    assert capture_t["clear_cache_calls"] == 1

    # cleanup_after=False path (final backend in a sequence)
    capture_f: dict = {}
    _install_mlx_mocks(monkeypatch, outputs, capture_f)
    bj.run_bench(
        backend_name="base",
        base_model="fake",
        adapter_path=None,
        records=records,
        max_tokens=16,
        temp=0.0,
        seed=1,
        quiet=True,
        cleanup_after=False,
    )
    assert capture_f["gc_collect_calls"] == 0
    assert capture_f["clear_cache_calls"] == 0


# ----------------------------- save_results_json ---------------------------


def test_save_results_json_atomic_and_valid_schema(tmp_path: Path):
    """Build minimal BackendResult fixtures, write JSON, re-read and assert
    schema-level expectations."""
    adapter = bj.BackendResult(
        name="adapter", base_model="b", adapter_path="/a", load_time_s=1.5
    )
    base = bj.BackendResult(name="base", base_model="b", adapter_path=None, load_time_s=2.0)
    # Add 3 records each so Pearson/Spearman are computable
    for i in range(3):
        gt = {"quality": 0.5 + 0.1 * i, "safety": 1.0, "tool_use": 0.5, "personalization": 0.5}
        adapter.records.append(
            bj.BenchRecord(
                interaction_idx=i,
                prompt_preview="p",
                ground_truth=gt,
                generated_scores=gt,
                parse_ok=True,
                parse_error=None,
                latency_s=0.1,
                raw_generated="{}",
            )
        )
        base.records.append(
            bj.BenchRecord(
                interaction_idx=i,
                prompt_preview="p",
                ground_truth=gt,
                generated_scores={"quality": 0.5, "safety": 0.5, "tool_use": 0.5, "personalization": 0.5},
                parse_ok=False,
                parse_error="malformed",
                latency_s=0.2,
                raw_generated="oops",
            )
        )

    out = tmp_path / "result.json"
    bj.save_results_json(
        adapter,
        base,
        out,
        base_model="b",
        test_jsonl_path=Path("/fake/test.jsonl"),
        test_jsonl_sha256="deadbeef",
        seed=42,
    )

    assert out.exists()
    # Tmp file was atomically renamed away
    assert not (tmp_path / "result.json.tmp").exists()

    data = json.loads(out.read_text())
    assert data["schema_version"] == 1
    assert data["base_model"] == "b"
    assert data["seed"] == 42
    assert data["test_jsonl_sha256"] == "deadbeef"
    assert data["dimensions"] == list(bj.DIMENSIONS)
    assert len(data["backends"]) == 2
    assert data["backends"][0]["name"] == "adapter"
    assert data["backends"][1]["name"] == "base"
    # Records preserved
    assert len(data["backends"][0]["records"]) == 3
    # Verdict present (two-backend payload)
    assert "verdict" in data
    assert "mean_pearson_delta" in data["verdict"]
    assert "parse_error_rate_delta" in data["verdict"]


# ----------------------------- argument validation ------------------------


def test_main_rejects_negative_temp(tmp_path: Path):
    """Code-reviewer R1: temp must be >= 0.0; negative values would silently
    pass to make_sampler and produce undefined behavior."""
    # Need a valid adapter dir so the temp check fires before any FS errors
    adapter_dir = tmp_path / "20260518T000000Z-test"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_bytes(b"\x00")
    (adapter_dir / "adapter_config.json").write_text("{}")
    with pytest.raises(SystemExit, match=r"--temp must be >= 0\.0"):
        bj.main([
            "--dry-run",
            "--temp", "-0.5",
            "--adapter-path", str(adapter_dir),
            "--test-jsonl", str(tmp_path / "fake.jsonl"),
        ])


def test_main_rejects_max_tokens_zero(tmp_path: Path):
    adapter_dir = tmp_path / "20260518T000000Z-test"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_bytes(b"\x00")
    (adapter_dir / "adapter_config.json").write_text("{}")
    with pytest.raises(SystemExit, match=r"--max-tokens must be >= 1"):
        bj.main([
            "--dry-run",
            "--max-tokens", "0",
            "--adapter-path", str(adapter_dir),
            "--test-jsonl", str(tmp_path / "fake.jsonl"),
        ])


def test_save_results_json_skip_base_omits_verdict(tmp_path: Path):
    adapter = bj.BackendResult(name="adapter", base_model="b", adapter_path="/a")
    for i in range(3):
        gt = {"quality": 0.9, "safety": 1.0, "tool_use": 0.7, "personalization": 0.8}
        adapter.records.append(
            bj.BenchRecord(
                interaction_idx=i,
                prompt_preview="p",
                ground_truth=gt,
                generated_scores=gt,
                parse_ok=True,
                parse_error=None,
                latency_s=0.1,
                raw_generated="{}",
            )
        )
    out = tmp_path / "adapter-only.json"
    bj.save_results_json(
        adapter,
        None,
        out,
        base_model="b",
        test_jsonl_path=Path("/fake/test.jsonl"),
        test_jsonl_sha256="deadbeef",
        seed=42,
    )
    data = json.loads(out.read_text())
    assert len(data["backends"]) == 1
    assert "verdict" not in data
