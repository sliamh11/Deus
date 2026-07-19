# Attribution — mattpocock/skills

The following 12 host skills were imported into Deus from the open-source repo
**[mattpocock/skills](https://github.com/mattpocock/skills)** (MIT-licensed):

| Skill | Upstream path |
|-------|---------------|
| `grilling` | `skills/productivity/grilling` |
| `grill-me` | `skills/productivity/grill-me` |
| `grill-with-docs` | `skills/engineering/grill-with-docs` |
| `domain-modeling` | `skills/engineering/domain-modeling` |
| `teach` | `skills/productivity/teach` |
| `writing-great-skills` | `skills/productivity/writing-great-skills` |
| `diagnosing-bugs` | `skills/engineering/diagnosing-bugs` |
| `tdd` | `skills/engineering/tdd` |
| `prototype` | `skills/engineering/prototype` |
| `codebase-design` | `skills/engineering/codebase-design` |
| `resolving-merge-conflicts` | `skills/engineering/resolving-merge-conflicts` |
| `improve-codebase-architecture` | `skills/engineering/improve-codebase-architecture` |

**Source:** `github.com/mattpocock/skills` @ commit `6eeb81b`
(`6eeb81b5fcfeeb5bd531dd47ab2f9f2bbea27461`).

Three more skills were imported later, at a newer upstream snapshot (importing at current upstream HEAD
rather than re-pinning to the older snapshot above; the 12 above are NOT refreshed to match — that's a
separate `/update-skills`-style concern, not part of this import):

| Skill | Upstream path |
|-------|---------------|
| `wayfinder` | `skills/engineering/wayfinder` |
| `to-spec` | `skills/engineering/to-spec` |
| `implement` | `skills/engineering/implement` |

**Source (these three only):** `github.com/mattpocock/skills` @ commit `9603c1c`
(`9603c1cc8118d08bc1b3bf34cf714f62178dea3b`).

A fourth skill, **`to-tickets`**, is a **Deus-native rewrite** of the pre-existing `linear-slice` skill
(renamed and generalized across trackers), not a straight import — but it lifts substantial verbatim prose
from upstream's own `skills/engineering/to-tickets` (@`9603c1c`): the `<vertical-slice-rules>` block, the
"Wide refactors are the exception..." paragraph, and the "What to build:" ticket-template wording are
upstream's, word-for-word. Everything else in `to-tickets` (the multi-tracker branching across
Linear/Asana/GitHub/local, the Linear pipeline integration, codegraph blast-radius sizing, cap-handling,
and exact-name state resolution) is Deus's own addition on top. Listed here for attribution completeness,
not in the 12+3 table above.

## Adaptations from upstream

The `grilling`/`grill-me`/`grill-with-docs`/`domain-modeling`/`teach`/`writing-great-skills`/
`diagnosing-bugs`/`tdd`/`prototype`/`codebase-design`/`resolving-merge-conflicts`/
`improve-codebase-architecture` bodies are **byte-identical** to upstream. The only change is additive:
Deus's `user_invocable: true` frontmatter field was added to the six user-only skills (`grill-me`,
`grill-with-docs`, `teach`, `writing-great-skills`, `prototype`, `improve-codebase-architecture`
— the ones carrying upstream's `disable-model-invocation: true`) for convention consistency.
Invocation semantics are unchanged.

The three later imports adapt further, each for a stated reason:

- **`wayfinder`**: additive `user_invocable: true` (stays genuinely user-invoked-only, matching upstream)
  + three contextual edits: (1) the `/setup-matt-pocock-skills` reference — a skill Deus doesn't have —
  rewritten to point at `/to-tickets`'s tracker-detection step; (2) the Prototype ticket-type bullet
  reworded to tell the user to run `/prototype` themselves rather than invoke it programmatically
  (`/prototype` is `disable-model-invocation: true`; no skill may invoke another such skill — confirmed
  precedent at `.claude/skills/customize/SKILL.md`); (3) the three `/research` subagent references
  reworded to inline parallel evidence-classified scout agents (the same technique `/deep-research`'s DEEP
  path uses) posting findings as ticket comments, since upstream's separate `research` skill is
  deliberately not imported.
- **`to-spec`**: additive `user_invocable: true` + the `/setup-matt-pocock-skills` reference edit + the
  tracker-aware publication branch (Linear/Asana/GitHub Issues/local, matching `/to-tickets`'s conventions
  instead of a single hardcoded triage label) + a narrowing parenthetical on the opening "Do NOT interview
  the user" line (an ai-eng-warden GPT co-gate finding: read literally, that line vs. the later seam-check
  step looked self-contradictory; the parenthetical scopes the claim to content-gathering specifically and
  points at the one decision-check exception, at the point the claim is made rather than as a downstream
  annotation — without changing upstream's actual behavior). No internal cross-skill invocation to fix
  beyond that.
- **`implement`**: a **deliberate, non-additive frontmatter change** — upstream's
  `disable-model-invocation: true` is **dropped entirely** (no `user_invocable: true` added either),
  with a new model-facing description carrying real trigger language in its place. This is Deus wiring
  `implement` into the automatic plan-reviewer→implement→tdd→code-reviewer cycle
  (`.claude/rules/core-behavioral-rules.md`), which a `disable-model-invocation: true` skill cannot be —
  no other skill/agent may invoke one (same precedent as `wayfinder`'s `/prototype` case above). Also two
  body edits: the final review step now invokes the `code-reviewer` Warden instead of a bare `/code-review`
  call, and the commit step now waits for explicit user approval instead of committing unconditionally
  (matching this repo's execution-gate convention).

### Notes for users

- **Portable internal paths kept as-is.** `domain-modeling`, `grill-with-docs`, and
  `diagnosing-bugs` reference `CONTEXT.md` and a `docs/adr/` directory that they create in
  whatever repo they run. This is upstream's portable convention. Deus's own canonical decision
  log lives in `docs/decisions/` (see `docs/decisions/INDEX.md`) — these skills were deliberately
  **not** re-routed to it, so they remain portable to any repo. When using them inside `~/deus`,
  be aware they default to `docs/adr/`, not `docs/decisions/`.
- **`diagnosing-bugs` hand-off resolves.** `diagnosing-bugs` (`SKILL.md`, final step) instructs an
  optional hand-off to the `/improve-codebase-architecture` skill, reached at the very end of a debug
  session (after the fix is in) as a "what would have prevented this bug?" follow-up. That skill is
  now imported (see the table above), so the hand-off resolves. It depends on `/codebase-design` for
  its architecture vocabulary, which is also imported.

## License

These skills are distributed under the MIT License, retained from upstream. The single notice below
covers every skill attributed in this file — including `to-tickets`'s partial/modified provenance above —
since MIT only requires the notice be included "in all copies or substantial portions of the Software,"
which one shared block already satisfies for the whole attributed set regardless of how many sources or
how much modification is involved:

```
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
