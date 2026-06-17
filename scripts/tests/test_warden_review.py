"""Tests for the provider-agnostic warden-review co-gate (Phase 2: code-reviewer).

Zero subscription quota: the codex backend's network seam is never hit — backend.review
is exercised by mocking ``codex_review.review`` (and ``codex_review.call_codex_exec`` is
covered separately in test_codex_review.py). Gate/loop/HITL logic is pure state-machine
over a tmp_path verdict store. The gate signals a block by calling ``_block_pre_tool``
(which only prints a deny-JSON and returns), so tests monkeypatch it to record calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_review as cr
import codex_warden_hooks as h
from _exit_codes import RATE_LIMIT
from warden_review import registry
from warden_review.backends.base import ReviewRequest, Verdict
from warden_review.constants import BACKEND_GPT, store_key
from warden_review.roles import ROLE_SPECS

_ROLE = "code-reviewer"
_GPT_KEY = store_key(_ROLE, BACKEND_GPT)   # "code-reviewer@gpt"


# ── Fixtures: isolate all gate state under tmp_path/.claude ───────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    cdir = tmp_path / ".claude"
    (cdir / "wardens").mkdir(parents=True)
    # Route every marker / verdict-store / state file into tmp_path/.claude.
    monkeypatch.setattr(h, "_claude_marker_dir", lambda root: cdir)
    # Treat the commit cwd as inside a worktree so the gate proceeds.
    monkeypatch.setattr(h, "_worktree_for_cwd", lambda cwd, root: tmp_path)
    return tmp_path


@pytest.fixture
def blocks(monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(h, "_block_pre_tool", lambda reason: recorded.append(reason))
    return recorded


def _config(repo: Path, cfg: dict) -> None:
    (repo / ".claude" / "wardens" / "config.json").write_text(json.dumps(cfg))


def _commit_event(repo: Path) -> dict:
    return {"cwd": str(repo), "tool_input": {"command": "git commit -m x"}}


def _set(repo: Path, key: str, verdict: str) -> None:
    h.record_script_verdict(repo, key, verdict, "test")


def _gate(repo: Path) -> int:
    return h.run_warden_backends_gate(_ROLE, _commit_event(repo), repo)


# ── Registry ──────────────────────────────────────────────────────────────────────

def test_registry_lists_and_resolves_gpt():
    assert registry.available_backends() == (BACKEND_GPT,)
    assert registry.is_registered(BACKEND_GPT)
    assert registry.get_backend(BACKEND_GPT).id() == BACKEND_GPT


def test_registry_unknown_backend_raises():
    assert not registry.is_registered("nope")
    with pytest.raises(KeyError):
        registry.get_backend("nope")


# ── Role spec ───────────────────────────────────────────────────────────────────

def test_code_reviewer_role_gathers_diff(monkeypatch):
    monkeypatch.setattr(cr.cfr, "get_diff", lambda root, rr, df: "THE DIFF")
    spec = ROLE_SPECS[_ROLE]
    assert spec.claude_marker == "code-reviewed"
    assert spec.gather("/x", None, None) == "THE DIFF"


# ── Phase 3 (LIA-303): ai-eng-warden + plan-reviewer role specs + gatherers ───────

def test_ai_eng_warden_role_gathers_diff(monkeypatch):
    monkeypatch.setattr(cr.cfr, "get_diff", lambda root, rr, df: "THE DIFF")
    spec = ROLE_SPECS["ai-eng-warden"]
    assert spec.claude_marker == "ai-eng-reviewed"
    assert spec.rules_path == ".claude/wardens/ai-engineering-rules.md"
    assert spec.gather("/x", None, None) == "THE DIFF"   # diff-based, like code-reviewer


def test_plan_reviewer_role_gathers_content_file(tmp_path):
    spec = ROLE_SPECS["plan-reviewer"]
    assert spec.claude_marker == "plan-reviewed"
    assert spec.rules_path == ".claude/wardens/plan-review-rules.md"
    plan = tmp_path / "plan.md"
    plan.write_text("THE PLAN TEXT", encoding="utf-8")
    # The content-file path arrives in the diff_file slot (codex_warden routes --content-file there).
    assert spec.gather("/x", None, str(plan)) == "THE PLAN TEXT"


def test_plan_reviewer_gather_without_content_file_raises():
    spec = ROLE_SPECS["plan-reviewer"]
    with pytest.raises(cr.ReviewError):
        spec.gather("/x", None, None)


# ── _evaluate_backends / _evaluate_model_backends (the extracted, trigger-agnostic core) ──

def test_evaluate_model_backends_skips_claude(tmp_path):
    repo = tmp_path
    cfg = {"plan-reviewer": {"backends": ["claude", BACKEND_GPT]}}
    gpt_key = store_key("plan-reviewer", BACKEND_GPT)   # "plan-reviewer@gpt"
    # Claude verdict deliberately absent; skip_claude must ignore it and judge only gpt.
    h._write_verdict(repo, gpt_key, "SHIP", "ok")
    assert h._evaluate_backends("plan-reviewer", cfg, repo, skip_claude=True) == []
    # And with gpt REVISE it blocks (still ignoring the missing claude verdict):
    h._write_verdict(repo, gpt_key, "REVISE", "no")
    blocking = h._evaluate_backends("plan-reviewer", cfg, repo, skip_claude=True)
    assert [b for b, _ in blocking] == [BACKEND_GPT]


def test_evaluate_backends_is_pure_no_event(tmp_path):
    """The extracted core takes no event/cwd/commit guard — callable standalone."""
    repo = tmp_path
    cfg = {"code-reviewer": {"backends": ["claude"]}}
    # No claude verdict in the store → blocking (fail-closed), no exception.
    assert h._evaluate_backends("code-reviewer", cfg, repo) == [("claude", None)]


def test_claude_trivial_verdict_satisfies_gate(tmp_path):
    """TRIVIAL is the human trivial-commit bypass — it must pass the Claude side like SHIP, for
    every role on the backends gate (regression: the GPT co-gate caught that ai-eng's TRIVIAL
    bypass broke when it moved onto _evaluate_backends; code-reviewer had the same latent bug)."""
    repo = tmp_path
    for role in ("code-reviewer", "ai-eng-warden"):
        h._write_verdict(repo, role, "TRIVIAL", "trivial commit")
        cfg = {role: {"backends": ["claude"]}}
        assert h._evaluate_backends(role, cfg, repo) == [], f"{role} TRIVIAL must pass the gate"


def test_model_backend_non_ship_still_blocks_under_trivial_claude(tmp_path):
    """TRIVIAL only excuses the Claude side; a co-gated model backend that is not SHIP still blocks."""
    repo = tmp_path
    h._write_verdict(repo, "code-reviewer", "TRIVIAL", "trivial")
    h._write_verdict(repo, store_key("code-reviewer", BACKEND_GPT), "REVISE", "gpt found a bug")
    cfg = {"code-reviewer": {"backends": ["claude", BACKEND_GPT]}}
    assert [b for b, _ in h._evaluate_backends("code-reviewer", cfg, repo)] == [BACKEND_GPT]


# ── Codex backend: result mapping + fail-open ─────────────────────────────────────

def test_backend_maps_success_to_verdict(monkeypatch):
    monkeypatch.setattr(cr, "review", lambda *a, **k: {
        "results": [{"file": "f.py", "flagged": True,
                     "findings": [{"severity": "MAJOR", "line": 2,
                                   "finding": "bug", "confidence": "high"}]}],
        "meta": {"verdict": "REVISE", "summary": "one bug"},
    })
    v = registry.get_backend(BACKEND_GPT).review(
        ReviewRequest(role=_ROLE, rules_path="/x", content="d", cwd="/r"))
    assert v.verdict == "REVISE"
    assert v.findings[0]["file"] == "f.py"
    assert v.findings[0]["severity"] == "MAJOR"


def test_backend_infra_error_maps_to_could_not_run(monkeypatch):
    def _boom(*a, **k):
        raise cr.ReviewError(RATE_LIMIT, "429 rate limited")
    monkeypatch.setattr(cr, "review", _boom)
    v = registry.get_backend(BACKEND_GPT).review(
        ReviewRequest(role=_ROLE, rules_path="/x", content="d", cwd="/r"))
    assert v.could_not_run
    assert v.category == "rate_limit"
    assert not v.is_ship


# ── Verdict store: COULD_NOT_RUN is distinct from SHIP ────────────────────────────

def test_record_and_read_verdict_roundtrip(repo):
    _set(repo, _GPT_KEY, "COULD_NOT_RUN")
    assert h._read_verdict(_GPT_KEY, repo) == "COULD_NOT_RUN"
    assert h._read_verdict(_GPT_KEY, repo) != "SHIP"


# ── Gate: claude-only default (backward compatibility) ────────────────────────────

def test_default_is_claude_only(repo, blocks):
    # No config → backends defaults to ["claude"]; gpt verdict is irrelevant.
    _set(repo, _ROLE, "SHIP")            # Claude SHIP (stored under the role key)
    _set(repo, _GPT_KEY, "REVISE")       # would block IF gpt were configured
    assert _gate(repo) == 0
    assert blocks == []                  # gpt not in default backends → not gating


def test_claude_only_blocks_without_verdict(repo, blocks):
    assert _gate(repo) == 0
    assert blocks and "BLOCKED" in blocks[0]


# ── Gate: co-gate strict AND ──────────────────────────────────────────────────────

def test_cogate_both_ship_allows(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")
    _set(repo, _GPT_KEY, "SHIP")
    assert _gate(repo) == 0
    assert blocks == []


def test_cogate_claude_ship_gpt_revise_blocks(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")
    _set(repo, _GPT_KEY, "REVISE")
    assert _gate(repo) == 0
    assert blocks and "gpt" in blocks[0]


def test_cogate_claude_ship_gpt_missing_blocks(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")            # gpt never recorded → blocks
    assert _gate(repo) == 0
    assert blocks and "gpt" in blocks[0]


def test_cogate_claude_revise_gpt_ship_blocks(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "REVISE")
    _set(repo, _GPT_KEY, "SHIP")
    assert _gate(repo) == 0
    assert blocks  # the claude side blocks (no Claude-SHIP short-circuit bypassing it)


def test_cogate_gpt_could_not_run_fails_open(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")
    _set(repo, _GPT_KEY, "COULD_NOT_RUN")
    assert _gate(repo) == 0
    assert blocks == []                  # infra failure → fail open, commit allowed


def test_unknown_backend_warned_not_gating(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "bogus"]}})
    _set(repo, _ROLE, "SHIP")
    assert _gate(repo) == 0
    assert blocks == []                  # unknown backend skipped, claude satisfied


def test_non_commit_command_ignored(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    ev = {"cwd": str(repo), "tool_input": {"command": "git status"}}
    assert h.run_warden_backends_gate(_ROLE, ev, repo) == 0
    assert blocks == []


def test_disabled_warden_does_not_gate(repo, blocks):
    _config(repo, {_ROLE: {"enabled": False, "backends": ["claude", "gpt"]}})
    assert _gate(repo) == 0
    assert blocks == []


# ── Loop guard ────────────────────────────────────────────────────────────────────

def test_loop_increments_until_escalation(repo):
    for _ in range(3):
        h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    assert h._read_loop(repo, _ROLE)["round"] == 3
    assert h._co_gate_escalation_active(repo, _ROLE)


def test_loop_resets_on_convergence(repo):
    h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "SHIP", "SHIP")   # both SHIP → converged
    assert h._read_loop(repo, _ROLE)["round"] == 0
    assert not h._co_gate_escalation_active(repo, _ROLE)


def test_could_not_run_does_not_advance_loop(repo):
    h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "COULD_NOT_RUN", "SHIP")
    assert h._read_loop(repo, _ROLE)["round"] == 1   # infra failure left it untouched


def test_escalation_message_in_block(repo, blocks):
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")
    _set(repo, _GPT_KEY, "REVISE")
    for _ in range(3):
        h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    _gate(repo)
    assert blocks and "LOOP GUARD" in blocks[0]
    assert "cross-review-override" in blocks[0]


# ── HITL override ─────────────────────────────────────────────────────────────────

def test_override_refused_in_bg_session(repo, monkeypatch):
    monkeypatch.setattr(h, "_is_bg_session", lambda: True)
    for _ in range(3):
        h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    assert h.cross_review_override(repo, _ROLE, "because") == 2
    assert h._read_verdict(_GPT_KEY, repo) != "SHIP"   # nothing written


def test_override_refused_without_escalation(repo, monkeypatch):
    monkeypatch.setattr(h, "_is_bg_session", lambda: False)
    assert h.cross_review_override(repo, _ROLE, "because") == 2  # no active loop


def test_override_when_escalated_writes_one_commit_ship(repo, blocks, monkeypatch):
    monkeypatch.setattr(h, "_is_bg_session", lambda: False)
    _config(repo, {_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _ROLE, "SHIP")
    _set(repo, _GPT_KEY, "REVISE")
    for _ in range(3):
        h.note_model_review_round(repo, _ROLE, BACKEND_GPT, "REVISE", "SHIP")
    assert h.cross_review_override(repo, _ROLE, "false positive, reviewed by hand") == 0
    assert h._read_verdict(_GPT_KEY, repo) == "SHIP"          # one-commit SHIP written
    assert not h._co_gate_escalation_active(repo, _ROLE)      # loop reset
    assert _gate(repo) == 0 and blocks == []                 # gate now allows


# ── Cross-awareness ───────────────────────────────────────────────────────────────

def test_read_cross_context_returns_claude_verdict(repo):
    _set(repo, _ROLE, "REVISE")
    ctx = h.read_cross_context(repo, _ROLE, for_backend=BACKEND_GPT)
    assert "Claude" in ctx and "REVISE" in ctx
    assert h.read_cross_context(repo, _ROLE, for_backend="claude") == ""  # Claude reads the file


def test_write_model_cross_review_file(repo):
    h.write_model_cross_review(
        repo, _ROLE, BACKEND_GPT, "REVISE",
        [{"file": "a.py", "severity": "MAJOR", "line": 3,
          "finding": "off-by-one", "confidence": "high"}], "summary")
    text = h._marker(repo, h.cross_review_file(_ROLE)).read_text()
    assert "off-by-one" in text and "REVISE" in text
    # security-stored-output-trust: prior model output is wrapped + flagged untrusted.
    assert '<stored-output source="model-cross-review">' in text
    assert "UNTRUSTED DATA" in text


def test_backend_missing_verdict_fails_closed(monkeypatch):
    # A schema-conformant response with no/invalid verdict must NOT auto-SHIP.
    monkeypatch.setattr(cr, "review", lambda *a, **k: {"results": [], "meta": {}})
    v = registry.get_backend(BACKEND_GPT).review(
        ReviewRequest(role=_ROLE, rules_path="/x", content="d", cwd="/r"))
    assert v.could_not_run and not v.is_ship


# ── Invalidation clears the model verdict + cross-review file ─────────────────────

def test_invalidator_clears_gpt_verdict(repo, monkeypatch):
    _set(repo, _GPT_KEY, "SHIP")
    h.write_model_cross_review(repo, _ROLE, BACKEND_GPT, "SHIP", [], "")
    monkeypatch.setattr(h, "_managed_paths", lambda event, root: (repo, [repo / "x.py"]))
    monkeypatch.setattr(h, "_in_commit_window", lambda root: False)
    ev = {"cwd": str(repo), "tool_input": {"command": "edit"}}
    h.run_code_review_invalidator(ev, repo)
    assert h._read_verdict(_GPT_KEY, repo) is None
    assert not h._marker(repo, h.cross_review_file(_ROLE)).exists()
