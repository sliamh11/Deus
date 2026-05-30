---
name: handoff
description: Write a handoff document summarising the current conversation so a fresh agent can continue the work.
user_invocable: true
---

# /handoff — Write Handoff Document

Write a forward-facing handoff document so the next agent can start with context instead of archaeology.

The vault is mounted at `/workspace/vault/`. If it doesn't exist, check `/workspace/extra/obsidian/Deus/` as a legacy fallback.

## Steps

1. **Resolve vault path:**
   ```bash
   VAULT_DIR="${DEUS_VAULT_DIR:-/workspace/vault}"
   [ ! -d "$VAULT_DIR" ] && VAULT_DIR="/workspace/extra/obsidian/Deus"
   ```

2. **Derive topic from user args** — the text after `/handoff` becomes the topic slug. If no args, infer from the main conversation theme.
   ```bash
   # Example: /handoff fix the auth bug → TOPIC="fix-the-auth-bug"
   TOPIC=$(echo "${args:-handoff}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | sed 's/^-\|-$//g')
   ```

3. **Set output path and create directory:**
   ```bash
   HANDOFF_FILE="$VAULT_DIR/shared/Handoffs/$(date +%Y-%m-%d-%H-%M)-${TOPIC}.md"
   mkdir -p "$(dirname "$HANDOFF_FILE")"
   ```

4. **Gather memory citations** for the topic — run semantic search if `memory_tree.py` is available, otherwise grep the vault:
   ```bash
   # Preferred
   python3 ~/deus/scripts/memory_tree.py query "$TOPIC" --top 3 2>/dev/null
   # Fallback
   grep -ril "$TOPIC" "$VAULT_DIR" 2>/dev/null | head -5
   ```

5. **Reflect on the conversation** — identify what matters for the next agent:
   - What was accomplished this session
   - What the next action is and why
   - Which skills to invoke first
   - Open Linear issues relevant to the topic
   - Files or paths the next agent will need
   - References (session logs, PRs, commits, ADRs) — link by path/URL, do not re-summarize

6. **Write the handoff document** with all 7 sections in order:

   ```markdown
   ---
   type: handoff
   date: YYYY-MM-DD HH:MM
   topic: <topic>
   tldr: |
     <What was done this session, 1 sentence.> Next: <first action for incoming agent>.
   ---

   ## Summary

   What happened this session — decisions made, problems solved, state of the system now.
   Reference existing logs by path rather than re-stating their content:
   `$VAULT_DIR/Session-Logs/YYYY-MM-DD-<topic>.md`

   ## Forward Brief

   <!-- This section is shaped by the user's args (e.g. /handoff implement dark mode → focus on dark mode next steps) -->

   What the incoming agent should do first. Be specific: file paths, function names, Linear issue IDs.
   Include the *why* — what context would take time to re-derive from scratch.

   ## Suggested Skills

   Skills the incoming agent should run at session start, in order:

   1. `/resume` — load CLAUDE.md and last 3 session logs
   2. <!-- add topic-specific skills, e.g. /get-qodo-rules, /debug, etc. -->

   ## Memory Citations

   Vault paths surfaced by semantic search for this topic:

   - <!-- $VAULT_DIR/path/to/relevant/leaf.md -->

   If no results, note: "No relevant vault nodes found for `<topic>`."

   ## Open Linear Issues

   Linear issues relevant to this handoff (from CLAUDE.md `pending:` block or MCP query):

   - [ ] <!-- LIA-XXX: title -->

   ## References

   Existing artifacts — link, don't re-state:

   - Session log: `$VAULT_DIR/Session-Logs/YYYY-MM-DD-<topic>.md`
   - PR: <!-- https://github.com/... -->
   - Commit: <!-- abc1234 -->
   - ADR: <!-- docs/decisions/... -->

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
