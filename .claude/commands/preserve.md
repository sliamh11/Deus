Scan this conversation and silently save anything worth permanent memory.

First, resolve the vault path by reading `~/.config/deus/config.json` and using the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

Save findings to: $VAULT/CLAUDE.md

Look for:
- Preferences or habits the user revealed
- Decisions made with lasting effect
- Things the user corrected or clarified
- Facts worth knowing in future sessions

Do not preserve one-off requests or temporary context.

Add findings using the same compact key:value format as the file — no prose bullets.
One line per insight.

If CLAUDE.md exceeds 200 lines, archive old content to:
$VAULT/CLAUDE-Archive.md

Confirm briefly what was added, or say nothing was worth preserving if nothing qualified.
