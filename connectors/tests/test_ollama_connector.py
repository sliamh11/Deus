"""Tests for the OllamaConnector's pure/mockable surface.

verify()'s live HTTP calls are integration-only (matches cliproxy_oauth's
own verify(), also untested at the unit level) -- not unit-testable
without a running Ollama instance, EXCEPT the version-gate at the top,
which is cheap to exercise with a mocked /api/version response and worth
it since that gate is exactly what round-2 code-review's MINOR finding was
about.
"""
import json
from unittest.mock import patch

import yaml

from connectors.providers.ollama import OllamaConnector


class _FakeResponse:
    """Minimal stand-in for the context-manager urlopen() returns."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def test_model_aliases_empty_when_no_config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "connectors.providers.ollama.LOCAL_CONFIG", tmp_path / "config.local.yaml"
    )
    connector = OllamaConnector()
    assert connector.model_aliases() == {}
    assert connector.is_configured() is False


def test_model_aliases_empty_when_deus_model_map_missing(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(config_path, {"host": "http://localhost:11434"})
    connector = OllamaConnector()
    assert connector.model_aliases() == {}
    assert connector.is_configured() is False


def test_model_aliases_populated(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "host": "http://localhost:11434",
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
            "default-model-alias": "qwen3:32b",
        },
    )
    connector = OllamaConnector()
    assert connector.model_aliases() == {"deus-ollama-local": "qwen3:32b"}
    assert connector.is_configured() is True


def test_env_for_launch_shape(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "host": "http://localhost:11434",
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
            "default-model-alias": "qwen3:32b",
        },
    )
    connector = OllamaConnector()
    assert connector.env_for_launch() == {
        "ANTHROPIC_BASE_URL": "http://localhost:11434",
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "qwen3:32b",
    }


def test_env_for_launch_defaults_host_when_unset(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
        },
    )
    connector = OllamaConnector()
    env = connector.env_for_launch()
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["ANTHROPIC_MODEL"] == "qwen3:32b"


def test_agents_for_launch_empty_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "connectors.providers.ollama.LOCAL_CONFIG", tmp_path / "config.local.yaml"
    )
    connector = OllamaConnector()
    assert connector.agents_for_launch() == {}


def test_agents_for_launch_single_entry_when_configured(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
            "default-model-alias": "qwen3:32b",
        },
    )
    connector = OllamaConnector()
    agents = connector.agents_for_launch()
    assert set(agents.keys()) == {"deus-ollama-local"}
    entry = agents["deus-ollama-local"]
    assert entry["model"] == "qwen3:32b"
    assert entry["tools"] == ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]
    assert "description" in entry and "prompt" in entry


def test_install_false_when_binary_missing():
    with patch("connectors.providers.ollama.shutil.which", return_value=None):
        connector = OllamaConnector()
        assert connector.setup_handler.install() is False


def test_install_true_when_binary_present(monkeypatch):
    # install() is binary-presence-only (matches cliproxy_oauth's own
    # install()) -- deliberately does NOT probe any host, since at
    # install-check time (Phase 4) a non-default-host user's real host
    # isn't known yet (Phase 5 collects it). Service liveness + version
    # compatibility are checked in verify() instead, once the real
    # configured host exists.
    monkeypatch.setattr(
        "connectors.providers.ollama.shutil.which", lambda _name: "/usr/local/bin/ollama"
    )
    connector = OllamaConnector()
    assert connector.setup_handler.install() is True


def test_parse_version_simple():
    from connectors.providers.ollama import _parse_version

    assert _parse_version("0.31.1") == (0, 31, 1)
    assert _parse_version("0.14.0") == (0, 14, 0)


def test_parse_version_compares_correctly_against_minimum():
    from connectors.providers.ollama import _MIN_OLLAMA_VERSION, _parse_version

    assert _parse_version("0.31.1") >= _MIN_OLLAMA_VERSION
    assert _parse_version("0.14.0") >= _MIN_OLLAMA_VERSION
    assert _parse_version("0.9.0") < _MIN_OLLAMA_VERSION
    assert _parse_version("0.9.5") < _MIN_OLLAMA_VERSION  # not a string-compare trap


def test_parse_version_handles_suffix_and_empty():
    from connectors.providers.ollama import _parse_version

    assert _parse_version("0.14.0-rc1") == (0, 14, 0)
    assert _parse_version("") == (0,)
    assert _parse_version("") < (0, 14, 0)


def test_authenticate_is_a_noop():
    connector = OllamaConnector()
    assert connector.setup_handler.authenticate() is True


def test_write_config_writes_local_config_only(tmp_path, monkeypatch):
    tracked_config = tmp_path / "tracked-config.yaml"
    tracked_config.write_text(
        yaml.safe_dump(
            {
                "host": "http://localhost:11434",
                "deus-model-map": {"deus-ollama-local": "REPLACE_ME"},
                "default-model-alias": "REPLACE_ME",
            }
        )
    )
    local_config = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.TRACKED_CONFIG", tracked_config)
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", local_config)

    connector = OllamaConnector()
    connector.setup_handler.write_config(
        {
            "host": "http://localhost:11434",
            "model_map": {"deus-ollama-local": "qwen3:32b"},
            "default_model_alias": "qwen3:32b",
        }
    )

    written = yaml.safe_load(local_config.read_text())
    assert written["deus-model-map"] == {"deus-ollama-local": "qwen3:32b"}
    assert written["default-model-alias"] == "qwen3:32b"
    assert local_config.stat().st_mode & 0o777 == 0o600


def test_verify_false_when_ollama_version_too_low(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "host": "http://localhost:11434",
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
            "default-model-alias": "qwen3:32b",
        },
    )
    body = json.dumps({"version": "0.9.0"}).encode()

    def _fake_urlopen(*_args, **_kwargs):
        return _FakeResponse(200, body)

    monkeypatch.setattr(
        "connectors.providers.ollama.urllib.request.urlopen", _fake_urlopen
    )
    connector = OllamaConnector()
    assert connector.setup_handler.verify() is False


def test_verify_false_when_version_field_malformed(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.yaml"
    monkeypatch.setattr("connectors.providers.ollama.LOCAL_CONFIG", config_path)
    _write_config(
        config_path,
        {
            "host": "http://localhost:11434",
            "deus-model-map": {"deus-ollama-local": "qwen3:32b"},
            "default-model-alias": "qwen3:32b",
        },
    )
    # A response that's valid JSON but the wrong shape must fail closed,
    # not raise -- this is exactly the crash round-2 code-review's warning
    # caught (a present `null` value passes .get()'s default, and
    # _parse_version(None) would previously raise AttributeError).
    body = json.dumps({"version": None}).encode()

    def _fake_urlopen(*_args, **_kwargs):
        return _FakeResponse(200, body)

    monkeypatch.setattr(
        "connectors.providers.ollama.urllib.request.urlopen", _fake_urlopen
    )
    connector = OllamaConnector()
    assert connector.setup_handler.verify() is False
