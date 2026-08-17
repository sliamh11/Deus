from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_deus_connect_rejects_user_supplied_settings_flag():
    """A user-supplied --settings in deus connect's trailing args would
    silently override the connector's own DEUS_CONNECT_SETTINGS_JSON
    injection (passthrough args are appended AFTER the injected ones) --
    must be rejected the same way --agents/--name/-n already are.

    Static-source assertion (the file is sourced into a running zsh, so this
    suite verifies the wiring is present, not the runtime branching — that is
    covered by the manual smoke test documented in the PR)."""
    script = (ROOT / "deus-cmd.sh").read_text()

    assert "--agents|--agents=*|--name|--name=*|-n|--settings|--settings=*)" in script


def test_launch_connect_forwards_settings_json_and_unsets_it():
    script = (ROOT / "deus-cmd.sh").read_text()

    # Conditional append -- mirrors launch_codex()'s codex_args pattern, so
    # a connector that never sets DEUS_CONNECT_SETTINGS_JSON (e.g. ollama)
    # never gets a bare `--settings ""` appended.
    assert "settings_args=()" in script
    assert 'if [ -n "$DEUS_CONNECT_SETTINGS_JSON" ]; then' in script
    assert 'settings_args=(--settings "$DEUS_CONNECT_SETTINGS_JSON")' in script
    # Unset immediately after reading -- must not leak into a nested
    # "deus connect <other-id>" call, same rationale as DEUS_CONNECT_ID.
    assert "unset DEUS_CONNECT_SETTINGS_JSON" in script
    # The final launch_claude call actually forwards the built array.
    assert (
        'launch_claude "$@" --agents "$agents_json" '
        '--name "connect:$id (non-Claude)" "${settings_args[@]}" '
        '"${DEUS_CONNECT_ARGS[@]}"' in script
    )


def test_nested_connect_clears_max_context_tokens():
    # CLAUDE_CODE_MAX_CONTEXT_TOKENS is a connector-scoped override (e.g.
    # cliproxy-oauth's 272000 for GPT-5.6's real Codex-OAuth context
    # window), exported into the launcher shell via `eval "$env_output"`.
    # Without clearing it, a nested `deus connect <other-id>` call from
    # inside a launched session would inherit the outer connector's value
    # and apply the wrong threshold to an unrelated connector's model.
    script = (ROOT / "deus-cmd.sh").read_text()

    assert (
        "for _dc_clear_var in ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK "
        "CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY CLAUDE_CODE_USE_MANTLE "
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS; do" in script
    )
