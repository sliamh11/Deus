# Attribution — mattpocock/skills

The following 14 host skills were imported into Deus from the open-source repo
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
| `wayfinder` | `skills/engineering/wayfinder` |
| `research` | `skills/engineering/research` |

**Source:** `github.com/mattpocock/skills` @ commit `ed37663`
(`ed37663cc5fbef691ddfecd080dff42f7e7e350d`) as of the `wayfinder`/`research` addition — originally
imported @ `6eeb81b` (2026-06-19).

## Adaptations from upstream

The skill bodies are **byte-identical** to upstream. The only change is additive: Deus's
`user_invocable: true` frontmatter field was added to the seven user-only skills (`grill-me`,
`grill-with-docs`, `teach`, `writing-great-skills`, `prototype`, `improve-codebase-architecture`,
`wayfinder` — the ones carrying upstream's `disable-model-invocation: true`) for convention
consistency. Invocation semantics are unchanged. `research` carries no such flag upstream and gets
no addition — it stays both user- and model-invocable, same class as `grilling`.

Upstream's `agents/openai.yaml` files (OpenAI/ChatGPT-platform skill config, not applicable to
Claude Code) are deliberately not copied. `wayfinder` and `research` are the first imported skills
to carry an `agents/` directory upstream at all — the previously-imported skills never had one at
import time, so there is no prior "omission" convention being followed here; the decision stands
on its own merits: this content has zero function in Claude Code and isn't part of the skill's
actual portable instructions.

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
- **`wayfinder` ships without Linear wiring.** Its own reference config skill,
  `/setup-matt-pocock-skills` (not imported — see below), only ships GitHub/GitLab/local-markdown
  tracker docs, no Linear one. Without that setup, `wayfinder` "default[s] to the local-markdown
  tracker" per its own `SKILL.md`, which is fully functional standalone. Wiring it to Deus's actual
  Linear pipeline is a separate, unscoped follow-up — not assumed to already work.
- **`research` is the mechanism `wayfinder` uses for its `research`-type tickets** ("Resolved by a
  `/research` subagent" — its own `SKILL.md`). It's independently useful outside `wayfinder` too:
  a general-purpose "investigate a question against primary sources, write findings to a file"
  primitive, distinct from Deus's own `/deep-research` (which classifies intent and fans out
  lit-scout/brainstormer/NotebookLM specifically).

## Evaluated, not imported

Genuinely new upstream since the last sync (@ `6eeb81b`, as of `ed37663`): `code-review`,
`to-spec`, `to-tickets` (plus `wayfinder`/`research`, imported above). `ask-matt`, `implement`, and
`triage` are NOT new — all three already existed at `6eeb81b`; `ask-matt`/`implement` were simply
never evaluated by the original 2026-06-19 import, and `triage` (along with upstream's now-vanished
`to-issues`/`to-prd`, likely reworked into today's `to-tickets`/`to-spec`) was already explicitly
evaluated and skipped by that import ("explicitly skipped triage/to-prd/to-issues (collide with
Linear)" — its own session log). `setup-matt-pocock-skills` also predates this sync and remains
unimported (see the `wayfinder` note above — its tracker docs don't cover Linear). All four
skills below that could be mistaken for duplicates of an existing Deus skill (`code-review`,
`to-tickets`, `to-spec`, `triage`) were read in FULL and diffed against their Deus-native
counterpart before deciding — not skipped on name/territory alone.

- **`code-review`** — name collision with Deus's own `/code-review` (a completely different
  warden/dual-backend-gate system; see `.claude/wardens/code-review-rules.md`). Not imported as a
  separate skill. Genuinely offers two ideas Deus's review lacks: a **Standards-vs-Spec two-axis
  split** (conformance to documented conventions vs. faithfulness to the originating issue/PRD, run
  as separate parallel sub-agent reviews so neither axis masks the other — Deus's own review checks
  style/logic/security bugs, not spec conformance as a distinct axis) and a concrete **12-item
  Fowler code-smell baseline checklist** (Mysterious Name, Duplicated Code, Feature Envy, Data
  Clumps, Primitive Obsession, Repeated Switches, Shotgun Surgery, Divergent Change, Speculative
  Generality, Message Chains, Middle Man, Refused Bequest) as an always-on fallback when a repo
  documents no standards of its own. Worth grafting into Deus's existing `/code-review` skill as a
  4th parallel agent — a real, well-specified follow-up, not done in this sync.
- **`to-tickets`** — near-duplicate of Deus's own `/linear-slice` for the Linear-native path (same
  tracer-bullet vertical-slice framing, same tracker-issue output). Not imported as a separate
  skill. Has a genuine gap `linear-slice` lacks: an explicit **"wide refactor" exception** —
  expand-contract sequencing (add the new form beside the old, migrate callers in blast-radius-
  sized batches, then delete the old form) for mechanical, codebase-wide-blast-radius changes (a
  column rename, a shared symbol retype) that can't be tracer-bulleted into independently-demoable
  vertical slices. Also confirmed a real, not just theoretical, incompatibility: `to-tickets`
  instructs applying the `ready-for-agent` triage label directly on publish — an analogous
  incompatibility with Deus's actual gate contract, which uses a differently-named but equally
  gate-owned label: `linear-slice`'s own `SKILL.md` states "Never apply a `Scoped` label... The
  gate manages that label." Even absent the naming overlap, this skill cannot be
  imported as-is against Deus's live Linear pipeline.
