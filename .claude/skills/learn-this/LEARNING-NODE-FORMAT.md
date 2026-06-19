# Learning Node Format

Companion doc for the `/learn-this` skill only (not a cross-skill format spec). It defines the
vault memory node `/learn-this` writes for each learning. It is the vault-native equivalent of
`/teach`'s `LEARNING-RECORD-FORMAT.md`, adapted to a semantic-vault node so the learning is
retrievable across sessions and projects.

## Location

```
$VAULT/Learning/<topic-slug>/NNNN-<dash-case-name>.md
```

`NNNN` increments per topic folder (scan for the highest existing number, add one).

## Frontmatter

```yaml
---
id: <32-char hex — REQUIRED; mint via the command below>
type: learning-record
title: "<Topic>: <what was learned> (short)"
description: >
  The embedding source. 1-3 sentences: WHAT was learned and WHY it matters for
  future sessions. Future `memory_tree.py query` calls match on THIS text, so make
  it a tight, searchable summary — not a section header.
level: 2
date: YYYY-MM-DD
topic: "<Topic>"
see_also:
  - STUDY.md
  - Learning/<topic-slug>/<related-node>.md
  # - <path to a related /teach workspace, if any>
---
```

Schema notes (matching `scripts/memory_tree.py` frontmatter parsing):
- **`id`** — REQUIRED. The indexer (`discover_node`) and the auto-embed hook return `no_id` and
  skip any node lacking it, so an id-less node is never retrievable. Mint a 32-char hex id in the
  `make_id` format (48-bit ms timestamp + 80-bit random):
  `python3 -c "import time,secrets; print((int(time.time()*1000).to_bytes(6,'big')+secrets.token_bytes(10)).hex())"`
- **`type`** — always `learning-record` for these nodes.
- **`title`** — display name.
- **`description`** — REQUIRED. This is what gets embedded and retrieved. If absent, the node is
  not indexable.
- **`level`** — depth in the tree (2 is fine for a leaf learning node).
- **`see_also`** — cross-edges to related nodes (other learnings, `STUDY.md`, a `/teach` workspace).
  This is the bridge that keeps `/learn-this` and `/teach` linked without duplicating content.

## Body

```markdown
## What was learned
- The concept, in the user's own compressed terms.

## Why it matters / mission link
- The real-world goal this serves.

## Misconception corrected (if any)
- Was: <the wrong prior belief>. Now: <the correction> — these predict future stumbling blocks.

## Evidence of understanding
- How the user demonstrated it (recalled, solved N problems, explained from first principles).

## Next (zone of proximal development)
- What this unlocks to teach next; what to defer and why.

## Sources
- NotebookLM notebook/artifact links + any external citations used.
```

A learning node can be short. The value is recording *that* this is now known and *why* it changes
what to teach next — not filling every section. Omit sections that add nothing.

## When to write one

Write a node only when at least one is true (mirrors `/teach`'s discipline):
1. The user demonstrated genuine understanding of something non-trivial (evidence, not exposure).
2. The user disclosed prior knowledge worth not re-teaching.
3. A misconception was corrected.
4. The mission shifted in response to learning.

Do **not** write a node for material merely covered. Coverage is not learning — wait for evidence.

## Supersession (never delete)

If a later learning corrects an earlier node, add a new node and set the old node's frontmatter to
`superseded_by: Learning/<topic-slug>/<new-node>.md` (and optionally `orphaned_at`/`orphan_reason`).
The vault is soft-append — never delete a node; the history of how understanding evolved is signal.
