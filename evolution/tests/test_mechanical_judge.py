"""Tests for the mechanical tool-economy scorer."""
import pytest

from evolution.judge.mechanical import score_tool_economy


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
