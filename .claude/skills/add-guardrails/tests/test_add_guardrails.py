"""Tests for the /add-guardrails installer and the vendored warden-gate.

Run from anywhere:  python3 -m pytest .claude/skills/add-guardrails/tests/ -v

These cover the deterministic, automatable parts: the settings.json deep-merge,
the CLAUDE.md section merge, the gate marker lifecycle, git-commit detection,
and template purity. Live Claude Code session behavior is covered separately by
the manual smoke test documented in skill.md.
"""

from __future__ import annotations

import importlib.util
import json
import re
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load(TEMPLATES / "hooks" / "warden-gate.py", "warden_gate")
installer = _load(SKILL_DIR / "add_guardrails.py", "add_guardrails")


# --- settings.json deep-merge ---------------------------------------------


def test_settings_merge_preserves_existing_and_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    seed = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": "echo keep-me"}]}
            ]
        },
    }
    settings.write_text(json.dumps(seed), encoding="utf-8")

    installer._merge_settings(tmp_path, dry_run=False)
    merged = json.loads(settings.read_text())

    # Pre-existing content survives.
    assert merged["permissions"] == {"allow": ["Bash(ls)"]}
    read_group = next(g for g in merged["hooks"]["PreToolUse"] if g.get("matcher") == "Read")
    assert read_group["hooks"][0]["command"] == "echo keep-me"

    # Our gate wiring is present.
    all_cmds = json.dumps(merged)
    assert "warden-gate.py" in all_cmds and "plan-review-gate" in all_cmds
    assert "session-init" in all_cmds

    # Second run is a no-op.
    before = settings.read_text()
    actions = installer._merge_settings(tmp_path, dry_run=False)
    assert settings.read_text() == before
    assert any("unchanged" in a for a in actions)


def test_settings_merge_creates_when_absent(tmp_path):
    actions = installer._merge_settings(tmp_path, dry_run=False)
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    assert "plan-review-gate" in settings.read_text()
    assert actions  # something was added


def test_settings_merge_dry_run_writes_nothing(tmp_path):
    installer._merge_settings(tmp_path, dry_run=True)
    assert not (tmp_path / ".claude" / "settings.json").exists()


# --- CLAUDE.md section merge ----------------------------------------------


