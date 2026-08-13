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


class _ConfigurableFakeConnector(Connector):
    """A connector whose is_configured() is controlled per-test."""

    def __init__(self, connector_id: str, configured: bool):
        self._id = connector_id
        self._configured = configured

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
        return {}

    def is_configured(self) -> bool:
        return self._configured

    def env_for_launch(self) -> dict[str, str]:
        return {}

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


class TestArgparseDashArgument:
    """Regression coverage for a real bug a verification-gate pass caught
    and live-reproduced: `deus connect default <id>` (deus-cmd.sh) passes a
    user-typed, unvalidated string straight through to connectors_cli.py's
    argv. Without a `--` separator before it, a leading-dash id like
    "--help" is consumed by argparse's own built-in -h/--help handling
    (prints help, exits 0) INSTEAD of reaching the subcommand's actual
    logic -- silently bypassing the is-configured validation gate the
    `deus connect default <id>` flow relies on to refuse bad input, and
    letting "--help" get persisted into default_connect as if it were a
    real connector id. deus-cmd.sh's fix is to always pass `--` before the
    id; this test confirms main()'s real argv-parsing path (not the
    Namespace-construction shortcut the other tests in this file use)
    actually respects that separator the way the shell-side fix assumes.
    """

    def test_dashed_id_without_separator_is_swallowed_by_argparse_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            connectors_cli.main(["is-configured", "--help"])
        captured = capsys.readouterr()
        assert exc_info.value.code == 0  # argparse's own help exit, not our code
        assert "usage:" in captured.out.lower()

    def test_dashed_id_with_separator_reaches_real_logic(self, capsys):
        rc = connectors_cli.main(["is-configured", "--", "--help"])
        captured = capsys.readouterr()
        assert rc == 1  # correctly treated as an unknown connector id
        assert "Unknown connector" in captured.err
        assert "--help" in captured.err


class TestIsConfigured:
    """`is-configured` is the gate `deus connect default <id>` uses at
    set-time -- distinct from `status`, whose exit code conflates
    "not configured" with "configured but unhealthy" (both print
    different text but return the same exit 1), which would make a
    setup-time default-connector check wrongly refuse a connector that's
    merely transiently unhealthy (e.g. its daemon isn't running right
    now) rather than genuinely unconfigured.
    """

    def test_configured_connector_exits_zero(self, capsys):
        reg = ConnectorRegistry.default()
        reg.register(_ConfigurableFakeConnector("good", configured=True))
        rc = connectors_cli.cmd_is_configured(argparse.Namespace(id="good"))
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "configured"

    def test_unconfigured_connector_exits_one(self, capsys):
        reg = ConnectorRegistry.default()
        reg.register(_ConfigurableFakeConnector("bad", configured=False))
        rc = connectors_cli.cmd_is_configured(argparse.Namespace(id="bad"))
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out.strip() == "not configured"

    def test_unknown_connector_exits_one(self, capsys):
        ConnectorRegistry.default()  # empty registry
        rc = connectors_cli.cmd_is_configured(argparse.Namespace(id="nonexistent"))
        captured = capsys.readouterr()
        assert rc == 1
        assert "Unknown connector" in captured.err
