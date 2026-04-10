# Deferred Work: Pattern Verification System

**Status:** Open — these are deferred verification gaps from `pattern-verification-system.md`
**Date:** 2026-04-10
**Scope:** `scripts/drift_check.py`, `.mex/ROUTER.md`

The pattern verification system catches 6 categories of gap. This doc lists the 5 categories it does **not** yet catch, along with concrete implementation plans. Each item is independent and can be picked up as a standalone PR.

## Priority order (highest leverage first)

| # | Task | Why | Effort | Blocks other work? |
|---|------|-----|--------|---------------------|
| **R** | Router tightening | Surfaced by `--validate-router`: 4/32 tasks currently route wrong | Small (S) | no |
| **3** | Cross-pattern contradictions | Nobody catches this today | Small (S) | no |
| **1** | Rules that live only in code | Architectural question as much as a feature | Medium (M) | no |
| **5** | Mtime false positives on pure-reordering source changes | Low value, trivial workaround exists | Small (S) | no |
| **4** | Source doc staleness vs actual code behavior | Biggest scope, needs its own design pass | Large (L) | no |

"Blocks other work?" is `no` for all five — the core system ships in PR #138 and is production-ready without any of these.

---

## R. Router tightening (revealed by this PR)

**Status: completed** — 2026-04-10

**What `--validate-router` found (4 residual mismatches after test_task cleanup):**
1. "Add a new storage provider under `evolution/storage/providers/`" → routes to `general-code.md`, expected `eval-change.md`
2. "Add a new atom type extraction step to `scripts/memory_indexer.py`" → routes to `general-code.md`, expected `eval-change.md`
3. "Add a new mount in `src/container-mounter.ts`" → routes to `cross-platform.md`, expected `security-review.md`
4. "Add a new sender to `src/sender-allowlist.ts` for a specific channel" → routes to `channel-add.md`, expected `security-review.md`

**Root cause:** ROUTER.md uses task-type keywords ("mount", "channel", "storage") but doesn't weight them by security implications or governs-overlap specificity. The router picks the first keyword match it sees.

**Plan:**
1. Add a **priority order** to ROUTER.md so `security-review` is always considered before any pattern that overlaps with its governs list. Concrete rule: "if the task touches `src/container-*`, `src/mount-*`, `src/credential-*`, `src/*allowlist*`, or `src/ipc.ts`, use `security-review` regardless of other keywords."
2. Disambiguate `memory_indexer.py`: it's currently listed under both `eval-change.md` and `general-code.md` governs. Decide which one owns it (proposal: `eval-change.md` since the memory indexer is part of the evolution loop's data layer). Remove from `general-code.md` governs.
3. Add a `compound tasks` note: tasks touching both `evolution/` and `src/`-only concerns should load both patterns (already documented in ROUTER.md §Compound tasks but not pruned enough).
4. Re-run `--validate-router` after each edit. Target: 0 mismatches.

**Size:** ~20 lines in ROUTER.md + small edit to `general-code.md` governs. No code changes.

**Success criterion:** `npm run pattern-validate-router` shows 0 mismatches.

---

## 3. Cross-pattern contradictions

**Status: completed** — 2026-04-10

**Gap:** If `deployment.md` says "always X" and `general-code.md` says "never X", nothing detects that. With only 8 patterns today contradictions are rare, but the number will grow.

**Plan:**
1. Add `check_cross_pattern(project_root)` in `scripts/drift_check.py`. Add `--contradictions` flag.
2. Implementation: single batched LLM call that loads all pattern files and asks "identify any rules in these patterns that directly contradict each other. Respond NO_CONTRADICTIONS if none. Otherwise list each contradiction with the two source patterns and the two conflicting rules."
3. Use temperature=0.0. Run on-demand, not in CI (LLM-based, same skip-gracefully-without-key treatment as `--validate`).
4. Wire to `npm run pattern-contradictions`.
5. Tests: 3 skip-path tests (no API key, no patterns, filter matches nothing). Full LLM flow covered by smoke test.

**Size:** ~80 lines in `drift_check.py` + ~30 lines of tests. Zero new files.

**Why it's deferred, not inlined into PR #138:** with 8 patterns, the failure mode is currently hypothetical. Build it when contradictions actually bite us (or preemptively if the pattern count doubles).

**Success criterion:** `npm run pattern-contradictions` runs clean on the current repo. Manual sanity test: introduce a deliberate contradiction in a feature branch, verify the check catches it.

---

## 1. Rules that live only in code (ESLint, commit-msg, pre-commit, test assertions)

**Gap:** A rule enforced only by `eslint-disable` comments, commit-msg hooks, or test assertions isn't visible to the pattern auditor. If the pattern doesn't mention it, Claude might violate it until CI slaps the PR.

**Architectural tension:** Patterns are for *knowledge* Claude needs to carry forward. CI is for *enforcement* that catches violations regardless of knowledge. Duplicating every ESLint rule into every pattern would bloat everything. But knowledge gaps still cause wasted cycles when Claude writes code that CI then rejects.

