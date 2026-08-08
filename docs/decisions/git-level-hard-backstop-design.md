---
name: Git-level hard backstop for `main` (LIA-522) — design; workflow + check script implemented (LIA-536), ruleset not yet activated
description: >
  Unifies LIA-467/517's dead-end text-parsing redesign with the OPA ADR's own
  deferred "later, separate phase" for a git-level enforcement point. Reuses
  the existing OPA decision endpoint; adds a non-bypassable GitHub ruleset as
  the actual enforcement mechanism.
type: decision
tags: [wardens, opa, git-level, github-ruleset, lia-522, lia-467, lia-517, security]
date: 2026-08-07
---

# Git-level hard backstop for `main` — design

**Date:** 2026-08-07
**Status:** Design complete; the `attestation-verify` workflow and check script are implemented
(LIA-536). The `main-attestation-backstop` ruleset is not yet created or activated — see "Not yet
started" below for what remains.
**Scope:** GitHub repository configuration (`sliamh11/Deus`), a new `.github/workflows/` check, a
new small query script reusing `scripts/warden_policy/opa_client.py`. No change to
`scripts/codex_warden_hooks.py`, `scripts/warden_policy/command_parser.py`, or
`scripts/hermes_warden_gate.py` — those are actively owned by a separate, in-progress LIA-467
mitigation round in this same repo as of this writing; this design deliberately introduces new,
independent infrastructure rather than touching that surface.
**Related:** LIA-467, LIA-517 (Backlog — this design supersedes their text-parsing direction, not
extends it), `docs/decisions/opa-warden-attestations-v1.md` (the shared policy substrate this
design consumes, not duplicates), LIA-527 (this design's real dependency — see "Dependency on
LIA-527" below, discovered while writing this, not assumed going in).

## 1. Why the existing enforcement can't be the hard backstop

Two independent threads converged on the same missing piece:

- **LIA-517**: four attempts (10 real review rounds) to fix cross-repo `repo_root` resolution via
  better command-text parsing all failed against a different shell-evasion mechanism each round
  (subshells, dynamic `cd`, wrapper builtins, env redirection, `--work-tree` collapsing).
  Converged conclusion: parsing an arbitrary shell command string to decide whether it's a
  git-commit needing review is not a soundly implementable mechanism *class* — not a bug to patch
  further.
- **The OPA ADR's own "What this is, and isn't" section**: the literal-`git`-token classifier
  bypass and the authorization-to-commit race window are both named as requiring "a git-level
  enforcement point — a later, separate phase" that was never designed.

Both gaps share one root cause: every enforcement point in this repo today — Claude Code's
PreToolUse hooks, Hermes's `pre_tool_call` OPA shim — sits **before** the actual `git`/`gh`
operation, inferring what's about to happen from the *driving agent's own command text or process
state*. Anything that controls its own command text (a sufficiently motivated local agent,
prompt-injected or otherwise) can eventually find a shell construction the classifier doesn't
recognize. A sound backstop cannot depend on classifying intent from text at all — it must observe
the actual git objects being written, at the point they're actually accepted, by something the
local agent's shell tricks cannot reach.

## 2. A git-level check does not automatically mean a hard backstop — verified against this repo's actual merge path, not assumed

This distinction is the main finding of this design pass, and it's the reason the obvious "add a
required CI check" answer is insufficient on its own.

**`gh pr merge --admin` bypasses branch protection and rulesets by design**, and this repo's own
process discipline (`.claude/rules/core-behavioral-rules.md`: "`--admin` is the accepted solo-dev
landing path once all REQUIRED checks are green... not emergency-only") relies on that bypass —
it exists specifically because a solo dev cannot obtain the "1 required approving review" a
protected branch would otherwise demand from someone else. Verified live against this exact repo,
not assumed from GitHub's general docs:

```
$ gh api repos/sliamh11/Deus/branches/main/protection
{"required_status_checks": {...7 contexts...}, "enforce_admins": {"enabled": false}, ...}

$ gh api repos/sliamh11/Deus/rulesets/14656014   # "main-branch-rules", enforcement: active
{"rules": [deletion, non_fast_forward, copilot_code_review, pull_request(1 review), required_signatures],
 "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
 "current_user_can_bypass": "always"}
```

`enforce_admins.enabled: false` on the classic branch-protection config means the 7 existing
required status checks are **already** admin-bypassable today — a required CI check added the
same way would inherit that same bypass. The newer ruleset (`main-branch-rules`) is stricter in
shape but has its own `bypass_actors` entry granting the repository-admin role `bypass_mode:
"always"`, confirmed live as `current_user_can_bypass: "always"` for the account this design was
verified under — so *every* rule in that ruleset (no force-push, no deletion, 1 review, signed
commits) is also unconditionally bypassable by the same account that runs `--admin` merges today.

**This means: naively adding "a required status check that verifies attestation" achieves nothing**
new. The exact same `--admin` flag that already bypasses the 7 existing checks would bypass an
8th one identically. A real hard backstop needs a rule with **empty `bypass_actors`** — enforced
even against the repository owner's own admin action — which GitHub's newer Rulesets API supports
per-ruleset (unlike classic branch protection's single all-or-nothing `enforce_admins` toggle).

## 3. Design

### 3.1 A new, separate GitHub ruleset with no bypass actors

A second ruleset (not a modification of the existing `main-branch-rules`, to avoid touching that
ruleset's already-relied-upon admin-bypass behavior for the review-count rule — a deliberate
separation of concerns, not an oversight):

```json
{
  "name": "main-attestation-backstop",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "required_status_checks": [{"context": "attestation-verify", "integration_id": 15368}],
        "strict_required_status_checks_policy": true
      }
    }
  ],
  "bypass_actors": []
}
```

`integration_id: 15368` — **not `null`, found wrong by the required GPT plan-review co-gate**: a
`null` `integration_id` matches a status/check with the right `context` string from *any* source,
including a direct, unauthenticated-by-workflow call to the Statuses or Checks API
(`gh api repos/sliamh11/Deus/statuses/<sha> -f state=success -f context=attestation-verify`) made
by the same `repo`-scoped credential named in §3.4 — no workflow file edit, no PR, no code review
of anything, just one API call forging a "success" that was never actually produced by querying
OPA. Binding `integration_id` to the specific app that publishes the real check closes this
directly: `15368` is confirmed live as the GitHub Actions app ID this repo's own seven existing
required checks already use (`gh api repos/sliamh11/Deus/branches/main/protection` →
`required_status_checks.checks[].app_id`, every entry `15368`) — so only a check genuinely
published by GitHub Actions (i.e., an actual workflow run, not an arbitrary API call) can satisfy
`attestation-verify`. This closes the *direct-status-forgery* vector specifically; it does not
close the *edit-or-add-a-workflow-file-that-lies* vector — a repo-write credential can still
modify `.github/workflows/attestation-verify.yml` (or add a new workflow) to report a fake success
under GitHub Actions' own attribution, which is the same root cause §3.4 already names
(credential separation for routine automation) rather than a new, separate gap.

`bypass_actors: []` closes the bypass mechanisms GitHub's merge UI/API exposes: once active, no
`--admin` flag, no "merge without waiting for checks" option, no force-push (already separately
blocked by the existing ruleset's `non_fast_forward` rule) can land a commit on `main` through the
normal merge path without the `attestation-verify` check reporting success — the check's
evaluation happens entirely on GitHub's servers against the actual proposed merge commit. This is
necessary but not sufficient on its own — §3.4 names the gap it does not close, and the paragraph
above names a second, narrower gap this `integration_id` binding does close.

### 3.2 The `attestation-verify` check reuses the existing OPA decision endpoint — extended by a new Phase-4 Rego rule (LIA-530)

The check does not invent a new attestation scheme. It calls the *same* `deus.wardens.decision`
Rego rule (`scripts/warden_policy/policy/guardrails.rego`, queried via
`scripts/warden_policy/opa_client.py`'s existing `query_decision`) that
`scripts/hermes_warden_gate.py` already calls for Hermes's own commit gate — the shared policy
substrate this whole roadmap chose OPA specifically to provide. **Corrected (was: "Zero new Rego,
zero new attestation store, zero new schema" — stale, contradicted by LIA-530's implementation):**
the decision now includes a dedicated attestation-verify block within that same Rego file
(`opa-warden-attestations-v1.md`'s "### Phase 4" section), with its own `operation` value.
**Corrected (was: "not yet written"; done since LIA-536, see §5):** a small script
(`scripts/warden_policy/attestation_verify_check.py`) builds the `opa_input` shape that block
expects: `operation: "attestation.verify"` (a distinct operation from the `git.commit` shape
Hermes's own adapter builds), `gate: "code-review"`, `repo_id`, `subject_key: git-tree:<the PR's
HEAD commit's tree sha>` (**corrected here too — was wrongly "merge commit's" in an earlier
draft**; the CI check resolves the tree of the PR's head commit specifically, fetched but never
checked out into the base-branch checkout, per §3.3/LIA-536's own design decisions — the merge
commit doesn't exist yet at the point this check needs to evaluate), `expected_generation`, plus
`expected_cc_generation` (new, checked against the isolated `data.warden_cc_attestations`
document's own `generation` counter) — and exits non-zero unless `result.allow is True`. **Both
`expected_generation` and `expected_cc_generation` must be read from their respective on-disk
ledgers, never from OPA's own served snapshot** — sourcing either from the snapshot being queried
would make its corresponding `supported`/`cc_supported` freshness guard tautologically true and
silently void it, exactly the "machine-local-input precondition" the design history behind this
rule already named (Fix 7, carried unchanged through every design revision). **Mechanism corrected
here (LIA-536 implementation deliberately diverges from a shared-lock-across-query pattern for
this specific CI script)**: `hermes_warden_gate.py`'s synchronous adapter holds one shared lock
across the whole read-then-query sequence, safe only because its own OPA call is tightly bounded;
`attestation_verify_check.py` instead reads both generations under brief, separately-released
locks, queries OPA with no lock held (this script's own timeout budget is deliberately more
generous, a CI step rather than a hook), then re-acquires both locks to confirm neither generation
moved, retrying once (bounded) before failing closed — an optimistic-concurrency approach chosen
specifically because holding two ledger locks across a longer CI-scale OPA call would stall every
concurrent local session's own commit-gate writes. See `opa-warden-attestations-v1.md`'s Phase 4
section for the full query contract and decision logic rather than duplicating it here.

### 3.3 Where the check runs, and why: self-hosted runner reads the local ledger directly

The attestation ledger (`~/.config/deus/guardrails/attestations-v1.json`) and the OPA daemon
(`com.deus.warden-opa`, loopback-only) live on the local machine only — a GitHub-hosted runner has
no path to either. Two ways to close that gap were considered:

- **Committed, cryptographically-signed attestation artifact** (sign at SHIP-time with a locally-held
  key, commit alongside the change, verify the signature + tree binding in CI against a public key).
  More portable, works on GitHub-hosted runners, but is meaningfully more new infrastructure
  (keypair generation and management, a signing step wired into every verdict-marking call site,
  a verifier). Not chosen for this pass — flagged as the natural next hardening step if the trust
  model ever needs to stop depending on this one machine (see "Alternatives considered").
- **A self-hosted GitHub Actions runner on this same machine** (chosen). The `attestation-verify`
  job runs where the ledger and the OPA daemon already live, querying `http://127.0.0.1:8181`
  exactly as `hermes_warden_gate.py` does today — reusing the existing trust root (this machine)
  rather than manufacturing a new one, and needing no new secret material at all.

**Named, real residual risk, not glossed over**: this repository is **public**
(`gh repo view sliamh11/Deus --json isPrivate` → `false`), and GitHub's own guidance against
self-hosted runners on public repos is specifically about workflows triggered by `pull_request`
from a fork — an untrusted external contributor's PR can execute arbitrary code on the runner
before any human looks at it. Mitigations: (a) this repo has no history of external contributor
PRs, and GitHub's documented default behavior is to require manual approval before running
Actions workflows for first-time outside contributors — **this specific setting was not
independently re-verified against this repo's own configuration in this session** (the
`repos/{owner}/{repo}` and `actions/permissions` API responses checked did not surface it under a
quick pass; a `gh api` endpoint that reads it directly was not found) — treat this mitigation as
"assumed GitHub default, not confirmed for this repo," not verified fact, unlike every other
claim in this document; (b) the `attestation-verify` workflow should be scoped to trigger only on
`pull_request_target` (runs with the base branch's workflow definition, not the PR's, closing the
"attacker edits the workflow file itself" variant) combined with an explicit actor check — **found
wrong in this design's first draft by the required GPT plan-review co-gate, corrected here across
two review rounds, not left as originally stated**: a non-owner PR must make the check **fail**
(non-zero exit / explicit `conclusion: failure`), never report `neutral` or `skipped`. GitHub's
branch-protection required-status-check evaluation treats both `neutral` and `skipped`
conclusions as *satisfying* the requirement — an earlier version of this design had the actor
guard report `neutral`/`skipped` for anyone but the owner specifically to avoid running untrusted
code on the runner, which would have meant any external PR's `attestation-verify` check trivially
"passed" with no attestation ever checked, silently defeating the exact protection §3.1's
`bypass_actors: []` exists to provide, for exactly the untrusted-PR case this mitigation was meant
to guard against. **Mechanism, stated precisely** (a second round of review caught that even the
first correction was underspecified): the actor guard must NOT be implemented as a job-level or
step-level `if:` conditional gating whether the check-failure step runs — a GitHub Actions
job/step whose `if:` evaluates false itself reports conclusion `skipped`, which is *exactly the
same passing conclusion* this fix exists to eliminate, just moved one level down. Instead: an
unconditional early step runs on every invocation, checks the actor inline, and explicitly calls
`exit 1` (shell) / `core.setFailed()` (JS action) for a non-owner actor before any OPA-query logic
runs — the step itself is never skipped, only its internal branch differs, so its conclusion is
always `success` or `failure`, never `skipped`, regardless of who triggered it. The actual OPA
query only executes past that point, so untrusted code still never runs real work on the runner;
only the *gating mechanism* changes, not the intent behind it. An external PR simply cannot pass
this check at all under the current design, an acceptable v1 posture given this repo has no
external-contribution workflow today, not a gap glossed over; (c) associating the check's result
with the *PR's actual head commit* — not the base-branch commit `pull_request_target`'s default
`GITHUB_SHA` context resolves to, a separate, well-known `pull_request_target` gotcha also found
by the same review pass — requires the job to explicitly create/update a Check Run via the Checks
API with `head_sha` set to `github.event.pull_request.head.sha` (a standard, documented pattern
for exactly this "run trusted workflow code, evaluate PR content, report against PR SHA" shape),
rather than relying on the workflow's own implicit job-status-to-check mapping, which would
otherwise report against the wrong commit and never actually satisfy the ruleset for the PR being
merged; (d) the runner's job should have no access to any repository secret beyond what
`opa_client.query_decision` already needs (nothing — it's an unauthenticated loopback HTTP call).
This is real, reduced-but-nonzero, partially-unconfirmed risk (the `neutral`/`skipped`-satisfies-
required-checks and `pull_request_target`'s base-branch `GITHUB_SHA` behaviors are confirmed
against GitHub's public documentation, not this repo's own live state, unlike the repo-specific
claims elsewhere in this document), appropriate to record here rather than assume away —
confirming mitigation (a) against this repo's actual settings, and implementing (b)/(c) correctly
per the precise mechanism above, are both required follow-up before activating the self-hosted
runner, not optional polish. Three of these four points (b's original neutral/skipped mechanism,
b's own follow-up if: conditional footgun, and c) were themselves found wrong across two rounds of
adversarial review, not designed correctly the first time — recorded plainly, matching this
document's own established practice of not smoothing over its own mistakes.

### 3.4 Who can defeat this by disabling the ruleset itself — a real, named gap, not closed in this pass

`bypass_actors: []` closes the *bypass* path GitHub's merge UI/API offers (`--admin`, "merge without
waiting for checks"). It does not close a *different* path: whoever holds a credential with
`repo`-scope `gh` access can call `PUT`/`DELETE` on the ruleset's own admin endpoint directly,
disabling or deleting it, then merging normally — no clever shell construction needed, just one
`gh api` call. Verified live in this session, not hypothesized: `gh auth status` shows this
session's ambient token scoped `gist, read:org, repo, workflow`; `repo` scope is what this same
session already used to read `repos/sliamh11/Deus/rulesets/14656014`, and per GitHub's own
documentation that scope is sufficient for ruleset administration on a repo the token's account
owns. **This is the same credential every routine local `gh pr merge`/CI-trigger operation in this
repo's existing automation already runs under** — Claude Code's and Hermes's own dispatched
sessions, not some separately-privileged actor. A locally-running agent doesn't need to defeat the
`attestation-verify` check at all; it can simply remove the rule that requires it.

This is a strictly easier, structurally different attack than everything §1-§3.2 defend against
(no shell evasion, no OPA-decision manipulation — just one authenticated API call), and this design
does not close it. The correct fix is credential separation: routine agent-driven `gh` operations
(PR creation, `gh pr merge`, CI triggering) should run under a credential that structurally lacks
repository-administration permission (a fine-grained PAT scoped to exactly the operations agents
actually need, with **no** "Administration" repository permission — which is what ruleset
management requires) **and no ability to write `.github/workflows/*`** — found necessary by the
same GPT review pass that flagged §3.1's workflow-file-lying vector: stripping only
"Administration" closes the ruleset-disable path but leaves a credential that retains the ambient
`workflow` OAuth scope (confirmed present today via `gh auth status`, quoted above) free to edit
`attestation-verify.yml` itself to fabricate a success, which still runs as a real GitHub Actions
job and so still carries the real `app_id: 15368` §3.1's `integration_id` binding checks for —
closing the ruleset-disable vector alone does not close this one, they need to be closed together,
by the same credential-separation effort, not treated as one fix covering both. Ruleset changes
similarly require a separately-held, broader credential the user's own interactive `gh`/browser
session uses, never exposed to an agent's environment. That re-architecture is real, non-trivial,
security-sensitive work spanning however this repo's automation currently obtains its `gh` token
(out of scope to discover and change in this design pass) — named here as required follow-up, not
solved. **Until that credential separation exists (covering both Administration permission and
workflow-write capability), this design's `bypass_actors: []` protects against the bypass mechanisms GitHub's UI/API exposes
for merging, but does not protect against an agent with the same standing `gh` access the user's
own automation already grants it choosing to administer the ruleset out of the way instead.** This
is the honest current ceiling of what a git-level backstop achieves on a single-operator setup
where the automation and the administrator share one credential — recorded plainly rather than
oversold, the same way §3.3 names the self-hosted-runner risk rather than assuming it away.

**Stated plainly, not buried**: until credential separation exists, activating this ruleset adds
real protection against the GitHub merge-UI/API bypass surface (accidental `--admin` use, human
error, a well-behaved agent that simply follows the merge flow as designed) but adds **no**
additional protection against the specific threat model §1 names as the whole reason this design
exists — a sufficiently motivated or prompt-injected local agent — since disabling the ruleset via
one `gh api` call is strictly easier than any shell-evasion trick LIA-467/517 spent 10 rounds
failing to classify away. This design closes the *text-parsing-is-unsound* problem completely; it
closes the *local agent with standing repo-admin credentials* problem only once §3.4's follow-up
lands.

### 3.5 Fail-closed

Matching every other gate in this system: OPA unreachable, malformed response, or the query script
itself crashing must all report the check as **failed** (non-zero exit), never skipped or
defaulted to pass. Since `bypass_actors` is empty, a failed/errored check genuinely blocks the
merge — there is no fallback path, which is the entire point of a hard backstop, but also means an
OPA daemon outage on this machine blocks all merges to `main` until resolved. Accepted consequence,
consistent with this system's existing fail-closed philosophy (`hermes_warden_gate.py`'s own shim
fails closed identically); not treated as a defect to design around in this pass.

### 3.6 Dependency on LIA-527 — and two more dependencies this pass and a later one found underneath it

`deus.wardens.decision`'s `valid_ship` rule reads `data.warden_attestations.latest[repo_id]
["code-review"][subject_key]` — populated today only by Hermes's own gate (`AttestationStore.issue()`
calls with `backend=None`, the legacy single-attestation path). **A commit reviewed and SHIPped
entirely through Claude Code's native gates has no corresponding entry in this index today**,
because nothing currently mirrors a Claude-Code-native SHIP into the OPA ledger — that mirroring is
what LIA-527 Phase 2's write path (`docs/decisions/opa-warden-attestations-v1.md`'s "### Phase 2"
section — designed in the same session this backstop design was written, **implemented since** in
PR #1138) was designed to add.

**Found wrong by the required GPT plan-review co-gate, corrected here**: an earlier version of
this section claimed LIA-527 Phase 2 alone was the activation gate. That is false, verified by
reading Phase 2's own text and the actual Rego source: Phase 2 writes to a **separate, isolated**
OPA document, `data.warden_cc_attestations` — deliberately isolated from `data.warden_attestations`
so a failed CC write can never fail-close Hermes's gate (that isolation is Phase 2's entire point).
Phase 2's own design doc said this explicitly, as of when this section was first written: *"No
Rego rule consults `data.warden_cc_attestations`. Writing and reading are separate decisions; this
section designs writing only"* and named the cutover as *"not designed yet."* Confirmed
independently against the policy file as it existed at the time: `valid_ship` (and its
backend-scoped counterpart) read exclusively from
`data.warden_attestations.latest[...]`/`latest_by_backend[...]` — `warden_cc_attestations` appeared
nowhere in that file. **Superseded below**: `valid_ship` itself is still byte-unchanged today, but
`guardrails.rego` as a whole now also contains a dedicated attestation-verify block (Phase 4,
LIA-530, implemented) that DOES consult `data.warden_cc_attestations` for the `attestation.verify`
operation specifically — see the corrected precondition list immediately below.

The real, complete precondition was **three** pieces of work; **all three now done, verified
directly against source, not assumed from a PR title**:
(a) LIA-527 Phase 2's write path (isolating CC-authored mirrors so they can't fail-close Hermes) —
**done, PR #1138**; (b) a **cutover Rego rule** that makes the decision Claude Code's own gate
would use actually consult `data.warden_cc_attestations` — **done, LIA-530, reviewed and
implemented; see `opa-warden-attestations-v1.md`'s "### Phase 4" section for the mechanism**; and
(c) **`cc_attestations.enqueue_verdict` wired into the real commit gates**
(`codex_warden_hooks.py`'s `run_warden_backends_gate`/`run_verification_gate` call sites) so the CC
ledger the Phase-4 Rego rule reads is genuinely populated on real commits, not empty — **done, PR
#1144/`dd90400d` (LIA-534), merged 2026-08-08, confirmed via direct `git show --stat` read during
LIA-536's own session, not merely a Linear-state check**: adds `_cc_mirror_verdicts(role, config,
repo_root)` mirroring every configured backend's verdict into the isolated CC ledger, reviewed
through 2 rounds of dual-backend plan review, code-reviewer, verification-gate (a 28-combination
fault-injection probe), and ai-eng-warden, all SHIP. (b) was out of scope for LIA-527 Phase 2
itself (its own doc: *"the cutover decision... is explicitly out of scope here"*) and was new,
undesigned follow-up work when this section was first written; it has since been designed,
reviewed, and implemented as LIA-530. (c) was the one genuinely open item as of LIA-536's session
start; **it landed on `origin/main` mid-session, discovered via this session's own routine
`git fetch`+diff freshness check, not sought out — see `.claude/rules/orchestration-rules.md`'s
"Session-Start State Freshness" discipline this reflects.**

Consequence, stated plainly, **corrected from this section's prior framing** ("landing (a) and (b)
alone does NOT clear activation while (c) remains open"): (a), (b), and (c) have **all** now landed
— the CC-mirror path `attestation-verify` reads is genuinely populated in production today, not
empty. This is **necessary, not sufficient**, for activation: `main-attestation-backstop` must
still not be activated (`enforcement: "active"`) until **LIA-531** (credential separation, §3.4)
also lands — still open as of this writing, confirmed via live coordination with the session
working it (round 9 of threat-model review, unshipped) — and the workflow's own temporary `paths:`
trigger filter (see the dedicated bullet in §5) is removed. The ruleset JSON above can be created
now with `enforcement: "disabled"` (a real, inspectable artifact with zero live effect) as
preparatory work, but must stay disabled until LIA-531 clears too. This is corrected here per this
session's own "log each forced deviation...
as a `Deviation:` note... at discovery" discipline, and per this document's own established
practice (§3.1, §3.3) of correcting a wrong claim in place rather than letting it stand once found.

## 4. What this design explicitly does NOT do

- Does not touch `command_parser.py`, `codex_warden_hooks.py`, or `hermes_warden_gate.py` — no
  risk of colliding with the concurrent LIA-467 mitigation round in this same repo.
- Does not modify the existing `main-branch-rules` ruleset's admin-bypass behavior for the
  review-count/no-force-push/signed-commit rules — those stay exactly as they are today.
- Does not implement a local "gate repo" (bare-repo `pre-receive` hook) pattern, despite LIA-522's
  own text naming it as an option. Investigated and rejected for this repo specifically: this
  repo's actual merge path is `gh pr merge`, which creates the merge commit server-side via
  GitHub's API regardless of local remote topology — no local hook, however this repo's push
  topology were rearchitected, could ever observe or intercept that server-side merge. A local
  gate repo would only protect a hypothetical direct-push workflow this repo doesn't use, while
  leaving the real merge path completely uncovered. Server-side (GitHub ruleset) enforcement is
  the only mechanism that actually sits in this repo's real merge path.
- Does not build cryptographic attestation signing. Named as the natural next hardening step (see
  §3.3), not attempted here — the self-hosted-runner approach is materially less new
  infrastructure and matches this system's existing single-machine trust root.
- Does not solve the credential-separation gap named in §3.4. That is real, distinct,
  security-sensitive follow-up work, not silently assumed solved by this design.

## 5. Not yet started, and why

- The `attestation-verify` GitHub Actions workflow file itself, and the small query script it
  runs (reusing `opa_client.query_decision`; `repo_id`/`subject_key` resolution deliberately
  diverges from how `git_subject.py` resolves them for Hermes's path — see §3.2's corrected text
  for why). **Done: implemented as LIA-536** (`.github/workflows/attestation-verify.yml` +
  `scripts/warden_policy/attestation_verify_check.py`) — covered end-to-end (unit tests plus an
  independently-authored oracle test against a real OPA instance), but not yet exercised by a
  live GitHub Actions run; the self-hosted runner it depends on (next bullet) remains open, and
  the workflow's own `paths:` trigger filter is itself a new, separate precondition (see the
  dedicated bullet below, alongside LIA-531/LIA-534/ruleset-creation).
- Registering and hardening the self-hosted runner (a real, consequential change to the user's
  GitHub account/repo security posture — explicitly not something to do autonomously without the
  user's direct awareness, distinct from a normal code change) — blocked on confirming the
  fork-approval setting per §3.3.
- **Live verification of the required-status-check registration itself** (found necessary by the
  GPT plan-review co-gate, a distinct issue from the mechanism fixes above): designing
  `attestation-verify` to publish an explicit Check Run against the PR's real head SHA does not,
  by itself, make GitHub's branch-protection required-check picker point at *that* check — the
  required-check name has to be (re-)registered to match. (The classic branch-protection UI picker
  requires a check to have run against the base branch within the prior 7 days to be selectable;
  the newer Rulesets API this design otherwise uses has no such UI-driven restriction — its
  `required_status_checks[].context` field is free-form and accepts an unregistered context string
  directly, not gated by any "run recently" requirement — so the exact mechanism differs from the
  UI's, confirmed via GitHub's public REST API reference, not this repo's own live state, but the
  underlying caution holds regardless of which path is used: an accepted `context` string in the
  ruleset config still has to actually match the name of a check that really runs and really
  reports against the right commit, which is exactly what needs live verification.) This cannot be verified at the
  design level; it requires the actual workflow existing and a real test PR (ideally including a
  simulated non-owner-actor case, confirming that PR genuinely cannot merge) before
  `main-attestation-backstop` is ever flipped to `enforcement: "active"`. Added explicitly here so
  this doesn't silently become "looks correct in the YAML, doesn't actually gate anything in
  practice" — the same failure shape the `pull_request_target`/neutral-skipped/`if:`-conditional
  fixes above exist to close, recurring one layer up at the required-check-registration boundary
  instead of the workflow's own logic.
- Creating the `main-attestation-backstop` ruleset via `gh api` (safe to create in `disabled`
  state now; must stay disabled until LIA-531 clears — §3.6's three-part CC-attestation dependency
  has fully cleared, see below, but LIA-531 is now the sole remaining activation blocker).
- LIA-527 Phase 2's actual implementation — **done, PR #1138** — the FIRST of §3.6's three
  CC-attestation preconditions.
- **The cutover Rego rule** making `attestation-verify`'s decision actually consult
  `data.warden_cc_attestations` (§3.6) — the SECOND precondition. **Done: designed, reviewed, and
  implemented as LIA-530** (`opa-warden-attestations-v1.md`'s "### Phase 4" section). Was
  undesigned when this section was first written; is not undesigned any longer.
- **`cc_attestations.enqueue_verdict` wired into the real commit gates**
  (`codex_warden_hooks.py`'s `run_warden_backends_gate`/`run_verification_gate` call sites) — the
  THIRD precondition. **Done: PR #1144/`dd90400d` (LIA-534), merged 2026-08-08** — was still open
  as of this section first being written; landed mid-LIA-536-session, discovered via that session's
  own routine freshness `git fetch`, confirmed directly via `git show --stat` rather than trusted
  from a title. **All three of §3.6's CC-attestation preconditions are now done** — the remaining
  activation blocker is LIA-531 (credential separation, next bullet) alone, plus this workflow's
  own temporary `paths:` filter (bullet above).
- Credential separation per §3.4 (routine agent `gh` access vs. ruleset-administration access),
  tracked as **LIA-531** — must cover both stripping repository-Administration permission (closes
  the ruleset-disable
  vector) AND the `workflow` scope specifically (closes the workflow-file-edit vector named in
  §3.1 — found by this round of review to be a real gap in §3.4's original remediation, which only
  named Administration permission; a credential retaining `workflow` scope could still edit
  `.github/workflows/attestation-verify.yml` to report a fabricated success under GitHub Actions'
  own real app identity, defeating the `integration_id: 15368` binding from the inside rather than
  around it). Without both, the backstop's non-bypassability claim is bounded, not absolute, as
  stated there.
- **Remove `attestation-verify.yml`'s temporary `paths:` trigger filter** (added during LIA-536's
  implementation, found necessary by verification-gate: with zero self-hosted runners registered as
  of the workflow's initial landing, an unfiltered `pull_request_target` trigger would queue an
  unpickable job on every PR to the repo indefinitely — non-blocking, since nothing requires this
  check while the ruleset doesn't exist yet, but real Actions-UI clutter). Scoped narrowly
  (`.github/workflows/attestation-verify.yml`, `scripts/warden_policy/**`) while inactive. **Must be
  removed before `main-attestation-backstop` is ever activated** — the whole point of the backstop
  is universal PR coverage, and a path-scoped trigger would silently exempt every PR that doesn't
  touch those paths from ever getting an `attestation-verify` check at all, defeating
  `bypass_actors: []` for those PRs by omission rather than by any bypass mechanism. Added as its
  own precondition here (not folded into the credential-separation bullet above) because it's an
  independent, purely mechanical follow-up with no security-design content of its own.

None of the above was implemented in **LIA-522's own original design pass** — this document was a
design document only at that time, matching the scope LIA-522 itself asked for ("Design...
+ implement + get an independently-authored oracle test before treating it as the hard backstop")
narrowed the same way LIA-527 was: the design was real and reviewed; implementation was real,
consequential follow-up work, deliberately not compressed into the same pass as first-cut design.
**Several items have since been implemented** in their own dedicated follow-up passes, corrected
in place above rather than left stale — see the "Done" markers throughout §3.2, §3.6, and §5.

## Alternatives considered

- **Add the check to the existing `main-branch-rules` ruleset** rather than a new one: rejected —
  would either inherit that ruleset's admin-bypass (defeating the purpose) or require disabling
  bypass for the *entire* ruleset, silently removing the solo-dev review-count accommodation as a
  side effect of an unrelated change. A separate ruleset keeps the two concerns independently
  toggleable.
- **Local bare "gate repo" with a `pre-receive` hook**: rejected, see §4 — doesn't cover this
  repo's actual merge path.
- **Cryptographically-signed attestation artifact, GitHub-hosted runner**: rejected for this pass
  as more new infrastructure than the problem currently needs; recorded as the natural upgrade if
  the self-hosted-runner trust model ever needs to change (e.g., real external contributors).
- **`enforce_admins: true` on the existing classic branch-protection config**: rejected — an
  all-or-nothing toggle that would also block the solo-dev review-count bypass this repo
  legitimately relies on; the ruleset-based approach achieves the narrower goal.

## Rollback

- **Disable the backstop**: `gh api -X PUT repos/sliamh11/Deus/rulesets/<id> -f enforcement=disabled`
  (GitHub's ruleset-update endpoint is `PUT`, not `PATCH` — confirmed against the REST API
  reference; `DELETE` is a separate, correct verb for outright removal: `gh api -X DELETE
  repos/sliamh11/Deus/rulesets/<id>`). This is the same operation §3.4 names as the unresolved
  gap. Recorded explicitly here as the legitimate emergency recovery path (e.g., a genuine OPA
  daemon outage per §3.5 with an urgent merge need), to be used deliberately and visibly by the
  user, not silently by routine agent automation.
- **Full teardown**: also unregister the self-hosted runner (Settings → Actions → Runners, or
  `gh api -X DELETE repos/sliamh11/Deus/actions/runners/<id>`) and remove the
  `.github/workflows/attestation-verify.yml` file, once both exist.
- **Per-repo, non-destructive**: the existing `main-branch-rules` ruleset and the 7 existing
  required status checks are entirely untouched by this design and need no rollback of their own.

## Verification

Every specific claim above (`enforce_admins`, the existing ruleset's `bypass_actors`, repo
visibility, `guardrails.rego`'s `valid_ship` reading only Hermes's `latest` index, the `git commit`
never happening through a local push in this repo's real merge flow, the ambient `gh` token's
scopes) was checked directly against live API responses and current source during this session,
not asserted from memory or general GitHub documentation — see the inline `gh api`/`gh auth
status`/`gh repo view` output quoted in §2, §3.4, and the direct citations in §3.6. The one
exception, flagged where it appears rather than silently blended in: §3.3's claim about GitHub's
default fork-PR-approval behavior is stated as an assumed default, not independently confirmed
against this repo's own settings.
