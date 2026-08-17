---
name: verify
description: "Prove or disprove that a change does what it claims, by exercising it at its real user-facing surface and comparing a control capture against a treatment capture. Use before committing or handing off a nontrivial behavioural change, for bug-fix reproduction, manual QA or smoke testing, acceptance-criteria checks, or when asked to show evidence that something works. Covers any change something outside it can observe — including a public type or API signature, whose surface is the compiler as an external consumer meets it. Skip only when nothing observes the change at all: prose, comments, or tests alone."
---

# Verify

A verification stands on two legs. Remove either one and it is not a verification.

- **Observation at the real surface.** The claim is exercised where a user meets
  it, and what the software actually emitted is captured.
- **A controlled contrast.** A second capture, from the unchanged state,
  measured identically — so the difference is attributable to the change and
  not to the weather.

One leg alone has a name. A treatment capture with no control is a demo. A
control/treatment pair measured somewhere other than the real surface is a
laboratory result about code nobody runs.

## What does not count

Three substitutes, each of which feels like evidence and is not:

1. **Suites and typecheckers.** They re-assert what someone already wrote down.
   They cannot speak to a claim nobody has encoded yet — which is precisely the
   claim under verification. Read a test as a specification if it helps; then go
   exercise the software.
2. **Calling the changed function directly.** Reaching past the public boundary
   to invoke an internal and printing the result is a test you authored moments
   ago, graded by its author. The function behaved as written; reading it told
   you that. Nothing that ships was exercised.
3. **Narrating the diff.** A description of what changed is a claim awaiting
   verification, never its discharge.

## 1. Fix the claim, and fix what would refute it

Two sentences, written **before** anything is measured, and not revised
afterwards:

> **Claim.** Under `<condition>`, at `<surface>`, `<observable>` `<holds>`.
> **Refuted if.** `<the specific observation that would end this as a failure>`.

Worked examples:

> Claim: invoked with an empty `--from`, the CLI exits non-zero and names the
> flag in its message. Refuted if: it exits 0, or the message omits `--from`.

> Claim: the twelfth request inside a minute receives 429 carrying
> `Retry-After`. Refuted if: a twelfth request succeeds, or the header is
> absent or unparseable.

> Claim: p95 cold start on this host is at or under 400 ms across 10 trials.
> Refuted if: p95 exceeds 400 ms, or the trials scatter too widely to place it.

The refutation sentence is the whole point of writing this down. Fixing the
failure condition in advance is what stops the bar from drifting to wherever the
output happened to land. Quote it verbatim in the report.

**Where the claim comes from.** Intended behaviour is owned by the request, the
issue, or the acceptance criteria — not by the diff. The diff is authoritative
only about what changed. Where the two disagree, that disagreement is the
finding. If no external statement of intent exists, derive the claim from the
diff and label it **inferred**: you may then verify that the observed change is
real, but you may not present it as proof the software does what was wanted.

Unmeasurable claims ("cleaner", "more robust", "better structured") carry no
refutation condition and so cannot be verified. Obtain a measurable one, or ask;
if you cannot ask — you are running as a subagent — report INCONCLUSIVE naming
the claim you could not pin down.

## 2. Establish scope

Resolve the real base branch first, then measure from the merge base. A branch may
be many commits, and the change may not be committed at all:

```bash
# The base is the PR's own, else whatever the remote calls its default.
# Test emptiness rather than chaining with ||: a pipeline's exit status is the
# LAST command's, so `base=$(git ... | sed ...)` succeeds even when git failed.
base=$(gh pr view --json baseRefName --jq .baseRefName 2>/dev/null)
[ -n "$base" ] || base=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')

# A named base is authoritative: use it, fetch it, or stop. Never substitute a
# trunk for a base you were actually told — that compares a different branch.
ref=""
if [ -n "$base" ]; then
  for c in "origin/$base" "$base"; do
    git rev-parse --verify -q "$c" >/dev/null 2>&1 && { ref="$c"; break; }
  done
  if [ -z "$ref" ] && git fetch -q origin "$base" 2>/dev/null; then
    git rev-parse --verify -q FETCH_HEAD >/dev/null 2>&1 && ref=FETCH_HEAD
  fi
else
  # Only with no base named anywhere is a conventional trunk a fair guess.
  for c in origin/main main origin/master master; do
    git rev-parse --verify -q "$c" >/dev/null 2>&1 && { ref="$c"; break; }
  done
fi

mb=""
[ -n "$ref" ] && mb=$(git merge-base HEAD "$ref" 2>/dev/null)

if [ -z "$mb" ]; then
  echo "STOP: could not resolve base '${base:-unnamed}' (tried '${ref:-nothing}')."
  echo "Ask which branch to compare against; do not guess one."
else
  git rev-list --count "$mb"..HEAD            # how many commits
  git --no-pager diff --stat "$mb"..HEAD      # committed work
  git --no-pager diff --stat HEAD             # staged + unstaged
  git ls-files --others --exclude-standard    # untracked — also part of the change
  git rev-parse "$mb" HEAD                    # the two SHAs, for the report
fi
```

