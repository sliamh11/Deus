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


def test_deus_sync_supports_origin_and_upstream_targets():
    """`deus sync` keeps origin behavior and adds an `upstream` target.

    Static-source assertions (the file is sourced into a running zsh, so this
    suite verifies the wiring is present, not the runtime branching — that is
    covered by the manual smoke test documented in the PR)."""
    script = (ROOT / "deus-cmd.sh").read_text()

    # The sync arm parameterizes on a remote and recognizes both targets.
    assert '""|origin) sync_remote="origin"' in script
    assert 'upstream)  sync_remote="upstream"' in script
    # Unknown targets are rejected, not silently treated as origin.
    assert "deus sync: unknown target" in script
    # Remote existence is checked before fetch, with an upstream add hint.
    assert 'git -C "$sync_repo" remote get-url "$sync_remote"' in script
    assert "remote add upstream https://github.com/sliamh11/Deus.git" in script
    # Fetch/merge are parameterized and stay non-destructive (ff-only).
    assert 'git -C "$sync_repo" fetch "$sync_remote" main' in script
    assert 'git -C "$sync_repo" merge --ff-only "$sync_remote/main"' in script
    # Fork-specific divergence guidance on ff failure.
    assert "Your main has commits not in upstream" in script
