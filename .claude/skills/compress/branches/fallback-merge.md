# Fallback Merge — Manual Pending Sync When sync_linear_pending.py Exits Non-Zero

This file is read by `/compress` when `python3 ~/deus/scripts/sync_linear_pending.py`
exits with a non-zero code (auth error, internal error, rate limit, or any other non-zero
exit). It describes the manual merge path for updating `pending:` in vault `CLAUDE.md` from
the session log instead.

Do NOT use this path if the script succeeded (exit 0) — Linear is the source of truth when
the script runs cleanly.

---

## Manual merge procedure

1. **Read the current `pending:` block** from vault `CLAUDE.md`. If the block is missing,
   treat it as an empty list.

2. **Remove completed items**: for each `[x]` item from the `## Pending Tasks` section of the
   session log just saved, find its match in the pending list and remove it:
   - **Match on core identifiers** (file names, feature names, PR numbers, tool names),
     ignoring parenthetical annotations like `(closed: ...)`, `(done)`, `(PR #N)` and minor
     wording differences.
   - **Compound items** (`A + B`): if a pending item has sub-tasks joined by ` + `, match each
     part independently. All parts matched → remove the whole item. Some parts matched →
     rewrite to keep only the unmatched parts.
   - **Dedup pass**: after removals, check each remaining `[ ]` item — if it is semantically
     covered by any `[x]` (same feature/identifier, different wording), remove it.

3. **Add new items**: add any new `[ ]` items from the session log that don't already exist in
   the pending list and aren't already covered by a `[x]` from this session (avoid duplicates).

4. **Write the merged list back** to the `pending:` block in vault `CLAUDE.md`.

---

## Notes

- No hard item cap on `pending:`. Total CLAUDE.md file size is the governor (see the archive
  trigger step in skill.md).
- Do NOT drop a live `[ ]` item solely to hit a count limit.
- This fallback does NOT cross-check `[x]` items against Linear active issues — that check is
  only meaningful when the Linear script runs successfully. In fallback mode, the session log
  is the only signal available.
- Log the script's stderr (if any) in your completion confirmation so the failure is visible.
