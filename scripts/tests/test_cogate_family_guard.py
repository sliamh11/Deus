"""Tests for the co-gate Claude-half model-family guard (LIA-560 defect 3).

No network: every test injects ``fetch``. The invariant these pin — and the reason several
of them exist at all — is that there is exactly ONE hard-BLOCK condition. Rounds 3 and 4 of
this feature's plan review each regressed by quietly adding a second one, so
``test_block_is_exhaustive`` asserts the property directly rather than trusting review.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warden_review import model_family as mf  # noqa: E402

GATEWAY = "http://localhost:8317"
CLOAKED_LUNA = "claude-fable-5-dd-xam-anul"      # -> luna-max   (GPT)
CLOAKED_OPUS = "claude-fable-5-dd-rennalp-supo"  # -> opus-planner (real Claude)


def _listing(*entries):
    """Build a fetch stub returning an OpenAI-shaped listing."""
    def fetch(url, api_key, timeout):
        assert url.startswith(GATEWAY), url
        return {"data": [{"id": i, "owned_by": o, "object": "model"} for i, o in entries]}
    return fetch


def _raises(exc=ConnectionError("gateway down")):
    def fetch(url, api_key, timeout):
        raise exc
    return fetch


@pytest.fixture()
def repo(tmp_path):
    """A repo root with a `sonnet`-pinned agent file, like the real co-gated roles."""
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\nmodel: sonnet\n---\nbody\n", encoding="utf-8"
    )
    return tmp_path


def _env(**over):
    env = {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_API_KEY": "k",
           "ANTHROPIC_DEFAULT_SONNET_MODEL": CLOAKED_LUNA}
    env.update(over)
    return env


# ── decode / normalise ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("cloaked", "plain"), [
    (CLOAKED_LUNA, "luna-max"),
    ("claude-fable-5-dd-los", "sol"),
    (CLOAKED_OPUS, "opus-planner"),
])
def test_decode_round_trips_known_ids(cloaked, plain):
    assert mf.decode_model_id(cloaked) == plain


def test_decode_leaves_plain_and_real_claude_ids_alone():
    assert mf.decode_model_id("luna-max") == "luna-max"
    assert mf.decode_model_id("claude-opus-5") == "claude-opus-5"


def test_decode_preserves_thinking_suffix():
    """The round-3 blocking bug: reversing the suffix along with the base garbles the id."""
    assert mf.decode_model_id(f"{CLOAKED_LUNA}(high)") == "luna-max(high)"
    assert mf.decode_model_id("claude-opus-5(high)") == "claude-opus-5(high)"


def test_strip_variant_suffix():
    assert mf.strip_variant_suffix("opus-planner[1m]") == "opus-planner"
    assert mf.strip_variant_suffix("opus-planner") == "opus-planner"


# ── alias resolution ────────────────────────────────────────────────────────────────

def test_alias_pin_resolves_through_env(repo):
    pin, resolved, source = mf.resolve_claude_half_model("code-reviewer", repo, _env())
    assert (pin, resolved, source) == ("sonnet", CLOAKED_LUNA, "ANTHROPIC_DEFAULT_SONNET_MODEL")


def test_explicit_pin_used_verbatim(repo):
    (repo / ".claude" / "agents" / "r.md").write_text(
        "---\nname: r\nmodel: opus-planner\n---\n", encoding="utf-8")
    pin, resolved, _ = mf.resolve_claude_half_model("r", repo, _env())
    assert (pin, resolved) == ("opus-planner", "opus-planner")


def test_missing_pin_falls_back_to_anthropic_model(repo):
    (repo / ".claude" / "agents" / "nopin.md").write_text("---\nname: nopin\n---\n", encoding="utf-8")
    pin, resolved, source = mf.resolve_claude_half_model(
        "nopin", repo, _env(ANTHROPIC_MODEL="claude-opus-5"))
    assert pin is None and resolved == "claude-opus-5" and "ANTHROPIC_MODEL" in source


# ── the decision table ──────────────────────────────────────────────────────────────

def test_ok_when_no_gateway(repo):
    """First-party API: the Claude half really is Claude, no check needed."""
    verdict, msg = mf.check_claude_half("code-reviewer", repo, _env(ANTHROPIC_BASE_URL=""),
                                        fetch=_raises())
    assert verdict == mf.OK and msg == ""


def test_ok_for_anthropic_owner(repo):
    verdict, _ = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=CLOAKED_OPUS),
        fetch=_listing(("opus-planner", "anthropic")))
    assert verdict == mf.OK


def test_ok_is_case_insensitive(repo):
    verdict, _ = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=CLOAKED_OPUS),
        fetch=_listing(("opus-planner", "Anthropic")))
    assert verdict == mf.OK


def test_blocks_the_measured_defect(repo):
    """The whole point: sonnet -> a cloaked GPT id -> owned_by openai."""
    verdict, msg = mf.check_claude_half("code-reviewer", repo, _env(),
                                        fetch=_listing(("luna-max", "openai")))
    assert verdict == mf.BLOCK
    assert "luna-max" in msg and "openai" in msg


def test_blocks_plain_id_spelling_too(repo):
    verdict, _ = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL="luna-max"),
        fetch=_listing(("luna-max", "openai")))
    assert verdict == mf.BLOCK


def test_block_owner_match_is_case_insensitive(repo):
    verdict, _ = mf.check_claude_half("code-reviewer", repo, _env(),
                                      fetch=_listing(("luna-max", "OpenAI")))
    assert verdict == mf.BLOCK


def test_block_message_carries_all_three_escapes(repo):
    """The R6 mitigation for the relabelling-proxy residual — asserted, not just documented."""
    _, msg = mf.check_claude_half("code-reviewer", repo, _env(),
                                  fetch=_listing(("luna-max", "openai")))
    assert "code-reviewer" in msg and "sonnet" in msg
    assert CLOAKED_LUNA in msg and "luna-max" in msg
    assert GATEWAY in msg
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" in msg
    assert "non-gateway session" in msg
    assert "rename that section" in msg


def test_unknown_owner_label_warns_not_blocks(repo):
    """A relabelling proxy may front a REAL Claude model — must never hard-block."""
    for label in ("litellm", "corporate", "acme-platform-team"):
        verdict, msg = mf.check_claude_half("code-reviewer", repo, _env(),
                                            fetch=_listing(("luna-max", label)))
        assert verdict == mf.WARN, label
        assert "UNVERIFIED" in msg


def test_absent_from_listing_warns_not_blocks(repo):
    """A delisted model may be mid-cooldown and genuinely Claude (round-4 finding)."""
    verdict, msg = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=CLOAKED_OPUS),
        fetch=_listing(("something-else", "openai")))
    assert verdict == mf.WARN and "cooldown" in msg


def test_real_claude_cloaked_id_with_failing_fetch_does_not_block(repo):
    """The exact round-3 counter-example: real Claude + transient blip must not hard-block."""
    verdict, _ = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=CLOAKED_OPUS),
        fetch=_raises())
    assert verdict == mf.WARN


def test_fetch_failure_never_blocks_any_id(repo):
    for model in (CLOAKED_LUNA, CLOAKED_OPUS, "luna-max", "claude-opus-5"):
        verdict, _ = mf.check_claude_half(
            "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=model), fetch=_raises())
        assert verdict == mf.WARN, model


def test_malformed_payloads_warn_and_do_not_crash(repo):
    for body in ({}, {"data": "nope"}, [], {"data": [None, 7, {"no_id": 1}]}, "text"):
        verdict, _ = mf.check_claude_half("code-reviewer", repo, _env(),
                                          fetch=lambda u, k, t, b=body: b)
        assert verdict == mf.WARN


def test_unset_resolution_env_warns(repo):
    verdict, msg = mf.check_claude_half(
        "code-reviewer", repo, _env(ANTHROPIC_DEFAULT_SONNET_MODEL=""), fetch=_raises())
    assert verdict == mf.WARN and "unset" in msg


def test_user_agent_is_not_claude_cli():
    """A claude-cli UA flips the gateway to the cloaked shape, per isAnthropicModelsRequest."""
    assert not mf._USER_AGENT.startswith("claude-cli")


def test_block_is_exhaustive(repo):
    """The ONLY BLOCK path is: listed model + known non-Anthropic owner.

    Pins the design principle so a future edit cannot quietly add a second hard-block, which
    is how two earlier revisions regressed.
    """
    cases = [
        (_env(ANTHROPIC_BASE_URL=""), _raises()),                            # no gateway
        (_env(), _raises()),                                                 # fetch fails
        (_env(), _listing(("other", "openai"))),                             # absent
        (_env(), _listing(("luna-max", "litellm"))),                         # unknown owner
        (_env(), _listing(("luna-max", "anthropic"))),                       # anthropic
        (_env(ANTHROPIC_DEFAULT_SONNET_MODEL=""), _listing(("x", "openai"))),  # unset
        (_env(), lambda u, k, t: {"data": "malformed"}),                     # malformed
    ]
    for env, fetch in cases:
        verdict, _ = mf.check_claude_half("code-reviewer", repo, env, fetch=fetch)
        assert verdict in (mf.OK, mf.WARN), (env, verdict)

    verdict, _ = mf.check_claude_half("code-reviewer", repo, _env(),
                                      fetch=_listing(("luna-max", "openai")))
    assert verdict == mf.BLOCK


# ── integration: the guard must short-circuit cogate.main() ─────────────────────────

def test_cogate_short_circuits_before_marking_or_running_gpt(monkeypatch, repo):
    """A BLOCK must record no verdict and burn no GPT call — unit tests alone can't show this."""
    import cogate

    called = {"mark": 0, "gpt": 0}
    monkeypatch.setattr(cogate.whooks, "mark_warden",
                        lambda *a, **k: called.__setitem__("mark", called["mark"] + 1) or 0)
    monkeypatch.setattr(cogate.codex_warden, "main",
                        lambda *a, **k: called.__setitem__("gpt", called["gpt"] + 1) or 0)
    monkeypatch.setattr(cogate.model_family, "check_claude_half",
                        lambda *a, **k: (cogate.model_family.BLOCK, "BLOCK: nope"))

    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "r", "--worktree-root", str(repo)])

    assert rc == 2
    assert called == {"mark": 0, "gpt": 0}
