"""Session type contract tests (RETRO-2026-05-14-02).

Static analysis only — parses settings JSON and greps source files.
No real Claude Code sessions are spawned.

See docs/session-type-contract.md for the full contract specification.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_SETTINGS = ROOT / ".claude" / "settings.json"
CONTAINER_RUNNER = ROOT / "container" / "agent-runner" / "src" / "index.ts"
STOP_HOOK = ROOT / "scripts" / "stop_hook.py"
DEUS_CMD = ROOT / "deus-cmd.sh"


def _load_project_settings() -> dict:
    return json.loads(PROJECT_SETTINGS.read_text())


def _extract_hook_commands(settings: dict) -> list[str]:
    """Extract all command strings from the nested hooks structure."""
    commands: list[str] = []
    for _event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if cmd:
                    commands.append(cmd)
    return commands


def _extract_hook_commands_for_event(settings: dict, event: str) -> list[str]:
    """Extract command strings for a specific hook event."""
    commands: list[str] = []
    for group in settings.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            cmd = hook.get("command", "")
            if cmd:
                commands.append(cmd)
    return commands


def _extract_script_paths(commands: list[str]) -> list[str]:
    """Extract script paths from hook command strings.

    Handles both:
      bash -c '"${CLAUDE_PROJECT_DIR:-.}/path/to/script.sh"'
      bash -c 'python3 "${CLAUDE_PROJECT_DIR:-.}/path/to/script.py"'
    """
    paths: list[str] = []
    for cmd in commands:
        for m in re.finditer(r'\$\{CLAUDE_PROJECT_DIR:-\.\}/([^\s"\']+)', cmd):
            paths.append(m.group(1))
    return paths


# ---------------------------------------------------------------------------
# TestProjectSettingsHooks — structural rules for hook presence
# ---------------------------------------------------------------------------


class TestProjectSettingsHooks:
    """Validates that .claude/settings.json has the expected hook structure."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.settings = _load_project_settings()

    def test_settings_file_exists(self):
        assert PROJECT_SETTINGS.exists()

    def test_all_hook_events_present(self):
        hooks = self.settings.get("hooks", {})
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            assert event in hooks, f"Missing hook event: {event}"

    def test_session_start_has_warden_shim(self):
        cmds = _extract_hook_commands_for_event(self.settings, "SessionStart")
        assert any("warden-shim" in c and "session-init" in c for c in cmds)

    def test_session_start_has_preflight_hook(self):
        # LIA-284: the concurrent-session collision warning must stay wired in.
        cmds = _extract_hook_commands_for_event(self.settings, "SessionStart")
        assert any("session_preflight_hook" in c for c in cmds)

    def test_user_prompt_submit_has_memory_retrieval(self):
        cmds = _extract_hook_commands_for_event(self.settings, "UserPromptSubmit")
        assert any("memory_retrieval" in c for c in cmds)

    def test_pretooluse_has_plan_review_gate(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PreToolUse")
        assert any("plan-review-gate" in c for c in cmds)

    def test_pretooluse_has_tdd_test_lock(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PreToolUse")
        assert any("tdd-test-lock" in c for c in cmds)

    def test_pretooluse_has_code_review_gate(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PreToolUse")
        assert any("code-review-gate" in c for c in cmds)

    def test_posttooluse_has_code_review_invalidator(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PostToolUse")
        assert any("code-review-invalidator" in c for c in cmds)

    def test_posttooluse_has_threat_model_gate(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PostToolUse")
        assert any("threat-model-gate" in c for c in cmds)

    def test_posttooluse_has_path_leak_detector(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PostToolUse")
        assert any("path-leak-detector" in c for c in cmds)

    def test_posttooluse_has_warden_verdict_tracker(self):
        cmds = _extract_hook_commands_for_event(self.settings, "PostToolUse")
        assert any("warden-verdict-tracker" in c for c in cmds)


# ---------------------------------------------------------------------------
# TestProjectHookFilesExist — every referenced script must exist on disk
# ---------------------------------------------------------------------------


class TestProjectHookFilesExist:
    """Verifies that all scripts referenced in project settings exist."""

    @pytest.fixture(autouse=True)
    def _load(self):
        settings = _load_project_settings()
        commands = _extract_hook_commands(settings)
        self.script_paths = _extract_script_paths(commands)

    def test_at_least_one_script_found(self):
        assert len(self.script_paths) > 0, "No script paths extracted from settings"

    @pytest.fixture(params=None)
    def script_path(self):
        """Dynamically parametrized — see pytest_generate_tests."""

    def test_script_file_exists(self, script_path: str):
        full_path = ROOT / script_path
        assert full_path.exists(), f"Hook script not found: {script_path}"


def pytest_generate_tests(metafunc):
    if "script_path" in metafunc.fixturenames:
        settings = _load_project_settings()
        commands = _extract_hook_commands(settings)
        paths = _extract_script_paths(commands)
        metafunc.parametrize("script_path", paths, ids=paths)


# ---------------------------------------------------------------------------
# TestContainerSession — invariants 2 and 3
# ---------------------------------------------------------------------------


class TestContainerSession:
    """Validates container session properties in agent-runner/src/index.ts."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.source = CONTAINER_RUNNER.read_text()

    def test_container_runner_exists(self):
        assert CONTAINER_RUNNER.exists()

    def test_permission_mode_is_bypass(self):
        assert "permissionMode: 'bypassPermissions'" in self.source

    def test_allow_dangerously_skip_permissions(self):
        assert "allowDangerouslySkipPermissions: true" in self.source

    def test_settings_sources_project_and_user(self):
        assert "settingSources: ['project', 'user']" in self.source

    def test_has_memory_retrieval_hook(self):
        assert "createMemoryRetrievalHook" in self.source

    def test_has_pre_compact_hook(self):
        assert "createPreCompactHook" in self.source

    def test_has_tool_size_log_hook(self):
        assert "createToolSizeLogHook" in self.source

    def test_has_tool_audit_hook(self):
        assert "createToolAuditHook" in self.source


# ---------------------------------------------------------------------------
# TestBackgroundSession — invariant 1
# ---------------------------------------------------------------------------


class TestBackgroundSession:
    """Validates background session detection in stop_hook.py."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.source = STOP_HOOK.read_text()

    def test_stop_hook_exists(self):
        assert STOP_HOOK.exists()

    def test_has_is_bg_session_function(self):
        assert "_is_bg_session" in self.source

    def test_bg_detection_uses_claude_job_dir(self):
        assert 'os.environ.get("CLAUDE_JOB_DIR")' in self.source

    def test_compress_gate_reads_claude_job_dir(self):
        assert 'os.environ["CLAUDE_JOB_DIR"]' in self.source

    def test_has_compress_gate_function(self):
        assert "_bg_compress_gate" in self.source


# ---------------------------------------------------------------------------
# TestCLISession — CLI launch properties
# ---------------------------------------------------------------------------


class TestCLISession:
    """Validates CLI session properties in deus-cmd.sh."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.source = DEUS_CMD.read_text()

    def test_deus_cmd_exists(self):
        assert DEUS_CMD.exists()

    def test_has_dangerously_skip_permissions_flag(self):
        assert "--dangerously-skip-permissions" in self.source

    def test_bypass_gated_by_prefs_bypass(self):
        assert "PREFS_BYPASS" in self.source

    def test_tui_exports_deus_tui_bypass(self):
        assert "export DEUS_TUI_BYPASS" in self.source

    def test_tui_exports_deus_tui_mode(self):
        assert "export DEUS_TUI_MODE" in self.source

    def test_tui_exports_deus_tui_backend(self):
        assert "export DEUS_TUI_BACKEND" in self.source


# ---------------------------------------------------------------------------
# TestWorktreeSettings — invariants 4 and 5 for worktrees
# ---------------------------------------------------------------------------


_worktree_settings = list(
    (ROOT / ".claude" / "worktrees").glob("*/.claude/settings.json")
)


if _worktree_settings:

    class TestWorktreeSettings:
        """Validates minimum-invariant hooks in worktree settings."""

        @pytest.fixture(params=_worktree_settings, ids=[str(p.relative_to(ROOT)) for p in _worktree_settings])
        def wt_settings(self, request) -> dict:
            return json.loads(request.param.read_text())

        def test_has_memory_retrieval_in_user_prompt_submit(self, wt_settings):
            cmds = _extract_hook_commands_for_event(wt_settings, "UserPromptSubmit")
            assert any("memory_retrieval" in c for c in cmds), (
                "Worktree missing memory_retrieval in UserPromptSubmit"
            )

        def test_has_tdd_test_lock_in_pretooluse(self, wt_settings):
            cmds = _extract_hook_commands_for_event(wt_settings, "PreToolUse")
            assert any("tdd-test-lock" in c for c in cmds), (
                "Worktree missing tdd-test-lock in PreToolUse"
            )


# ---------------------------------------------------------------------------
# TestCrossSessionInvariants — all 5 cross-cutting invariants
# ---------------------------------------------------------------------------


class TestCrossSessionInvariants:
    """Cross-cutting contract assertions across session types."""

    def test_bg_detection_is_env_var_not_flag(self):
        """Invariant 1: bg detection uses CLAUDE_JOB_DIR env var, not CLI flag."""
        source = STOP_HOOK.read_text()
        assert "_is_bg_session" in source
        assert 'os.environ.get("CLAUDE_JOB_DIR")' in source
        assert "--bg" not in source.split("_is_bg_session")[1].split("\n")[0]

    def test_container_uses_no_shell_hooks(self):
        """Invariant 2: container hooks are TypeScript, not shell scripts."""
        source = CONTAINER_RUNNER.read_text()
        hooks_start = source.index("hooks:")
        depth, end = 0, hooks_start
        for i, ch in enumerate(source[hooks_start:], hooks_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        hooks_section = source[hooks_start:end]
        assert "bash -c" not in hooks_section
        assert ".sh" not in hooks_section

    def test_container_hardcodes_bypass_permissions(self):
        """Invariant 3: container always uses bypassPermissions."""
        source = CONTAINER_RUNNER.read_text()
        assert source.count("permissionMode: 'bypassPermissions'") >= 1

    def test_project_settings_has_memory_retrieval(self):
        """Invariant 4: memory retrieval in project UserPromptSubmit."""
        settings = _load_project_settings()
        cmds = _extract_hook_commands_for_event(settings, "UserPromptSubmit")
        assert any("memory_retrieval" in c for c in cmds)

    def test_container_has_memory_retrieval_equivalent(self):
        """Invariant 4: container has TypeScript memory retrieval hook."""
        source = CONTAINER_RUNNER.read_text()
        assert "createMemoryRetrievalHook" in source

    def test_project_settings_has_tdd_test_lock(self):
        """Invariant 5: tdd-test-lock in project PreToolUse."""
        settings = _load_project_settings()
        cmds = _extract_hook_commands_for_event(settings, "PreToolUse")
        assert any("tdd-test-lock" in c for c in cmds)

    def test_container_has_no_tdd_test_lock(self):
        """Invariant 5: container does NOT have tdd-test-lock."""
        source = CONTAINER_RUNNER.read_text()
        assert "tdd-test-lock" not in source
