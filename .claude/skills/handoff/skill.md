---
name: handoff
description: Write a handoff document summarising the current conversation so a fresh agent can continue the work.
user_invocable: true
---

# /handoff — Write Handoff Document

Write a forward-facing handoff document so the next agent can start with context instead of archaeology.

This skill runs **host-side** (like `/compress` and `/resume`), so it resolves the vault the same way they do.

## Steps

1. **Resolve vault path** — read `vault_path` from `~/.config/deus/config.json`; if the env var `DEUS_VAULT_PATH` is set, it overrides. `$VAULT` means this resolved path. Fail loudly if neither is set — never write to a guessed path:
   ```bash
   VAULT="${DEUS_VAULT_PATH:-$(python3 -c "import json,os;print(os.path.expanduser(json.load(open(os.path.expanduser('~/.config/deus/config.json'))).get('vault_path','')))" 2>/dev/null)}"
   VAULT="${VAULT/#\~/$HOME}"  # expand a leading ~ (e.g. from DEUS_VAULT_PATH); expanduser already handled the config value
   [ -z "$VAULT" ] && { echo "handoff: cannot resolve vault path — set DEUS_VAULT_PATH or config.json vault_path"; exit 1; }
   ```

2. **Derive topic from user args** — the text after `/handoff` becomes the topic slug. If no args, infer from the main conversation theme.
   ```bash
   # Example: /handoff fix the auth bug → TOPIC="fix-the-auth-bug"
   # The sed allowlist guarantees TOPIC is [a-z0-9-] only (no shell metacharacters);
   # the fallback covers all-symbol args that would otherwise collapse to empty.
   TOPIC=$(echo "${args:-handoff}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | sed 's/^-\|-$//g')
   TOPIC="${TOPIC:-handoff}"
   ```

3. **Set output path and create directory:**
   ```bash
   HANDOFF_FILE="$VAULT/Handoffs/$(date +%Y-%m-%d-%H-%M)-${TOPIC}.md"
   mkdir -p "$(dirname "$HANDOFF_FILE")"
   ```

4. **Gather memory citations** for the topic — run semantic search if `memory_tree.py` is available, otherwise grep the vault:
   ```bash
   # Preferred
   python3 ~/deus/scripts/memory_tree.py query "$TOPIC" 2>/dev/null
   # Fallback
   grep -ril "$TOPIC" "$VAULT" 2>/dev/null | head -5
   ```

5. **Reflect on the conversation** — identify what matters for the next agent:
   - What was accomplished this session
   - What the next action is and why
   - Which skills to invoke first
   - Open Linear issues relevant to the topic
   - Files or paths the next agent will need
   - References (session logs, PRs, commits, ADRs) — link by path/URL, do not re-summarize

6. **Write the handoff document** with these sections in order:

   ```markdown
   ---
   type: handoff
   date: YYYY-MM-DD HH:MM
   topic: <topic>
   tldr: |
     <What was done this session, 1 sentence.> Next: <first action for incoming agent>.
   ---

   ## Already Done (don't redo)

   What shipped this session, with the key symbol/function/file names so the next
   agent doesn't re-derive it. Include the merged PR/commit anchor (so they can
   `git show` it). Reference logs by path, don't re-state:
   `$VAULT/Session-Logs/YYYY-MM-DD/<topic>.md`

   ## Read Before Any Work

   The load-bearing gotcha(s) that would burn time if missed — environment quirks,
   things that look done but aren't, wrong assumptions to avoid. This is the
   highest-value section; omit it only if there genuinely are none.

   ## Forward Brief

   <!-- Shaped by the user's args (e.g. /handoff implement dark mode → dark-mode next steps) -->

   What the incoming agent should do first, in **priority order** (prerequisites
   first). Each item: Linear issue ID + concrete file:line seam + the *why*.

   ## Suggested Skills

   Skills the incoming agent should run at session start, in order:

   1. `/resume` — load CLAUDE.md and last 3 session logs
   2. <!-- add topic-specific skills, e.g. /debug, /deep-research, etc. -->

   ## Memory Citations

   Vault paths surfaced by semantic search for this topic:

   - <!-- $VAULT/path/to/relevant/leaf.md -->

   If no results, note: "No relevant vault nodes found for `<topic>`."

   ## References

   Existing artifacts — link, don't re-state:

   - Session log: `$VAULT/Session-Logs/YYYY-MM-DD/<topic>.md`
   - PR: <!-- https://github.com/... -->
   - Commit: <!-- abc1234 -->
   - ADR / Linear: <!-- docs/decisions/... | LIA-XXX -->

   ## Quick-start line

   One copy-paste prompt the user can drop into the new session to resume immediately:

   > "Resume <topic> from `<this handoff path>`. <first concrete action + any precondition to confirm>."

   ---
   *Handoff written: YYYY-MM-DD HH:MM*
   ```

7. **Redact secrets** before writing — scan the draft for:
   - API keys and tokens (patterns: `sk-`, `Bearer `, `ghp_`, `AKIA`, hex strings >32 chars)
   - Environment variable values that look like credentials
   - PII (email addresses, phone numbers)
   Replace all matches with `[REDACTED]`.

8. **Confirm:** Print `Handoff saved to: {HANDOFF_FILE}` on completion.

## Argument Variants

- `/handoff` — topic inferred from conversation theme
- `/handoff fix the auth bug` — Forward Brief focused on fixing the auth bug
- `/handoff implement dark mode` — Forward Brief focused on dark mode implementation

## Notes

- Do not re-summarize content already in session logs, PRs, or ADRs — reference by path/URL.
- Keep `tldr` to 2-3 lines — this is what gets scanned by the incoming agent first.
- The `Handoffs/` directory is created with `mkdir -p` if absent — no manual setup needed.
