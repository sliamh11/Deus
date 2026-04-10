# ADR: Pattern Verification System

**Status:** Accepted
**Date:** 2026-04-10
**Scope:** `scripts/drift_check.py`, `patterns/`, `.mex/ROUTER.md`, `docs/decisions/`

## What this is about (in plain English)

Deus has a folder full of **pattern files**. A pattern file is a short cheat-sheet that tells Claude what rules to follow for a certain type of task — like "deploying the service", "adding a channel", or "debugging a silent failure". These cheat-sheets replace loading huge source docs every time. They save a ton of tokens and make Claude much faster.

There's a catch: **the cheat-sheets were written by hand, so they could silently miss rules**. Nothing checked whether a cheat-sheet actually matched what the real documentation said. If someone added a new rule to the main docs and forgot to update the matching pattern, Claude would just follow the outdated cheat-sheet forever.

This ADR describes the automated system that catches missing rules before they cause problems.

## Why the old approach failed

When we audited the patterns manually, we found **8 real content gaps** — rules the docs required but the patterns didn't mention. Some were serious: one pattern was missing a rule that caused containers to always get SIGKILLed because two timers collided.

Looking at *why* those gaps existed, we found 6 root causes:

1. **Writing a summary once and never checking it again.** Easy to miss non-obvious rules.
2. **Important context split across multiple docs.** A writer distilling one doc can't see rules that live in a different doc.
3. **"Exceptions to the rule" not documented alongside the rule.** The pattern says "always X" but the ADR said "except when Y".
4. **Universal rules stranded in one pattern file.** Rules that apply to *every* task only lived in one specific pattern, so task-specific patterns never saw them.
5. **New architecture decisions written after the pattern was last reviewed.** The pattern never got re-checked.
6. **References to files that don't exist anymore.** Patterns quoted paths that had been renamed or deleted.

Each of these is a *category* of failure, not a one-off mistake. A robust solution has to catch categories, not instances.

## What we built

One existing script (`scripts/drift_check.py`) grew from a simple mtime checker into a 6-mode verification system. One new frontmatter field (`test_tasks:`) was added to every pattern. Zero new files for the core logic.

Each mode catches one of the failure categories:

| Mode | Catches which category | Speed | Runs in CI? |
|------|------------------------|-------|-------------|
| `--drift` *(existing)* | Source doc modified after pattern last updated | fast | yes |
| `--paths` *(new)* | Pattern references a file/dir that doesn't exist (category 6) | fast | yes |
| `--adr` *(new)* | A new ADR was written after the pattern's `last_verified:` date (category 5) | fast | yes |
| `--all` *(new)* | Aggregator that runs all the fast checks | fast | yes — this is what CI calls |
| `--validate` *(new)* | **Semantic content gaps** — rule exists in source docs but missing from pattern (categories 1–4) | slow (LLM) | weekly cron, skips without API key |
| `--validate-router` *(new)* | `.mex/ROUTER.md` routes a task to the wrong pattern | slow (LLM) | weekly cron, skips without API key |
| `--contradictions` *(new)* | Two patterns give directly conflicting advice | slow (LLM) | on-demand, skips without API key |

## Two insights that make this work

**Insight 1: Behavioral tests beat bookkeeping tests.** We briefly considered building a "rule inventory" system — tagging every rule in every source doc and tracking which pattern cites it. That's exactly the manual bookkeeping that caused the problem in the first place. Instead, `--validate` asks a deeper question: "given this pattern alone, does Claude produce a correct plan for this task?" If yes, the pattern content is complete *by definition*. No hand-maintained mappings.

**Insight 2: The source docs themselves are the ground truth.** The `--validate` auditor doesn't compare against a golden answer we wrote by hand. It compares the plan (from the planner LLM that only saw the pattern) against the *full* source documentation (which the auditor LLM can see). The source docs are already the canonical rulebook — we just didn't have an automated way to check patterns against them before.

## How each check works

### `--paths`
Walks every pattern file. For each pattern, reads the `governs:` frontmatter list AND extracts backtick-quoted paths from the body. Checks that every referenced file exists on disk. Takes ~10 ms.

### `--adr`
Reads `docs/decisions/*.md`. Each ADR declares a `**Date:**` and a `**Scope:**` (list of file paths the ADR affects). For each pattern, compares the pattern's `last_verified:` date against every ADR whose Scope overlaps the pattern's `governs:` list. If the pattern is older than an overlapping ADR, it's flagged for re-review. Takes ~20 ms.

### `--validate`
Two-LLM-call process per task:
1. **Planner call**: Sends only `patterns/{the-pattern}.md` + `.mex/ROUTER.md` + universal rules (from `general-code.md`) + a task description to Gemini. Asks: "list every rule you would follow for this task, based only on what you see above."
2. **Auditor call**: Sends the plan from step 1 + *all* source docs (CONTRIBUTING-AI.md, DEVELOPMENT.md, SECURITY.md, every ADR) + the original task to Gemini. Asks: "list every rule from the source docs that the plan missed for this specific task."

