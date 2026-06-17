"""Driver-level tests for scripts/codex_warden.py.

Uses a REAL temporary git repo (not a fully-mocked store) so the driver exercises the
genuine ``cfr.repo_root()`` (returns a str) → warden-hooks helpers (need a Path) chain.
This guards the str/Path regression that the fully-mocked unit tests could not catch.
The model backend is faked, so no codex CLI / subscription quota is involved.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_warden as cw
from warden_review.backends.base import Verdict

_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+bad = 1\n"


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _fake_backend(verdict: Verdict):
    return types.SimpleNamespace(review=lambda req: verdict)


def test_driver_records_verdict_with_real_repo_root_str(git_repo, monkeypatch):
    # cfr.repo_root() returns a str in reality — the driver must coerce to Path before
    # handing it to the hooks helpers, or _worktree_for_cwd's `repo_root / ".git"` raises.
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cw.cr.cfr, "get_diff", lambda root, rr, df: _DIFF)
    monkeypatch.setattr(cw.registry, "is_registered", lambda b: True)
    monkeypatch.setattr(cw.registry, "get_backend", lambda b: _fake_backend(
        Verdict("REVISE", [{"file": "x.py", "severity": "MAJOR", "line": 1,
                            "finding": "bad", "confidence": "high"}], "one issue")))

    rc = cw.main(["--role", "code-reviewer", "--warden-mark", "--json"])
    assert rc == 0
    assert cw.whooks._read_verdict("code-reviewer@gpt", git_repo) == "REVISE"
    # cross-review file written for the Claude side
    assert cw.whooks._marker(git_repo, cw.whooks.cross_review_file("code-reviewer")).exists()


def test_driver_abstains_on_empty_diff(git_repo, monkeypatch):
    from _exit_codes import ABSTAIN
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cw.cr.cfr, "get_diff", lambda root, rr, df: "   \n")
    monkeypatch.setattr(cw.registry, "is_registered", lambda b: True)
    # Backend must NOT be called for an empty diff.
    monkeypatch.setattr(cw.registry, "get_backend",
                        lambda b: _fake_backend(Verdict("REVISE", [])))
    rc = cw.main(["--role", "code-reviewer", "--warden-mark"])
    assert rc == ABSTAIN
    assert cw.whooks._read_verdict("code-reviewer@gpt", git_repo) == "SHIP"  # abstain → SHIP


def test_driver_unknown_backend_usage_error(git_repo, monkeypatch):
    from _exit_codes import USAGE_ERROR
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    rc = cw.main(["--role", "code-reviewer", "--backend", "bogus"])
    assert rc == USAGE_ERROR


# ── Phase 3 (LIA-303): plan-reviewer reviews a --content-file (no git diff) ────────

def test_driver_plan_reviewer_content_file_records_verdict(git_repo, tmp_path, monkeypatch):
    # plan-reviewer has no diff; the plan text is read verbatim from --content-file and the
    # gpt verdict is recorded under plan-reviewer@gpt for the co-gate to read.
    plan = tmp_path / "plan.md"
    plan.write_text("## Plan\nDo the thing safely.", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cw.registry, "is_registered", lambda b: True)

    def _backend(b):
        def review(req):
            captured["content"] = req.content      # prove the plan text reached the backend
            return Verdict("SHIP", [], "plan looks sound")
        return types.SimpleNamespace(review=review)
    monkeypatch.setattr(cw.registry, "get_backend", _backend)

    rc = cw.main(["--role", "plan-reviewer", "--content-file", str(plan), "--warden-mark"])
    assert rc == 0
    assert "Do the thing safely." in captured["content"]
    assert cw.whooks._read_verdict("plan-reviewer@gpt", git_repo) == "SHIP"


def test_driver_plan_reviewer_without_content_file_errors(git_repo, monkeypatch):
    from _exit_codes import USAGE_ERROR
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cw.registry, "is_registered", lambda b: True)
    # No --content-file → _gather_file raises ReviewError(USAGE_ERROR); driver maps to non-zero.
    rc = cw.main(["--role", "plan-reviewer", "--warden-mark"])
    assert rc == USAGE_ERROR


def test_driver_out_writes_json(git_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(cw.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cw.cr.cfr, "get_diff", lambda root, rr, df: _DIFF)
    monkeypatch.setattr(cw.registry, "is_registered", lambda b: True)
    monkeypatch.setattr(cw.registry, "get_backend", lambda b: _fake_backend(
        Verdict("SHIP", [], "clean")))
    outp = tmp_path / "verdict.json"
    rc = cw.main(["--role", "code-reviewer", "--out", str(outp)])
    assert rc == 0
    import json
    written = json.loads(outp.read_text())
    assert written["verdict"] == "SHIP" and written["backend"] == "gpt"
