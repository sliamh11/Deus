---
governs:
  - CLAUDE.md
  - groups/global/CLAUDE.md.template
  - groups/main/CLAUDE.md.template
last_verified: "2026-04-25"
test_tasks:
  - "Compress the root CLAUDE.md to remove a redundant section"
  - "Add a new rule to groups/main/CLAUDE.md.template"
  - "Update the global container template to mention a new shared command"
---
# Pattern: claude-md-edit

CLAUDE.md files (the project root + the two `groups/*/CLAUDE.md.template` container templates) auto-load on every session. Bytes here are the most expensive bytes in the repo — they cost tokens on every turn for every user. They are also the most fragile: removing or paraphrasing a rule can silently change agent behavior.

This pattern protects against drift in both directions: bloat that wastes tokens, and slimming that drops a load-bearing rule.

## Before editing

- Each gated file has a curated facts file under `scripts/token_bench/facts/` listing the rules it must convey.
  - `CLAUDE.md` ↔ `scripts/token_bench/facts/root_claudemd.txt`
  - `groups/global/CLAUDE.md.template` ↔ `scripts/token_bench/facts/global_template.txt`
  - `groups/main/CLAUDE.md.template` ↔ `scripts/token_bench/facts/main_template.txt`
- Read the facts file first. If your edit *changes* what the file should convey (adds a new permanent rule, removes an obsolete one), update the facts file in the same PR.

## When adding content

- Prefer placing the rule in a more specific home — a pattern file, a skill's `SKILL.md`, or `docs/CONTRIBUTING-AI.md` — and link from CLAUDE.md only if it must apply to every session.
- Three similar rules can stay as three lines; resist abstracting them into a meta-rule that's easier to misread.

## When removing or paraphrasing content

- Keep the **signal words** of each fact: proper nouns, paths, slash commands, MCP tool names, file extensions. The keyword bench matches on these.
- After your edit, run the gate locally:

  ```bash
  scripts/token_bench/ci_coverage_gate.sh
  ```

  This runs `keyword_bench.py` on each gated file changed in your branch and fails if critical-fact coverage drops below 90%.
- Audit every `MISS`. If the fact survives in paraphrase, add a `# kw=token1,token2` override to the facts file pointing at the new wording. If the fact was dropped intentionally, remove it from the facts file with a note in the PR description.

## CI enforcement

The `CLAUDE.md keyword coverage` step in `.github/workflows/ci.yml` runs the same gate on every PR that touches any gated file or its facts file. PRs below the 90% floor will fail.

The gate is conservative — keyword matches can false-negative on heavy paraphrase. That's intentional: a `MISS` is a prompt to look at the diff, not an automatic verdict.

## What this pattern does **not** cover

- The live `groups/<channel>_main/CLAUDE.md` files (e.g. `groups/whatsapp_main/CLAUDE.md`). These contain user-specific persona/tool wiring and are not gated.
- The semantic correctness of remaining rules. The keyword gate checks fact preservation; behavioral verification still requires `scripts/token_bench/real_claude_probe.sh` (manual, not CI).
