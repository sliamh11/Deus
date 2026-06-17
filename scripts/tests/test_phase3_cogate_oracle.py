"""Oracle tests for LIA-303 Phase 3: plan-reviewer co-gate.

Each test is derived FROM THE SPEC, blind to the implementation. They are RED
against the current (pre-Phase-3) codebase and GREEN only on a correct
implementation.

Run:
    python3 -m pytest scripts/tests/test_phase3_cogate_oracle.py -v

Oracle tagging convention (oracle-rules.md § oracle-tagged):
    # @oracle: <one-line spec reference>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_warden_hooks as h
from warden_review.constants import BACKEND_GPT, store_key

_PLAN_ROLE = "plan-reviewer"
_GPT_KEY = store_key(_PLAN_ROLE, BACKEND_GPT)   # "plan-reviewer@gpt"
_CODE_ROLE = "code-reviewer"
_CODE_GPT_KEY = store_key(_CODE_ROLE, BACKEND_GPT)   # "code-reviewer@gpt"


# ── Fixtures (mirrors test_warden_review.py harness exactly) ──────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Isolated repo-root with all gate state under tmp_path/.claude."""
    cdir = tmp_path / ".claude"
    (cdir / "wardens").mkdir(parents=True)
    monkeypatch.setattr(h, "_claude_marker_dir", lambda root: cdir)
    monkeypatch.setattr(h, "_worktree_for_cwd", lambda cwd, root: tmp_path)
    return tmp_path


@pytest.fixture
def blocks(monkeypatch):
    """Capture _block_pre_tool calls so tests can assert ALLOW vs BLOCK."""
    recorded: list[str] = []
    monkeypatch.setattr(h, "_block_pre_tool", lambda reason: recorded.append(reason))
    return recorded


def _config(repo: Path, cfg: dict) -> None:
    (repo / ".claude" / "wardens" / "config.json").write_text(json.dumps(cfg))


def _set(repo: Path, key: str, verdict: str) -> None:
    """Write a verdict into the store (same helper used by test_warden_review.py)."""
    h.record_script_verdict(repo, key, verdict, "oracle-test")


def _plan_edit_event(repo: Path) -> dict:
    """A PreToolUse Edit event inside the repo worktree.

    The hook reads ``tool_input["file_path"]`` (not ``path``) per _event_paths
    in codex_warden_hooks.py:281.  Using the wrong key produces an empty path
    list, which causes the gate to take the 'outside-worktree' early-return path
    instead of blocking — masking the missing marker.
    """
    return {
        "tool_name": "Edit",
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(repo / "src" / "main.py"),
            "old_string": "a",
            "new_string": "b",
        },
    }


def _commit_event(repo: Path) -> dict:
    return {"cwd": str(repo), "tool_input": {"command": "git commit -m x"}}


def _plan_mode_event(repo: Path) -> dict:
    """A UserPromptSubmit /plan event that should trigger the invalidator."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(repo),
        "prompt": "/plan redesign the auth module",
    }


def _exit_plan_mode_event(repo: Path) -> dict:
    """ExitPlanMode tool event that should also trigger the invalidator."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "cwd": str(repo),
        "tool_input": {},
    }


# ── Contract A: plan-reviewer co-gate ─────────────────────────────────────────
#
# Gate allows ONLY when BOTH the .plan-reviewed marker exists AND the GPT
# verdict is SHIP.  Missing or REVISE GPT verdict must block even when the
# marker is present.

def test_cogate_plan_marker_and_gpt_ship_allows(repo, blocks):
    # @oracle: Contract A — BOTH marker present AND GPT SHIP → ALLOW
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")
    h.run_plan_review_gate(_plan_edit_event(repo), repo)
    assert blocks == [], (
        "gate must ALLOW when both .plan-reviewed marker exists and GPT verdict is SHIP"
    )


