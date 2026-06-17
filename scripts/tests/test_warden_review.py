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

import httpx

import codex_review as cr
import codex_warden_hooks as h
from _exit_codes import RATE_LIMIT
from warden_review import registry
from warden_review.backends import openai_compat as oac
from warden_review.backends.base import ReviewRequest, Verdict
from warden_review.constants import BACKEND_GPT, BACKEND_OPENAI_COMPAT, store_key
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
    assert registry.is_registered(BACKEND_GPT)
    assert registry.get_backend(BACKEND_GPT).id() == BACKEND_GPT
    # The registry now holds both model backends (gpt + openai_compat); claude is never registered.
    assert set(registry.available_backends()) == {BACKEND_GPT, BACKEND_OPENAI_COMPAT}


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


# ── Driver records into the gate's per-worktree bucket (worktree-marker-root fix) ──

def test_primary_repo_root_normal_repo_returns_toplevel(monkeypatch):
    # Non-worktree repo: git-common-dir is <top>/.git, so its parent is <top>.
    monkeypatch.setattr(h, "_git",
                        lambda cwd, *a: "/repo/.git" if a[-1] == "--git-common-dir" else None)
    assert h.primary_repo_root(Path("/repo")) == Path("/repo")


def test_primary_repo_root_worktree_returns_common_dir_parent(monkeypatch):
    # A linked worktree's git-common-dir points at the PRIMARY repo's .git, not its own.
    monkeypatch.setattr(h, "_git",
                        lambda cwd, *a: "/primary/.git" if a[-1] == "--git-common-dir" else None)
    assert h.primary_repo_root(Path("/primary/wt-feature")) == Path("/primary")


def test_primary_repo_root_falls_back_to_toplevel(monkeypatch):
    def fake_git(cwd, *a):
        if a[-1] == "--show-toplevel":
            return "/fallback/top"
        return None  # no common dir
    monkeypatch.setattr(h, "_git", fake_git)
    assert h.primary_repo_root(Path("/whatever")) == Path("/fallback/top")


def test_worktree_override_makes_write_path_equal_gate_read_path(tmp_path):
    # Frozen invariant: under worktree_override(wt) with repo_root=primary, the driver's
    # WRITE path (_verdicts_path) equals the gate's deterministic READ path
    # (_verdicts_path_for_worktree) — independent of os.getcwd(). And WITHOUT the override
    # (the pre-fix path) they differ — that divergence is exactly the bug being fixed.
    primary, wt = tmp_path / "primary", tmp_path / "wt-feature"
    prev = h._WORKTREE_OVERRIDE
    assert h._verdicts_path(primary) != h._verdicts_path_for_worktree(primary, wt)
    with h.worktree_override(wt):
        assert h._verdicts_path(primary) == h._verdicts_path_for_worktree(primary, wt)
    assert h._WORKTREE_OVERRIDE is prev  # restored on exit, no leak


def test_worktree_override_main_repo_is_flat(tmp_path):
    # Back-compat: when the worktree IS the repo root, resolution stays flat.
    primary = tmp_path / "primary"
    with h.worktree_override(primary):
        assert h._verdicts_path(primary) == primary / ".claude" / ".warden-verdicts.json"


def test_worktree_override_restores_prior_value():
    prev = h._WORKTREE_OVERRIDE
    h._WORKTREE_OVERRIDE = Path("/prev/override")
    try:
        with h.worktree_override(Path("/new/wt")):
            assert h._WORKTREE_OVERRIDE == Path("/new/wt")
        assert h._WORKTREE_OVERRIDE == Path("/prev/override")
    finally:
        h._WORKTREE_OVERRIDE = prev


def test_worktree_override_nesting_is_stack_safe():
    # Outer sets /a, inner sets /b; inner exit restores /a, outer exit restores the original.
    prev = h._WORKTREE_OVERRIDE
    with h.worktree_override(Path("/a")):
        assert h._WORKTREE_OVERRIDE == Path("/a")
        with h.worktree_override(Path("/b")):
            assert h._WORKTREE_OVERRIDE == Path("/b")
        assert h._WORKTREE_OVERRIDE == Path("/a")
    assert h._WORKTREE_OVERRIDE is prev


# ── Content-kind flag: diff roles vs non-diff (plan) roles ────────────────────────

def test_role_specs_mark_plan_reviewer_as_non_diff():
    # plan-reviewer reviews plan TEXT (no `diff --git` boundaries); the others review diffs.
    assert ROLE_SPECS["plan-reviewer"].is_diff is False
    assert ROLE_SPECS["code-reviewer"].is_diff is True
    assert ROLE_SPECS["ai-eng-warden"].is_diff is True


def test_review_request_defaults_to_diff():
    # Back-compat: every existing diff-role caller that omits is_diff keeps diff semantics.
    assert ReviewRequest(role=_ROLE, rules_path="/x", content="d", cwd="/r").is_diff is True