- **`triage`** — read in full and compared against the actual documented Linear pipeline contract
  (`Backlog → Todo → Ready for Agent → Agent Working → In Review → Done`, enrichment-gate/
  bouncer-gate, both automated on state transitions). `triage`'s own state model
  (`needs-triage`/`needs-info`/`ready-for-agent`/`ready-for-human`/`wontfix`, driven by a
  conversational human-in-the-loop flow) does not map onto Deus's actual configured states or its
  automated gate-driven flow — a confirmed, not assumed, state-model conflict. Not imported. Three
  extractable ideas worth a dedicated follow-up evaluation against the REAL enrichment-gate
  implementation (not read during this sync, so deliberately not blindly merged): a pre-triage
  **redundancy check** ("is this already implemented — search by domain concept, not just the
  request's wording"), a **verify-the-claim-before-grilling** step (reproduce a bug / confirm a PR
  diff does what it claims before investing in further triage), and an **`.out-of-scope/`
  rejected-request knowledge base** pattern (persist *why* a request was rejected so it doesn't get
  silently re-proposed later).
- **`to-spec`** — on full read, this is NOT redundant with anything Deus has: `linear-slice` only
  decomposes an *already-existing* plan into tickets, nothing turns a conversation into a published
  spec/PRD first. It shares `to-tickets`'s exact `ready-for-agent`-label conflict with Deus's gate
  contract, so a safe import needs the same kind of Linear-specific adaptation `linear-slice` itself
  already received (land in Backlog, let the enrichment gate own scope-writing, never apply a
  gate-owned label directly) — real engineering, not a byte-identical copy. A concrete, well-scoped
  candidate for a dedicated future skill (a Linear-adapted companion to `linear-slice`), not
  bundled into this sync.
- **`implement`** — genuinely novel (the execution counterpart to `wayfinder`'s "plan, don't do"
  philosophy) but not requested, and Deus already has its own plan → build → review discipline
  (Execution Gates, `core-behavioral-rules.md`) that a generic `/implement` would partially
  duplicate. Worth a dedicated evaluation later, not bundled into this sync.
- **`ask-matt`** — a router over mattpocock's *own* skill repo's inventory. Deus has its own
  routing (`.mex/ROUTER.md`) and a materially different skill set — low standalone value here.
- **`setup-matt-pocock-skills`** — not imported (see the `wayfinder` note above): its
  tracker-config docs cover GitHub/GitLab/local-markdown only, no Linear, so importing it would not
  by itself wire `wayfinder`/future skills to Deus's actual tracker.

## License

These skills are distributed under the MIT License, retained from upstream:

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
