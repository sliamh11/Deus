"""
Tests for scripts/connectors_cli.py's `env` subcommand's key-validation
guard -- the highest-risk line in the `deus connect` diff: deus-cmd.sh
`eval`s this command's stdout, so an unvalidated env var *key* (unlike
`value`, which is shlex-quoted) from a future/buggy connector's
env_for_launch() dict would be arbitrary shell injection the moment it ran.
"""
import argparse
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure scripts/ is importable so `import connectors_cli` resolves.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import connectors_cli  # noqa: E402
from connectors.base import Connector, ConnectorSetupHandler  # noqa: E402
from connectors.registry import ConnectorRegistry  # noqa: E402


class _NoopSetupHandler(ConnectorSetupHandler):
    def install(self) -> bool:
        return True

    def authenticate(self) -> bool:
        return True

    def write_config(self, values: dict[str, Any]) -> None:
        pass

    def verify(self) -> bool:
        return True


class _EnvFakeConnector(Connector):
    """A connector whose env_for_launch() is controlled per-test."""

    def __init__(self, connector_id: str, env: dict[str, str]):
        self._id = connector_id
        self._env = env

    @property
    def id(self) -> str:
        return self._id

    @property
    def description(self) -> str:
        return "fake"

    @property
    def engine(self) -> str:
        return "fake"

    @property
    def risk_level(self) -> str:
        return "none"

    @property
    def setup_handler(self) -> ConnectorSetupHandler:
        return _NoopSetupHandler()

    def model_aliases(self) -> dict[str, str]:
        return {"fake-agent": "fake-alias"}

    def is_configured(self) -> bool:
        return True

    def env_for_launch(self) -> dict[str, str]:
        return self._env

    def agents_for_launch(self) -> dict[str, Any]:
        return {}


@pytest.fixture(autouse=True)
def clean_registry():
    ConnectorRegistry.reset()
    yield
    ConnectorRegistry.reset()


def test_env_rejects_malicious_key(capsys):
    reg = ConnectorRegistry.default()
    reg.register(_EnvFakeConnector("evil", {"FOO; rm -rf /": "value"}))
    rc = connectors_cli.cmd_env(argparse.Namespace(id="evil"))
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""  # no export lines leaked before the abort


def test_env_rejects_if_any_key_in_batch_is_invalid(capsys):
    reg = ConnectorRegistry.default()
    reg.register(
        _EnvFakeConnector(
            "evil", {"ANTHROPIC_API_KEY": "ok-value", "$(whoami)": "value"}
        )
    )
    rc = connectors_cli.cmd_env(argparse.Namespace(id="evil"))
    captured = capsys.readouterr()
    assert rc == 1
    # Full-batch abort, not a partial drop of just the bad key.
    assert captured.out == ""


def test_env_accepts_valid_keys(capsys):
    reg = ConnectorRegistry.default()
    reg.register(
        _EnvFakeConnector(
            "good",
            {"ANTHROPIC_BASE_URL": "http://localhost:8317", "ANTHROPIC_API_KEY": "k"},
        )
    )
    rc = connectors_cli.cmd_env(argparse.Namespace(id="good"))
    captured = capsys.readouterr()
    assert rc == 0
    assert "export ANTHROPIC_BASE_URL=" in captured.out
    assert "export ANTHROPIC_API_KEY=" in captured.out