def test_cogate_plan_marker_present_gpt_missing_blocks(repo, blocks):
    # @oracle: Contract A — marker present but GPT verdict absent → BLOCK
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    # GPT verdict deliberately NOT written
    h.run_plan_review_gate(_plan_edit_event(repo), repo)
    assert blocks, (
        "gate must BLOCK when .plan-reviewed marker exists but GPT verdict is missing; "
        "this fails on the current (pre-Phase-3) implementation where marker alone suffices"
    )


def test_cogate_plan_marker_present_gpt_revise_blocks(repo, blocks):
    # @oracle: Contract A — marker present but GPT verdict is REVISE → BLOCK
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "REVISE")
    h.run_plan_review_gate(_plan_edit_event(repo), repo)
    assert blocks, (
        "gate must BLOCK when marker exists but GPT verdict is REVISE; "
        "the pre-Phase-3 gate ignores GPT entirely and would ALLOW — that is the bug"
    )


def test_cogate_plan_marker_absent_blocks_regardless_of_gpt(repo, blocks):
    # @oracle: Contract A — no marker → BLOCK even if GPT is SHIP (pre-existing behaviour)
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})
    # Marker deliberately absent
    _set(repo, _GPT_KEY, "SHIP")
    h.run_plan_review_gate(_plan_edit_event(repo), repo)
    assert blocks, (
        "gate must BLOCK when marker is absent, regardless of GPT verdict"
    )


def test_cogate_plan_gpt_could_not_run_fails_open(repo, blocks):
    # @oracle: Contract A — COULD_NOT_RUN is infra failure → fail open (ALLOW when marker present)
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "COULD_NOT_RUN")
    h.run_plan_review_gate(_plan_edit_event(repo), repo)
    assert blocks == [], (
        "COULD_NOT_RUN is an infra failure — must fail open and ALLOW the edit"
    )


# ── Contract B: /plan invalidation clears BOTH marker AND GPT verdict ─────────
#
# The discriminating shape: seed marker-present + stale GPT SHIP, fire the
# invalidator, re-create the marker (simulating a fresh Claude plan-review SHIP
# on the new plan, but NO new GPT review), then assert the gate BLOCKS.
#
# A buggy impl that clears only the marker (not the GPT store) would re-use the
# stale SHIP verdict after the marker is re-created → gate ALLOWS → test FAILS.

def test_o1_plan_invalidation_clears_gpt_verdict(repo, blocks):
    # @oracle: Contract B — /plan invalidator must clear plan-reviewer@gpt store key
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})

    # Seed: stale state from a prior plan-review cycle
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")  # stale GPT SHIP from the OLD plan

    # Fire the plan-mode invalidator (triggered by /plan prompt)
    h.run_plan_mode_invalidator(_plan_mode_event(repo), repo)

    # Simulate fresh Claude plan-review SHIP on the NEW plan (just the marker)
    _marker_path.touch()

    # Gate must now BLOCK: the new plan has no GPT review
    h.run_plan_review_gate(_plan_edit_event(repo), repo)

    assert blocks, (
        "after /plan invalidation + re-created marker, gate must BLOCK because "
        "the stale plan-reviewer@gpt SHIP was cleared; "
        "a buggy impl that clears only the marker leaves the stale GPT SHIP intact "
        "and the gate would ALLOW — this test catches that bypass"
    )


def test_o1_exit_plan_mode_also_clears_gpt_verdict(repo, blocks):
    # @oracle: Contract B (ExitPlanMode path) — ExitPlanMode invalidator must also clear plan-reviewer@gpt
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})

    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")  # stale GPT SHIP

    # Fire via ExitPlanMode
    h.run_plan_mode_invalidator(_exit_plan_mode_event(repo), repo)

    # Re-create marker (fresh Claude SHIP on new plan, but no new GPT review)
    _marker_path.touch()

    h.run_plan_review_gate(_plan_edit_event(repo), repo)

    assert blocks, (
        "ExitPlanMode invalidation path must also clear plan-reviewer@gpt verdict; "
        "stale GPT SHIP + re-created marker must BLOCK, not ALLOW"
    )


