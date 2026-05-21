# Execution Plans

Execution plans (EPs) are versioned artifacts that capture **why and how** decisions are made during multi-step implementations. They are not design docs — they are living records written as work happens.

## When to write an EP

Write an EP when a task spans multiple sessions, involves non-obvious tradeoffs, or requires coordination between agents. Skip it for trivial fixes or single-step changes.

## Naming scheme

Files are named `EP-NNN-<slug>.md` where NNN is a zero-padded three-digit sequence number (EP-001, EP-002, ...). The slug is a short hyphenated phrase matching the branch name.

## Directories

| Directory | Contents |
|-----------|----------|
| `active/` | In-progress EPs — branch is open, work is ongoing |
| `completed/` | Finished EPs — branch merged or work abandoned |

## Lifecycle

1. Open an EP when you branch. Copy `TEMPLATE.md`, fill in the header, and commit it to `active/`.
2. Update the progress checklist and decision log as work progresses. Commit updates alongside code changes.
3. When the branch merges (or is abandoned), move the file from `active/` to `completed/` and set the **Closed** date and final **Status**.

## Relationship to ADRs

ADRs in `docs/decisions/` record permanent architectural rulings. EPs record the _path_ taken during a specific implementation. An EP may reference ADRs it consulted; ADRs do not reference EPs.

## Quality grades

Per-subsystem health grades are tracked separately in [`../QUALITY_GRADES.md`](../QUALITY_GRADES.md). EPs may note grade changes they expect to introduce.
