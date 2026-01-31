---
name: resume
description: Load context from permanent memory and recent session logs. Tiered loading — CLAUDE.md full, session logs frontmatter-only by default.
---

# /resume — Load Context

Load persistent context from the Obsidian vault.

## Steps

1. **Read permanent memory (full — it's compact):**
   ```bash
   cat /workspace/extra/obsidian/Deus/CLAUDE.md
   ```

2. **Find recent session logs** (default: last 3, or use number given as argument):
   ```bash
   ls -t /workspace/extra/obsidian/Deus/Session-Logs/*.md 2>/dev/null | head -${args:-3}
   ```

3. **For each session log: read frontmatter only** (the YAML block between the two `---` lines):
   ```bash
   awk '/^---/{n++} n==2{exit} {print}' <file>
   ```
   This gives ~10 lines per log (~80 tokens) instead of the full file (~900 tokens).
   **Only read the full log** if the user asks for details or context is unclear.

4. **If a search term was given** (e.g. `/resume auth`), grep logs and read frontmatter of matches:
   ```bash
   grep -ril "<term>" /workspace/extra/obsidian/Deus/Session-Logs/ 2>/dev/null
   ```

5. **Summarize in 2–3 lines:** ongoing context, pending tasks, ready to continue.

## Argument Variants

- `/resume` — CLAUDE.md + last 3 session frontmatters
- `/resume 5` — CLAUDE.md + last 5 session frontmatters
- `/resume auth` — + search logs for "auth", read frontmatters of matches
- `/resume full` — load last 3 session logs in full (use sparingly)