def test_o1_gpt_verdict_is_absent_after_invalidation(repo):
    # @oracle: Contract B — directly verify plan-reviewer@gpt is None after invalidator runs
    #
    # NOTE: This test correctly discriminates only once Phase 3 adds "plan-reviewer" to
    # WIRED_ROLES in warden_review/constants.py (so that _read_verdict / _write_verdict
    # route through MARKER_NAMES for "plan-reviewer@gpt").  On the pre-Phase-3 codebase,
    # _read_verdict("plan-reviewer@gpt") returns None unconditionally (key not in
    # MARKER_NAMES), so the test passes vacuously.  The compound end-to-end test
    # test_o1_plan_invalidation_clears_gpt_verdict is the primary discriminator.
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})

    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")

    h.run_plan_mode_invalidator(_plan_mode_event(repo), repo)

    stored = h._read_verdict(_GPT_KEY, repo)
    assert stored is None, (
        f"plan-reviewer@gpt must be None after /plan invalidation, got {stored!r}; "
        "a Phase-3 impl that adds plan-reviewer to WIRED_ROLES but forgets to clear "
        "the verdict from the invalidator would fail this test"
    )


# ── Contract C: SessionStart invalidation clears BOTH marker AND GPT verdict ──
#
# Same discriminating shape as O1, but driven by run_session_init instead of
# the plan-mode invalidator.

def test_o2_session_init_clears_gpt_verdict(repo, blocks):
    # @oracle: Contract C — run_session_init must clear plan-reviewer@gpt store key
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})

    # Seed: stale state left over from the previous session
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")  # stale GPT SHIP from old session

    # Session start fires
    h.run_session_init(repo)

    # Simulate fresh Claude plan-review SHIP on first plan of the new session
    _marker_path.touch()

    # Gate must BLOCK: new session's plan has no fresh GPT review
    h.run_plan_review_gate(_plan_edit_event(repo), repo)

    assert blocks, (
        "after run_session_init + re-created marker, gate must BLOCK because the "
        "stale plan-reviewer@gpt SHIP was cleared by session init; "
        "a buggy impl that does not clear the verdict store would carry the stale "
        "SHIP forward and the gate would ALLOW — this test catches that bypass"
    )


def test_o2_gpt_verdict_is_absent_after_session_init(repo):
    # @oracle: Contract C — directly verify plan-reviewer@gpt is None after run_session_init
    #
    # NOTE: same WIRED_ROLES caveat as test_o1_gpt_verdict_is_absent_after_invalidation.
    # Discriminates correctly only once "plan-reviewer" is in WIRED_ROLES.  On the
    # pre-Phase-3 codebase this passes vacuously.  The primary discriminator is
    # test_o2_session_init_clears_gpt_verdict (end-to-end gate check).
    _config(repo, {_PLAN_ROLE: {"backends": ["claude", "gpt"]}})

    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()
    _set(repo, _GPT_KEY, "SHIP")

    h.run_session_init(repo)

    stored = h._read_verdict(_GPT_KEY, repo)
    assert stored is None, (
        f"plan-reviewer@gpt must be None after run_session_init, got {stored!r}; "
        "a Phase-3 impl that adds plan-reviewer to WIRED_ROLES but forgets to clear "
        "the verdict from session_init would fail this test"
    )


def test_o2_session_init_still_clears_plan_reviewed_marker(repo):
    # @oracle: Contract C — run_session_init must still clear .plan-reviewed marker (regression guard)
    _marker_path = h._marker(repo, ".plan-reviewed")
    _marker_path.parent.mkdir(parents=True, exist_ok=True)
    _marker_path.touch()

    h.run_session_init(repo)

    assert not _marker_path.exists(), (
        ".plan-reviewed marker must be cleared by run_session_init (pre-existing behaviour)"
    )


