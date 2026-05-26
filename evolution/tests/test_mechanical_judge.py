"""Tests for the mechanical scorers (tool-economy + gate-audit)."""
import pytest

from evolution.judge.mechanical import score_tool_economy, score_gate_audit


class TestEmptyInput:
    def test_empty_list(self):
        score, diag = score_tool_economy([])
        assert score == 1.0
        assert diag["violations"] == 0
        assert diag["rules_fired"] == []


class TestR1EditWithoutRead:
    def test_edit_unread_path(self):
        calls = [{"name": "Edit", "file_path": "/a.py"}]
        score, diag = score_tool_economy(calls)
        assert score < 1.0
        assert "R1" in diag["rules_fired"]
        assert diag["edit_without_read"] == 1

    def test_write_unread_path(self):
        calls = [{"name": "Write", "file_path": "/a.py"}]
        score, diag = score_tool_economy(calls)
        assert "R1" in diag["rules_fired"]

    def test_edit_after_read_same_path(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Edit", "file_path": "/a.py"},
        ]
        score, diag = score_tool_economy(calls)
        assert score == 1.0
        assert diag["edit_without_read"] == 0

    def test_edit_after_read_different_path(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Edit", "file_path": "/b.py"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["edit_without_read"] == 1

    def test_write_after_read_same_path(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Write", "file_path": "/a.py"},
        ]
        score, diag = score_tool_economy(calls)
        assert score == 1.0

    def test_edit_no_file_path_no_violation(self):
        calls = [{"name": "Edit"}]
        score, diag = score_tool_economy(calls)
        assert diag["edit_without_read"] == 0


class TestR2DuplicateRead:
    def test_read_same_file_twice(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Read", "file_path": "/a.py"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["duplicate_read"] == 1

    def test_read_same_file_three_times(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Read", "file_path": "/a.py"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["duplicate_read"] == 2

    def test_read_two_different_files(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Read", "file_path": "/b.py"},
        ]
        score, diag = score_tool_economy(calls)
        assert score == 1.0
        assert diag["duplicate_read"] == 0


class TestR4aExploreNoRecon:
    def test_explore_with_no_prior_read(self):
        calls = [{"name": "Agent", "subagent_type": "Explore"}]
        _, diag = score_tool_economy(calls)
        assert diag["explore_no_prior_recon"] == 1

    def test_explore_after_read(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Agent", "subagent_type": "Explore"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["explore_no_prior_recon"] == 0

    def test_explore_after_bash_grep(self):
        calls = [
            {"name": "Bash", "command": "grep -rn 'foo' src/"},
            {"name": "Agent", "subagent_type": "Explore"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["explore_no_prior_recon"] == 0

    def test_non_explore_agent_no_violation(self):
        calls = [{"name": "Agent", "subagent_type": "plan-reviewer"}]
        _, diag = score_tool_economy(calls)
        assert diag["explore_no_prior_recon"] == 0


class TestScoreFormula:
    def test_zero_violations(self):
        score, _ = score_tool_economy([{"name": "Read", "file_path": "/a.py"}])
        assert score == 1.0

    def test_one_violation(self):
        score, _ = score_tool_economy([{"name": "Edit", "file_path": "/a.py"}])
        assert score == pytest.approx(0.85)

    def test_six_violations_near_zero(self):
        calls = [{"name": "Edit", "file_path": f"/{i}.py"} for i in range(6)]
        score, _ = score_tool_economy(calls)
        assert score == pytest.approx(0.10)

    def test_seven_violations_clamped(self):
        calls = [{"name": "Edit", "file_path": f"/{i}.py"} for i in range(7)]
        score, _ = score_tool_economy(calls)
        assert score == 0.0


class TestMixedPattern:
    def test_r1_and_r2_combined(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Edit", "file_path": "/b.py"},
        ]
        _, diag = score_tool_economy(calls)
        assert diag["violations"] == 2
        assert diag["duplicate_read"] == 1
        assert diag["edit_without_read"] == 1

    def test_clean_workflow(self):
        calls = [
            {"name": "Bash", "command": "grep -rn 'pattern' src/"},
            {"name": "Read", "file_path": "/src/foo.py"},
            {"name": "Edit", "file_path": "/src/foo.py"},
            {"name": "Agent", "subagent_type": "Explore"},
            {"name": "Read", "file_path": "/src/bar.py"},
            {"name": "Write", "file_path": "/src/bar.py"},
        ]
        score, diag = score_tool_economy(calls)
        assert score == 1.0
        assert diag["violations"] == 0


# ===================================================================
# Gate-Audit scorer tests
# ===================================================================


class TestGateAuditEmpty:
    def test_empty_list(self):
        score, diag = score_gate_audit([])
        assert score == 1.0
        assert diag["violations"] == 0
        assert diag["rules_fired"] == []

    def test_no_marks(self):
        calls = [
            {"name": "Read", "file_path": "/a.py"},
            {"name": "Edit", "file_path": "/a.py"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == 1.0


class TestG1MarkWithoutWarden:
    def test_plan_mark_no_reviewer(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'reason'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == pytest.approx(0.75)
        assert diag["mark_without_warden"] == 1

    def test_code_mark_no_reviewer(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark code-reviewed SHIP 'passed'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["mark_without_warden"] == 1

    def test_plan_mark_with_reviewer(self):
        calls = [
            {"name": "Agent", "subagent_type": "plan-reviewer"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'R1 SHIP'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == 1.0
        assert diag["mark_without_warden"] == 0

    def test_code_mark_with_reviewer(self):
        calls = [
            {"name": "Agent", "subagent_type": "code-reviewer"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark code-reviewed SHIP 'clean'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == 1.0

    def test_plan_mark_with_wrong_reviewer(self):
        calls = [
            {"name": "Agent", "subagent_type": "code-reviewer"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'reason'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["mark_without_warden"] == 1

    def test_multiple_marks_without_wardens(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'r'"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark code-reviewed SHIP 'r'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == pytest.approx(0.50)
        assert diag["mark_without_warden"] == 2

    def test_verified_mark_exempted(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark verified SHIP 'all checks pass'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == 1.0
        assert diag["mark_without_warden"] == 0

    def test_absolute_path_mark(self):
        calls = [
            {"name": "Bash", "command": "python3 /Users/user/deus/scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'r'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["mark_without_warden"] == 1

    def test_cd_prefix_mark(self):
        calls = [
            {"name": "Bash", "command": "cd ~/deus && python3 scripts/codex_warden_hooks.py mark code-reviewed SHIP 'r'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["mark_without_warden"] == 1


class TestG2TrivialOnSourceEdit:
    def test_trivial_with_source_edits(self):
        calls = [
            {"name": "Edit", "file_path": "/src/router.ts"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed TRIVIAL 'drift'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["trivial_on_source_edit"] == 1
        assert diag["mark_without_warden"] == 1

    def test_trivial_with_non_source_edits(self):
        calls = [
            {"name": "Edit", "file_path": "/docs/README.md"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed TRIVIAL 'docs only'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["trivial_on_source_edit"] == 0

    def test_trivial_lowercase_detected(self):
        calls = [
            {"name": "Write", "file_path": "/src/new.py"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark code-reviewed trivial 'small fix'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["trivial_on_source_edit"] == 1

    def test_ship_mark_no_g2(self):
        calls = [
            {"name": "Edit", "file_path": "/src/router.ts"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'reason'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["trivial_on_source_edit"] == 0

    def test_source_extensions(self):
        for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".rs"):
            calls = [
                {"name": "Edit", "file_path": f"/src/file{ext}"},
                {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed TRIVIAL 'x'"},
            ]
            _, diag = score_gate_audit(calls)
            assert diag["trivial_on_source_edit"] == 1, f"Failed for extension {ext}"

    def test_non_source_extensions(self):
        for ext in (".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".env"):
            calls = [
                {"name": "Edit", "file_path": f"/config/file{ext}"},
                {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed TRIVIAL 'x'"},
            ]
            _, diag = score_gate_audit(calls)
            assert diag["trivial_on_source_edit"] == 0, f"False positive for extension {ext}"


class TestGateAuditScoreFormula:
    def test_g1_penalty(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'r'"},
        ]
        score, _ = score_gate_audit(calls)
        assert score == pytest.approx(0.75)

    def test_g1_plus_g2_compound(self):
        calls = [
            {"name": "Edit", "file_path": "/src/a.py"},
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed TRIVIAL 'drift'"},
        ]
        score, diag = score_gate_audit(calls)
        assert score == pytest.approx(0.60)
        assert diag["mark_without_warden"] == 1
        assert diag["trivial_on_source_edit"] == 1

    def test_floor_at_zero(self):
        calls = []
        for gate in ("plan-reviewed", "code-reviewed"):
            calls.append({"name": "Edit", "file_path": "/src/a.py"})
            calls.append({"name": "Bash", "command": f"python3 scripts/codex_warden_hooks.py mark {gate} TRIVIAL 'x'"})
            calls.append({"name": "Bash", "command": f"python3 scripts/codex_warden_hooks.py mark {gate} TRIVIAL 'y'"})
        score, _ = score_gate_audit(calls)
        assert score == 0.0


class TestGateAuditKnownFalsePositive:
    """V1 limitation: reviewer in turn N, mark in turn N+1 fires G1."""
    def test_mark_only_turn_fires_g1(self):
        calls = [
            {"name": "Bash", "command": "python3 scripts/codex_warden_hooks.py mark plan-reviewed SHIP 'SHIP from previous turn'"},
        ]
        _, diag = score_gate_audit(calls)
        assert diag["mark_without_warden"] == 1