def test_non_diff_request_threads_is_diff_false_into_review(monkeypatch):
    # The codex backend must forward ReviewRequest.is_diff onto the CodexReviewConfig it
    # builds, so review() takes the whole-content path for plan-reviewer.
    captured = {}

    def _fake_review(content, cfg, cwd, cross_context=""):
        captured["is_diff"] = cfg.is_diff
        return {"results": [], "meta": {"verdict": "SHIP", "summary": "ok"}}

    monkeypatch.setattr(cr, "review", _fake_review)  # codex backend calls codex_review.review
    registry.get_backend(BACKEND_GPT).review(
        ReviewRequest(role="plan-reviewer", rules_path="/x", content="a plan",
                      cwd="/r", is_diff=False))
    assert captured["is_diff"] is False


# ── openai_compat backend (LIA-304): OpenAI-compatible /v1 transport ──────────────
# The single network seam ``_post_chat_completion`` is mocked wholesale — zero real HTTP,
# so this runs offline in CI. Covers result mapping, the fail-closed verdict invariant
# (no/invalid verdict NEVER becomes SHIP), every fail-open path (no base URL, transport
# error, non-200, oversize), and request shaping (json_object, auth header, model override).

_OAC_BASE = "http://127.0.0.1:8080/v1"


@pytest.fixture
def oac_env(monkeypatch):
    """A configured base URL, with the optional key/model deliberately UNSET."""
    monkeypatch.setenv("WARDEN_OPENAI_COMPAT_BASE_URL", _OAC_BASE)
    monkeypatch.delenv("WARDEN_OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("WARDEN_OPENAI_COMPAT_MODEL", raising=False)


def _oac_body(verdict="SHIP", results=None, summary="ok"):
    """An OpenAI-compatible chat-completions body whose content is the findings JSON."""
    content = json.dumps({"verdict": verdict, "summary": summary, "results": results or []})
    return {"choices": [{"message": {"content": content}}]}


def _oac_req(content="diff", model=None):
    # rules_path is intentionally missing so build_rules_digest uses its generic fallback
    # (no fixture file needed); the diff content is what we assert on.
    return ReviewRequest(role=_ROLE, rules_path="/nonexistent-rules.md", content=content,
                         cwd="/r", model=model)


def test_oac_registry_registration():
    assert registry.is_registered(BACKEND_OPENAI_COMPAT)
    assert registry.get_backend(BACKEND_OPENAI_COMPAT).id() == BACKEND_OPENAI_COMPAT
    assert oac.OpenAICompatBackend().id() == BACKEND_OPENAI_COMPAT


@pytest.mark.parametrize("verdict", ["SHIP", "REVISE", "BLOCK"])
def test_oac_parses_review_verdicts_and_maps_findings(monkeypatch, oac_env, verdict):
    results = [{"file": "f.py", "flagged": True,
                "findings": [{"severity": "MAJOR", "line": 3,
                              "finding": "bug", "confidence": "high"}]}]
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda *a, **k: (200, _oac_body(verdict, results, "one bug")))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.verdict == verdict
    assert v.summary == "one bug"
    assert v.findings[0]["file"] == "f.py"
    assert v.findings[0]["severity"] == "MAJOR"


def test_oac_missing_base_url_could_not_run(monkeypatch):
    monkeypatch.delenv("WARDEN_OPENAI_COMPAT_BASE_URL", raising=False)
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship
    assert v.category == "auth"


def test_oac_connection_error_fails_open(monkeypatch, oac_env):
    def _boom(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(oac, "_post_chat_completion", _boom)
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


@pytest.mark.parametrize("status,category", [(401, "auth"), (403, "auth"),
                                             (429, "rate_limit"), (500, "")])
def test_oac_non_200_fails_open_with_category(monkeypatch, oac_env, status, category):
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda *a, **k: (status, {"error": "boom"}))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship
    assert v.category == category


def test_oac_invalid_verdict_fails_closed(monkeypatch, oac_env):
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda *a, **k: (200, _oac_body(verdict="MAYBE")))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship   # invalid verdict must NOT auto-SHIP


