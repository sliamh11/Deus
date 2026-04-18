---
name: compress
description: Save the current session to the vault. Writes a compact session log with a TL;DR frontmatter (always) and full details below (when useful).
---

# /compress — Save Session

Save a summary of this conversation to the vault.

The vault is mounted at `/workspace/vault/`. If it doesn't exist, check `/workspace/extra/obsidian/Deus/` as a legacy fallback.

## Steps

1. **Resolve vault path:**
   ```bash
   VAULT_DIR="${DEUS_VAULT_DIR:-/workspace/vault}"
   [ ! -d "$VAULT_DIR" ] && VAULT_DIR="/workspace/extra/obsidian/Deus"
   ```

2. **Reflect on the conversation** — identify what's worth saving:
   - Decisions made, key learnings, files modified, pending tasks, errors/workarounds

3. **Determine a short topic slug** from the main theme (e.g. `google-calendar-setup`, `ui-debugging`).

4. **Write the session log:**
   ```
   $VAULT_DIR/Session-Logs/YYYY-MM-DD-{topic}.md
   ```

   Use this format — the `tldr` frontmatter field is **mandatory**; full sections are optional:
   ```markdown
   ---
   type: session
   date: YYYY-MM-DD
   topics: [topic1, topic2]
   tldr: |
     What happened (1 sentence). Key decision or outcome. Pending: X, Y.
   ---

   <!-- Full details — only loaded on demand -->

   ## Decisions Made
   - ...

   ## Key Learnings
   - ...

   ## Files Modified
   - ...

   ## Pending Tasks
   - [ ] ...

   ## Errors & Workarounds
   - ...
   ```

   Keep `tldr` to 2–3 lines max — this is what gets loaded every session.
   Skip any full section that has no content.

5. **Update pending tasks in the state file** if there are carry-forward items:
   - Prefer `$VAULT_DIR/STATE.md` (slim structure); fall back to `$VAULT_DIR/CLAUDE.md` (legacy monolithic).
   - Update the `pending:` block
   - Write it back

5. **Confirm:** "Session saved to `{filename}`. {N} pending tasks carried forward."