**If it prints STOP, stop.** An unresolved base is not a scope of zero commits —
it is an unknown scope, and continuing past it reports an empty diff for a change
that may be substantial. Repositories built on `master`, or with no remote and a
differently-named trunk, land here legitimately: ask which branch to compare
against rather than guessing.

Two traps those commands exist to avoid. **`@{u}` is the wrong base:** an upstream
is usually the remote copy of this same branch, so diffing against it hides every
commit already pushed. **`git diff HEAD` is not the whole change:** it omits
untracked files, so a newly added file is invisible in it.

Carry both SHAs and whether the tree was dirty into the report — those are what
let someone else reproduce the comparison. A large diff that truncates goes to a
file you then read. No repository → the scope is whatever the requester named; ask
if they did not.

## 3. Locate the surface

The surface is wherever something outside the change first observes it. Pick the
row; the mechanics live in [reference/surfaces.md](reference/surfaces.md).

| The change surfaces at | Observe by |
|---|---|
| A terminal (CLI, TUI) | issuing the command, capturing the screen |
| A socket (server, API) | sending the request, capturing the whole response |
| A window (GUI, web) | driving it headless, then **looking at** the screenshot |
| A package boundary | consuming the published export as an outside caller would |
| A compiler | compiling an external consumer against the shipped declarations |
| An agent (prompt, role spec) | running the agent, capturing what it did |
| A CI runner | triggering the real event, then confirming the run's head SHA is yours — a manual dispatch defaults to the default branch and can grade a different version of the workflow |

**Internals are not surfaces.** Every internal has a caller, and following the
callers terminates at one of the rows above. The observable behaviour of a
commit-blocking hook is not what its function returns — it is whether the commit
is refused when you type it.

**Nothing observes it → SKIP,** one line saying why. Prose, comments, and
declarations that emit nothing and that no consumer compiles against qualify. A
diff of only tests qualifies too: those are the author's evidence, and re-running
them re-runs CI. Where a diff carries both source and tests, verify the source.

Do not stretch SKIP to cover a change you merely found inconvenient to reach. A
public type with no runtime emission still has the compiler as its surface, and
an external consumer that fails to compile before and succeeds after is a real
control/treatment pair.

## 4. Obtain a handle on the software

Getting the software built and running is `/run`'s subject, not this one. Invoke
it. It already knows the per-project-type recipes and will find a project skill
that supersedes them.

Before falling back to anything generic, look for what this repository has
already committed — at its root and at every directory level the diff touches,
because within a multi-package tree whatever makes a given package runnable
tends to be stored next to it:

```bash
ls .claude/skills/
ls <each-touched-dir>/.claude/skills/
```

A `verifier-*` skill matching your surface is the repository's own
evidence-capture protocol; prefer it, because a reviewer can replay what it
records. Mismatched surface, try the next. Where it misdirects you over plumbing that
has nothing to do with what you are checking, it has rotted — say so, and never
let its decay become the change's verdict.

Nothing to lean on, and `/run` cannot get there either? Timebox the cold start
to roughly fifteen minutes, then report **BLOCKED** with the exact point of
failure.

If a cold start did succeed, write the recipe down **after reporting** —
`.claude/skills/verifier-<surface>/SKILL.md`, beside the code it applies to.
Commands, worthwhile flows, traps; nothing more. Amend an existing one only where
it misled you.

Two rules about that file. **Never write during a verification** — a new file
lands in the very diff being measured, and for a prompt or config change it can
alter the behaviour under observation. And **never name it `verify`**: a
project-scope skill of that name shadows this one, so the next run would load a
build recipe stripped of the claim, the control, the probe, and the verdicts —
strictly worse than having nothing. The `verifier-` prefix is also what the
search above actually matches.

## 5. Capture the control

Skipped more often than any other step, and the reason unfounded PASSes ship.
How to obtain one, what to record before starting the treatment, and the state
that leaks between runs: [reference/control.md](reference/control.md).