If the auditor finds nothing missing, output is `NO_GAPS`. Otherwise, each gap is listed with a citation. Takes ~30–60 seconds per pattern.

### `--validate-router`
One LLM call per task:
- Sends `.mex/ROUTER.md` + the list of valid pattern filenames + the task to Gemini.
- Asks: "which pattern file would you load for this task?"
- Compares the answer to the pattern the task was declared in.

Mismatch = either the router is confused or the test_task is too generic to disambiguate. Takes ~5–10 seconds per task.

### `--contradictions`
Single LLM call that concatenates all pattern bodies (separated by `--- filename ---`) and asks: "find rules that directly contradict each other across different patterns." The model responds `NO_CONTRADICTIONS` or lists each pair with file references. Not wired into `--all` (opt-in only — LLM-based). Takes ~5 seconds for the full set.

### Router precedence rule

`.mex/ROUTER.md` now has a `## Precedence` section that resolves routing ambiguity with a ranked-specificity principle:
1. Security-sensitive code → `security-review`
2. Subsystem-internal changes → the subsystem's own pattern
3. `general-code` is the fallback

This is a general principle, not a hardcoded list. Adding a new pattern doesn't require updating the precedence rule — it applies automatically.

## The `test_tasks:` frontmatter

Each pattern declares 3+ short task descriptions in its YAML frontmatter:

```yaml
test_tasks:
  - "Add a Discord channel with OAuth login"
  - "Add capabilities: logging to a new MCP channel server"
  - "Register a new MCP tool on the Telegram channel with a Zod input schema"
```

These serve as **golden inputs** for the LLM checks. There are no golden *answers* to maintain — the source docs provide the ground truth, and the LLM does the comparison. When someone adds a new pattern, they just add 3 example tasks. No test fixtures to keep in sync.

## How to add a new verification layer

The system is designed to grow. Adding a new check (say, "layer 7") is a repeatable 5-step pattern:

1. **Write a `check_X(project_root)` function** in `scripts/drift_check.py`. It takes the project root and returns an exit code: `0` = pass, `1` = real problem found, `2` = unrecoverable error.
2. **Wire it into argparse.** Add a new flag in the `if __name__ == "__main__"` block and a dispatcher case.
3. **Wire it into `check_all`** if it's fast enough to run on every PR. Skip this step if it requires LLM calls or network — those stay opt-in.
4. **Add an npm script** in `package.json` so humans can run it by name (e.g. `npm run pattern-validate-router`).
5. **Write unit tests** in `scripts/tests/test_drift_check.py`. Use `monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)` to sandbox the check against a minimal fake project tree. For LLM-based checks, test the skip paths (no API key, no target file, empty filter) — the full LLM flow is covered by periodic smoke tests.

Every existing check follows this shape. Grep for `def check_` to see them.

When to add a new layer: if a future audit finds that a new *category* of gap keeps slipping through (not just a specific bug), that category gets its own check. The 6 existing modes catch the 6 categories we found. We don't speculate about future categories.

## Known gaps

Being honest about what this system does *not* catch:

1. **Rules that live only in code** — ESLint configs, commit-msg hooks, test assertions. These are already enforced by CI, so the pattern system deliberately doesn't duplicate them. Mixing patterns (knowledge) with CI (enforcement) would bloat everything.
2. **Source doc staleness vs actual code behavior** — a doc could describe behavior the code no longer implements. This is a docs-validation problem, not a pattern-validation problem. Would need AST analysis or behavioral tests — out of scope.
3. **Semantic no-op source changes** — if a source doc is reorganized without changing its rules, `--drift` flags the pattern anyway. The workaround is trivial (bump `last_verified:`), so no fix.

Each of these is documented as a deferred task with an implementation plan in `docs/decisions/pattern-verification-deferred.md`.

## Consequences

- **Every PR gets fast pattern checks for free.** CI runs `--all` automatically.
- **Content gaps can be found on demand.** `npm run pattern-validate` surfaces rules missing from patterns vs source docs. Runs with Gemini, costs ~$0.25 for a full scan.
- **Router mistakes can be found on demand.** `npm run pattern-validate-router` surfaces cases where the router picks the wrong pattern.
- **Cross-pattern contradictions caught.** `npm run pattern-contradictions` flags rules that directly conflict across patterns.
- **New patterns plug in with minimal work.** Add a pattern, add 3+ test_tasks, everything else flows through automatically.
- **The system is explainable to anyone.** This ADR. Seven checks, one file. No complex abstractions.

## Reasons alternative approaches were rejected

