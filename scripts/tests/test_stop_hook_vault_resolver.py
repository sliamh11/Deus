"""Tests for stop_hook._load_vault_root's 3-tier resolution (extended for the
SessionEnd auto-save feature): DEUS_VAULT_PATH env -> <cwd>/.deus/config.json
(per-instance override, STOP on partial file) -> ~/.config/deus/config.json
(global fallback). Matches .claude/skills/compress/skill.md's own contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


stop_hook = _load("stop_hook", _ROOT / "scripts" / "stop_hook.py")


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("DEUS_VAULT_PATH", str(tmp_path / "env-vault"))
    instance_cfg = tmp_path / ".deus" / "config.json"
    instance_cfg.parent.mkdir()
    instance_cfg.write_text(json.dumps({"vault_path": str(tmp_path / "instance-vault")}))
    assert stop_hook._load_vault_root(cwd=tmp_path) == tmp_path / "env-vault"


def test_per_instance_config_used_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    instance_cfg = tmp_path / ".deus" / "config.json"
    instance_cfg.parent.mkdir()
    instance_cfg.write_text(json.dumps({"vault_path": str(tmp_path / "instance-vault")}))
    assert stop_hook._load_vault_root(cwd=tmp_path) == tmp_path / "instance-vault"


def test_per_instance_config_missing_vault_path_stops_no_fallthrough(tmp_path, monkeypatch, home_config):
    """The critical anti-corruption rule: a present-but-broken per-instance
    file must STOP, never silently fall through to the global config."""
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    instance_cfg = tmp_path / ".deus" / "config.json"
    instance_cfg.parent.mkdir()
    instance_cfg.write_text(json.dumps({"other_key": "no vault_path here"}))
    assert stop_hook._load_vault_root(cwd=tmp_path) is None


def test_per_instance_config_malformed_json_stops_no_fallthrough(tmp_path, monkeypatch, home_config):
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    instance_cfg = tmp_path / ".deus" / "config.json"
    instance_cfg.parent.mkdir()
    instance_cfg.write_text("{ not valid json")
    assert stop_hook._load_vault_root(cwd=tmp_path) is None


def test_falls_back_to_global_config_when_no_instance_file(tmp_path, monkeypatch, home_config):
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    assert stop_hook._load_vault_root(cwd=tmp_path) == home_config


def test_cwd_none_preserves_prior_2tier_behavior(tmp_path, monkeypatch, home_config):
    """Backward-compat: existing call sites (cwd=None) must be unaffected."""
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    assert stop_hook._load_vault_root() == home_config


@pytest.fixture
def home_config(tmp_path, monkeypatch):
    """Redirect the global-config tier to a tmp path so tests never read the
    real ~/.config/deus/config.json."""
    fake_home_cfg = tmp_path / "global-config.json"
    fake_vault = tmp_path / "global-vault"
    fake_home_cfg.write_text(json.dumps({"vault_path": str(fake_vault)}))

    real_expanduser = Path.expanduser

    def _patched_expanduser(self):
        if str(self) == "~/.config/deus/config.json":
            return fake_home_cfg
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", _patched_expanduser)
    return fake_vault
