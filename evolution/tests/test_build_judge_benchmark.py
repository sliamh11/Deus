"""Deterministic tests for the judge-benchmark fixture builder (no Gemini/DB calls)."""
import json
from dataclasses import dataclass

import pytest

from evolution import build_judge_benchmark as bb


# ── band allocation ────────────────────────────────────────────────────────────

def test_allocate_even_split_when_ample():
    assert bb.allocate([100, 100, 100, 100], 200) == [50, 50, 50, 50]


def test_allocate_redistributes_scarce_band():
    # Live shape: bottom band scarce (13), rest populous → take all 13, spread the rest.
    alloc = bb.allocate([13, 564, 181, 916], 200)
    assert alloc[0] == 13          # scarce band taken whole
    assert sum(alloc) == 200       # total preserved
    assert all(a <= avail for a, avail in zip(alloc, [13, 564, 181, 916]))


def test_allocate_caps_at_total_availability():
    assert bb.allocate([5, 5, 5, 5], 200) == [5, 5, 5, 5]  # can't exceed what exists


def test_band_index_boundaries():
    got = [bb._band_index(s) for s in (0.0, 0.39, 0.4, 0.59, 0.6, 0.84, 0.85, 1.0)]
    assert got == [0, 0, 1, 1, 2, 2, 3, 3]


# ── sampling (fake storage) ─────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def get_recent_interactions(self, **_):
        return list(self._rows)


def _rows(n_per_band=20):
    rows = []
    bands = {0: 0.2, 1: 0.5, 2: 0.7, 3: 0.95}
    for b, score in bands.items():
        for i in range(n_per_band):
            rows.append({
                "id": f"b{b}-{i}", "prompt": f"p{b}{i}", "response": "r",
                "tools_used": None, "judge_score": score, "group_folder": "g",
                "eval_suite": "runtime", "has_code": False,
            })
    return rows


def test_sample_is_seed_reproducible(monkeypatch):
    monkeypatch.setattr(bb, "get_storage", lambda: _FakeStore(_rows()))
    a = [r["id"] for r in bb.sample_interactions(40, seed=7)]
    b = [r["id"] for r in bb.sample_interactions(40, seed=7)]
    assert a == b                       # same seed → identical
    c = [r["id"] for r in bb.sample_interactions(40, seed=8)]
    assert set(a) != set(c) or a != c   # different seed → different draw/order


def test_sample_is_stratified_across_bands(monkeypatch):
    monkeypatch.setattr(bb, "get_storage", lambda: _FakeStore(_rows()))
    sample = bb.sample_interactions(40, seed=1)
    bands = {bb._band_index(float(r["judge_score"])) for r in sample}
    assert bands == {0, 1, 2, 3}        # all four bands represented


def test_sample_excludes_empty_responses(monkeypatch):
    rows = _rows()
    rows.append({"id": "empty", "prompt": "real prompt here", "response": "   ",
                 "tools_used": None, "judge_score": 0.5, "group_folder": "g",
                 "eval_suite": "runtime", "has_code": False})
    rows.append({"id": "none-resp", "prompt": "real prompt", "response": None,
                 "tools_used": None, "judge_score": 0.5, "group_folder": "g",
                 "eval_suite": "runtime", "has_code": False})
    monkeypatch.setattr(bb, "get_storage", lambda: _FakeStore(rows))
    ids = {r["id"] for r in bb.sample_interactions(100, seed=1)}
    assert "empty" not in ids and "none-resp" not in ids  # ungradable → excluded


def test_sample_filters_noise(monkeypatch):
    rows = _rows()
    rows.append({"id": "noise", "prompt": "/compact", "response": "No response requested",
                 "tools_used": None, "judge_score": 0.5, "group_folder": "g",
                 "eval_suite": "runtime", "has_code": False})
    monkeypatch.setattr(bb, "get_storage", lambda: _FakeStore(rows))
    ids = {r["id"] for r in bb.sample_interactions(100, seed=1)}
    assert "noise" not in ids


# ── record serialization + resume ───────────────────────────────────────────────

@dataclass
class _FakeResult:
    quality: float = 0.75
    safety: float = 1.0
    tool_use: float = 1.0
    personalization: float = 0.5
    score: float = 0.7
    is_parse_error: bool = False


def test_build_record_shape_and_digest():
    row = {"id": "x1", "prompt": "P", "response": "R", "tools_used": '["Bash","Read"]',
           "group_folder": "g", "eval_suite": "runtime", "has_code": 1}
    rec = bb.build_record(row, _FakeResult(), digest="work-style: concise", rubric_version=1)
    assert rec["id"] == "x1"
    assert rec["tools_used"] == ["Bash", "Read"]      # JSON string parsed
    assert rec["gemini_dims"] == {"quality": 0.75, "safety": 1.0, "tool_use": 1.0, "personalization": 0.5}
    assert rec["gemini_composite"] == 0.7
    assert rec["digest_text"] == "work-style: concise"  # text stored, not just a bool
    assert rec["digest_injected"] is True
    assert rec["has_code"] is True


def test_build_record_no_digest():
    row = {"id": "x2", "prompt": "P", "response": "", "tools_used": None,
           "group_folder": None, "eval_suite": "cc", "has_code": 0}
    rec = bb.build_record(row, _FakeResult(), digest=None, rubric_version=1)
    assert rec["digest_text"] == "" and rec["digest_injected"] is False
    assert rec["tools_used"] is None


def test_load_existing_ids_roundtrip(tmp_path):
    p = tmp_path / "fix.jsonl"
    p.write_text(
        json.dumps({"id": "a", "gemini_dims": {}}) + "\n" +
        "\n" +  # blank line tolerated
        json.dumps({"id": "b", "gemini_dims": {}}) + "\n",
        encoding="utf-8",
    )
    assert bb.load_existing_ids(p) == {"a", "b"}
    assert bb.load_existing_ids(tmp_path / "missing.jsonl") == set()