- **Markdown anchors + bidirectional rule tracking.** Would require editing every source doc to tag every rule, and maintaining a rule inventory by hand. That's the manual process that caused the original problem.
- **Mirroring ESLint/lint rules into patterns.** CI already enforces these. Duplicating them would double the maintenance burden and confuse the architectural split between "knowledge" (patterns) and "enforcement" (CI).
- **Runtime behavioral tests that exercise patterns.** Interesting idea but would require building a task-dispatch harness. `--validate` achieves the same signal (does this pattern produce a correct plan?) without any new infrastructure — it just asks an LLM the question directly.
- **Auto-generating ROUTER.md from pattern frontmatter.** Would eliminate the one manual step (adding a routing table row) when creating a new pattern. Rejected because: (a) some rows map multiple task types to one pattern and include extra-doc hints — that metadata doesn't naturally fit in frontmatter, (b) the Precedence and Compound tasks sections are prose requiring human judgment and can't be generated, (c) the cost is one line per pattern which is rare (~8 patterns in the project's lifetime), (d) `--paths` already catches stale references, and (e) indirection hurts readability — right now you open ROUTER.md and see everything.

## Managing at scale

As the number of patterns, source docs, and ADRs grows, use this guide to stay on top of the verification system.

### Quick reference — all commands

| Command | What it does | When to run |
|---------|-------------|-------------|
| `npm run drift-check` | All fast checks (drift + paths + adr + test_tasks + coverage) | Every PR (CI does this automatically) |
| `npm run drift-check:mtime` | Just the mtime-based drift check | Quick local sanity check |
| `npm run drift-check:paths` | Verify all pattern-referenced paths exist | After renaming/deleting files |
| `npm run drift-check:adr` | Flag patterns stale vs recent ADRs | After writing a new ADR |
| `npm run drift-check:coverage` | Report docs/ files no pattern covers | After adding new docs |
| `npm run pattern-validate` | LLM content audit (slow, needs GEMINI_API_KEY) | After editing a pattern or source doc |
| `npm run pattern-validate-router` | LLM router correctness (slow) | After editing ROUTER.md or pattern test_tasks |
| `npm run pattern-contradictions` | LLM cross-pattern conflict detection (slow) | After adding a new pattern or changing rules |

### Adding a new pattern

1. Create `patterns/<name>.md` with YAML frontmatter: `governs:`, `last_verified:`, `test_tasks:` (3+ entries)
2. Add a row to `patterns/INDEX.md`
3. Add a routing row to `.mex/ROUTER.md`
4. Run `npm run drift-check` — should pass with the new pattern included
5. Run `npm run pattern-validate -- <name>` — verify content completeness
6. Run `npm run pattern-validate-router -- <name>` — verify routing accuracy
7. Run `npm run pattern-contradictions` — verify no conflicts with existing patterns

### Updating an existing pattern

1. Edit the pattern content
2. Update `last_verified:` to today's date
3. Run `npm run pattern-validate -- <name>` to verify the edit didn't introduce gaps
4. If you changed `test_tasks:`, also run `npm run pattern-validate-router -- <name>`

### After writing a new ADR

1. Add `**Scope:**` to the ADR frontmatter (list of file paths the decision affects)
2. Add the ADR to `docs/decisions/INDEX.md`
3. Run `npm run drift-check:adr` — any pattern whose `last_verified:` predates the new ADR and overlaps its scope will be flagged for re-review

### Interpreting results

- **DRIFTED** (`--drift`): a governed source file was committed more recently than the pattern. Re-read the source and update the pattern, then bump `last_verified:`.
- **MISSING** (`--paths`): a path referenced in the pattern no longer exists. Remove or update the reference.
- **STALE** (`--adr`): a new ADR affects files this pattern covers. Re-read the ADR and incorporate any relevant rules, then bump `last_verified:`.
- **MISMATCH** (`--validate-router`): either the router text is ambiguous or the test_task is too generic. Tighten the test_task wording or adjust `ROUTER.md`'s precedence/routing table.
- **GAPS** (`--validate`): the auditor found rules in source docs that the pattern doesn't mention. Re-distill the missing rules into the pattern.
- **CONTRADICTION** (`--contradictions`): two patterns give opposite advice. Resolve by scoping the rule to its context or removing the conflict.

### Scaling characteristics

- **Fast checks (`--all`)**: O(patterns × ADRs) — milliseconds. CI runs on every PR. No degradation at 50+ patterns.
- **LLM checks**: O(patterns × test_tasks) API calls. Each call is independent and could be parallelized. Current cost at 8 patterns × 4 tasks = 32 calls ≈ $0.25 per full scan.
- **Pattern filter**: every LLM command accepts an optional pattern name to scope to a single file, so you don't need to scan everything every time.
