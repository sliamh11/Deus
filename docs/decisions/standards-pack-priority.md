# ADR: Standards-pack priority field

**Status:** Accepted
**Date:** 2026-05-15
**Scope:** `scripts/standards_pack.py`, `scripts/tests/test_memory_tree.py`

## Context

`standards_pack.py` packs `kind: standard` atoms into the SessionStart
"Working Standards" block within a token budget (1200 tokens since PR #413).
When the budget is exceeded, atoms are dropped in directory-sort order —
alphabetical by filename. That is an unprincipled ordering: it treats a
non-negotiable safety rule identically to a minor style preference, so the
first atom whose oneliner overruns the budget gets evicted regardless of
importance.

PR #413 surfaced the gap empirically: at the pre-bump 800-token budget,
`feedback_warden_loop` and `feedback_wait_for_approval` were silently
dropped — both Execution Gate rules from `.claude/rules/core-behavioral-rules.md`.
Bumping to 1200 made this moot today (all 27 atoms fit in 876 tokens), but
the ordering contract remains broken: future atom growth will again push
the pack toward the budget, and without priority ordering safety rules are
at the same risk as workflow hints.

## Decision

Add an optional `priority` field to atom frontmatter with three values:
`high | med | low`. Default when absent: `med`. Sort key becomes
`(priority_rank, filename)` where `high=0, med=1, low=2`. Pack-time loop
remains first-fit with break-on-overrun — only the iteration order
changes.

The CRITICAL stderr message (additive to the existing WARN) fires when an
atom that would have been included is dropped AND it had `priority: high`.
The cache JSON gains `dropped_high: list[str]` so operators can poll
state without re-running the hook.

### What this is NOT

**Inclusion of `priority: high` is not unconditionally guaranteed.** This
is priority-ordered packing with CRITICAL overflow detection, not budget
reservation. If a single `high`-priority atom's one-liner alone exceeds
the budget (or its insertion would exhaust the remaining budget for other
`high` atoms), it is dropped. The CRITICAL stderr message + the
`dropped_high` cache field are the loudness contract on overflow.

Operators who hit a CRITICAL must:
1. Tighten the offending atom's `description:` (one-liner cost is `name +
   description`).
2. Raise `DEUS_STANDARDS_TOKEN_BUDGET`.
3. Or accept the loss for that session and address the contention.

Budget reservation was considered and rejected — at ~30 tokens per
one-liner and a 1200-token budget, the realistic overflow path is "atom
list grew past the budget," not "a single atom is too big." The
declarative CRITICAL message is loud enough for the realistic case and
avoids the operational complexity of a reserved-token sub-budget.

## Rule: what qualifies as `priority: high`

Only atoms that map to **Execution Gates** or **Data & Security** rules
in `.claude/rules/core-behavioral-rules.md` qualify for `priority: high`.
Each tagging commit MUST include a citation to the specific section/line
of that file. The pattern: small set of non-negotiables whose violation
is irreversible (data loss, unauthorized execution, security exposure,
CI contamination). Workflow guidance, honesty principles, and quality
preferences can tolerate budget pressure without permanent harm and
remain `med` (the default).

### Seed set (to be applied host-locally — see Tagging Recipe below)

| Atom | Citation |
|------|---------|
| `feedback_warden_loop.md` | Execution Gates L18: "REVISE from any warden means re-run after fixes until SHIP, against a bounded artifact (bounding is the round-count-circuit-breaker re-scope checkpoint in `plan-review-rules.md` — never a self-declared exemption) — no exceptions, no 'close enough,' no time-pressure rationalization. Never touch markers, commit, or proceed on REVISE." |
| `feedback_wait_for_approval.md` | Execution Gates L13: "Never execute without explicit user approval. Wait to be told." |
| `feedback_data_integrity.md` | Data & Security L6: "Never lose, overwrite, or downgrade user data. Merge, don't replace." |
| `feedback_no_speculation.md` | Verification & Honesty L22: "Never speculate. Only state verified facts. If unsure, say so." |
| `feedback_no_merge_failed_tests.md` | Execution Gates L19: "Never merge failing CI." |
| `feedback_security_first.md` | Data & Security L7: "Audit security before every commit. Treat the repo as public." |

## Tagging Recipe

The 27 atoms in `~/.claude/projects/<encoded>/memory/*.md` are host-local
and not committed to the repo. The code change in this PR is a no-op
until the seed atoms are tagged on the user's machine. Run this once
post-merge:

```bash
# Derive the encoded project dir the same way standards_pack.py does:
# leading "-" + slashes replaced with "-". Substitute your Deus checkout
# path for $DEUS_DIR.
DEUS_DIR="$HOME/deus"
ENCODED="-$(echo "$DEUS_DIR" | sed 's|^/||; s|/|-|g')"
MEM_DIR="$HOME/.claude/projects/$ENCODED/memory"

for atom in feedback_warden_loop feedback_wait_for_approval feedback_data_integrity \
            feedback_no_speculation feedback_no_merge_failed_tests feedback_security_first; do
  f="$MEM_DIR/${atom}.md"
  # Idempotent: skip if already tagged. Running the recipe twice without
  # this guard would double-insert `priority: high` into the frontmatter.
  grep -q '^priority:' "$f" || \
    perl -i -pe 's/^kind: standard$/kind: standard\npriority: high/' "$f"
done

# M0.5's content-hash will invalidate the cache on next run, but
# explicitly clearing it is safer:
rm -f ~/.deus/standards_pack_cache.json
```

`perl -i -pe` behaves identically on macOS and Linux (unlike `sed -i`,
which requires `''` on macOS but not on GNU). Run the smoke test after
tagging:

```bash
python3 ~/deus/scripts/standards_pack.py < /dev/null
# Check the cache:
cat ~/.deus/standards_pack_cache.json | python3 -m json.tool | head -20
# Expected: atom_count: 27, dropped: [], dropped_high: []
```

## Consequences

- Sort order on overflow is now principled. Alphabetical filename is the
  stable secondary key within a priority tier.
- The code change is a no-op until host-local tagging runs. PR description
  points to this ADR's Tagging Recipe.
- Atoms without `priority:` continue to behave as pre-M2 (default med,
  pure filename sort within tier).
- The cache JSON gains `dropped_high` (defaults to empty list). Old caches
  written by pre-M2 code do NOT have this field — they will fall through
  M0.5's content-hash signature mismatch and rebuild cleanly on first run.
  Consumers doing strict-schema validation should treat absence as
  semantically equivalent to `[]`.
- Future atoms claiming `priority: high` must cite
  `core-behavioral-rules.md` in the tagging commit message.

## Alternatives considered

- **Env-var override list of always-included atom names.** Rejected.
  Declarative frontmatter follows the established pattern (`kind:` from
  PR #380/#409); env vars would create a split source of truth.
- **Per-atom numeric weight (float 0.0-1.0).** Rejected. Ordinal
  `high/med/low` is sufficient and avoids bikeshedding about specific
  numbers.
- **Reserved budget slice for `high` atoms.** Considered. Rejected for
  simplicity — the CRITICAL detection covers the realistic overflow
  case, and reservation adds operational complexity (separate
  sub-budget accounting, reservation overflow rules) without a real
  caller justifying it.
- **`kind: critical` as a fourth atom kind.** Rejected. Priority is a
  property of `kind: standard` atoms, not a sibling kind — they have
  the same surface (one-liner pack), different ordering.

## Forward compatibility

The framework is intentionally open to M5 (telemetry-driven priority).
That future system layers an EWMA violation score on top of declared
priority — `(declared_priority, -ewma_score, filename)` — without
changing the frontmatter contract. Atoms can ship with `priority: high`
today and continue to work when telemetry layers in later.
