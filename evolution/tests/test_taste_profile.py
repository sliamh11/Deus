"""Tests for taste profile generation."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def vault_dir(tmp_path):
    """Create a temporary vault directory."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Persona" / "work-style").mkdir(parents=True)
    # Write a minimal config.json
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"vault_path": str(vault)}))
    return vault, config_file


def test_detect_conflicts_empty_old():
    """No conflicts when old profile is empty."""
    from evolution.taste_profile import detect_conflicts
    assert detect_conflicts("", "- Prefers short responses") == []


def test_detect_conflicts_removed_hypothesis():
    """Detects removed hypotheses."""
    from evolution.taste_profile import detect_conflicts
    old = "- Prefers flat functions\n- Uses early returns\n- Likes TypeScript"
    new = "- Uses early returns\n- Likes TypeScript\n- Prefers async/await"
    conflicts = detect_conflicts(old, new)
    assert len(conflicts) == 1
    assert "prefers flat functions" in conflicts[0].lower()


def test_detect_conflicts_no_change():
    """No conflicts when hypotheses are the same."""
    from evolution.taste_profile import detect_conflicts
    profile = "- Prefers short responses\n- Uses early returns"
    assert detect_conflicts(profile, profile) == []


def test_write_profile_creates_file(vault_dir, monkeypatch):
    """Test profile file creation with frontmatter."""
    vault, config_file = vault_dir
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    from evolution.taste_profile import write_profile, _HYPOTHESIS_START, _HYPOTHESIS_END
    path = write_profile("- Prefers concise responses\n- Uses early returns")

    assert path.exists()
    content = path.read_text()
    assert "type: taste-profile" in content
    assert "Prefers concise responses" in content
    assert _HYPOTHESIS_START in content
    assert _HYPOTHESIS_END in content


def test_write_profile_idempotent(vault_dir, monkeypatch):
    """Calling write_profile twice doesn't duplicate content."""
    vault, config_file = vault_dir
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    from evolution.taste_profile import write_profile, _HYPOTHESIS_START
    write_profile("- Hypothesis A\n- Hypothesis B")
    write_profile("- Hypothesis C\n- Hypothesis D")

    path = vault / "Persona" / "work-style" / "communication.md"
    content = path.read_text()
    assert content.count(_HYPOTHESIS_START) == 1
    assert "Hypothesis C" in content
    assert "Hypothesis A" not in content  # Replaced, not appended


def test_gather_evidence_returns_structure():
    """Test gather_evidence returns expected dict structure."""
    with patch("evolution.taste_profile.get_recent") as mock_recent, \
         patch("evolution.taste_profile.get_storage") as mock_storage:
        mock_recent.return_value = [{"prompt": "test", "response": "ok", "judge_score": 0.8}] * 15
        mock_storage.return_value.get_interactions_with_signals.return_value = []

        from evolution.taste_profile import gather_evidence
        result = gather_evidence(min_interactions=5)
        assert "good" in result
        assert "bad" in result
        assert "sufficient" in result
        assert result["sufficient"] is True


def test_infer_hypotheses_calls_generate():
    """Test that infer_hypotheses calls the LLM generate function."""
    with patch("evolution.taste_profile.generate") as mock_gen:
        mock_gen.return_value = "- Prefers short responses"
        from evolution.taste_profile import infer_hypotheses
        result = infer_hypotheses(
            {"good": [], "bad": [], "signals": []},
            current_profile="",
        )
        assert mock_gen.called
        assert "Prefers short responses" in result


def test_consolidation_sentinel_guard(vault_dir, monkeypatch):
    """Consolidation section uses sentinels for idempotency."""
    vault, config_file = vault_dir
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    from evolution.taste_profile import (
        write_profile, consolidate_style_reflections,
        _CONSOLIDATION_START,
    )

    # First write the hypotheses
    write_profile("- Hypothesis A")

    # Mock the storage and LLM for consolidation
    with patch("evolution.taste_profile.get_storage") as mock_storage, \
         patch("evolution.taste_profile.generate") as mock_gen:
        mock_storage.return_value.get_style_reflections.return_value = [
            {"content": "Style reflection 1", "category": "style", "score_at_gen": 0.8, "timestamp": "2026-01-01"},
            {"content": "Style reflection 2", "category": "style", "score_at_gen": 0.9, "timestamp": "2026-01-02"},
            {"content": "Style reflection 3", "category": "style", "score_at_gen": 0.85, "timestamp": "2026-01-03"},
        ]
        mock_gen.return_value = "- Consolidated style point"

        result = consolidate_style_reflections(force=True)
        assert result is not None

        # Run again — should overwrite, not append
        consolidate_style_reflections(force=True)

        path = vault / "Persona" / "work-style" / "communication.md"
        content = path.read_text()
        assert content.count(_CONSOLIDATION_START) == 1
