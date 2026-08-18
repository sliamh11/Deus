# Provenance — `verify`

Locally authored. Converges two predecessor skills and replaces both.

## What it replaces

| Predecessor | What it contributed | Status |
|---|---|---|
| Claude Code's bundled `/verify` | Runtime observation: build, launch, drive the affected flow, capture evidence. Verdicts PASS/FAIL/BLOCKED/SKIP. **No control requirement.** | Cannot be removed — it ships inside the CLI itself rather than as a file on disk. Shadowed by this skill, which wins the by-name lookup. |
| `verify-this` | Falsifiable claim, baseline vs treatment, artefact comparison. Verdicts VERIFIED / NOT VERIFIED / INCONCLUSIVE. **Agnostic about where measurement happens,** so it accepted unit-test evidence. | Removed 2026-08-16. Was a verbatim copy of `cursor-team-kit/skills/verify-this/SKILL.md` from `cursor/plugins`, pinned commit `2a8044425c7bddf429c3bdedf3ab61e791d34d65`, SHA-256 `c1c7b27c1133085bd3409c601ea12b6e6f61b4b23debcd52bc248fc01907e7de`. |

Neither was a complete verification alone. The bundled skill could not separate
"the change caused this" from "it was already so." `verify-this` demanded the
contrast but tolerated measuring somewhere other than the software a user meets.
Here both are mandatory, and a missing control downgrades the verdict to
INCONCLUSIVE rather than being silently dropped.

## Licence — how this became publishable

An earlier revision of this file claimed the prose was "newly written rather than
copied." **That claim was wrong, and it had never been measured.** A word-sequence
comparison against the bundled skill's own wording found **181 shared 12-word
sequences, 7.1% of the file at 12-grams and 13.7% at 8-grams**. That is
derivative expression, not merely shared methodology, and it made the file
unpublishable.

The current text was then rewritten clean-room and re-measured:

| Compared against | 12-gram | 10-gram | 8-gram | 6-gram |
|---|---|---|---|---|
| bundled `/verify` (before rewrite) | 181 | 248 | 348 | 489 |
| bundled `/verify` (after rewrite) | **0** | **0** | **0** | 4 |
| `verify-this` | **0** | **0** | **0** | **0** |

The four surviving 6-grams are generic technical English ("at one of the rows
above", "only the one in the diff"). Re-measure before any future edit that
borrows phrasing from either predecessor.

What is retained from both is **method**, which is not ownable: exercise the real
surface, contrast against a control, probe the adjacent cases, report a verdict
from a fixed rubric. What was removed is expression.

**On structural similarity, asked and answered.** Word-sequence overlap is not the
only theory of copying; shared structure, sequence and organisation is another.
Answered honestly: this skill and the bundled one do share part of a skeleton —
establish scope, find the surface, get the software running, drive it, capture,
report, map to a verdict. That order is the functional order of the underlying
task rather than an authored arrangement: you cannot drive software before you can
run it, or compare captures before you have taken them. The steps that carry this
skill's actual thesis are additions, not rearrangements — fixing a falsifiable
claim with its refutation condition up front, capturing a control as its own
gated step, comparing as its own step, and a five-way verdict ladder decided on
evidence standing. The residual similarity is task order, which no one owns; it is
recorded here rather than left implied.

`verify-this` was never the constraint — at zero shared 8-grams, this skill took
its idea and none of its words. `cursor/plugins` publishes no licence (no
`LICENSE` at the repo root, GitHub API reports `license: null`), so its
expression could not have been redistributed; its methodology always could.

## Layout

```
verify/
├── SKILL.md                    the protocol — always loaded on invocation
├── PROVENANCE.md               this file — never linked from SKILL.md, so never loaded
└── reference/
    ├── surfaces.md             per-surface driving mechanics and what to capture
    ├── control.md              obtaining a control, state that leaks between runs
    └── measurement.md          performance and non-deterministic methodology
```

No `scripts/` directory, deliberately. An earlier draft put the §2 scope logic in
`scripts/scope.sh`; two things ruled that out. This repository excludes
`.claude/skills/*/scripts/` from version control by design — community skill
templates ship documentation, not private implementations — so the file would
never have reached a clone, leaving the body pointing at something absent. And
more fundamentally, a skill installed at user scope runs inside arbitrary
projects, so it must not depend on a file that exists in only one of them. The
logic is inlined instead, which is portable and self-contained.

Progressive disclosure is deliberate, following the bundled `/verify` and `/run`,
which both ship `examples/*.md` rather than inlining them. The body carries what
every run needs — the claim, the contrast, the verdict rubric, the report. The
reference files carry what only some runs reach: you are on exactly one surface
out of seven, so six of those sections would be dead weight in context. §8
(probes) is deliberately **not** disclosed, because every run probes, and it is
the step most likely to be skipped.

Launching the software is delegated to the bundled `/run` skill rather than
reimplemented. `/run` is model-invocable (unlike `/verify`) and already ships six
per-project-type recipes.

§2's command block was exercised before shipping, first as a script and then in
its inlined form, against a clean tree with a remote and against a repository with
no remote plus uncommitted and untracked work. Testing caught a real defect in the
first inlined draft: chaining the base-ref fallbacks with `||` does not work,
because a pipeline's exit status is its last command's, so `base=$(git
symbolic-ref … | sed …)` reports success even when `git` failed and left the
variable empty. It now tests emptiness explicitly.

## Why the bundled skill could not simply be invoked

Model invocation of the bundled skill sits behind a runtime feature flag, observed
off in the CLI build current at the time of writing. While it is off, that skill
is user-invocable only: it does not appear in the model-facing skill list and the
`Skill` tool refuses it, so an agent can neither reach it nor be told to. `/commit`
behaves the same way.

Because this is a gated rollout rather than a fixed property, it may switch on in
a later release — at which point the bundled skill becomes model-invocable too,
but this skill still shadows it by name.

This skill carries a `description` and no `disable-model-invocation`, so it **is**
model-invocable: reachable as `Skill(skill="verify")`.

## Precedence

- A **project-level** `.claude/skills/verify/SKILL.md` would shadow this one. §4
  therefore directs cold-start recipes to `verifier-<surface>/` instead. An
  earlier revision wrote them to `verify/`, which meant the first successful cold
  start in any repository silently replaced this protocol with a bare build
  recipe — no claim, no control, no probe, no rubric. That path also did not
  match §4's own `verifier-*` search, so the skill could not find what it had
  told a previous session to write.
- Do not add a second `verify` skill elsewhere under `~/.claude/skills/`; the
  by-name lookup resolves to one winner and a duplicate makes it ambiguous which
  ran.

## Not done yet

`context: fork` (with `background: false` and `effort: high`) would run this in a
separate context from the work under review. The fields exist in this CLI build,
and the argument is that the verdict is otherwise produced by the same agent that
did the work and wants to be finished. It is unshipped because it changes the
execution model and has not been exercised on a real change — precisely the kind
of unverified claim this skill exists to refuse. Test it, watch the first two
runs, then decide.
