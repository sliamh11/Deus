# The control capture — obtaining one, and keeping it honest

Loaded on demand from `SKILL.md` §5.

## Acquiring the unchanged state

Ranked by how little they disturb what you are measuring.

**A second worktree at the base ref.** The default. Both states exist at once,
each with its own build output, and your working tree is never touched.

```bash
ctl=$(mktemp -d)                       # unique per run
git worktree add "$ctl" "$mb"          # $mb — the merge base §2 already resolved
# build inside "$ctl" with its OWN install/build step, then capture
git worktree remove --force "$ctl"     # when finished, before reporting
```

Three requirements, and the reasons they are not optional:

- **Use `$mb` from §2, don't re-derive it.** §2 may have resolved the base through
  a local ref or a targeted fetch; recomputing against `origin/<base>` here can
  fail on a repository whose scope resolved perfectly well, blocking the control
  capture for no reason. One resolution, one SHA, reused.
- **A fresh directory per run, removed afterwards.** Git keeps worktrees
  registered, so a fixed path makes a second or concurrent run fail there — and if
  that failure goes unnoticed, the next command captures a stale control from an
  earlier run. `git worktree list` shows what is still registered.
- **Build separately in each.** A shared build directory or module cache hands the
  control the treatment's artefacts. A shared build directory, a shared `node_modules`, or a
shared compiler cache silently gives the control the treatment's artefacts, which
is the failure mode most likely to manufacture a false PASS — the two runs agree
because they were, in the part that matters, the same binary.

**A behaviour flag.** Where the change sits behind one, flip it. Cheapest
possible pair and it holds every other variable fixed. Confirm the flag actually
gates the changed path rather than only its entry point.

**An existing documented reproduction.** The issue's own steps. Note that these
supply a *recipe*, not an artefact: unless you run them yourself, at a known
revision, in your environment, you have inherited someone else's evidence. Run
them.

**`git stash`.** Available, and the one to be careful with: nothing about a
verification reminds you to restore it. If you stash, restore before reporting —
and prefer a worktree, which cannot leave the user's uncommitted work parked
somewhere they did not put it.

## Before you begin the treatment

Four things recorded, or the comparison is not reproducible:

1. the control's revision (a SHA, not "main")
2. the exact invocation, identical to the one the treatment will get
3. the initial state — seed, fixture, database snapshot, cache condition
4. the raw artefact the control produced

Only then start the treatment. Where you cannot hold all four, the honest
destination is INCONCLUSIVE; discovering afterwards that the control was never
captured properly is how a verification quietly becomes a demo.

## State that carries between runs

The confound this file exists for. Measuring the control can consume the very
condition the treatment needs, and the resulting difference looks exactly like a
working change:

- **Counters and quotas** — rate limits, retry budgets, per-window allowances.
  Exhaust the window measuring the control and the treatment's 429 is yours, not
  the code's.
- **One-shot transitions** — migrations, first-run setup, lazy initialisation,
  "create if absent". The second run takes a different path by design.
- **Caches at every level** — filesystem, DNS, HTTP, compiler, module resolution,
  connection pools. A cold control against a warm treatment is a cache
  measurement wearing a performance claim's clothes.
- **Seeded data** — rows the control run wrote, consumed, or locked.
- **Ambient conditions** — a background job, a laptop that thermally throttled
  during the first run, a neighbour saturating the disk.

Countermeasures: re-seed or restore between runs; separate worktrees and build
outputs; alternate which side runs first, or randomise it, so any ordering effect
shows up as noise instead of as your result; and where you cannot isolate a piece
of state, say in the report which one and why.

## The receipt

A control that violates the claim beside a treatment that satisfies it is also
consistent with: two different builds, two different environments, a stale
artefact, or never having reached the changed lines at all.

So keep something only the new path could have produced — a log line unique to
it, a version string, an error only it raises, a marker you added and then
removed. Cheap to obtain, and it converts a circumstantial pair into a causal
one.

## When there is genuinely no "before"

For a purely additive change the absence *is* the control, and it is capturable
rather than merely assertable:

| Added | The control's artefact |
|---|---|
| A flag | the old binary rejecting it — unrecognised-option error, non-zero status |
| A route | the old server's 404 |
| An export | the old package failing to compile or import it, error naming the member |
| A subcommand | the old entrypoint's usage message, which does not list it |

Capture that. "It did not exist before" written as prose is an assertion; the
error message proving it is evidence.

Where the old state cannot even be built, say so and report INCONCLUSIVE. Never
present a treatment-only run as a verification.