def test_oac_missing_verdict_key_fails_closed(monkeypatch, oac_env):
    body = {"choices": [{"message": {"content": json.dumps({"summary": "x", "results": []})}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


def test_oac_non_json_content_fails_closed(monkeypatch, oac_env):
    body = {"choices": [{"message": {"content": "I cannot comply, here is prose."}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


def test_oac_unexpected_response_shape_fails_closed(monkeypatch, oac_env):
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, {"no": "choices"}))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


def test_oac_oversize_prompt_could_not_run_without_network(monkeypatch, oac_env):
    called: list[int] = []
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda *a, **k: called.append(1) or (200, _oac_body()))
    huge = "x" * (oac._MAX_PROMPT_CHARS + 1)
    v = oac.OpenAICompatBackend().review(_oac_req(content=huge))
    assert v.could_not_run and not v.is_ship
    assert called == []   # guarded BEFORE any network call (never truncate-then-SHIP)


def test_oac_strips_markdown_fence(monkeypatch, oac_env):
    fenced = "```json\n" + json.dumps({"verdict": "SHIP", "summary": "", "results": []}) + "\n```"
    body = {"choices": [{"message": {"content": fenced}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.verdict == "SHIP"


def test_oac_request_shaping_and_bearer_when_key_set(monkeypatch, oac_env):
    monkeypatch.setenv("WARDEN_OPENAI_COMPAT_API_KEY", "secret-key")
    monkeypatch.setenv("WARDEN_OPENAI_COMPAT_MODEL", "env-model")
    seen: dict = {}

    def _capture(endpoint, payload, headers, timeout):
        seen.update(endpoint=endpoint, payload=payload, headers=headers)
        return (200, _oac_body())

    monkeypatch.setattr(oac, "_post_chat_completion", _capture)
    oac.OpenAICompatBackend().review(_oac_req())
    assert seen["endpoint"] == _OAC_BASE + "/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["temperature"] == 0
    assert seen["payload"]["model"] == "env-model"


def test_oac_no_auth_header_when_key_absent(monkeypatch, oac_env):
    seen: dict = {}
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda e, p, hdr, t: seen.update(headers=hdr) or (200, _oac_body()))
    oac.OpenAICompatBackend().review(_oac_req())
    assert "Authorization" not in seen["headers"]


def test_oac_request_model_overrides_env(monkeypatch, oac_env):
    monkeypatch.setenv("WARDEN_OPENAI_COMPAT_MODEL", "env-model")
    seen: dict = {}
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda e, p, hdr, t: seen.update(payload=p) or (200, _oac_body()))
    oac.OpenAICompatBackend().review(_oac_req(model="req-model"))
    assert seen["payload"]["model"] == "req-model"


def test_oac_wraps_untrusted_diff_and_appends_shape(monkeypatch, oac_env):
    """Security: the diff stays inside the untrusted-data boundary; the JSON shape is appended."""
    seen: dict = {}
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda e, p, hdr, t: seen.update(payload=p) or (200, _oac_body()))
    oac.OpenAICompatBackend().review(_oac_req(content="MALICIOUS-DIFF-MARKER"))
    prompt = seen["payload"]["messages"][0]["content"]
    assert "MALICIOUS-DIFF-MARKER" in prompt
    assert "UNTRUSTED" in prompt                          # sentinel boundary framing present
    assert '"verdict": "SHIP|REVISE|BLOCK"' in prompt     # appended shape instruction present


def test_oac_post_valueerror_fails_open(monkeypatch, oac_env):
    """Contract: review() MUST NOT raise for infra failures. A ValueError from the transport
    (e.g. a malformed base URL on some httpx versions) must become COULD_NOT_RUN, not crash."""
    def _boom(*a, **k):
        raise ValueError("unknown url type")
    monkeypatch.setattr(oac, "_post_chat_completion", _boom)
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


def test_oac_malformed_base_url_does_not_raise(monkeypatch):
    """End-to-end (no mock): a schemeless base URL must fail open, never raise out of review()."""
    monkeypatch.setenv("WARDEN_OPENAI_COMPAT_BASE_URL", "not-a-url")
    monkeypatch.delenv("WARDEN_OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("WARDEN_OPENAI_COMPAT_MODEL", raising=False)
    v = oac.OpenAICompatBackend().review(_oac_req())   # real httpx; UnsupportedProtocol/ValueError
    assert v.could_not_run and not v.is_ship


def test_oac_parses_with_trailing_prose(monkeypatch, oac_env):
    """A model that appends a human note after the JSON object still yields a real verdict."""
    obj = json.dumps({"verdict": "SHIP", "summary": "clean", "results": []})
    body = {"choices": [{"message": {"content": obj + "\n\nNote: looks good to me!"}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.verdict == "SHIP"


def test_oac_parses_fence_without_language_tag(monkeypatch, oac_env):
    obj = json.dumps({"verdict": "REVISE", "summary": "x", "results": []})
    body = {"choices": [{"message": {"content": "```\n" + obj + "\n```"}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.verdict == "REVISE"


def test_oac_non_object_json_fails_closed(monkeypatch, oac_env):
    """A valid-JSON but non-object body (e.g. an array) must NOT become a verdict."""
    body = {"choices": [{"message": {"content": "[1, 2, 3]"}}]}
    monkeypatch.setattr(oac, "_post_chat_completion", lambda *a, **k: (200, body))
    v = oac.OpenAICompatBackend().review(_oac_req())
    assert v.could_not_run and not v.is_ship


def test_oac_model_omitted_from_payload_when_unset(monkeypatch, oac_env):
    """No env model + no request.model -> the payload carries no 'model' key (server default)."""
    seen: dict = {}
    monkeypatch.setattr(oac, "_post_chat_completion",
                        lambda e, p, hdr, t: seen.update(payload=p) or (200, _oac_body()))
    oac.OpenAICompatBackend().review(_oac_req())   # oac_env unsets WARDEN_OPENAI_COMPAT_MODEL
    assert "model" not in seen["payload"]
