#!/usr/bin/env python3
"""connectors_cli.py — bash/Python bridge for `deus connect` (deus-cmd.sh).

Subcommands:
  list                  enumerate registered connectors + configured status
  status <id>            engine health + functional probe for one connector
  is-configured <id>      is_configured() only, no health probe -- for a
                           setup-time gate (e.g. `deus connect default <id>`)
                           that shouldn't require the daemon to be up right now
  env <id>                print 'export KEY=<shlex-quoted value>' lines for
                           deus-cmd.sh to `eval`; exits non-zero with no
                           stdout on an unknown/unconfigured id
  agents-json <id>        print agents_for_launch()'s dict as one-line JSON

  Setup orchestration, used by the add-connector skill (not deus-cmd.sh):
  install-check <id>      prints "installed"/"not installed" per setup_handler.install()
  authenticate <id>        runs setup_handler.authenticate() interactively
  write-config <id>        reads a JSON values object from stdin, calls
                            setup_handler.write_config(values)
  verify-setup <id>        runs setup_handler.verify() (distinct from `status`,
                            which also requires is_configured() first)

Invoked by deus-cmd.sh via an absolute path from whatever directory the
caller happens to be in (`deus connect` must work from anywhere) — so
sys.path is fixed up before importing the sibling `connectors` package,
mirroring scripts/code_search.py:35-40's existing convention.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    # connectors.providers.cliproxy_oauth does `import yaml` at module load
    # (CLIProxyAPI's own config format is YAML) -- PyYAML is not a repo-wide
    # dependency (this repo has no root requirements.txt; host-side Python
    # deps are installed on demand per feature, matching e.g. the setup
    # skill's `pip install mcp sqlite_vec` pattern for code-search), so a
    # fresh install without it must fail with an actionable message here,
    # not a raw traceback the first time any `deus connect` subcommand runs.
    import yaml  # noqa: F401
except ModuleNotFoundError:
    print(
        "Error: deus connect requires PyYAML (used for the CLIProxyAPI "
        "engine's config file format). Install it: pip3 install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

# Importing `connectors` (below) already runs connectors/__init__.py, which
# imports `providers` as a side effect to register the built-ins -- no
# separate explicit import needed here.
from connectors.registry import ConnectorRegistry, UnknownConnectorError  # noqa: E402

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _registry() -> ConnectorRegistry:
    return ConnectorRegistry.default()


def cmd_list(_args: argparse.Namespace) -> int:
    for connector in _registry().list_connectors():
        status = "configured" if connector.is_configured() else "not configured"
        print(
            f"{connector.id}\t{connector.engine}\t{connector.risk_level}\t"
            f"{status}\t{connector.description}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not connector.is_configured():
        print("not configured")
        return 1
    healthy = connector.setup_handler.verify()
    print("healthy" if healthy else "unhealthy")
    return 0 if healthy else 1


def cmd_is_configured(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    configured = connector.is_configured()
    print("configured" if configured else "not configured")
    return 0 if configured else 1


def cmd_env(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError:
        return 1
    if not connector.is_configured():
        return 1
    env = connector.env_for_launch()
    if not env:
        return 1
    # env_for_launch() is an abstract contract any future connector
    # implements -- deus-cmd.sh `eval`s this output, so an unvalidated key
    # (unlike `value`, which is shlex-quoted) would be arbitrary shell
    # injection the moment a connector's dict contains one.
    for key, value in env.items():
        if not _ENV_KEY_RE.match(key):
            print(f"Error: connector produced an invalid env var name: {key!r}", file=sys.stderr)
            return 1
    for key, value in env.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


def cmd_agents_json(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError:
        return 1
    if not connector.is_configured():
        return 1
    print(json.dumps(connector.agents_for_launch()))
    return 0


def cmd_install_check(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    installed = connector.setup_handler.install()
    print("installed" if installed else "not installed")
    return 0 if installed else 1


def cmd_authenticate(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if connector.setup_handler.authenticate() else 1


def cmd_write_config(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        values = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 1
    connector.setup_handler.write_config(values)
    return 0


def cmd_verify_setup(args: argparse.Namespace) -> int:
    try:
        connector = _registry().resolve(args.id)
    except UnknownConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    healthy = connector.setup_handler.verify()
    print("healthy" if healthy else "unhealthy")
    return 0 if healthy else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="connectors_cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p_status = sub.add_parser("status")
    p_status.add_argument("id")
    p_status.set_defaults(func=cmd_status)

    p_is_configured = sub.add_parser("is-configured")
    p_is_configured.add_argument("id")
    p_is_configured.set_defaults(func=cmd_is_configured)

    p_env = sub.add_parser("env")
    p_env.add_argument("id")
    p_env.set_defaults(func=cmd_env)

    p_agents = sub.add_parser("agents-json")
    p_agents.add_argument("id")
    p_agents.set_defaults(func=cmd_agents_json)

    p_install = sub.add_parser("install-check")
    p_install.add_argument("id")
    p_install.set_defaults(func=cmd_install_check)

    p_auth = sub.add_parser("authenticate")
    p_auth.add_argument("id")
    p_auth.set_defaults(func=cmd_authenticate)

    p_write = sub.add_parser("write-config")
    p_write.add_argument("id")
    p_write.set_defaults(func=cmd_write_config)

    p_verify = sub.add_parser("verify-setup")
    p_verify.add_argument("id")
    p_verify.set_defaults(func=cmd_verify_setup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
