from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_zsh_backend_prefixes_set_cli_and_runtime_backend():
    script = (ROOT / "deus-cmd.sh").read_text()

    assert 'export DEUS_CLI_AGENT="claude"' in script
    assert 'export DEUS_AGENT_BACKEND="claude"' in script
    assert 'export DEUS_CLI_AGENT="codex"' in script
    assert 'export DEUS_AGENT_BACKEND="openai"' in script


def test_powershell_backend_prefixes_set_cli_and_runtime_backend():
    script = (ROOT / "deus-cmd.ps1").read_text()

    assert '$env:DEUS_CLI_AGENT = "claude"' in script
    assert '$env:DEUS_AGENT_BACKEND = "claude"' in script
    assert '$env:DEUS_CLI_AGENT = "codex"' in script
    assert '$env:DEUS_AGENT_BACKEND = "openai"' in script
