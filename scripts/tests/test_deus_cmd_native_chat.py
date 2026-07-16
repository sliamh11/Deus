"""Structural launcher tests for `deus chat` (LIA-428 / G1).

Static-source assertions in the style of test_deus_cmd_backend_prefix.py:
the launcher scripts are sourced into live shells, so this suite proves the
wiring is present (the chat arm reaches the compiled client and the existing
bare/prefixed branches are untouched) without invoking the live daemon.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_zsh_chat_arm_reaches_the_compiled_client():
    script = (ROOT / "deus-cmd.sh").read_text()

    # The dispatch arm exists and executes the compiled client.
    assert "chat)" in script
    assert 'exec node "$SCRIPT_DIR/dist/cli/deus-native-chat-client.js"' in script
    # No `cd` before the exec: the client must keep the user's original cwd.
    # (Assert on executable lines only — the arm's comments explain the rule.)
    chat_arm = script.split("chat)", 1)[1].split(";;", 1)[0]
    chat_arm_code = "\n".join(
        line
        for line in chat_arm.splitlines()
        if not line.strip().startswith("#")
    )
    assert "cd " not in chat_arm_code
    assert "deus-native-chat-client.js" in chat_arm_code
    # Help mentions the new command.
    assert "deus chat" in script


def test_powershell_chat_arm_reaches_the_compiled_client():
    script = (ROOT / "deus-cmd.ps1").read_text()

    assert '"chat" {' in script
    assert "dist\\cli\\deus-native-chat-client.js" in script
    # No Set-Location in the chat arm: original cwd is part of the contract.
    # (Assert on executable lines only — the arm's comments explain the rule.)
    chat_arm = script.split('"chat" {', 1)[1].split("}", 1)[0]
    chat_arm_code = "\n".join(
        line
        for line in chat_arm.splitlines()
        if not line.strip().startswith("#")
    )
    assert "Set-Location" not in chat_arm_code
    assert "deus chat" in script


def test_existing_bare_and_prefixed_branches_are_unchanged():
    """`deus`, `deus claude`, `deus codex`, and `deus fcc` keep their meanings."""
    zsh = (ROOT / "deus-cmd.sh").read_text()
    ps1 = (ROOT / "deus-cmd.ps1").read_text()

    # zsh prefix handling still assigns both CLI agent and runtime backend.
    assert 'export DEUS_CLI_AGENT="claude"' in zsh
    assert 'export DEUS_AGENT_BACKEND="claude"' in zsh
    assert 'export DEUS_CLI_AGENT="codex"' in zsh
    assert 'export DEUS_AGENT_BACKEND="openai"' in zsh
    assert 'export DEUS_CLI_AGENT="fcc"' in zsh

    # PowerShell prefix handling unchanged.
    assert '$env:DEUS_CLI_AGENT = "claude"' in ps1
    assert '$env:DEUS_AGENT_BACKEND = "claude"' in ps1
    assert '$env:DEUS_CLI_AGENT = "codex"' in ps1
    assert '$env:DEUS_AGENT_BACKEND = "openai"' in ps1

    # `chat` must NOT touch the backend-selection environment variables.
    zsh_chat_arm = zsh.split("chat)", 1)[1].split(";;", 1)[0]
    assert "DEUS_CLI_AGENT" not in zsh_chat_arm
    assert "DEUS_AGENT_BACKEND" not in zsh_chat_arm