**Proposal: surface enforced rules without duplicating them.** Generate a single `patterns/_enforced-rules.md` file automatically from the CI config, and tell ROUTER.md to load it alongside the task pattern.

**Plan:**
1. Write a new script `scripts/generate_enforced_rules.py` that:
   - Reads `eslint.config.mjs` and extracts rule names + severities
   - Reads `commitlint.config.mjs` and extracts allowed types + scope rules
   - Reads `.husky/pre-commit` and extracts what each hook checks
   - Optionally reads `scripts/checks.ts` (cross-platform test assertions) and extracts lint-style rules
   - Writes a single markdown file `patterns/_enforced-rules.md` with one section per source, short summaries only
2. Add `_enforced-rules.md` to `.gitignore` (regenerated on every build)
3. Add a `generate:enforced-rules` npm script and run it in `prebuild` (so it's always up to date)
4. Update `.mex/ROUTER.md` §Universal rules to say "also load `patterns/_enforced-rules.md` — CI-enforced rules Claude should follow proactively"
5. Update `--validate` to include `_enforced-rules.md` in the planner prompt context

**Size:** ~150 lines of new script + small wiring changes.

**Why it's deferred:** the existing CI catches violations anyway. This only improves *proactive* compliance (writing code that passes on the first try). Useful but not load-bearing.

**Success criterion:** `patterns/_enforced-rules.md` exists after `npm run build`, contains a flattened list of every enforced rule, and `--validate` stops flagging "missing ESLint rule X" gaps.

---

## 5. Mtime false positives on pure-reordering source changes

**Gap:** Someone reorganizes `docs/CONTRIBUTING-AI.md` (reorders sections, no content change). `--drift` flags every pattern governing content from that doc as stale, even though nothing semantic changed.

**Current workaround:** `touch patterns/foo.md` to reset its mtime (~2 seconds of effort per drift report).

**Plan:**
1. Extend the pattern frontmatter with a `sources:` field: list of `{path, hash}` entries.
2. Extend `check_drift` to compare the stored hash against a fresh hash of the source file. If identical → not drifted, even if mtime changed.
3. Provide a helper `scripts/drift_check.py --refresh-hashes` that recomputes all hashes and writes them back into pattern frontmatter.
4. Update every existing pattern to include initial hashes.

**Size:** ~100 lines for the parser + hash logic, ~50 lines of tests, ~30 pattern edits.

**Why it's deferred:** the fix adds a permanent maintenance tax (every pattern now has a `sources:` block that grows with the governed docs) to avoid a 2-second annoyance. Poor cost/benefit. Build only if reorganizations become frequent enough that the `touch` workaround is painful.

**Success criterion:** pure reorganization of a source doc does not trigger `--drift` for any pattern that governs it.

---

## 4. Source doc staleness vs actual code behavior

**Gap:** `docs/SECURITY.md` could describe a defense the code no longer implements. Patterns pass all checks because they match the (stale) doc, but the doc itself is lying.

**Why this is hard:** there's no automated way to verify "does this piece of prose match the behavior of this piece of code?" The options are:
- **AST analysis:** parse the source code, build a semantic model, compare against doc claims. Massive scope, unreliable for free-form prose.
- **Runtime behavioral tests:** write assertions that exercise the claimed behavior. Already how we'd do this in regular TDD — nothing special to build.
- **LLM-based cross-check:** send doc + relevant source file to an LLM, ask "does the code still do what this doc claims?" Cheap and promising, but high false-positive risk.

**Proposal:** start with the LLM approach as a `--docs-vs-code` mode.

**Plan:**
1. For each source doc, identify the code files it describes. Option A: explicit `**Describes:**` field per doc section. Option B: heuristic based on file paths mentioned in the doc body.
2. For each (doc section, source file) pair, send both to Gemini with the prompt: "does the code still implement what the doc claims? List any discrepancies."
3. Aggregate discrepancies into a report. Run on-demand, not in CI.
4. Wire to `npm run docs-vs-code`.

**Size estimate:** ~200 lines in `drift_check.py` or a separate script. Needs design work to pick doc-to-code mappings.

**Why it's deferred:** this is a docs-validation problem, not a pattern-validation problem. It has its own design space, its own failure modes, and doesn't belong in the same system. Should be its own ADR when we're ready to invest.

**Success criterion:** `npm run docs-vs-code` flags at least one real stale-doc case in a seeded test. False-positive rate < 25% on clean runs.

---

## Tracking

Each of these 5 tasks is pending in `~/deus/CLAUDE.md` (or the vault `CLAUDE.md` depending on scope). When one becomes ready to implement, open a PR with title `feat(patterns): <task>` referencing this doc.

**Do not bundle multiple of these into one PR.** Each addresses a different category of gap and is independently reviewable. Shipping them as separate PRs keeps review scope small and rollback easy.
