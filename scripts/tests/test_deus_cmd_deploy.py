"""Functional tests for `deus deploy`'s rebuild-decision logic.

`deus-cmd.sh` is sourced into a running shell as the live CLI, so this suite
verifies the `deploy)` wiring statically (matching the test_deus_sync convention)
AND exercises the pure `_deploy_plan()` decision function for real: the function
is wrapped in `# >>> deploy-plan` / `# <<< deploy-plan` sentinels so we extract
it, source it in bash, and feed it sample diffs — testing the OUTCOME (which
rebuild steps a diff triggers), not just string presence.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deus-cmd.sh"


def _extract_deploy_plan() -> str:
    text = SCRIPT.read_text()
    m = re.search(r"# >>> deploy-plan\n(.*?)# <<< deploy-plan", text, re.DOTALL)
    assert m, "deploy-plan sentinel markers not found in deus-cmd.sh"
    return m.group(1)


def _run_plan(changed_files: str) -> list[str]:
    """Source the extracted _deploy_plan and run it over a newline-separated list."""
    func = _extract_deploy_plan()
    # Pass the file list as a positional arg ($1), not interpolated into the script, so
    # paths containing shell-special chars ($, `, !) stay literal and never get evaluated.
    script = func + '\nprintf "%s" "$1" | _deploy_plan\n'
    result = subprocess.run(
        ["bash", "-c", script, "bash", changed_files],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.split()


# --- functional: the decision function over each bucket -----------------------


def test_src_change_triggers_build_only():
    assert _run_plan("src/index.ts") == ["build"]


def test_private_src_change_triggers_build():
    assert _run_plan("src/private/whatsapp.ts") == ["build"]


def test_container_agent_runner_triggers_container_only():
    assert _run_plan("container/agent-runner/broker.ts") == ["container"]


def test_container_dockerfile_triggers_container_only():
    assert _run_plan("container/Dockerfile") == ["container"]


def test_skills_change_triggers_container_only():
    # Skills are staged into the image by container/build.sh -> needs a rebuild.
    assert _run_plan(".claude/skills/add-slack/agent.ts") == ["container"]


def test_docs_only_triggers_nothing():
    assert _run_plan("docs/DEVELOPMENT.md") == []


def test_patterns_and_readme_only_triggers_nothing():
    assert _run_plan("patterns/general-code.md\nREADME.md") == []


def test_scripts_only_triggers_nothing():
    # scripts/*.py run live from the tree — no host build needed.
    assert _run_plan("scripts/maintenance/morning_report.py") == []


def test_mixed_src_and_container_triggers_both_in_order():
    assert _run_plan("src/a.ts\ncontainer/Dockerfile\ndocs/z.md") == ["build", "container"]


def test_empty_diff_triggers_nothing():
    assert _run_plan("") == []


def test_substrings_do_not_misfire():
    # Anchored regexes: paths that merely CONTAIN the words but are not under the
    # governed directories must NOT trigger a rebuild.
    assert _run_plan("docs/container-notes.md\nmysrc/x.ts\ntools/src-helper.md") == []


# --- static: the deploy) arm wiring is present --------------------------------


def test_deploy_arm_wiring_present():
    text = SCRIPT.read_text()
    # ff-only merge of origin/main (non-destructive, like sync).
    assert 'git -C "$deploy_repo" merge --ff-only origin/main' in text
    # Worktree guard = the private-wipe-trap encoding.
    assert "rev-parse --git-common-dir" in text
    # Plain fetch (robust tracking-ref update), diff before merge, dry-run.
    assert 'git -C "$deploy_repo" fetch origin' in text
    assert 'git -C "$deploy_repo" diff --name-only HEAD origin/main' in text
    assert "deploy_dry_run" in text
    # Conditional rebuild steps wired to the plan tokens.
    assert "_deploy_plan" in text
    assert "container/build.sh" in text
    # Cross-platform guard mirrors sync / _build_and_restart.
    assert '"$OSTYPE" != darwin*' in text


def test_deploy_is_skipped_by_freshness_nag():
    text = SCRIPT.read_text()
    # deploy does its own fetch + report, so the drift nag should not double-fire.
    assert "sync|deploy|" in text
