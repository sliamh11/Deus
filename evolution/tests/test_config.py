"""Tests for evolution/config.py — load_api_key fallback chain."""
import os
from pathlib import Path

import pytest

import evolution.config as config_mod
from evolution.config import load_api_key


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Ensure GEMINI_API_KEY is unset before each test."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _write_env(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"GEMINI_API_KEY={key}\n")


def test_repo_env_returned(tmp_path, monkeypatch):
    """Case 1: GEMINI_API_KEY in repo .env → returned (current behavior preserved)."""
    repo_env = tmp_path / ".env"
    _write_env(repo_env, "repo-key-123")
    user_env = tmp_path / "user" / ".env"

    monkeypatch.setattr(config_mod, "CONFIG_ENV", repo_env)
    monkeypatch.setattr(config_mod, "USER_CONFIG_ENV", user_env)
    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    assert load_api_key() == "repo-key-123"


def test_user_level_env_fallback(tmp_path, monkeypatch):
    """Case 2: Repo .env missing, user-level .env has key → returned."""
    repo_env = tmp_path / "missing" / ".env"
    user_env = tmp_path / "user" / ".env"
    _write_env(user_env, "user-key-456")

    monkeypatch.setattr(config_mod, "CONFIG_ENV", repo_env)
    monkeypatch.setattr(config_mod, "USER_CONFIG_ENV", user_env)
    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    assert load_api_key() == "user-key-456"


def test_env_var_fallback(tmp_path, monkeypatch):
    """Case 3: Both .env files missing, env var set → env-var value returned."""
    repo_env = tmp_path / "missing1" / ".env"
    user_env = tmp_path / "missing2" / ".env"

    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])
    monkeypatch.setenv("GEMINI_API_KEY", "envvar-key-789")

    assert load_api_key() == "envvar-key-789"


def test_all_sources_missing_raises(tmp_path, monkeypatch):
    """Case 4: All sources empty → RuntimeError mentioning both paths + env var."""
    repo_env = tmp_path / "missing1" / ".env"
    user_env = tmp_path / "missing2" / ".env"

    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    with pytest.raises(RuntimeError) as exc_info:
        load_api_key()

    msg = str(exc_info.value)
    assert str(repo_env) in msg
    assert str(user_env) in msg
    assert "env var" in msg


def test_repo_env_no_key_falls_through_to_user(tmp_path, monkeypatch):
    """Case 5: Repo .env exists but has no GEMINI_API_KEY, user-level .env has it → user-level wins."""
    repo_env = tmp_path / ".env"
    repo_env.write_text("OTHER_KEY=something\nFOO=bar\n")

    user_env = tmp_path / "user" / ".env"
    _write_env(user_env, "user-fallback-key")

    monkeypatch.setattr(config_mod, "CONFIG_ENV", repo_env)
    monkeypatch.setattr(config_mod, "USER_CONFIG_ENV", user_env)
    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    assert load_api_key() == "user-fallback-key"


def test_empty_value_falls_through_to_user_env(tmp_path, monkeypatch):
    """Case 6: Empty `GEMINI_API_KEY=` in repo .env, real key in user-level .env → user-level wins.

    Regression for issue #1006: an empty value was returned as "" instead of
    falling through, silently disabling the eval loop.
    """
    repo_env = tmp_path / ".env"
    _write_env(repo_env, "")

    user_env = tmp_path / "user" / ".env"
    _write_env(user_env, "user-real-key")

    monkeypatch.setattr(config_mod, "CONFIG_ENV", repo_env)
    monkeypatch.setattr(config_mod, "USER_CONFIG_ENV", user_env)
    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    assert load_api_key() == "user-real-key"


def test_empty_value_falls_through_to_env_var(tmp_path, monkeypatch):
    """Case 7: Empty value in both .env files, env var set → env-var value returned."""
    repo_env = tmp_path / ".env"
    _write_env(repo_env, "")
    user_env = tmp_path / "user" / ".env"
    _write_env(user_env, "")

    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])
    monkeypatch.setenv("GEMINI_API_KEY", "envvar-real-key")

    assert load_api_key() == "envvar-real-key"


def test_empty_value_everywhere_raises(tmp_path, monkeypatch):
    """Case 8: Empty value in repo .env, nothing else → RuntimeError (same as absent)."""
    repo_env = tmp_path / ".env"
    _write_env(repo_env, "")
    user_env = tmp_path / "missing" / ".env"

    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    with pytest.raises(RuntimeError):
        load_api_key()


def test_whitespace_only_value_treated_as_empty(tmp_path, monkeypatch):
    """Case 9: Whitespace-only value strips to empty → same fallthrough as Case 6."""
    repo_env = tmp_path / ".env"
    _write_env(repo_env, "   ")

    user_env = tmp_path / "user" / ".env"
    _write_env(user_env, "user-ws-key")

    monkeypatch.setattr(config_mod, "_ENV_SEARCH_PATHS", [repo_env, user_env])

    assert load_api_key() == "user-ws-key"