def test_claude_md_merge_is_idempotent(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# My Project\n\nSome existing guidance.\n", encoding="utf-8")

    installer._merge_claude_md(tmp_path, dry_run=False)
    first = claude.read_text()
    assert "# My Project" in first
    assert first.count(installer.CLAUDE_START) == 1
    assert first.count(installer.CLAUDE_END) == 1

    installer._merge_claude_md(tmp_path, dry_run=False)
    assert claude.read_text() == first  # stable
    assert claude.read_text().count(installer.CLAUDE_START) == 1


def test_claude_md_created_when_absent(tmp_path):
    installer._merge_claude_md(tmp_path, dry_run=False)
    claude = tmp_path / "CLAUDE.md"
    assert claude.exists()
    assert installer.CLAUDE_START in claude.read_text()


# --- .gitignore sync -------------------------------------------------------


def test_gitignore_created_with_gate_state(tmp_path):
    installer._ensure_gitignore(tmp_path, dry_run=False)
    gi = (tmp_path / ".gitignore").read_text()
    for entry in installer.GITIGNORE_ENTRIES:
        assert entry in gi
    assert ".warden-verdicts.json" in gi  # the stale-REVISE-travels guard


def test_gitignore_preserves_existing_and_is_idempotent(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n*.log\n", encoding="utf-8")

    installer._ensure_gitignore(tmp_path, dry_run=False)
    after = gi.read_text()
    assert "node_modules/" in after and "*.log" in after  # preserved
    assert ".claude/.plan-reviewed" in after

    action = installer._ensure_gitignore(tmp_path, dry_run=False)
    assert gi.read_text() == after  # idempotent
    assert "unchanged" in action


def test_gitignore_dry_run_writes_nothing(tmp_path):
    installer._ensure_gitignore(tmp_path, dry_run=True)
    assert not (tmp_path / ".gitignore").exists()


def test_gitignore_adds_only_missing_entries(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(".claude/.plan-reviewed\n.claude/.code-reviewed\n", encoding="utf-8")
    installer._ensure_gitignore(tmp_path, dry_run=False)
    lines = gi.read_text().splitlines()
    # the two already-present entries are not duplicated
    assert lines.count(".claude/.plan-reviewed") == 1
    assert ".claude/.verified" in lines
    assert ".claude/.warden-verdicts.json" in lines


# --- gate marker lifecycle -------------------------------------------------


def test_plan_review_gate_blocks_without_marker_allows_with(tmp_path, capsys):
    event = {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}

    assert gate.plan_review_gate(tmp_path, event) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    gate.mark(tmp_path, "plan-reviewed", "SHIP", "reviewed")
    capsys.readouterr()  # drain the mark confirmation
    assert gate.plan_review_gate(tmp_path, event) == 0
    assert capsys.readouterr().out.strip() == ""  # allowed, no deny


def test_session_init_clears_markers(tmp_path):
    gate.mark(tmp_path, "plan-reviewed", "SHIP", "x")
    assert gate._has_marker(tmp_path, "plan-reviewed")
    gate.session_init(tmp_path)
    assert not gate._has_marker(tmp_path, "plan-reviewed")


def test_commit_gates_only_fire_on_git_commit(tmp_path, capsys):
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert gate.code_review_gate(tmp_path, status) == 0
    assert capsys.readouterr().out.strip() == ""  # not a commit, not blocked

    commit = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
    assert gate.code_review_gate(tmp_path, commit) == 0
    assert "deny" in capsys.readouterr().out  # commit without marker is blocked

    gate.mark(tmp_path, "code-reviewed", "SHIP", "ok")
    capsys.readouterr()  # drain the mark confirmation
    assert gate.code_review_gate(tmp_path, commit) == 0
    assert capsys.readouterr().out.strip() == ""  # allowed after marker


def test_plan_mode_invalidator_clears_on_plan_subagent(tmp_path):
    gate.mark(tmp_path, "plan-reviewed", "SHIP", "x")
    event = {"tool_name": "Task", "tool_input": {"subagent_type": "Plan"}}
    gate.plan_mode_invalidator(tmp_path, event)
    assert not gate._has_marker(tmp_path, "plan-reviewed")


# --- git-commit detection --------------------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git commit -m 'x'", True),
        ("git -C /tmp/repo commit", True),
        ("cd foo && git commit", True),
        ("git commit -- file.py", True),
        ("git status", False),
        ("git committed-files", False),
        ("echo git commit", False),
        # LIA-518: broadened bypass coverage (mirrors
        # scripts/tests/test_codex_warden_hooks.py::test_git_commit_re)
        ("git --no-pager commit", True),
        ("git -C /a -C b commit", True),
        ("git -C '/path with spaces' commit -m x", True),
        ("env git commit", True),
        ("GIT_DIR=/foo git commit", True),
        ("sudo git commit", True),
        ("echo hi\ngit commit -m x", True),
        ("foo; git commit -m x", True),
        ("echo ready\n  git commit", True),
        ("echo ready\n\tgit commit", True),
        ("  git commit -m x", True),
        ("FOO= git commit", True),
        ("FOO='two words' git commit", True),
        ("git --git-dir=/foo commit", True),
        # Other single-letter global flags must stay matched (the ReDoS fix
        # narrowed the generic short-flag branch to exclude only C/c).
        ("git -p commit", True),
        ("git -P commit", True),
        ("git -q commit", True),
        ("git -s commit", True),
        ("git commitment", False),
        ("git log --oneline", False),
        ('git log --grep="git commit"', False),
        # Known, accepted limitation (see GIT_COMMIT_RE's docstring): -c does
        # not parse embedded/partial shell quoting.
        ('git -c user.name="John Doe" commit', False),
    ],
)
def test_git_commit_regex(command, expected):
    assert bool(gate.GIT_COMMIT_RE.search(command)) is expected


def test_git_commit_regex_heredoc_mention_is_a_known_accepted_over_trigger():
    # LIA-518: re.MULTILINE means a heredoc merely mentioning "git commit" on
    # its own line also matches -- a deliberately accepted tradeoff (see the
    # host copy's docstring for rationale). Documented here so it isn't later
    # mistaken for a bug and "fixed" back into a false negative.
    heredoc = (
        "cat <<'EOF' > README.md\n"
        "Remember to run:\n"
        "git commit -m \"your message\"\n"
        "EOF"
    )
    assert gate.GIT_COMMIT_RE.search(heredoc) is not None


def test_git_commit_regex_no_redos_on_consecutive_newlines():
    payload = ("\n" * 200_000) + "notgit"
    start = time.perf_counter()
    gate.GIT_COMMIT_RE.search(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on 200k consecutive newlines"


def test_git_commit_regex_no_redos_on_ambiguous_flag_alternation():
    # CodeQL py/redos (high severity, caught in CI on the host copy's PR):
    # a generic `-[A-Za-z]` short-flag alternative overlapped with the
    # dedicated `-C`/`-c` branches, causing exponential-backtracking
    # ambiguity. Fixed by narrowing the branch to exclude only C/c
    # (`-[A-BD-Za-bd-z]`), not removing it outright -- an earlier attempt
    # removed it entirely and silently regressed -p/-P/-q/-s coverage (see
    # test_git_commit_regex for that positive-match guard). Regression guard
    # here mirrors the host copy's test.
    payload = "&git " + ("-C -A " * 2000) + "nope"
    start = time.perf_counter()
    gate.GIT_COMMIT_RE.search(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on an ambiguous -C/-A chain"


def test_git_commit_regex_no_redos_on_ambiguous_quoted_value_alternation():
    # CodeQL py/redos (high severity): the -C/env-var value patterns' bare
    # `\S+`/`\S*` fallback could also match already-quoted content, creating
    # the same alternation-ambiguity ReDoS shape. Fixed by excluding quote
    # characters from the bare fallback.
    payload_c = "git -C " + ('"" -C ' * 2000) + "nope"
    payload_env = "&git " + ('"" A=' * 2000) + "nope"
    for payload in (payload_c, payload_env):
        start = time.perf_counter()
        gate.GIT_COMMIT_RE.search(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on ambiguous quoted-value alternation"


# --- trivial-bypass discipline ---------------------------------------------


def test_trivial_bypass_blocked_after_revise(tmp_path):
    assert gate.mark(tmp_path, "plan-reviewed", "REVISE", "issues") == 0
    assert not gate._has_marker(tmp_path, "plan-reviewed")
    # trivial bypass refused after REVISE
    assert gate.mark(tmp_path, "plan-reviewed", "TRIVIAL", "skip") == 2
    # a real re-review still works
    assert gate.mark(tmp_path, "plan-reviewed", "SHIP", "fixed") == 0
    assert gate._has_marker(tmp_path, "plan-reviewed")


def test_mark_rejects_unknown_marker_and_verdict(tmp_path):
    assert gate.mark(tmp_path, "bogus", "SHIP", "x") == 2
    assert gate.mark(tmp_path, "plan-reviewed", "MAYBE", "x") == 2


# --- template purity (no host-coupling leaks into shipped templates) -------

# Patterns that would couple a template to the source project instead of the
# target repo. None of these is a personal identifier, so this list is safe to
# commit; personal-identifier leakage is caught by the separate CI scan.
_FORBIDDEN = [
    r"~/deus",
    r"/Users/",
    r"\bdeus\b",
    r"codex_warden",
    r"evolution/cli",
    r"\.mex",
    r"feedback_",
    r"RETRO-",
    r"LIA-",
]


def test_templates_contain_no_host_coupling():
    offenders: list[str] = []
    for path in sorted(TEMPLATES.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN:
            if re.search(pattern, text, re.IGNORECASE):
                offenders.append(f"{path.relative_to(TEMPLATES)}: {pattern}")
    assert not offenders, "host-coupling found in templates:\n" + "\n".join(offenders)