# ── Contract A prerequisite: plan-reviewer must be in WIRED_ROLES ────────────
#
# The gate can only read and write "plan-reviewer@gpt" through the verdict store
# if "plan-reviewer" is in WIRED_ROLES (warden_review/constants.py).  MARKER_NAMES
# is built from WIRED_ROLES × KNOWN_MODEL_BACKENDS — if the role is absent, all
# _read_verdict / _write_verdict calls for "plan-reviewer@gpt" silently no-op and
# the gate can never read back a verdict it was supposed to check.

def test_plan_reviewer_in_wired_roles(repo):
    # @oracle: Contract A prerequisite — plan-reviewer must be wired so its GPT key is routable
    from warden_review.constants import WIRED_ROLES
    assert _PLAN_ROLE in WIRED_ROLES, (
        f"'plan-reviewer' must be in WIRED_ROLES (warden_review/constants.py) so that "
        f"'{_GPT_KEY}' is present in MARKER_NAMES and _read_verdict/_write_verdict can "
        "route it; without this, all verdict reads for plan-reviewer@gpt return None "
        "and the co-gate is a no-op regardless of what the invalidator does"
    )


def test_plan_reviewer_gpt_key_in_marker_names(repo):
    # @oracle: Contract A prerequisite — plan-reviewer@gpt must be in MARKER_NAMES identity map
    assert _GPT_KEY in h.MARKER_NAMES, (
        f"'{_GPT_KEY}' must be in MARKER_NAMES so _read_verdict can look it up; "
        "adding plan-reviewer to WIRED_ROLES generates this entry automatically"
    )


# ── Contract D: code-reviewer co-gate regression guard ────────────────────────
#
# The _evaluate_backends extraction in Phase 3 must not break the existing
# code-reviewer co-gate. Mirror the key existing tests from test_warden_review.py.

def test_o3_code_reviewer_both_ship_allows(repo, blocks):
    # @oracle: Contract D — code-reviewer co-gate: both SHIP → ALLOW (regression)
    _config(repo, {_CODE_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _CODE_ROLE, "SHIP")
    _set(repo, _CODE_GPT_KEY, "SHIP")
    rc = h.run_warden_backends_gate(_CODE_ROLE, _commit_event(repo), repo)
    assert rc == 0
    assert blocks == [], (
        "code-reviewer co-gate must ALLOW when both Claude and GPT verdict are SHIP"
    )


def test_o3_code_reviewer_gpt_revise_blocks(repo, blocks):
    # @oracle: Contract D — code-reviewer co-gate: GPT REVISE → BLOCK (regression)
    _config(repo, {_CODE_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _CODE_ROLE, "SHIP")
    _set(repo, _CODE_GPT_KEY, "REVISE")
    h.run_warden_backends_gate(_CODE_ROLE, _commit_event(repo), repo)
    assert blocks, (
        "code-reviewer co-gate must BLOCK when GPT verdict is REVISE; "
        "regression: _evaluate_backends extraction must not break this"
    )


def test_o3_code_reviewer_gpt_missing_blocks(repo, blocks):
    # @oracle: Contract D — code-reviewer co-gate: GPT missing → BLOCK (regression)
    _config(repo, {_CODE_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _CODE_ROLE, "SHIP")
    # GPT verdict not set
    h.run_warden_backends_gate(_CODE_ROLE, _commit_event(repo), repo)
    assert blocks, (
        "code-reviewer co-gate must BLOCK when GPT verdict is absent"
    )


def test_o3_code_reviewer_gpt_could_not_run_fails_open(repo, blocks):
    # @oracle: Contract D — code-reviewer co-gate: COULD_NOT_RUN → ALLOW (regression)
    _config(repo, {_CODE_ROLE: {"backends": ["claude", "gpt"]}})
    _set(repo, _CODE_ROLE, "SHIP")
    _set(repo, _CODE_GPT_KEY, "COULD_NOT_RUN")
    h.run_warden_backends_gate(_CODE_ROLE, _commit_event(repo), repo)
    assert blocks == [], (
        "COULD_NOT_RUN must fail open — infra failures must not block commits"
    )
