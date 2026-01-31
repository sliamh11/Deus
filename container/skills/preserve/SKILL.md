---
name: preserve
description: Scan the current conversation and silently save anything worth permanent memory to CLAUDE.md. Keeps the file compact — structured key:value, no prose.
---

# /preserve — Update Permanent Memory

Silently extract and save lasting knowledge from this conversation to `/workspace/extra/obsidian/Deus/CLAUDE.md`.

## Steps

1. **Read the current CLAUDE.md:**
   ```bash
   cat /workspace/extra/obsidian/Deus/CLAUDE.md
   wc -l /workspace/extra/obsidian/Deus/CLAUDE.md
   ```

2. **Review the conversation** — identify what's genuinely worth permanent memory:
   - User preferences or habits revealed
   - Facts about the user (schedule, people, places, situations)
   - Decisions with lasting effect
   - Things the user corrected or clarified
   - Recurring patterns worth anticipating

   **Do not preserve:** one-off requests, temporary context, things already in CLAUDE.md.

3. **Add findings** to the appropriate section using the same compact format as the file:
   - Use `key: value` shorthand, not prose bullets
   - One line per insight
   - Write as data, not narrative

4. **Auto-archive if file exceeds 200 lines:**
   - Move completed projects and old decisions to `/workspace/extra/obsidian/Deus/CLAUDE-Archive.md`
   - Keep core blocks: identity line, tools, projects, pending, decisions

5. **Confirm briefly** what was added, or "Nothing new worth preserving." if nothing qualified.