**Name the contrast before measuring it.** Old-breaks-new-works is one shape
among several, and assuming it universally rejects correct work:

| The change claims | Its contrast |
|---|---|
| A defect removed, a capability added | control violates the claim; treatment satisfies it |
| Behaviour preserved (refactor, swap, migration) | the two agree, inside a margin you state |
| Failures made rarer | the **rate** over repeated trials falls — one clean control run refutes nothing |
| Better performance | named statistic across enough trials to separate the distributions |
| Depth of defence | the guard is reached under an injected fault that otherwise masks it |
| Wider compatibility | previously-working environments still work **and** the newly-targeted one flips |

Where the control contradicts the contrast you named, that is information, not a
stop sign: confirm you are reaching the changed path at all, and that the
measurement could detect the effect if it were there. Then either re-state the
contrast or report what you actually found.

**Measure both sides the same way** — same invocation, same inputs, same
environment, same warm-up. A control obtained differently is not a control; it is
a second variable wearing one's coat.

**Do not let measuring the control disturb what the treatment needs.** Quotas,
rate-limit counters, one-shot migrations, caches, seeded rows, and
first-invocation warm-up all persist across runs — consume the counter while
measuring the control and the 429 you then attribute to the fix was your own
doing. Re-seed between runs, keep the two builds separate, and say what you
reset. The full list, and the countermeasures, are in
[reference/control.md](reference/control.md).

**Control genuinely unobtainable?** Say so and report INCONCLUSIVE, unless the
change is purely additive — then the absence *is* the control, and it is
capturable: the unrecognised flag, the 404, the compile error naming the missing
export. Assert an absence and you have asserted, not verified.

## 6. Drive the treatment

Shortest route that puts the changed lines into execution. Then read your own
plan back: if every step builds, typechecks, or runs a suite, you have planned a
CI re-run. Replace one of them with something an observer at the surface could
witness, or report BLOCKED.

Go through the interface, never around it. Components passing in isolation says
nothing about the joins, and joins are where this fails. Where users press a
button, press the button.

Stochastic surface — agents, concurrency, anything network-adjacent — is not
settled by one run each. Fix inputs and scoring in advance, run the pair
repeatedly, and report trials and rates ([reference/measurement.md](reference/measurement.md)).
One fortunate control failure beside one fortunate treatment success is a fully
compliant false PASS.

**Prove you reached the changed code.** A control that fails and a treatment
that passes is also consistent with two different environments, or a stale
build, or a cache. Keep something that could only come from the new path: a log
line it alone emits, a version string, an error only it raises. Without that
receipt the pair is circumstantial.

**Irreversible operations** — deleting, publishing, sending, writing outside the
workspace — do not get driven live without a dry-run or a disposable target.
Exercise what is safe, then name the path you left alone. A required destructive
effect you never exercised is BLOCKED or INCONCLUSIVE, never PASS.

## 7. Compare

Set the two captures beside each other as artefacts — exit statuses, response
bodies, screen dumps, images, trial series. Not as your recollection, which is
not evidence.

Numbers carry four figures: control, treatment, difference, and the threshold
from §1. Two samples cannot distinguish a real effect from cache warming,
thermal drift, or a noisy neighbour — if the difference does not clear the
spread, it has not been shown. Declare the statistic and trial count before
collecting, alternate which side runs first, and keep cold and warm trials
apart: [reference/measurement.md](reference/measurement.md).

Name the confounds you can think of — carried-over state, unrelated commits
riding along in the treatment, an environment that shifted between captures —
rather than waiting to be asked. Where the treatment contains changes beyond the
one claimed, either isolate the target or say plainly that the verdict covers
the whole branch.

## 8. Probe the edges

Confirming the claim is the first half. The claim is what the author already
believed; your value is what they had not thought to check.

You know exactly what moved, so push on what sits beside it, at the same surface:

- **A new flag or option** — empty, doubled, contradicted, misspelt (does the
  error name it?)
- **A new route or handler** — wrong verb, malformed body, absent required
  field, oversized payload
- **A reworked error path** — the neighbouring errors it did *not* touch: did
  the rework reach them, or only the one in the diff?
- **Anything interactive** — interrupt mid-operation, resize, paste rubbish,
  hammer a key, escape at the wrong moment
- **Anything stateful** — twice in a row, on top of stale state, from two
  sessions at once

Choose what the change points at; this is not a list to exhaust. **At least one
probe, and its result, always.** One that finds nothing still earns its line —
recording that empty `--from` produces a clean `error: --from requires a value`
and exit 2 tells the author what is now known to hold, which a bare PASS cannot.

A probe that misbehaves gets re-run against the control before it can drive a
FAIL. Identical misbehaviour on both sides is pre-existing, and pre-existing is a
finding, not a regression.

## Evidence discipline

What was captured is evidence; what you remember is not. Something surprising
appears — capture it, record it, and decide whether it belongs to the change or
the environment. Breakage you did not cause is still a finding.

Isolate shared state: private tmux socket, port 0, a fresh temporary directory.
The host is not yours alone.

Artefacts on disk are optional, one directory per claim, holding the claim, the
two captures, and the verdict. Where they would contain credentials, personal
data, prompts, or images of private material, keep the minimum inline and leave
the rest unwritten unless the requester agrees.

**Evidence has to arrive.** A path is evidence only to someone who can open it.
Screen dumps and response bodies travel inside the report; a bare path works only
where the reader shares your filesystem. On a remote surface with a file-sending
tool available, send the images and say in the report what was sent.

## Report

Inline, in the final message:

```text
## Verification: <what changed, one line>
**Verdict:** PASS | FAIL | INCONCLUSIVE | BLOCKED | SKIP
**Claim:** <verbatim from §1>
**Refuted if:** <verbatim from §1 — unchanged since>
**Scope:** <base SHA>..<head SHA>, <n> commits, <clean|dirty tree>
**Handle:** <verifier or run skill used, or cold start; what was launched>
**Reached the changed code:** <the receipt — log line, version, unique error>

### Control vs treatment
| | Control (<ref>) | Treatment (<ref>) |
|---|---|---|
| <observable> | <artefact> | <artefact> |
<numeric claims add difference + threshold + trial count>

### Steps
Things done to running software, and what came back. Building, installing and
checking out are preparation, not steps.
1. ✅/❌/⚠️/🔍 <what was done> → <what was observed>
   <the software's own output>

### Findings
<What stood out. Bugs, and also friction, surprises, whatever a newcomer would
stumble over. The bar is low: a pause while exercising the software earns a
line. It has to be your pause, from running it — relaying a red check or
someone else's comment is not an observation. Claim/diff disagreements,
pre-existing breakage and environment notes belong here. Every probe gets a
line, including the ones that held. Lead with ⚠️ where the reader should stop
and look.>
```

🔍 marks a probe. **A report with no 🔍 cannot be PASS** — it is an incomplete
verification, whatever the happy path showed.

## Verdicts

Decide on the evidence's standing, not on how confident you feel. In order:

1. Treatment unreachable → **BLOCKED**
2. Nothing observes the change → **SKIP**
3. Either capture untrustworthy → **INCONCLUSIVE**
4. Trustworthy captures meet the named contrast → **PASS**, else → **FAIL**

- **PASS** — software exercised at its surface, trustworthy captures satisfy the
  §5 contrast, the refutation condition did not occur, and at least one probe is
  recorded.
- **FAIL** — exercised, captures trusted, and the contrast is not met: wrong
  direction, short of threshold, collateral breakage, or intent and diff
  disagreeing about the very observable claimed.
- **INCONCLUSIVE** — it ran, but the evidence cannot bear a verdict: no valid
  control, noise larger than the difference, environments that drifted apart,
  state carried between runs, a measurement that failed. **Name what would
  settle it** — the access, data, trial count, or rerun that decides. Without
  that sentence this is an abandoned verification wearing a verdict's clothes,
  and it is the exit an agent takes when it would rather be finished.
- **BLOCKED** — the treatment could not be reached at all: build broken,
  dependency absent, nothing would start. Says nothing about the change. A
  *control* you cannot obtain is INCONCLUSIVE (§5), not this. Do not claim it
  before searching the skills beside the touched code, where the unlock usually
  is. Say where it stopped.
- **SKIP** — nothing observes the change. One line why.

**Nothing partial passes, but partial does not automatically mean FAIL** — route
it by the same ladder. Where several claims are in scope: any one of them validly
falsified makes the whole thing FAIL. Where none was falsified but one was never
measured, the aggregate is INCONCLUSIVE, or BLOCKED if that one's surface could
not be reached at all — and name which claim is outstanding. Only an author
placing a claim outside the scope removes it from the count.

**Ambiguity never resolves upward into a PASS.** Sound captures falling short is
FAIL, stated plainly. Unsound captures is INCONCLUSIVE with the settling step
named. A shipped false PASS costs far more than another look — and downgrading a
real defect to INCONCLUSIVE to avoid delivering the news is the same failure in
the other direction.
