---
name: Credential separation for main-attestation-backstop admin surface (LIA-531) — design only, not yet implemented
description: >
  Closes the credential-separation gap named but deferred by
  git-level-hard-backstop-design.md §3.4: today's single ambient `gh` credential can disable the
  git-level backstop's own ruleset, or edit the workflow file it depends on, defeating the backstop
  entirely. Proposes a local allowlisted API proxy so no agent process ever holds a raw GitHub
  credential, plus a human-approval broker for the admin-capable surface the allowlist denies.
type: decision
tags: [wardens, opa, credential-separation, github-ruleset, lia-531, lia-522, security]
date: 2026-08-08
---

# Credential separation for the `main-attestation-backstop` admin surface — design

**Date:** 2026-08-08
**Scope:** A design for eliminating raw-GitHub-credential exposure to autonomous agent sessions via
a local allowlisted proxy, plus a human-approval broker for the admin-capable surface the proxy
denies. No GitHub account changes, no fine-grained PAT creation, no code in this pass. (Placed
directly after **Date:** — earlier than the sibling
`git-level-hard-backstop-design.md`'s own Date/Status/Scope order — because
`scripts/drift_check.py`'s ADR-freshness check only scans the first 20 header lines for a
`**Scope:**` field, confirmed by direct read of `parse_adr()`; this header's Status paragraph had
grown long enough across review rounds to push Scope past line 20, which produced a real
drift-check failure at push time, not a hypothetical one.)
**Status:** Design only, threat-modeler SHIP followed by an extended code-reviewer/GPT co-gate cycle
that found and fixed one CRITICAL post-threat-model finding plus several further findings the fix
itself surfaced (see below and §8 for the full, exact round-by-round log — deliberately not
hard-counted here, since a specific number in this header has already gone stale twice as further
rounds landed after it was written; §8's log is the only place a current count should be trusted).
**Two** residual gaps remain explicitly unresolved rather than falsely claimed closed — a
base-retarget TOCTOU issue (§5) and the broker's approval-artifact forgery gap (§4's already-named,
distinct unauthenticated verdict store) — see "Not yet started" below for both. No implementation in
this pass.
**Related:** LIA-522 (`docs/decisions/git-level-hard-backstop-design.md`, merged PR #1129 — the
design this closes a named gap in, §3.1's `main-attestation-backstop` ruleset and §3.3's
self-hosted-runner risk are both load-bearing prior art here), LIA-530 (LIA-527 Phase 2 + Rego
cutover, unrelated substrate, no file overlap with this design).
**Revision note**: this document went through twelve threat-modeler rounds (ten BLOCK, a round-11
SHIP, a round-12 fast confirmatory pass), then an **extended** code-reviewer/GPT co-gate cycle — the
GPT co-gate alone found and fixed a CRITICAL finding the entire threat-modeler process had missed,
then several further real findings the fix itself surfaced (a Rollback-section contradiction, a
multi-round closure-claim propagation gap, a cost-framing contradiction, a too-narrow credential-scope
definition, and a base-retarget TOCTOU gap the first attempted fix didn't fully close) — §8 has the
full, exact log, updated each round. **The threat-modeler count above (twelve) is stable and safe to
state — that phase concluded before the code-review cycle began, so it cannot drift further. The
code-review/GPT cycle's own count is deliberately NOT given a number here** — an earlier version of
this sentence stated one, and it went stale (undercounted) as further rounds landed after it was
written, the same duplication-drift problem that caused rounds 4, 6, and 9's own stale-reference
findings, and that this specific sentence has now suffered from twice. §8 is the only place that
count should be trusted, since it is still actively growing as of this document's most recent edits.

Round 1 found a real internal contradiction in the original §3 (`--admin` bypass conclusion vs.
LIA-522's `bypass_actors: []`) and two unclosed CI-secret-exposure paths in the original raw-PAT
approach. Round 2 found the proxy-based revision's allowlist claim didn't actually close
CI-execution for unfiltered-trigger workflows, and an unearned "credential containment" claim.
Round 3 found the narrowed release-path claim still didn't hold, and that blanket `--admin`-broker
routing would break the autonomous pipeline (`--admin` is this repo's only viable merge path) —
correctly, at the time, since no verified alternative existed yet. **Rounds 4-8 spent their entire
effort on a proposed auto-forward fast-path for `--admin` merges meant to avoid that cost, and each
round found the previous round's version unsound in a new way** (vacuous-green twice, a TOCTOU gap,
an unnecessary credential tier, an under-inclusive rule enumeration, a category error affecting 4 of
5 live rule types, a fail-open window during ruleset pre-activation) **— round 8 concluded this is a
genuinely hard sub-problem needing live-API integration testing, not further design review, and
descoped it entirely to unconditional broker-routing**, accepting round 3's original cost concern as
a stated Phase 1 tradeoff rather than continuing to engineer around it. A real, independent fix
(denying direct pushes to the default branch, found in round 7) is retained, and generalized to
cover tags and REST-transport ref mutations after round 9 found the round-7/8 fix scoped to fewer
cases than it claimed. Round 10 closed a real TOCTOU gap: the broker's approval was never bound to
the exact SHA it executed (the fix the descoped predicate carried at round 4 wasn't migrated to the
broker path at round 8) — brokered merges are now pinned to the approved head SHA, with a moved head
invalidating the approval rather than silently merging past it.

**The most consequential single finding in this document's entire review history came after
threat-modeler SHIP, at the code-review gate, not from any of the 12 threat-modeler rounds**: the
GPT code-review co-gate found that brokering only "`--admin`-flagged" merge calls (the framing every
prior round had used, including round 10's own fix above) cannot work at all, because `--admin` is a
pure `gh`-CLI-client-side behavior with no corresponding parameter on GitHub's actual merge endpoint
— verified independently against GitHub's REST API reference (the endpoint accepts only
`commit_title`/`commit_message`/`sha`/`merge_method`). A network-level proxy, this design's own
transport model, cannot observe the flag at all, and the routine credential — issued under the same
account as the admin credential — retains full bypass eligibility for *every* merge call regardless
of what CLI flags produced it. **The fix, and this document's actual current rule**: every PR-merge
call — `--admin`-flagged or not — is broker-tier, unconditionally, executed pinned to the approved
head SHA. This is not a refinement of round 10's fix; it replaces the CLI-flag-based distinction
round 10 (and every earlier round) assumed was meaningful. **Three further GPT-caught issues
followed in the same code-review-gate cycle**: the `## Rollback` section briefly suggested a
partial-revert path back to the old, CLI-flag-based `approve_admin_merge` marker flow as a
"legitimate intermediate state" — fixed to state plainly that it is not, since it reintroduces the
identical flaw; §6's leaked-credential blast-radius analysis needed correction to reflect a real,
positive consequence of the merge-endpoint fix — since every merge is now broker-gated and
`release-please.yml` only triggers on a commit landing on `main`, the release-via-ordinary-merge
`NPM_TOKEN` vector is closed *by design*, not merely unaddressed, unlike CI-secret-exfiltration
(which needs no merge at all and remains genuinely open); and that closure claim itself then needed
its actual preconditions stated explicitly — it holds only once the proxy/broker run with genuine
OS-isolation from the agent (§3's still-open gap) *and* every raw GitHub credential path an agent
process can reach is retired or routed through the proxy (§5/§7's Phase 1 completion criterion,
broadened after a further GPT pass found this document's original "rotate the keyring token" framing
too narrow — a second, independent `GITHUB_TOKEN` env-var credential path is confirmed real in this
repo's own source, §7), not as a property already true of this design document's mere existence.

**One further finding is a *second* unresolved residual gap, named honestly rather than claimed
fixed — alongside §4's already-named unauthenticated-verdict-store gap, correcting an earlier version
of this note that overstated the base-retarget issue as the document's only such gap when it is one
of two**: a TOCTOU issue, structurally analogous to the head-SHA one round 10 closed, exists for a
merge's target base branch — an approved merge and a separately-approved base-retarget are
independent requests, and unlike the head SHA, GitHub's merge endpoint has no atomic "expected base"
parameter to bind against, so a straightforward "re-check the base before merging" fix (the first
thing tried) is itself still a race, just a narrower one. This document recommends per-PR
serialization between retarget and merge requests as the actual closing mechanism (§5, §7) but does
not design it in this pass. Together with §4's verdict-store gap, these are the two places in this
document where "closed" cannot yet be said, both named as such rather than glossed over.

The document was set to Manual Review Required after threat-modeler round 4 per this session's own
gate-blocking discipline, then resumed at the user's direction. This version incorporates the full
review history above — a lengthy threat-modeler cycle followed by an even lengthier code-review-gate
correction cycle, exact counts in §8 only — and states remaining open items plainly (§3, §5, §7)
rather than presenting any of it as solved beyond what it actually is.

## 1. The gap this closes

`git-level-hard-backstop-design.md` §3.4 named this plainly and deferred it:

> "A locally-running agent doesn't need to defeat the `attestation-verify` check at all; it can
> simply remove the rule that requires it. ... The correct fix is credential separation: routine
> agent-driven `gh` operations ... should run under a credential that structurally lacks
> repository-administration permission ... and no ability to write `.github/workflows/*` ...
> That re-architecture is real, non-trivial, security-sensitive work ... (out of scope to discover
> and change in this design pass) — named here as required follow-up, not solved."

Verified live in this session (`gh auth status`, run directly, 2026-08-08): the ambient credential
every Claude Code / Hermes session on this host runs `gh` under — interactive and
autonomous/background/dispatched alike — is a single keyring-stored OAuth token, scoped `gist,
read:org, repo, workflow`, account `sliamh11`. No `GH_TOKEN`/`GITHUB_TOKEN` env var currently
overrides it. This is the same credential that would let any process running as this OS user call
`gh api PUT/DELETE` on the `main-attestation-backstop` ruleset directly, or edit
`.github/workflows/attestation-verify.yml` to fabricate a passing check — the exact "sufficiently
motivated or prompt-injected local agent" threat model §1 of the backstop design names as the whole
reason it exists.

## 2. Why a plain permission-scoped PAT is not enough, on its own

The first draft of this design proposed a fine-grained PAT scoped to Contents/Pull
requests/Issues/Metadata, explicitly withholding Administration and Workflows permissions, handed
directly to agent sessions as `GH_TOKEN`. **A dedicated threat-modeler pass found this insufficient
on its own — not because the permission split is wrong, but because GitHub's permission categories
don't align with this repo's actual trust boundary:**

- **Releases fall under the Contents permission category in GitHub's fine-grained PAT model** — a
  Contents-write PAT (which routine automation genuinely needs, for ordinary commits/pushes) can
  also create a GitHub Release. `publish-packages.yml` triggers on `release: published` and runs
  `NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}` (confirmed directly: `publish-packages.yml:41,72`,
  `on: release: published` / `workflow_dispatch`). A routine credential can reach `NPM_TOKEN`
  without touching Administration or Workflows at all.
- **Pushing a branch and opening a same-repo PR is exactly what "routine" automation is supposed to
  do — and it's also sufficient to run repo-controlled code inside CI with real secrets.**
  Confirmed directly: `ci.yml`, `eval.yml`, and `judge-gate.yml` all trigger on `pull_request`
  (`ci.yml:4-5`, `eval.yml:4-11`, referenced by `judge-gate.yml`'s own header comment) and inject
  `PERSONAL_ID_PATTERN` (`ci.yml:33`), `ANTHROPIC_API_KEY`/`DEEPEVAL_API_KEY`/`GEMINI_API_KEY`
  (`eval.yml:61-106`), and `GEMINI_API_KEY` (`judge-gate.yml:80,92`) into jobs that run on a
  same-repo pull request — no fork-PR restriction applies, since these are same-repo pushes by
  design. A Contents-write-only credential can push a branch containing a script that reads and
  exfiltrates any of these env vars, then open the PR that triggers the job. Withholding
  Administration/Workflows does nothing against this — the attack needs neither.

**This means GitHub's own permission model cannot express the actual grant this design wants**:
"push code and open PRs, but never trigger a job that holds a real secret via that push, and never
create a release." No combination of fine-grained PAT permissions captures that distinction — it
cuts across GitHub's category boundaries (Contents governs both "push a harmless doc fix" and
"push a secret-exfiltrating CI trigger" identically). A raw PAT handed to an agent process, however
narrowly scoped, cannot close this gap by permission scoping alone.

## 3. Target mechanism: a local allowlisted proxy, not a raw credential in agent hands

**Revised recommendation (primary), replacing the first draft's "hand the agent a scoped PAT
directly" approach**: extend `src/credential-proxy.ts`'s already-proven structural pattern ("the
proxy injects real credentials, the client process never sees them" — confirmed directly,
`credential-proxy.ts:1-17`, today used for Anthropic/OpenAI/Gemini API keys, not GitHub) to `gh`/the
GitHub API. A local process holds the actual credential(s); an agent session's `gh`/`git` calls route through
it; the proxy checks each request against an explicit **endpoint + payload allowlist** before
forwarding it, rather than handing out a token the caller can use for anything the token's scope
permits.

**Transport, corrected after round-2 review flagged the first revision's phrasing as not actually
satisfying its own invariant**: a `git credential.helper` does not work here — a helper's entire
contract is handing the real credential to the `git` process that invoked it, which puts the token
back in the agent's reach. A wrapped `gh` shadowing the real binary on `PATH` is equally
insufficient — nothing stops the agent process from invoking the real `gh` binary by absolute path,
or calling the GitHub API directly via `curl`, bypassing the wrapper entirely (the same class of
routing-bypass §4's predecessor argument already rejected for the routine-vs-admin split in an
earlier round). The only transport that actually satisfies "the agent process never holds a raw
token" is one where the real credential never enters the agent's environment or is reachable from
it at all — e.g. the proxy sits on the network path itself (an HTTP(S) endpoint that injects the
real `Authorization` header server-side, with `git`/`gh` in the agent's environment configured to
route through it and holding no credential of their own), not a helper or wrapper the agent process
could route around. This transport mechanism is named as a requirement here; it is not fully
specified in this pass (§7).

**A second, more fundamental gap round-2 review found and this revision does not yet close**: even
with a non-bypassable transport, the proxy process itself currently sits in the *same OS-user trust
domain* as the agent process it's meant to contain — nothing in this design yet prevents a
compromised/adversarial agent process (§1's own named threat model) from reading the proxy's local
credential store or access token the same way it could read a raw `GH_TOKEN` today. **The
"credential containment... holds even if the approval mechanism itself is imperfect" claim in §4
below is therefore not yet earned as stated** — it requires the same separate-OS-user isolation §4
already names for the *broker's* approval integrity, applied to the *proxy* itself, not just the
broker. Until the proxy runs as a genuinely separate OS user (a `launchd` job with its own
`UserName`, reachable only via a Unix socket the agent's user can connect to but not read
arbitrary files from), this design's containment claim should be read as covering the *accidental*
leak/misuse case (a well-behaved agent that simply doesn't have a token to leak) — not as a hard
boundary against §1's actual adversarial threat model. Closing that gap fully is named in §7 as
required before this design can be relied on operationally against a compromised agent, not
asserted as already solved.

**Canonical statement, added after this exact caveat needed re-deriving and re-propagating across
this document's own review history more times than any other single point**: this OS-isolation gap,
together with §7's credential-retirement completion criterion (broadened from "rotate the keyring
token" after a further GPT code-review co-gate pass — see §7 — to cover every raw GitHub credential
path an agent process can reach, not just the one this document originally focused on), governs
**every** closure claim this document makes — not just the containment property discussed above, and
not just the merge-brokering fix's release-via-merge closure (§6, this document's authoritative
treatment of that specific case). The allowlist's endpoint denials (ruleset administration,
workflow-file writes, direct release-creation, default-branch/tag-ref mutations) are equally
contingent: a compromised agent that can read the proxy's held credential directly, or that still has
*any* pre-existing raw credential available — the keyring token, an env-var credential like
`GITHUB_TOKEN` (confirmed real in this repo's own source, §7), or a registered SSH key reachable via
a remote-URL change — doesn't need to defeat any of this design's logic — it bypasses the proxy and
broker entirely and calls GitHub directly. **Nothing in this document should be read as claiming an
operational security boundary — as opposed to a well-architected design for one — exists before both
preconditions land, and "the ambient-token precondition" specifically means every such path, not
only the one instance this document happened to name first.** Individual sections throughout this
document (including the summary above) state their own closure claims relative to this one; none
restate this reasoning in full.

**Correction (round 3): the previous version of this sentence claimed this closes the release/
`NPM_TOKEN` half of §2's problem "completely" — that overclaimed, and a third threat-modeler pass
caught it.** The proxy genuinely closes the *direct* release-creation endpoint
(`POST /repos/{owner}/{repo}/releases`) — there's no legitimate routine use of it, so denying it
outright costs nothing. At the time of this correction, it did **not** yet close the broader
`NPM_TOKEN` vector: `release-please.yml` triggers on ordinary `push: branches: [main]` (confirmed,
`release-please.yml:3-5`) and — as part of its normal, legitimate job — creates the GitHub Release
itself using `secrets.PAT` (`release-please.yml:18`), which then triggers `publish-packages.yml`'s
`on: release: published` (confirmed, `publish-packages.yml:3-5,41`) with `NPM_TOKEN`.

**This vector is now closed, corrected after the merge-endpoint fix later in this document (§3's
`--admin`/PR-merge correction below) — a GPT code-review co-gate pass caught this paragraph never
being updated to reflect that fix, the same duplication-drift class this document's history keeps
finding.** The reasoning above ("an ordinary merge to `main`... reaches `NPM_TOKEN` without the
routine credential ever calling the release-creation endpoint directly") was accurate when written,
under the round-3-era assumption that only `--admin`-flagged merges were broker-gated. That
assumption was itself found wrong later in this document: **every** PR-merge call is now broker-tier,
unconditionally, and any direct push to `main` is independently denied/brokered (§3's
default-branch/tag invariant). Since `release-please.yml` only fires on a commit actually *landing*
on `main`, and there is no longer any way for the routine credential to land a commit on `main`
without broker approval — merge or direct push — **triggering `release-please` (and therefore
reaching `NPM_TOKEN` via that path) now requires the same human approval as every other brokered
operation.** This is a real, structural improvement over the round-3-era understanding, not a
wording tweak: the release-via-ordinary-merge vector is closed by the merge-brokering fix — **once
two further preconditions this section doesn't itself cover are met (see §6's full treatment and
§7's now-broadened credential-retirement item): the proxy/broker must run with genuine OS-isolation
from the agent, and every raw GitHub credential path an agent process can reach — not only the
keyring token — must be retired or routed through the proxy. Until then, a compromised agent or a
leaked proxy-access credential can bypass the proxy/broker system entirely (not defeat the
merge-brokering rule — skip it) via any of these gaps.**
Once both preconditions hold, the closure's remaining dependency is the broker's own
approval-integrity (§4's already-named, distinct residual gap — an unauthenticated verdict store).

**Both halves of §2's original problem are workflow-side only when landing on `main` isn't itself
gated — one of them (release-via-merge) no longer fits that description; the other (CI-execution,
below) still does**: the CI-secret-exfiltration path requires no merge or push to `main` at all — it
fires on an ordinary PR *open/update*, which stays on the allowlist and triggers `ci.yml` immediately,
before any broker involvement. That path remains workflow-side, unclosed by this design, exactly as
originally stated. `secrets.PAT`'s audit priority (§7) is correspondingly reduced from "critical
path" to standard hygiene, contingent on the same two preconditions §6 states for the
release-via-merge closure itself — this document's authoritative treatment of what's actually closed
today versus once Phase 1 is built, not restated here to avoid the drift this exact restatement has
already caused multiple times in this document's review history.

**It does not, on its own, close the CI-execution half either — stated plainly after a second
threat-modeler pass caught the first revision overclaiming this** (round 2 finding, not glossed
over): pushing a branch and opening or updating a PR is exactly the ordinary operation routine
automation needs to keep doing, and `ci.yml` has **no `paths:` filter** (confirmed directly,
`ci.yml:3-5` — `on: pull_request: branches: [main]`, unconditional) and runs `npm ci`
(`ci.yml:26`) — which executes this repo's own `prepare` script (`package.json:18`: `"prepare":
"husky && node scripts/migrate.mjs --quiet"`) — *before* any secret-bearing step. An allowlist
that permits ordinary PR pushes (which it must, to be useful at all) cannot distinguish "an
ordinary push" from "a push whose `prepare` script exfiltrates `PERSONAL_ID_PATTERN`
(`ci.yml:33`)" by endpoint/payload shape alone — both are the identical API call. `eval.yml` and
`judge-gate.yml` are less exposed (both are genuinely `paths:`-scoped — `container/**`, `eval/**`,
`src/ipc.ts`, and `evolution/**` respectively — so a payload-diff check *could* flag a push
touching those paths specifically), but `ci.yml`'s unfiltered trigger means the general case is not
closed by the proxy design as stated.

**The design's actual position, stated honestly rather than claimed as solved**: the CI-execution
boundary is a workflow-side problem, not a proxy-side one, and closing it requires the mitigations
named in §7 (pinning `permissions:` per job, `npm ci --ignore-scripts`, moving secret-bearing steps
behind a GitHub Environment with required reviewers) to actually land — the proxy's allowlist alone
cannot substitute for that. This design does not collapse the routine/admin split down to "every PR
push needs broker approval" (which would defeat the point of having a routine credential at all);
it instead names workflow-side hardening as a **required, sequenced prerequisite** alongside the
proxy, not an optional follow-up — this hardening's absence leaves the CI-secret-exfiltration vector
open regardless of any other precondition; the ruleset-admin, workflow-file-edit, and release-creation
vectors this bullet's scope statement contrasts it against are attributed to a *different* mechanism
(the allowlist's endpoint denial), whose own operational status — same as everything else this
design proposes — is governed by §6's authoritative OS-isolation/rotation preconditions, not
restated here.

Concretely, the proxy's allowlist should, at minimum:

- Allow: PR create/view/comment/label, issue create/view/comment/label, ordinary Contents pushes
  **and equivalent REST ref mutations** (round 10 correction — an earlier version phrased this bullet
  in git-protocol terms only, while the deny invariant below is explicitly transport-independent;
  under deny-by-default that asymmetry would have accidentally denied a routine, already-documented
  operation: this repo's own orchestration rules require manual `gh api -X DELETE
  repos/<owner>/<repo>/git/refs/heads/<branch>` cleanup after an `--admin --delete-branch` merge's
  local worktree step fails, confirmed twice, PRs #1106/#1115 — a REST ref mutation on a non-default
  branch, which must stay allowed) **to `refs/heads/*` other than the default branch** (round 9
  correction — an earlier version said "any ref other than the default branch," which still permitted
  `refs/tags/*` pushes/deletes despite the deny bullet and the invariant below denying tags; scoping
  to branch refs specifically closes that self-contradiction), Checks/Commit-statuses reads
  (explicitly enumerated — the first draft's §2 claimed "CI-status reads" run under the routine
  credential without naming the specific read-only permissions this requires).
- Deny by default, route to the human-approval broker (§4) instead of silently rejecting: ruleset
  administration (PUT/POST/DELETE on `rulesets` endpoints and on `.../branches/{branch}/protection*`),
  `.github/workflows/*` file writes, release creation, tag creation/deletion, **and every PR-merge
  call, `--admin`-flagged or not** — see the correction immediately below, which replaces a real
  design flaw the GPT code-review co-gate caught (not another threat-modeler round; this was found
  at the code-review gate, after all 12 threat-modeler rounds, and is recorded honestly rather than
  smoothed over).

  **Correction, replacing a broken exemption (post-threat-modeler, code-review-gate finding)**: every
  prior version of this bullet exempted plain `gh pr merge` (no `--admin`) from brokering, reasoning
  that GitHub's own required-checks/required-reviews enforcement gates that endpoint server-side. **That
  reasoning is wrong, verified independently against GitHub's own REST API reference**: `--admin` is
  a pure `gh`-CLI client-side behavior, not an API-level concept — the actual merge endpoint (`PUT
  /repos/{owner}/{repo}/pulls/{pull_number}/merge`) accepts exactly four body parameters
  (`commit_title`, `commit_message`, `sha`, `merge_method`), none of them related to admin/bypass/force
  in any way. Bypass eligibility is governed entirely by the authenticated account's own role (§5:
  account-role-gated, not request-flag-gated) — the same fact this document already established for a
  different reason, but never connected to this implication until now. Since the routine credential is
  a fine-grained PAT issued under the *same* account as the admin credential (§2 — a PAT narrows
  permission *categories*, it cannot strip the underlying account's branch-protection *role*), **the
  routine credential retains full bypass eligibility for every merge call it makes, regardless of
  whether the caller's `gh` CLI happened to pass `--admin`**. A network-level proxy sitting on the REST
  API path (§3's own transport model) cannot observe the `--admin` flag at all — it only ever sees the
  same four-parameter merge request, whether the caller's local `gh` process added `--admin` or not.
  **There is therefore no reliable way to broker only the "intended-as-bypass" merges and allow the
  rest through unbrokered — the distinction this document built for 10+ rounds does not exist at the
  layer the proxy actually operates on.** The only sound fix is what this bullet now states: broker
  every PR-merge call, full stop, regardless of CLI flags — executed pinned to the approved head SHA
  (§5), same mechanism already specified for `--admin` calls, now correctly scoped to cover the
  merge endpoint as a whole rather than a CLI-level distinction the proxy cannot see.
- **A direct push to the default branch (`main`) or a tag ref, round 7 finding — a real, serious gap,
  not a refinement of an existing one**: every round 3-7 fix hardened the `--admin`-merge mechanism,
  but a threat-modeler pass found the allowlist never scoped ordinary Contents pushes away from `main`
  itself. Live-verified: this repo's `main` has `enforce_admins.enabled: false` and the ruleset grants
  the admin role `bypass_mode: "always"` — so a direct `git push origin <sha>:main` (or any ref
  update targeting `main`) lands arbitrary code with **zero** checks, no PR, no review, entirely
  bypassing every merge-based control this document specifies, since those only ever fire on a
  `gh pr merge` call. This reaches the exact same `NPM_TOKEN` vector §2 already names via
  `release-please.yml`
  (`on: push: branches: [main]`) with no merge call involved at all, and lands unattested code during
  precisely the pre-`main-attestation-backstop` window §5 exists to close. Confirmed no legitimate
  caller needs this: both `linear-auto-merge.ts` and `merge_train.py` land changes exclusively via
  `gh pr merge --admin`, never a direct push. **Fix, stated as one transport-independent invariant
  after round 8 found the round-7 fix scoped to only one transport**: any operation that would update
  `refs/heads/main` (the default branch) or `refs/tags/*`, by **any** transport — smart-HTTP
  `git-receive-pack`, or the equivalent REST mutations (`PATCH .../git/refs/heads/main`, `POST
  .../merges` with `base: main`, tag-ref creation/deletion endpoints) — is denied/brokered, same tier
  as ruleset administration. Stating it as one invariant rather than a git-protocol rule plus a
  separate REST list avoids exactly the kind of one-transport-only gap round 7's version had. Not yet
  specified in full at the transport/implementation level (§7).
- **PR merges (all of them, not just `--admin`-flagged ones — see the correction above): broker-tier,
  unconditionally — descoped after 4 rounds (4-7) of a proposed auto-forward fast-path each proving
  unsound in a new way, and a decision made deliberately rather than by exhaustion**: rounds 4-7
  iterated on a predicate meant to auto-forward `--admin`
  merges when independently verified safe, so the autonomous pipeline (which uses `--admin` for
  *every* merge, per §5 — not an occasional override) wouldn't need a human in the loop for the
  common case. Each round's fix for the previous round's unsoundness introduced a new one: vacuous
  green (twice, via two different scoping errors), a TOCTOU gap, an unnecessary credential tier, and
  finally — round 8 — a fundamental category error: of this repo's five live ruleset rules
  (`deletion`, `non_fast_forward`, `copilot_code_review`, `pull_request`, `required_signatures`),
  four have no sound "verify on the pinned SHA" semantics at all (`required_signatures` constrains
  the commit GitHub creates on merge, not the PR head; `deletion`/`non_fast_forward` are properties of
  the ref-update operation, not a commit; `copilot_code_review` has no stated verification semantics
  anywhere in this document's seven prior rounds). Round 8 also found the predicate would silently
  auto-forward unattested code during the entire pre-`main-attestation-backstop` window, since that
  ruleset doesn't exist yet and the predicate only ever derived its rule set from what currently
  exists — directly contradicting this section's own stated purpose.

  **This document's position, after several rounds of evidence that a sound auto-forward predicate is
  a genuinely hard sub-problem**: building one correctly requires classifying each of GitHub's rule
  types by verification semantics (not-implicated-by-this-operation / verifiable-on-the-resulting-ref
  / verifiable-on-the-pinned-SHA / unverifiable) and testing that classification against this repo's
  *live* API responses — work that needs real integration testing, not further design-review rounds,
  which have demonstrably found a new category of defect on nearly every pass rather than converging.
  Continuing to iterate the predicate in this document risks presenting an untested classification as
  reviewed and safe, which would be worse than being honest about the gap. **This design pass
  therefore does not attempt the auto-forward predicate.** Every PR-merge call — `--admin`-flagged or
  not (the flag is invisible to the proxy and doesn't gate anything at the API level, per the
  correction above), unconditionally, no fast path — routes to the human-approval broker (§4), the
  same as every other denied surface, **executed pinned to the exact head SHA the approver saw**
  (round 10 fix — see §5 for the full TOCTOU reasoning; a head that moves after approval invalidates
  it rather than silently merging the new head). This accepts the real cost round 3's B4 named (every
  autonomous merge needs a
  human approval, not just occasional overrides) as a **deliberate, stated Phase 1 tradeoff**, not a
  silently-reopened debate: it closes the timing gap this section exists for (no `--admin` merge ever
  proceeds unbrokered or unpinned, regardless of `main-attestation-backstop`'s activation state) with
  a mechanism simple enough to actually verify sound, at the cost of pipeline throughput until a
  Phase 2 auto-forward optimization is built and tested separately (§7).

  **`merge_group`**: not relevant under blanket broker-routing — this repo doesn't have GitHub's
  merge queue enabled regardless (confirmed live), so the question that mattered only for an
  auto-forward fast-path doesn't arise.
- **Explicitly out of scope for this allowlist, stated plainly rather than implied solved**:
  ordinary PR-open/`synchronize` pushes are allowed (routine automation needs this), and this
  allowlist alone does **not** close the CI-secret-exfiltration path those pushes can trigger for
  unfiltered-trigger workflows like `ci.yml` — see §3's transport/scope discussion above. Closing
  that is workflow-side hardening (§7), a required prerequisite alongside this proxy, not a proxy
  responsibility.
- **GitHub Actions endpoints, named explicitly rather than left to deny-by-default's implicit
  coverage** (round 3 addition): `POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches` and
  the runs-rerun endpoints must be on the same deny/broker list as ruleset-admin and workflow-file
  writes — confirmed directly, `publish-packages.yml:5` (`workflow_dispatch`) means a direct
  Actions-dispatch call reaches `NPM_TOKEN` with no release involved at all. Deny-by-default already
  covers this if the allowlist is genuinely allowlist-shaped (permit named things, deny everything
  else), but naming it explicitly here prevents an implementer who reads "CI-status reads" (§3's
  allow bullet) loosely enough to also allow dispatch calls.
- The proxy itself, not the agent process, holds whichever underlying credential(s) are needed to
  service allowed requests. An agent session never receives a raw `GH_TOKEN`/PAT value at all — this
  is the structural fix for the leaked-credential blast radius named in §6.

## 4. The admin-capable surface: human-approval broker, with its value proposition stated correctly

For the full denied/brokered surface — see §3's allowlist bullets for the authoritative list
(deliberately not re-enumerated here a third time; this is the same duplication-drift risk named
throughout this document's review history) — this design proposes generalizing this repo's existing
`approve_admin_merge`/`_admin_merge_marker` pattern (`scripts/codex_warden_hooks.py`, as of commit
`812512f1`: `approve_admin_merge` at lines 656-683, `_admin_merge_marker` at lines 635-636 — cited by
function name as the durable anchor, since this file is under active, fast-moving edits elsewhere in
the repo and line numbers alone drifted twice during this design's own review rounds) into a broker
step inside the proxy from §3: a denied request is held pending explicit human approval before the
proxy (which alone holds the elevated credential) executes it.

**The broker's actual value proposition, stated correctly this round, and corrected again in round
2** (the first draft overclaimed this; round 1's fix still overclaimed a "holds even if the
approval mechanism itself is imperfect" containment property the design didn't yet earn, per §3's
now-corrected transport discussion above): the broker's value is **API-surface reduction** — an
agent process, **once the OS-isolation requirement below is met** (stated as a precondition here,
not implied — round 3 review found the prior wording asserted this property inline before the very
next sentence disclaimed it), is limited to whatever the proxy's allowlist permits, which is a real,
auditable narrowing versus a raw scoped token. Whether that narrowing amounts to genuine
**credential containment** (the agent literally cannot reach the admin-capable surface, full stop)
depends entirely on the proxy and broker both running outside the agent's own OS-user trust domain,
per §3's revised transport section — **not yet true of this design as specified**, so this document
does not claim containment as an already-achieved property. What *is* true regardless of that open
item: the allowlist reduction alone (the proxy's endpoint/payload filtering, independent of the
merge-brokering fix discussed separately below) is strictly better than the first draft's raw-PAT
approach for the *direct*-release-endpoint and ruleset/workflow-edit vectors specifically (§2) —
**not**, by the allowlist alone, for CI-execution, which §2/§3 attribute to workflow-side hardening,
not the allowlist. **The `NPM_TOKEN`-via-ordinary-merge vector is a different case, corrected twice
after this paragraph needed further propagation fixes**: it is closed *by design*, but not by the
allowlist — by the separate merge-brokering fix (§3/§5), which makes landing any commit on `main`
require broker approval regardless of what triggers `release-please.yml` afterward. **This closure
is not yet operational, matching §6's authoritative treatment rather than restated here**: it holds
only once the proxy/broker run with genuine OS-isolation from the agent (§3's still-open gap) *and*
every raw GitHub credential path an agent process can reach is retired or routed through the proxy
(§5/§7's now-broadened Phase 1 completion criterion) — without both, a compromised agent bypasses the
proxy/broker system entirely rather than being stopped by the
merge-brokering rule (see §6 for the full reasoning). So of the two vectors named alongside
CI-execution in earlier drafts of this document, only CI-execution remains attributed to
workflow-side hardening as its closure mechanism; this is a real, partial improvement over the raw-PAT
approach, not "no improvement until everything is perfect," but its scope — and its own
preconditions — should be read precisely rather than generously.

Separately from containment, the proxy/broker chokepoint is also a natural place to add **audit
logging** as a stated design property, not an afterthought: every request the proxy forwards or the
broker approves can be logged (actor, timestamp, endpoint, payload hash) to a location the agent
process cannot rewrite once the OS-isolation gap above closes — turning today's "no attribution
between routine and admin actions, both authenticate as `sliamh11`" gap into a free byproduct of
this design rather than a separate mechanism to build. Not implemented in this pass; named as a
design requirement for §7's follow-up.

**A named residual gap in the approval-integrity property, not glossed over**: the broker's approval
check would most naturally reuse this repo's existing verdict-store mechanism
(`scripts/warden_hooks/verdict_store.py`) as its "was this approved" signal. That store has no
signing, HMAC, or authentication of any kind — confirmed by direct read, only `fcntl`-based file
locking and a `hashlib.sha256` content-diff hash, neither of which authenticates *who* wrote a
verdict. It is a plain JSON file writable by whoever runs the CLI, under the same OS-user account
this design's own threat model treats as potentially adversarial. Today's `approve_admin_merge`
already carries this same trust assumption for `--admin` merges, and this design does not close it
for the broker's admin-capable surface either — it is a real gap in approval integrity, distinct
from (and not fixed by) the credential-containment property above.

**The invariant a real fix needs, named for the follow-up threat-model/implementation pass**: the
approval artifact must be unforgeable by any process running as the agent's OS user. Concrete
directions, not committed to in this pass: (a) run the broker as a genuinely separate OS user
(e.g. a `launchd` job with its own `UserName`, reachable only via a Unix socket the agent's user can
connect to but not write arbitrary files into), which converts "same-user, therefore forgeable" into
a real privilege boundary; or (b) an out-of-band approval channel (this repo already ships
WhatsApp/Telegram/Slack integrations) verified against the platform's own API using a credential the
agent session cannot reach, rather than a local file. Additionally, whatever mechanism is chosen must
bind approval to the **full request payload** (the actual ruleset JSON body or file diff, hashed or
held in broker memory), not a command string re-read from a writable path between approval and
execution — the first draft's broker design (mirroring `approve_admin_merge`'s literal
command-string hash) has this TOCTOU gap; approved bytes must be executed from what the approver
actually saw, not re-fetched.

## 5. Resolved (revised after threat-modeler review): `--admin` merge behavior is NOT uniform across rulesets

The first draft's §3 answered a narrower question than the one that matters, and a threat-modeler
pass caught the gap: it is **not sufficient** to ask "does `--admin` merge-bypass require the
Administration PAT permission" without also asking "bypass of *which* ruleset."

**Two independent GitHub-doc-sourced facts, both true and not in tension with each other once
properly scoped:**

- For GitHub's **classic branch protection** and rulesets that grant a `bypass_actors` entry to the
  repository-admin role (this repo's *existing* `main-branch-rules` ruleset does exactly this,
  confirmed live in `git-level-hard-backstop-design.md` §2: "the newer ruleset (`main-branch-rules`)
  ... has its own `bypass_actors` entry granting the repository-admin role `bypass_mode: "always"`"
  — i.e. this is not a hypothetical, it is this repo's own currently-active configuration): bypass
  eligibility is account-role-gated, not PAT-permission-gated, per GitHub's own documentation
  ("restrictions ... don't apply to people with admin permissions to the repository"). A
  fine-grained PAT's merge-endpoint permission requirement is Contents write, not Administration —
  so a routine credential issued under an admin-role account retains `--admin` bypass capability for
  checks governed by *this* ruleset, same as today.
- For LIA-522's **new** `main-attestation-backstop` ruleset specifically, `bypass_actors: []` is the
  entire point of the design — confirmed directly, `git-level-hard-backstop-design.md:83-85`: "A
  real hard backstop needs a rule with empty `bypass_actors` — enforced even against the repository
  owner's own admin action." Once that ruleset is active, **no credential and no account role** —
  not the routine credential, not the full-scope admin credential, not the interactive human's own
  `--admin` merge — can bypass the `attestation-verify` check via `--admin`. This is unrelated to
  which permissions any PAT holds; it is enforced by the ruleset's own configuration, which the
  first draft's §3 did not account for.

**Practical consequence, stated plainly, and corrected in round 3 (this paragraph previously
contradicted the routing fix below it — round 3 review caught the stale sentence, not deleted, just
scoped correctly)**: at the **GitHub-mechanism level**, `--admin` merge-bypass eligibility for the
repo's *existing* seven checks is account-role-gated, same as today, and provides **zero** bypass
capability against `attestation-verify` specifically once `main-attestation-backstop` is active, by
the ruleset's own design — this was already LIA-522's stated goal, and nothing here needs to build
anything additional to preserve *that* property; it only needed to stop conflating the two rulesets,
which the first draft did. **This is a statement about what GitHub itself enforces, not about this
design's own authorization path** — the paragraphs below (timing gap, then the corrected
authorization mechanism) govern whether an agent actually reaches `--admin` through this design's
proxy, which is a separate, stricter question resolved below.

**The live spot-check this design still needs before relying on the above operationally must target
the correct enforcement engine**: a ruleset with `bypass_actors: []` (matching
`main-attestation-backstop`'s actual configuration), not classic branch protection (the first
draft's mistake — testing against branch protection would validate a different mechanism than the
one deployed and could return a false-positive green). This spot-check remains listed in §7 as not
yet performed.

**A timing gap round-2 review surfaced and this design closes with a mechanism simple enough to
verify sound, not a redesign — corrected here after a verification-gate pass found this lead
sentence still called it "near-zero-cost," directly contradicted by the correction 22 lines below
in this same paragraph, the ninth recurrence of this document's persistent stale-framing pattern,
this time on the cost axis rather than the closure axis the canonical statement above governs**: the
real throughput cost is accepted as a deliberate Phase 1 tradeoff, not avoided — see the correction
immediately below for why, and §4's parallel framing for the settled position. `main-attestation-backstop` must stay `enforcement: disabled` until *both* LIA-527
Phase 2's write path and a still-undesigned Rego cutover rule land (confirmed directly,
`git-level-hard-backstop-design.md`'s Phase-2-dependency and fail-closed sections) — i.e. there is a
real window where this design, as stated so far, would let the routine credential run `--admin`
merges completely unbrokered, with **no backstop in force at all** for `attestation-verify`, since
the ruleset that would prevent it isn't active yet. Rather than accept that window as a residual gap,
this design **has the proxy intercept every PR-merge call — `--admin`-flagged or not, per the
post-threat-modeler correction in §3 — unconditionally, and route it to the human-approval broker**
(§3, §4 — this is the current, final mechanism after several intermediate designs across rounds 3-8
and a further code-review-gate correction that this document no longer restates in full here; see §8
for that history), independent of whether `main-attestation-backstop` is active.

**Correction (round 3): the previous version of this paragraph claimed unconditional broker-routing
"costs essentially nothing" — a third threat-modeler pass falsified this against the actual
codebase, not a hypothetical.** `src/linear-auto-merge.ts:213-216` states, as a load-bearing repo
fact: "`--admin` is the only viable merge path on this solo repo (LIA-215/LIA-147): branch
protection requires an approving review no second human can give, so GitHub-native `--auto` can
never complete" — and `:242` (`mergePr`) passes `--admin` on *every* pipeline merge; `merge_train.py`
does the same. Plain `gh pr merge` cannot land a PR here at all, on any repo path, autonomous or
interactive. Routing `--admin` unconditionally to a broker that requires *literal human* approval
would therefore gate **every autonomous merge** on a human, converting the autonomous pipeline into
a manual one — not a near-zero-cost fix. `merge_train.py:20-24` already documents that its subprocess
path exists specifically because per-merge interactive approval is impractical for the pipeline,
which predicts this control is exactly the kind that gets routed around under real pressure rather
than obeyed. **This document does not dismiss that prediction (round 9 fix — an earlier version left
it unrebutted, stated and then silently dropped)**: it is accepted as a real, bounded-duration risk
of Phase 1 specifically, not resolved. Two things distinguish this from a control that will simply
rot: (1) Phase 1's scope is narrow and temporary by design — a working Phase 2 auto-forward ticket
already exists (§7) as the pressure-release valve, not an open-ended promise; (2) **the real
structural answer, corrected in round 10 — an earlier version of this point (fail-closed-on-
non-response) answered a different failure mode than the one being rebutted here, since that
property describes what happens when a request IS routed to the broker, not whether the broker can
be routed around entirely**: `merge_train.py:20-24`'s prediction describes operators bypassing an
*inconvenient* control by finding another path around it — the actual structural defense against
that is §7's credential-retirement item (broadened after a further GPT pass from "rotate the
keyring token" to "retire every raw GitHub credential path an agent process can reach" — this repo's
own source confirms a second, independent `GITHUB_TOKEN` env-var path exists, §7). As long as *any*
of those paths — not only the keyring OAuth token (§1: `gist, read:org, repo, workflow`, live on
every session on this host) — remains usable, an agent frustrated by broker latency could in
principle fall back to it directly, bypassing the proxy entirely. **This makes retiring every such
path an explicit Phase 1 completion criterion, not an open-ended §7 item to get to eventually**:
until all of them are scoped down or removed from routine sessions' reach, the proxy/broker system
is advisory against §1's own threat model, not a real boundary, regardless of how sound the broker's
own logic is. The fail-closed-on-non-response property (§5, below) remains true and worth keeping,
but as a property of requests that already reach the broker — not as the answer to whether the
broker can be routed around.

**Mechanism, descoped in round 8 after four rounds (4-7) of a proposed auto-forward predicate each
proving unsound in a new way** — see §3's `gh pr merge --admin` bullet for the full history and
reasoning, not restated here (deliberately: restating it in two places is exactly what let this
paragraph go stale in round 4, a mistake this document does not want to repeat a third time). This
document's position, stated plainly rather than engineered around further: the cost concern above —
routing every autonomous merge to a human — is real and is **accepted as a deliberate Phase 1
tradeoff**, not solved by any caller-side shortcut. **Corrected (round 9): an earlier version of this
paragraph suggested `linear-auto-merge.ts`'s caller-side `queryPrChecks` self-check meant "most
brokered merges will already be green... the broker step adds latency, not usually a substantive
decision" — this is wrong, and a threat-modeler pass correctly rejected it.** `queryPrChecks` runs
inside the agent process — the *untrusted* side of the exact boundary the broker exists to enforce
(§1's own threat model). A compromised or prompt-injected agent's self-report is not evidence of
anything to the broker; treating it as such both mis-scopes the trust boundary and risks training a
human approver to rubber-stamp requests instead of actually checking them. **What the approver must
actually see**, sourced by the broker independently, not reported by the calling agent: the PR
identity, the exact head SHA being merged, and the target ref/branch — read by the broker itself from
GitHub, the same "never trust the caller's claim" principle §3 already applies elsewhere in this
design.

**The approval must bind to that exact SHA at execution time, not just at display time — round 10
fix, a real gap: showing the approver the correct SHA is necessary but not sufficient if the merge
that actually executes isn't pinned to it.** This is the same class of defect the (now-descoped)
auto-forward predicate closed for its own fast path at round 4 (TOCTOU) — the fix was deleted along
with the predicate at round 8 rather than migrated to the broker path, leaving a real window: the
allowlist permits unbrokered pushes to non-default-branch refs (§3), so an agent could get a SHA
approved, then push a new commit to the same PR head before the broker executes the merge, landing
unattested code under a human's approval of a different commit. **Fix**: every broker-executed PR
merge runs pinned to the approved SHA — `gh pr merge --match-head-commit <sha> [--admin]` (the flag
in brackets since, per §3's correction, brokering and pinning both apply regardless of whether the
caller's `gh` process happens to add `--admin`; the flag itself is not what determines whether
pinning applies) — `--match-head-commit` verified as a real flag earlier in this document's own
review history, §8's round-4/5 log —
and a head that has moved since approval **invalidates that approval**, requiring a fresh one against
the new head, rather than silently merging whatever the head currently is. **The target ref/branch
the approver saw must match at execution too, not just the head SHA** (round 11 addition, making
explicit what §4's payload-binding invariant — "approved bytes must be executed from what the
approver actually saw, not re-fetched" — already generalizes to).

**Two further GPT code-review co-gate findings on this exact point, the second correcting the
first's own fix**: (1) brokering the retarget operation itself is necessary but not sufficient — an
earlier version of this paragraph reasoned that since a base retarget (`PATCH .../pulls/{n}` with a
`base` field) isn't on §3's allow list, deny-by-default routes it to the broker "the same way the SHA
mismatch does," and concluded no separate enforcement was needed. That missed a real sequencing gap:
retargeting and merging are two *independent* brokered requests, each with its own approval — an
agent could get a merge of SHA X into base A approved, then separately get a retarget to base B
approved, and the original merge approval, untouched by the retarget's own approval, would remain
live. (2) **The first fix for this — "re-read the base immediately before executing" — is itself
incomplete, and this document names the gap honestly rather than claiming a fix that doesn't fully
hold**: unlike the head SHA, which GitHub's own merge endpoint accepts as an atomic server-side
precondition (`sha` parameter, `--match-head-commit` — the merge fails server-side if the head has
moved, no window for a race), there is **no equivalent atomic parameter for the base** on
`PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge` (confirmed earlier in this document, §3: the
endpoint accepts only `commit_title`/`commit_message`/`sha`/`merge_method`). "Read the base, then
merge" is therefore still a check-then-act race — a retarget could in principle land in the window
between the broker's read and the merge call executing, however small that window is. **This is a
real, named residual gap this design pass does not fully close, matching how §4 already names the
verdict-store forgery gap rather than claiming false completeness**: the recommended mitigation is
serialization — the broker must not allow a retarget request and a pending merge for the same PR to
both be in-flight simultaneously (queue one behind the other, per-PR), closing the race by
construction rather than by a narrower and narrower re-check window. This serialization mechanism is
not designed in this pass; named here as a specific requirement for §7's broker-implementation item.

**Bounding re-approval churn, so this doesn't become a
rubber-stamp risk in its own right (round 11)**: since non-default-branch pushes stay unbrokered by
design, an agent could in principle push repeatedly to force repeated re-approval prompts; a
re-approval request should show the approver what specifically changed since the prior request (new
commits, diff summary) — **sourced by the broker itself from GitHub, same as the PR identity/head
SHA/target ref above, never from the calling agent's own report** (round 12 clarification — stated
explicitly so this doesn't become B20's exact defect class in new text) — not an identical-looking
prompt, so repeated requests remain legible rather than becoming background noise an approver learns
to click through.

**Operational answer to a question raised in round 7 and left open until now**: this repo's existing
chat-channel integrations (WhatsApp/Telegram/Slack, already cited in §4 above as the out-of-band
approval-channel option) are the natural dispatch path for these requests, reusing infrastructure
rather than building new notification plumbing; and the broker is **fail-closed on non-response** —
an approval request that times out leaves the merge blocked in an explicit "awaiting approval" state,
never auto-approves, and never silently retries as if nothing were pending. **Every PR-merge call —
`--admin`-flagged or not, per §3's correction — is brokered, unconditionally and pinned**, until a
separately-designed, separately-tested Phase 2 auto-forward mechanism exists (§7) — this closes the
timing gap this section exists to fix completely and verifiably, at the cost of throughput this
document names rather than hides.
Once `main-attestation-backstop` is active, broker-routing remains load-bearing for the *other*,
`bypass_actors`-permissive rulesets this repo already has (the ruleset itself denies bypass of
`attestation-verify` specifically, per §5's account-role-gated analysis above, but says nothing about
this repo's other required checks, which a same-account merge would otherwise bypass unbrokered —
"`--admin`" here describes GitHub's own bypass mechanism, not a distinction this design's proxy can
or needs to make, per §3).

## 6. Blast radius if the routine credential leaks, independent of the adversarial-bypass question

Distinct from whether an adversarial process can defeat the boundary (§2-§4 above), this design's
core value proposition should also hold for the more mundane case of a leaked/exfiltrated routine
credential (compromised dependency, log exposure, a prompt-injection attack convincing an agent to
paste it somewhere).

**Under the raw-PAT-in-agent-hands approach (first draft)**: the narrowing is real for the
ruleset-disable and workflow-file-edit vectors specifically (those genuinely require Administration/
Workflows, which the routine PAT lacks) — but incomplete for the common leak case, since a leaked
routine PAT still yields the CI-secret-exfiltration path and the release/`NPM_TOKEN` path named in
§2, both of which are Contents-permission-reachable. The blast-radius reduction is smaller than
"Contents/PRs/Issues, no admin" suggests at face value.

**Under the proxy approach (§3, this revision's primary recommendation)**: a leaked credential
scenario changes shape, because there is no longer a raw GitHub token *in the agent's environment*
to leak in the first place — the proxy holds it. What could leak instead is the proxy's own local
access credential (e.g. a Unix-socket permission or a local auth token gating "can talk to the proxy
at all"), which at most grants everything the *allowlist* permits.

**The two vectors named in §2 no longer have the same status, and this section previously conflated
them (caught and corrected across two further GPT code-review co-gate passes, after the
merge-endpoint fix, the Rollback fix, and §3's own round-3-paragraph correction above)**:

- **CI-secret-exfiltration**: still genuinely open. §3 is explicit that ordinary Contents pushes and
  PR-open/`synchronize` operations must stay on the allowlist — routine automation genuinely needs
  them — and that the allowlist alone does **not** close the CI-secret-exfiltration path those pushes
  trigger (§3's own "Explicitly out of scope for this allowlist" bullet). This path needs no merge
  and no broker involvement at all — a PR-open alone triggers `ci.yml`. A leaked proxy-access
  credential, or a compromised agent reaching the proxy directly (no leak needed — see below),
  retains this path exactly as before, until the workflow-side hardening named in §2/§3/§7 actually
  lands.
- **Release/`NPM_TOKEN` via ordinary merge**: **closed *by design*, not yet closed *operationally* —
  corrected once more after a further GPT code-review co-gate pass found the closure claim below
  incomplete, not just per §3's round-3-paragraph update.** `release-please.yml` only fires on a
  commit landing on `main`, and landing on `main` (by merge or direct push) is now unconditionally
  broker-gated *for requests that actually reach the proxy* (§3). But this closure has **two
  preconditions this section previously left implicit, both already named elsewhere in this document
  but not tied explicitly to this specific claim**: (1) the proxy and broker must run with genuine
  OS-isolation from the agent (§3's still-open gap) — without it, a compromised agent doesn't need to
  ask the broker anything; it can read the proxy's own held GitHub credential directly (same OS user)
  and call `git push`/`gh pr merge` itself, bypassing the proxy's brokering logic entirely rather than
  defeating it; (2) *every* raw GitHub credential path an agent process can reach must actually be
  retired or routed through the proxy (§7's Phase 1 completion criterion, broadened from "rotate the
  keyring token" after this same code-review cycle found that scoping too narrow — confirmed real,
  this repo's own `src/index.ts:491` loads a second, independent `GITHUB_TOKEN` env-var credential
  path, currently unset but a documented config slot `gh` would prefer over the keyring token if
  set), not yet done for any of them — until then, any session on this host can simply use whichever
  un-rotated/un-retired path is available, with its original scope, never touching the proxy or
  broker at all. **Neither precondition holds yet.** So today, absent both fixes, an agent — leaked
  credential or compromised, no meaningful difference per the paragraph below — can still land a
  commit on `main` unbrokered, via either bypass. The *design*, once both preconditions are met, does
  close this vector by requiring broker approval for every merge (contingent, at that point, only on
  the broker's own approval-integrity — §4's separately-named, distinct residual gap, an
  unauthenticated verdict store). Stating this as unconditionally "closed" before those preconditions
  land would overstate what Phase 1, in its current unbuilt state, actually protects against.

The proxy's genuine, already-realized narrowing under a leak/compromise scenario is therefore
architectural, not yet operational: **once** OS-isolation and ambient-token rotation both land, it
covers the ruleset-admin, workflow-file-edit, direct-release-endpoint, default-branch/tag-ref, and
release-via-merge vectors — the release-via-merge closure being a consequence of the merge-brokering
fix, not something §3's allowlist does directly. **Until then**, none of these vectors are actually
closed for a same-OS-user compromised agent or a leaked proxy-access credential — both bypass routes
above route around the entire proxy/broker system, not just the merge-brokering rule specifically.
What remains genuinely open *regardless* of those two preconditions (i.e. even after they land) is
narrower than the original §2 framing suggested: CI-secret-exfiltration specifically, pending
workflow-side hardening.

**Distinct from a leak**: a compromised (not merely leaked-credential) agent doesn't need to
exfiltrate anything to reach the allowlist's permitted surface — it can simply call the proxy
directly, same as any well-behaved routine automation would. This is exactly why §3's OS-isolation
gap matters even for the "leak" framing this section is titled around: without it, "leaked
credential" and "compromised agent" collapse into the same case, since both reach the same proxy
from the same trust domain, and both retain the same CI-secret-exfiltration exposure named above —
**and, until OS-isolation and ambient-token rotation both land (the two preconditions named above),
both also retain the ability to bypass the proxy/broker system entirely for release-via-merge, not
just a shared exposure to one still-open vector**; only once both preconditions are met does
release-via-merge close equally for both cases, by the same broker gate.

**A related, previously-unconsidered risk this design does not resolve**: LIA-522 §3.3's
self-hosted-runner mitigations (first-time-contributor approval, an owner-actor guard) are keyed on
**actor identity** — and the routine credential, whether raw or proxied, ultimately authenticates as
`sliamh11`, the same identity those mitigations trust. A leaked routine credential (or, under the
proxy model, a compromised proxy or a leaked proxy-access credential) is therefore not just "narrow
GitHub API access" — it is a trusted, auto-approved trigger for LIA-522's self-hosted runner, which
executes on the user's own machine. This is a real, named residual risk this design does not close;
mitigating it is the same work as closing the CI-execution boundary in §2/§3 (minimal `permissions:`
per job, no untrusted checkout in privileged jobs) — not a separate mechanism, but worth stating
explicitly rather than leaving implicit.

## 7. Not yet started

Matching `git-level-hard-backstop-design.md`'s own precedent of naming deferred work rather than
implying it's done — expanded after the threat-modeler pass found the first draft's list itself
incomplete:

- **Actual fine-grained PAT creation** for whatever credential(s) the proxy ends up holding — a
  GitHub account action only the user can perform.
- **The live spot-check named in §5**, corrected to target a ruleset with `bypass_actors: []`
  (matching `main-attestation-backstop`'s real configuration), not classic branch protection.
- **A live spot-check of `--match-head-commit` under an actual `--admin` merge** (round 11 addition):
  the flag's existence and general semantics are verified against `gh`'s own help text, but its
  interaction specifically with `--admin` (does the SHA precondition still apply when bypassing
  required checks, or does `--admin` bypass it too) is not — this document has already had two
  technical claims falsified by live checks that generic verification missed (§8: the Administration-
  permission claim, made round 6 and withdrawn round 7; the `.strict`-field-location claim, made
  round 7 and falsified round 8), so this is named explicitly rather than assumed safe by association
  with the already-verified flag.
- **The proxy service itself** (§3) — this design proposes extending `credential-proxy.ts`'s pattern,
  specifies the allowlist's required denials at a high level, and (after round-2 review) specifies
  the transport invariant correctly (no credential-helper, no bypassable wrapper — a genuine
  network-path proxy injecting auth server-side). It does not implement the proxy, fully enumerate
  every allowed/denied endpoint, or — the round-2 finding still open — run the proxy as a separate
  OS user from the agent, which §3 now states explicitly is required before this design's
  containment claim holds against a compromised (not just careless) agent.
- **The broker/approval-gate mechanism** (§4) for the proxy's full denied surface (§3 is the
  authoritative list, not re-enumerated here — round 10: this is the fourth place that enumeration
  had started drifting toward being restated, closing it off pre-emptively) — including resolving the
  approval-integrity gap named there (separate-OS-user broker vs. out-of-band channel), the
  head-SHA-pinning fix for the TOCTOU gap (§5), and — a further, distinct TOCTOU gap a GPT
  code-review co-gate pass found unclosed by the head-SHA fix's own pattern applied naively to the
  base ref (§5) — **per-PR serialization between retarget and merge requests**, since the merge
  endpoint has no atomic "expected base" precondition analogous to `--match-head-commit`'s `sha`
  parameter, so a "read the base, then merge" check-then-act sequence alone cannot fully close the
  race. The separate-OS-user requirement here and the proxy's own (above) are the same underlying
  mechanism — likely a single piece of infrastructure to build, not two.
- **The proxy's transport-level ref-update filtering for direct default-branch/tag pushes**
  (round 7/8, §3) — denying/brokering this is specified as one transport-independent invariant, but
  the mechanism (inspecting `git-receive-pack` traffic for the target ref, in addition to filtering
  the equivalent REST mutations) is not yet designed.
- **Phase 2: a well-tested PR-merge auto-forward fast-path** (round 8 addition, replacing the
  four-round predicate design attempt this document's §3/§5 now explicitly abandon; scope corrected
  post-threat-modeler at the code-review gate — see §3 — from "`--admin`-flagged merges" to "PR merges
  generally," since the proxy cannot observe the `--admin` flag at all) — real,
  valuable follow-up work, deliberately scoped OUT of this design pass and out of its review history:
  rounds 4-7's attempts converged on needing a per-rule-type verification classification
  (not-implicated / verifiable-on-ref-update / verifiable-on-pinned-SHA / unverifiable) tested against
  this repo's live GitHub API responses — work needing real integration testing, which is why this
  document stops proposing new predicate designs rather than attempting a ninth round. A future
  ticket, scoped narrowly to just this optimization, with its own oracle tests against a real (ideally
  disposable/test) repo's actual rule configuration, is the recommended path — not another
  design-review pass on this document. **Two scoping questions for that future ticket, answered here
  so it doesn't need to re-derive them (round 9)**: (a) whether the broker should support a
  time-boxed/batch approval ("approve the next N merges of this run") rather than strictly one
  approval per merge is an open UX question for that ticket, not decided here — either is compatible
  with this design's fail-closed-on-non-response invariant (§5); (b) Phase 2's auto-forward, once
  built, should cover `--admin` merges bypassing this repo's *other*, `bypass_actors`-permissive
  rulesets (§5) — it does not need to (and structurally cannot) cover `main-attestation-backstop`
  itself, since that ruleset's `bypass_actors: []` means no credential or mechanism can auto-forward
  past it once active; GitHub's own server-side enforcement is already the backstop for that specific
  check.
- **A full workflow-hardening specification for the CI-execution path** (§2/§6 — the
  release-via-merge path is closed by the merge-brokering fix once §3's OS-isolation and §7's
  credential-retirement item below both land, §3/§6, and does not need *workflow-side* hardening on
  top of that to close) — e.g. pinning `permissions:` per workflow job to the
  minimum, moving secret-bearing steps behind a GitHub Environment with required reviewers, `npm ci
  --ignore-scripts` where applicable. This design names the requirement and candidate mitigations;
  it does not choose or implement one.
- **Credential lifecycle** for whatever the proxy ends up holding — storage mechanism (macOS
  keychain, not a plaintext file), expiry (fine-grained PATs cap at 1 year — a rotation cadence needs
  picking), and a revocation path. Not specified in this pass.
- **Auditing whether `secrets.PAT`** (already in use by `release-please.yml:18` and
  `sponsors.yml:18`) **is itself a broad-scope classic PAT** — if so, it is a second admin-capable
  credential this design has not inventoried, independent of anything this design's own routine/admin
  split covers (this is a GitHub Actions secret used by CI jobs directly, not by the agent's own `gh`
  credential at all). Not discoverable from the repo alone (it's a live GitHub secret). **Priority,
  corrected after §3/§6's merge-brokering fix reduced one (but not the only) reason to audit this**:
  round 2 review originally flagged this partly because a leaked/compromised *routine* credential
  could trigger `release-please`'s use of `secrets.PAT` via an unbrokered ordinary merge — that
  specific reasoning is reduced (not eliminated) once every merge is broker-gated **and** §3's
  OS-isolation and §7's credential-retirement item (a separate bullet in this same section, not this
  one) both land (§6's full treatment of why both preconditions are needed, not just merge-brokering
  alone). The audit remains worthwhile for the
  separate, still-live reason regardless of Phase 1's build status: if `secrets.PAT` is itself
  broad-scope, it's an unaudited second admin-capable credential in this repo independent of anything
  this design's own routine/admin split covers, and should be scoped as part of the same
  implementation effort rather than a separately-forgotten follow-up — but once Phase 1's
  preconditions are actually met, it is no longer "on the critical path" of closing this design's own
  leaked-credential blast radius (§6) the way it was under the original raw-PAT approach.
- **Env-injection wiring** — getting agent sessions pointed at the local proxy (rather than raw `gh`
  credentials) for both interactive and background/cron-launched Claude Code CLI sessions. This
  touches host-level Claude Code CLI infrastructure outside `~/deus`'s own version control (this
  session confirmed no existing process-level signal reliably distinguishes "autonomous session" from
  "interactive session" in this repo's own source beyond an inferred `CLAUDE_JOB_DIR`-style marker,
  whose exact scope of applicability wasn't independently confirmed beyond this session's own
  environment).
- **Retiring every raw GitHub credential path reachable by an agent process — an explicit Phase 1
  completion criterion, not an open-ended item, and broader in scope than this document previously
  stated (round 10 named only the keyring token; a GPT code-review co-gate pass found that scoping
  too narrow)**: this document's earlier drafts framed this item as "rotate the ambient keyring
  OAuth token's scope down" — true but incomplete, since the keyring token is not the only raw
  credential path this repo's own source exposes to agent processes. **Confirmed directly, not
  assumed**: `src/index.ts:491` loads `GITHUB_TOKEN` from an env file into `process.env` for the
  Linear-dispatcher subsystem (currently unset in this session, but a documented, legitimate
  configuration slot per `.env.example:248` — `gh` CLI prefers `GH_TOKEN`/`GITHUB_TOKEN` over the
  keyring credential whenever either is set, per general `gh` behavior). If populated, this env var
  is a second, entirely independent bypass of the keyring-token-focused rotation this document
  previously described alone — rotating the keyring token down does nothing to an already-set
  `GITHUB_TOKEN`. A same-OS-user compromised agent (or a leaked env file) reaching either path calls
  GitHub directly, same as reaching the keyring token today. **A third potential path, named for
  completeness though not currently live in this session's environment (`ssh -T git@github.com`
  returns `Permission denied`, confirmed directly)**: git's default push transport for this repo is
  HTTPS (`git remote -v` confirms `origin`/`deus-v2-origin` both use `https://github.com/...`), but
  an agent could change the remote URL to an SSH form and push via any SSH key registered with
  GitHub, if one exists — this repo's design should not assume no such key will ever be registered,
  the same "don't assume the current state persists" discipline this document applies to every other
  claim in it.

  **The completion criterion, corrected**: Phase 1 is not "done" in the sense that matters until
  *every* raw-credential path an agent process can reach — the keyring token, `GITHUB_TOKEN` (or any
  equivalent env-var/config-file credential this or a future subsystem loads), and any
  GitHub-registered SSH key reachable via a remote-URL change — is either removed from agent reach
  entirely or routed through the same proxy/broker discipline this design proposes for the primary
  credential. This must happen **after** (a) the proxy and its injection path both exist and are
  verified working, **and** (b) the workflow-side hardening named in §2/§3 has actually landed —
  rotating down before (a) leaves automation with no working credential path at all; rotating down
  before (b) leaves the CI-secret-exfiltration vector open (round-3-correction, §3/§6: the
  `NPM_TOKEN`-via-merge vector's closure is governed by the merge-brokering fix, not by this bullet's
  workflow-side hardening condition — but note this item itself is one of *that* closure's own two
  preconditions, §6; condition (b) above governs only the still-open CI-execution vector) while
  giving the false impression that "credential separation is done" even once this item lands, absent
  (b). **This document now states plainly (§5, the canonical statement in §3) that until every raw
  credential path is retired or routed through the proxy, the proxy/broker system is advisory, not a
  real boundary**, against §1's own threat model — any of these paths remains reachable by any
  session on this host regardless of how sound the proxy's allowlist or the broker's approval logic
  are. This is real, unfinished-implementation-level scoping work this design pass names but does not
  perform — enumerating every credential-loading code path in this repo (beyond the one confirmed
  above) is out of scope here and belongs to the implementation ticket.
- **The proxy's own failure mode** (round 3 addition, `dos-surface`): once agent sessions route
  through the proxy, it becomes a hard dependency for all `git`/`gh` traffic, including the
  unattended autonomous pipeline. The design's position is fail-closed — if the proxy is unreachable,
  automation halts rather than silently falling back to an ambient credential (which would reopen
  every gap this document exists to close). Not implemented in this pass; named as a requirement for
  whoever builds the proxy.
- **A follow-up threat-modeler pass** on the concrete allowlist specification and broker
  implementation once those exist, since this pass reviewed the mechanism at a design level, not a
  fully specified one.

## 8. Review discipline applied to this design pass

Plan (this document's implementation plan): plan-reviewer SHIP (Claude, after 5 REVISE rounds) + GPT
co-gate SHIP, both on the plan preceding this document's first draft.

**Threat-modeler round 1: BLOCK.** Findings: the raw-PAT approach didn't close CI-secret-
exfiltration or release/`NPM_TOKEN` paths (§2, §6), and §3's `--admin` conclusion contradicted
LIA-522's own `bypass_actors: []` mechanism without reconciling it (§5). Revised: replaced the
raw-PAT approach with a proxy (§3), separated the two `--admin`-bypass mechanisms (§5).

**Threat-modeler round 2: BLOCK.** Confirmed §5's `bypass_actors: []` reconciliation held. Two new
findings, both genuine and not present in round 1's version of the design (round 1 reviewed a
materially different mechanism): (B1) the proxy's allowlist claim didn't actually close the
CI-execution half of §2's problem for unfiltered-trigger workflows like `ci.yml` — the doc's own §7
remediation list was workflow-side, silently contradicting §3's "concrete answer" framing; (B2) the
"credential containment... holds even if the approval mechanism is imperfect" claim assumed an
OS-user isolation boundary the design never actually specified for the proxy (only named for the
broker). Revised: §3 now states plainly that the CI-execution path is NOT closed by the proxy alone
and requires workflow-side hardening as a sequenced prerequisite, not a solved problem; §3/§4 now
require separate-OS-user isolation for both the proxy and the broker before any containment claim
holds, downgrading the claim to API-surface-reduction until that lands; §5 now routes `gh pr merge
--admin` unconditionally to the broker rather than leaving a window where it's unbrokered while
`main-attestation-backstop`'s own activation is still pending; §7 elevates the `secrets.PAT` audit
question raised in round 2's own questions-for-the-author.

**Threat-modeler round 3: BLOCK.** Confirmed §5's broker/proxy separation and the CI-execution
disclaimer were both honest and correctly scoped. Three findings, explicitly assessed by the
reviewer as traceable to this round's own edits (a narrowing claim becoming load-bearing, and two
new claims introduced by the round-2 fix itself) rather than a non-convergent moving target: (B3)
§3's narrowed "closes the release/`NPM_TOKEN` half completely" claim was still false — an ordinary
merge to `main` reaches `NPM_TOKEN` via `release-please.yml` → `publish-packages.yml` without the
routine credential ever calling the release-creation endpoint directly, so the proxy only closes the
*direct*-endpoint sub-case, not the vector as a whole; (B4) §5's "costs essentially nothing" claim
for unconditional `--admin`-to-broker routing was falsified directly against
`src/linear-auto-merge.ts:213-216,242` — `--admin` is this repo's *only* viable merge path, not an
occasional override, so blanket human-approval routing would gate every autonomous merge; (B5) a
stale sentence in §5 still asserted unbrokered `--admin` availability, contradicting the routing fix
added later in the same section. Revised: §3 now states both release-path and CI-execution vectors
as workflow-side, elevates `secrets.PAT` auditing onto the critical path (not just pre-implementation
hygiene); §5 replaces blanket human-approval routing with proxy-side self-verification of required
checks (auto-forward when checks are independently confirmed green, broker only for non-green
`--admin`) — preserving pipeline throughput while still closing the timing gap; the stale sentence is
corrected to state only the GitHub-mechanism-level fact, deferring the authorization path to the
paragraph below it. Also addressed: an inline containment-claim qualifier (§4), explicit naming of
Actions-dispatch endpoints in the deny list (§3), the proxy's fail-closed failure mode, and the
rotation-ordering predicate (§7).

**Threat-modeler round 4: BLOCK.** This document was set to "Manual Review Required" in Linear
(LIA-531) after this round, per this session's own gate-blocking discipline (stop after 3 blocks
rather than force a 4th) — round 4 was itself a 4th attempt, already past that threshold, and was
followed by a deliberate pause rather than an immediate round 5. Two findings, both introduced by
round 3's own fix and confirmed by the reviewer as cheap, sentence-level corrections rather than a
design-direction problem: (B6) the round-3 auto-forward predicate ("every required check is
`SUCCESS`") was unsound — vacuous-green on an incomplete/absent required-check set (exactly the race
`scripts/ci/wait_for_checks.py` exists to guard against) and no binding between the checked commit
and the merged one (`mergePr` has no `--match-head-commit`), a real TOCTOU gap; (B7) the round-3 fix
for round-2's stale-sentence problem (B5) was itself incomplete — two more references to the
superseded "unconditional broker routing" survived elsewhere in §5 and §7, the same defect class
recurring. After the pause, this session resumed (user: "lets continue") and made one further,
carefully cross-checked revision: §3's predicate now queries a specific head SHA against the known
required-check set (not a vacuous "whatever's present" check) and only auto-forwards a merge pinned
to that exact SHA, treating anything else (not-green, missing checks, or a moved head) as broker
territory; every auto-forwarded merge is logged with its verified SHA (closing the audit-trail
recommendation from round 4 at the same time); the duplicate mechanism description in §5 was
replaced with a pointer to §3's single authoritative version specifically to prevent a third
stale-duplicate recurrence, rather than re-describing the mechanism a second place it could drift
from again; §4's and §7's enumerations of the broker's covered surface were updated to match; a
self-audit grep pass across the full document (for "unconditionally," "blanket," "costs essentially
nothing," and the old vacuous-green predicate phrasing) found no further stale references before
resubmission. The merge-queue (`merge_group`) divergence question round 4 raised is named as a
still-open item in §3/§7, not resolved in this pass.

**Threat-modeler round 5: BLOCK, but confirmed convergence** — both round-4 findings closed (B7's
stale-cross-reference class confirmed fully clean via a full-document grep sweep, not just the
flagged spots; B6's TOCTOU half confirmed fixed, `--match-head-commit` verified as a real `gh` flag
with exactly the claimed semantics). One new, narrow finding (B8): the round-4 fix for B6's
vacuous-green half pointed to `wait_for_checks.py` as the source of "known required check set"
semantics, but that script only fail-closes the zero-required-checks case — it has no
partially-registered-set handling and no branch-protection/ruleset-API read, so it couldn't actually
provide the property claimed. Revised: the predicate now derives the required-context set from
authoritative server-side configuration directly (branch protection's `required_status_checks`
**and** the ruleset API, both engines this repo uses), corrects the `wait_for_checks.py` citation to
its actual, narrower guarantee, folds the `merge_group` caveat into the same predicate fix (it's a
set-membership question, not just a query-selection one — `--admin` bypasses the merge queue
entirely per `gh pr merge --help`, which narrows but doesn't fully resolve it), answers two
previously-silent implementation questions (fresh-read vs. cached required-set, and whether the
proxy's own check should still cover `attestation-verify` once the ruleset is active — yes, cheap
belt-and-suspenders), and collapses §5's redundant case-list restatement (the same
duplication-drift risk that caused round 4's B7) down to a bare pointer at §3.

**Threat-modeler round 6: BLOCK.** Confirmed B7 (stale cross-references) and B6's TOCTOU half fully
closed. One new finding (B9/B10, closely related): the round-5 fix for the vacuous-green half scoped
the predicate to "what GitHub enforces for an `--admin` merge" — which round 6 correctly identified
as empty by construction against this repo's live config (`enforce_admins: false`, admin
`bypass_mode: always`), so that framing auto-forwarded every merge, reintroducing the exact defect
the predicate exists to prevent. Revised: reframed the predicate to check "what this repo requires
of a *normal, non-bypassing* merge" instead — over-inclusion here is fail-safe (costs extra broker
routing, never a wrong authorization); made the empty/unreadable-set case explicitly not-green rather
than left to infer; resolved (not just narrowed) the `merge_group` question — this repo doesn't have
GitHub's merge queue enabled at all, so there's no divergence to resolve; and answered a new
credential question round 6 raised and this session independently verified against GitHub's own
docs — reading `required_status_checks.contexts` requires Administration permission even to read,
so the proxy needs a third, Administration-*read-only* credential tier distinct from both the
routine and admin credentials, named but not yet built. Three more stale cross-references from
round 5's own edits were found and fixed (top-of-doc round count, §5's merge_group reference, §7's
merge-queue bullet).

**Threat-modeler round 7: BLOCK.** Confirmed round 6's vacuous-green fix (B9) fully closed and the
stale-reference sweep clean. Three new findings in the same predicate/allowlist surface: (B11, the
most serious of the whole review) a direct `git push` to `main` was never denied by the allowlist,
completely bypassing the `--admin` predicate this design spent rounds 4-7 hardening — live-verified,
this repo's `main` has `enforce_admins: false` and admin `bypass_mode: always`, so a direct push
lands unattested code with zero checks; (B12) the round-6 claim that reading required-checks data
needs Administration permission was itself wrong — a live API check found `Contents`/`Metadata`
read access sufficient, so the proposed third credential tier was unnecessary privilege and is
withdrawn; (B13) the predicate's "check required status contexts" framing was under-inclusive
(missed `strict`/up-to-date-with-base and `required_signatures`) — resolved not by adding more
enumerated cases but by inverting the predicate's structure per the reviewer's own suggestion: ask
"what rules apply here that this merge would bypass" (a single `GET .../rules/branches/{branch}`
call) and independently verify each, fail-closed by construction, rather than hand-enumerating rule
types that future review rounds keep finding gaps in. Revised: added default-branch push denial
(and named the git-protocol-level transport gap this reveals, not yet fully designed); withdrew the
third credential tier; reframed §3's predicate around the rules-API read with one named, deliberate
exception (the review-count rule, which this repo's own design already treats as unsatisfiable by
construction — the entire reason `--admin` exists).

**Threat-modeler round 8: BLOCK — five findings (B14-B18), and the round-8 verdict itself became the
trigger for a re-scope rather than another patch.** B14: round 7's claim that `GET
.../branches/{branch}` returns `.strict` was itself wrong (that field lives only under
`.../branches/{branch}/protection`, Administration-scoped) — a second instance of an unverified
technical claim from this session being falsified by a live check, after round 6's Administration
claim. B15: the round-7 "verify every rule on the pinned SHA" framing was a category error for 4 of
5 of this repo's live rule types (signatures/deletion/non-fast-forward/copilot-review have no sound
per-SHA verification semantics), and its own fail-closed catch-all would therefore route nearly every
merge to the broker anyway — reproducing round 3's B4 by a different path. B16: the predicate
derived its rule set entirely from *live* configuration, so during the pre-`main-attestation-backstop`
window (that ruleset doesn't exist yet) it would auto-forward unattested code — directly contradicting
§5's stated purpose. B17: the direct-push fix was scoped to one transport (git protocol) while REST
mutations of the same ref were left open, and the tag-push contradiction (allowed by one bullet,
denied by another) went unnoticed. B18: the review-count exception was scoped to the whole rule
object rather than the one parameter it was designed around.

**Re-scope, not a ninth patch**: given eight rounds — four of them (4-7) spent entirely on the
`--admin` auto-forward predicate, each fixing the previous round's unsoundness by introducing a new
one — this session concluded the predicate is a genuinely hard sub-problem not well-suited to further
text-based design review, and descoped it out of this design pass entirely (§3, §4, §5 revised): every
`--admin` merge now routes to the broker unconditionally, accepting round 3's B4 cost as a named,
deliberate Phase 1 tradeoff rather than continuing to engineer around it. This closes B14/B15/B16 by
removing the mechanism they were findings against, rather than patching that mechanism a fifth time.
B17 and B18 no longer apply (their target text was removed); the direct-push allowlist fix itself
(round 7's actual contribution, independent of the predicate) is retained and generalized to a single
transport-independent invariant per B17's REST/tag gap. A Phase 2 ticket for the auto-forward
optimization, with its own live-API-tested oracle, is named in §7 as the correct venue for that work
going forward — not this document.

**Threat-modeler round 9: BLOCK — but explicitly confirmed the descope itself was sound and didn't
reopen the design direction.** Two findings, both narrow: (B19) the allow bullet's "any ref other
than the default branch" still permitted tag pushes despite the deny bullet and invariant denying
them — the tag half of round 7/8's fix was closed on the deny side only; (B20) a newly-added
tradeoff-rationale sentence treated the agent's own caller-side `queryPrChecks` self-check as
reducing the broker's decision to "not usually substantive" — wrong, since that self-check runs
inside the very untrusted process the broker boundary exists to check, and stating otherwise risked
training a human approver to rubber-stamp. Also flagged (non-blocking): three remaining stale
references to the abandoned predicate mechanism my own self-audit missed, a third duplicated
enumeration of the denied surface in §4, and an unrebutted "this control gets bypassed, not obeyed"
prediction left standing without a response. Revised: scoped the allow bullet to `refs/heads/*`
excluding the default branch; rewrote the tradeoff paragraph to state what the broker must
independently verify (PR identity, head SHA, target ref — read by the broker itself, not the
caller's report) and removed the "not usually substantive" framing; rebutted the bypass-risk
prediction directly (bounded Phase 1 scope with an existing Phase 2 ticket as the release valve, and
fail-closed-on-non-response meaning bypass isn't silently available); fixed the three stale
predicate references; collapsed §4's enumeration to a pointer at §3; answered round 7's operational
question (approval requests route over this repo's existing chat-channel integrations; unanswered
requests block explicitly rather than timing out into either auto-approval or a hang); and answered
two forward-looking scoping questions for the Phase 2 ticket in §7 so it doesn't need to re-derive
them.

**Threat-modeler round 10: BLOCK — one finding (B21), explicitly characterized by the reviewer as
the sole remaining blocker, confirmed traceable to this round's own edits, "one sentence" to fix,
with SHIP expected once closed.** B21: the broker approval (§5's round-9 fix) specified what the
human approver must see (PR identity, head SHA, target ref, read by the broker itself) but never
required the merge that *executes* to be pinned to that same SHA — the exact TOCTOU class the
(now-descoped) auto-forward predicate closed at round 4, whose fix was deleted along with the
predicate at round 8 rather than migrated to the broker path. An agent could get a SHA approved, then
push a new commit to the same PR head (unbrokered, since non-default-branch pushes are allowed) before
the broker executes, landing unattested code under approval of a different commit. Revised: every
broker-executed `--admin` merge now runs pinned to the approved SHA
(`gh pr merge --match-head-commit <sha> --admin`), with a moved head invalidating the approval rather
than silently merging the new head — stated in both §3 and §5. Also applied three non-blocking
recommended controls from the same round: made the allow bullet transport-symmetric with the deny
invariant (closing an availability gap against this repo's own documented `--admin --delete-branch`
cleanup pattern, PRs #1106/#1115); stated the structural reason plain `gh pr merge` is exempt from
the default-branch invariant (GitHub's own required-checks/reviews gate it server-side) rather than
leaving it a bare exemption; and corrected the bypass-risk rebuttal's mismatched second ground —
swapped "fail-closed on non-response" (which answers a different failure mode) for the real
structural defense, ambient-token rotation, now stated as an explicit Phase 1 completion criterion
rather than an open-ended §7 item.

**Threat-modeler round 11: SHIP.** "B21 is closed correctly and consistently; no blocking gaps
remain, and no unmitigated High-impact threat survives verification." Full-document stale-reference
sweep clean. Four non-blocking recommended controls applied in this same round before commit: two
wrong section pointers for the fail-closed-on-non-response property (cited as §4, actually defined
in §5) corrected; a live spot-check of `--match-head-commit`'s interaction specifically with
`--admin` added to §7 (the flag's general existence is verified, its behavior under an actual admin
bypass is not, and this document has twice had a technical claim falsified by exactly this kind of
unverified-by-association gap); the target ref/branch named alongside the head SHA as something the
broker's execution must also match, not just display; and re-approval-churn UX guidance added so a
legitimate design property (unbrokered non-default-branch pushes forcing fresh approval on every
head move) doesn't become its own rubber-stamp risk through repeated, indistinguishable prompts.

**Threat-modeler round 12 (fast confirmatory pass): SHIP, re-confirmed on the file as it actually
stands** after round 11's four recommended controls were applied — "ready to proceed to
code-reviewer + GPT co-gate; the three recommendations above are hygiene and can be applied or
dropped without another threat-modeler round." Those three (structural note on base-ref brokering,
explicit broker-sourced attribution for the re-approval diff summary, and a round-6/round-7 citation
consistency fix) were applied directly per that explicit clearance, with no further review round
needed.

**Code-reviewer SHIP (round 1 REVISE, round 2 SHIP)**: round 1 found INDEX.md asserting a specific,
decided credential-tier design the source document doesn't actually commit to (fixed — §7's genuine
deferral restored), and a stale round-count self-reference (fixed to match §8's actual log). Applied
one recommendation (non-blocking): added this document's own `## Rollback` section, matching
`git-level-hard-backstop-design.md`'s structural convention for a document proposing a hard pipeline
dependency. Round 2: SHIP, both findings verified fixed, no regressions.

**GPT code-review co-gate: BLOCK, one CRITICAL finding — the most consequential single finding in
this document's entire review history, caught only after all 12 threat-modeler rounds had already
passed.** `--admin` is a `gh`-CLI-client-side concept with no corresponding parameter on GitHub's
actual merge endpoint (verified independently against GitHub's REST API reference: the endpoint
accepts exactly `commit_title`/`commit_message`/`sha`/`merge_method`, nothing bypass-related). Since
bypass eligibility is account-role-gated (already established in §5, for a different reason) and the
routine credential authenticates as the *same* account as the admin credential (a PAT narrows
permission categories, not the underlying account's role), the routine credential retains full bypass
eligibility for **every** merge call it makes — a network-level proxy (this design's own transport
model, §3) cannot observe the `--admin` CLI flag at all, so there was no way to broker only
"intended-as-bypass" merges while letting "plain" ones through unbrokered, as every prior version of
this document assumed. **Fixed**: every PR-merge call — `--admin`-flagged or not — is now broker-tier,
unconditionally, executed pinned to the approved head SHA, with no CLI-flag-based distinction
anywhere in the design. This is recorded plainly as a real gap 12 rounds of threat-modeling missed,
not smoothed over — the two review mechanisms (adversarial security review vs. code-level
correctness review) caught genuinely different classes of defect here, which is exactly why this
repo runs both rather than treating either as sufficient alone.

**Code-reviewer SHIP (round 3, confirming the merge-endpoint fix above)**: fix verified sound
independently against GitHub's live API, all cross-references consistent, framing honest.

**GPT code-review co-gate, second pass: REVISE — one MAJOR finding on this document's own `##
Rollback` section** (added earlier this round): the "Partial rollback" bullet reintroduced the exact
flaw the merge-endpoint fix above closes, by suggesting a revert path to the old
`approve_admin_merge` marker-file flow as a "legitimate intermediate state" — that flow has the same
CLI-flag-blindness and no authentication (§4's named gap). **Fixed**: the bullet now states plainly
this is not a safe intermediate state, generalizing the merge-endpoint finding to the marker-file
flow rather than treating it as a separate concern. **Code-reviewer SHIP (round 4, confirming this
fix)**: both load-bearing claims trace to controlling lines already present and reviewed earlier in
this document, not new unverified assertions; no other place still frames the old marker-file
revert as legitimate.

**GPT co-gate: SHIP** on the Rollback fix. **A subsequent Claude code-reviewer pass (verification-
gate-triggered) then found the doc's own Status/Revision-note/§5 summary still described the
pre-merge-endpoint-fix rule** ("every `--admin` merge" rather than "every PR-merge call") — fixed
with a consistency sweep across those spots.

**GPT co-gate, re-run on that sweep: one further MAJOR finding** — §6's leaked-credential
blast-radius analysis contradicted §3's own explicit position: §6 implied the allowlist itself must
exclude the CI-secret-exfiltration and release/`NPM_TOKEN`-via-ordinary-merge paths, when §3 states
those stay open under the allowlist alone, closed only by separate workflow-side hardening. This
first fix (round A) brought §6 into agreement with what §3 said *at that point in the document's own
history* — but §3's own text on this point predated the merge-endpoint fix later in the same
document, and was itself stale.

**GPT co-gate, re-run once more on that fix: one further, more consequential MAJOR finding**,
resolving a real load-bearing contradiction rather than a wording mismatch — since every PR-merge
call is now unconditionally broker-gated (the merge-endpoint fix, §3), and `release-please.yml` only
triggers on a commit actually landing on `main`, **the release-via-ordinary-merge vector is not
open at all — it is closed by the merge-brokering fix**, contingent only on the broker's own
approval-integrity (§4's separately-named gap). Only CI-secret-exfiltration (which fires on a PR
open/update, no merge required) remains genuinely open. **Fixed**: §3's own
round-3 correction paragraph, §6 in full, §7's `secrets.PAT`-audit-priority item, §7's rotation-
ordering item, and §7's workflow-hardening-specification item were all updated to state the release-
via-merge vector as closed and the CI-secret-exfiltration vector as the sole remaining open one.

**A subsequent Claude code-reviewer pass found this propagation itself incomplete: §4's own
value-proposition paragraph still paired `NPM_TOKEN`-via-ordinary-merge with CI-execution under a
single "attribute to workflow-side hardening" claim** — the same stale-attribution pattern this
round's fix was meant to eliminate everywhere, missed in one more location. **Fixed**: §4 now states
the allowlist-alone improvement and the merge-brokering-fix improvement as two separate claims, only
the latter covering `NPM_TOKEN`-via-merge. A full-document `grep -n "workflow-side hardening"` sweep
after this fix confirmed no other paragraph pairs the two vectors under one attribution.

**GPT co-gate, re-run on the §4 fix: one further MAJOR finding — the "closed" claim for
release-via-merge was correct architecturally but stated without its actual preconditions,
overstating what Phase 1 protects against in its current, not-yet-built state.** §3 already names an
open OS-isolation gap (the proxy/broker share the agent's OS user) and §5/§7 already name
ambient-token rotation as a still-pending Phase 1 completion criterion — but §6's "closed... 
contingent only on the broker's own approval-integrity" claim, and matching language in §3/§4/§7,
never connected those two already-named gaps to the release-via-merge closure claim specifically.
Without OS-isolation, a compromised agent doesn't need to defeat the merge-brokering rule — it can
read the proxy's held credential directly and bypass the proxy/broker system entirely; without
rotation, any session can simply use the still-live ambient token, same bypass. **Fixed**: every
"closed" claim for this vector (§3's round-3-correction paragraph, §6 in full, §7's `secrets.PAT` and
workflow-hardening items) now states the OS-isolation and ambient-token-rotation preconditions
explicitly, and describes the closure as achieved *once Phase 1 is actually built to spec*, not as a
property already true of this design document's mere existence.

**A subsequent Claude code-reviewer pass found this propagation itself still incomplete, at two more
locations — the identical duplication-drift failure mode recurring a further time**: §4's
value-proposition paragraph still stated release-via-merge closure unconditionally, contradicting
§6's corrected two-precondition framing directly below it in the same document; and the top-of-doc
revision note (the document's own compressed summary, read before any section) still described
closure without the caveat, predating this round's finding. **Fixed**: both updated to match §6's
authoritative framing. A subsequent full-document `grep -n "\bclosed\b"` self-audit (all ~40 hits,
per the reviewer's own recommendation given this propagation gap had now recurred at least six times
across this document's history) found one further instance — §7's ambient-token-rotation item itself
stated the `NPM_TOKEN`-via-merge closure without noting that *this rotation item's own subject* is
one of that closure's two preconditions — fixed to make that connection explicit rather than leaving
it to be inferred.

**A subsequent verification-gate pass (run before committing) found this recurrence pattern had
struck an eighth time — two more locations, both in §3 near the release-endpoint/CI-execution
discussion**: the `secrets.PAT`-audit-priority sentence stated it "is no longer reachable... that
merge itself is now brokered" as present-tense fact, contradicting §6's own explicit "Neither
precondition holds yet... can still land a commit on `main` unbrokered" statement about today; and a
nearby sentence attributing the ruleset-admin/workflow-file-edit/release-creation vectors to the
routine credential's "blast-radius reduction... covers" made the same present-tense overclaim about
the allowlist's own endpoint denials, which are equally contingent on the same two preconditions.
**Fixed, structurally this time rather than with another inline restatement**: both sentences now
point to §6 rather than re-deriving the caveat, and — per the verification-gate's own recommendation,
since this exact recurrence had by then happened often enough to indicate a structural problem, not
a discipline lapse — a new canonical statement was added at the point in §3 where the OS-isolation
gap is first introduced, explicitly declaring that this precondition (together with §7's rotation
item) governs **every** closure claim in the document, not merely the ones already caught, and that
individual sections should not restate this reasoning going forward.

**A follow-up verification-gate check confirmed the closure-axis structural fix genuinely held, then
found the identical pattern a ninth time on a different axis — cost claims, not closure claims,
which the canonical statement's own scope (explicitly "every closure claim") did not cover**: §5's
timing-gap paragraph still opened with "this design closes with a near-zero-cost fix," 22 lines above
the correction in the same paragraph that states the opposite ("not a near-zero-cost fix"). A second,
related finding in the same pass: the SHA-pinning fix's own "Fix" sentence was still scoped to
"`--admin` merge" specifically, reintroducing at the pinning layer the exact CLI-flag distinction the
merge-endpoint correction elsewhere in this document removed. **Fixed**: §5's lead sentence rewritten
to match the settled cost framing stated consistently everywhere else in the document; the pinning
fix's "Fix" sentence corrected to "every broker-executed PR merge," with `--admin` noted as
irrelevant to whether pinning applies, not the determining condition. A minor "below"→"throughout"
wording fix was also applied to the canonical statement's own closing sentence, since the top-of-doc
revision note that precedes it in reading order also makes closure claims covered by its scope.

**GPT co-gate, re-run on the cost/pinning fix: one further MAJOR finding, on a genuinely new axis
this document's own review history had not yet caught — the "ambient token" precondition itself was
scoped too narrowly.** Every mention of §7's Phase 1 completion criterion described it as "rotate the
keyring OAuth token's scope down," but this repo's own source exposes a second, independent raw
GitHub credential path: `src/index.ts:491` loads `GITHUB_TOKEN` from an env file into
`process.env` for the Linear-dispatcher subsystem — confirmed directly, currently unset in this
session but a documented, legitimate config slot (`.env.example:248`) that `gh` CLI prefers over the
keyring credential whenever set. Rotating the keyring token down does nothing to an already-set
`GITHUB_TOKEN` — a compromised agent or leaked env file reaching either path bypasses the proxy
identically. A third path was named for completeness though not currently live (`ssh -T
git@github.com` returns `Permission denied`, confirmed directly): an agent could in principle change
this repo's remote URL to an SSH form and push via any GitHub-registered SSH key, if one existed.
**Fixed**: §7's completion-criterion item rewritten from "rotate the keyring token" to "retire every
raw GitHub credential path an agent process can reach," naming all three paths explicitly; the
canonical statement in §3, §5's authoritative treatment in §6, and the top-of-doc revision note all
updated to match — this is real, unfinished-implementation-level scoping work this design pass names
but does not perform (a full audit of every credential-loading code path in this repo is out of
scope here, belongs to the implementation ticket).

**A subsequent Claude code-reviewer pass found this propagation itself incomplete: §5's own
standalone paragraph on the timing-gap correction still stated the narrow "rotate the keyring token"
sufficiency condition, not updated to reference the broadened credential set** — the two nearby
pointer references (top-of-doc note, §4) correctly say "§5/§7's broadened criterion," but §5's own
underlying text was never itself updated, making that attribution inaccurate. **Fixed**: §5's
paragraph rewritten to reference the credential-retirement item generally (not "the ambient OAuth
token" specifically) and state that *any* of the multiple paths remaining usable defeats the
boundary — matching §3/§6/§7's already-corrected framing exactly rather than introducing a fourth
variant. A follow-up sweep for "rotate the keyring token," "scoped down or removed from routine
sessions," and similar singular-path phrasing found no further live (non-historical,
non-explanatory) instance.

**GPT co-gate, re-run on the §5 fix: one further MAJOR finding, a genuinely new gap not previously
caught — brokering a base-retarget operation itself does not bind or invalidate a separately-pending
merge approval.** §5's target-ref/branch-matching claim had reasoned that since retargeting a PR's
base is itself a denied/brokered operation, no separate enforcement was needed — but retargeting and
merging are two independent brokered requests, each with its own approval; approving one does
nothing to a different, already-approved-but-not-yet-executed request. An agent could get a merge of
SHA X into base A approved, then separately get a retarget to base B approved, and the original merge
approval — untouched by the retarget's own approval — could still execute against the new base
without ever having been approved for it. **Fixed**: the broker must re-read the PR's current base
immediately before executing any approved merge, exactly the same way it re-verifies the head SHA,
and treat a base change since that merge's specific approval as invalidating it, requiring a fresh
approval — closing the gap with the same mechanism already proven for the head-SHA case, not a new
one.

**GPT co-gate, re-run on that fix: one further MAJOR finding — the "re-read the base immediately
before executing" mechanism, modeled directly on the head-SHA pinning fix, doesn't actually close the
gap the way the head-SHA fix does.** The head-SHA fix works because GitHub's merge endpoint accepts
`sha` as an atomic server-side precondition (`--match-head-commit`) — the merge itself fails if the
head has moved, with no window for a race. There is no equivalent atomic parameter for the base on
that same endpoint (confirmed, §3: only `commit_title`/`commit_message`/`sha`/`merge_method`
accepted). "Read the base, then merge" is therefore still check-then-act, just with a narrower
window than the original gap — a retarget could in principle land between the broker's read and the
merge call executing. **Fixed, honestly rather than by claiming a false completeness**: this document
now names this as a real, unresolved residual gap (matching how §4 already names the verdict-store
forgery gap rather than asserting completeness it hasn't earned) and recommends per-PR serialization
between retarget and merge requests as the actual closing mechanism — the broker must not allow both
to be in-flight simultaneously for the same PR, closing the race by construction rather than by an
ever-narrower re-check window. This serialization mechanism is not designed in this pass; added to
§7 as a specific requirement for the broker-implementation item.

**A verification-gate check, run before committing, found two remaining defects — both in the
header/summary block, the exact section the check treats as most load-bearing since it's what a
reader trusts first.** (1) The header's round-count claim ("twelve threat-modeler rounds plus three
rounds of code-review-gate correction") had itself gone stale, undercounting the review cycle after
further rounds landed following when that sentence was written — §8's own log by then held far more
than three post-threat-model events, and the verdict store's own reason string said "round 14." (2)
The header enumerated the OS-isolation and credential-retirement residual gaps but omitted the base-
retarget TOCTOU gap entirely — this document's *only* genuinely unresolved residual issue, silently
missing from a summary that otherwise takes care to name every open item. **Fixed, structurally
rather than with another hardcoded number**: the Status line and revision note no longer state a
specific round count at all — they point to §8 as the sole source of truth for that count, with an
explicit note that a specific number in this exact spot has now gone stale twice, which is itself the
reason not to repeat the mistake a third time with a different number. The base-retarget TOCTOU gap
is now named explicitly in the revision note, matching the treatment already given to the other two
preconditions. Two smaller nits from the same check — a bullet's dangling self-reference ("this
bullet's own rotation requirement," which doesn't exist in that bullet) and a stale shorthand label
("ambient-token-rotation" after the item was renamed to "credential-retirement") — were also fixed.

**A follow-up Claude code-reviewer pass found one self-contradiction the header fix itself
introduced**: the revision note's "not a round count" claim sat in the same sentence as an explicit
"twelve threat-modeler rounds" count, which is false as a blanket statement — that specific count was
never the problem (the threat-modeler phase concluded before the code-review cycle began and cannot
drift further); only the code-review/GPT cycle's own count had gone stale. **Fixed**: the sentence
now explicitly scopes "not a round count" to the code-review/GPT cycle specifically, states plainly
that the threat-modeler count is stable and safe to keep, and explains why — the same distinction
`INDEX.md`'s own summary row was brought in line with for consistency between the two files.

**A final verification-gate pass found two more real defects in this same header block, both
propagated from a wording choice made two rounds earlier**: (1) the header's residual-gap accounting
claimed exactly "one" unresolved gap (the base-retarget TOCTOU issue), but the body itself — at four
separate points (§4, §5, §7) — treats the broker's unauthenticated verdict-store forgery gap as a
second, distinct, still-unresolved residual gap that survives even after the OS-isolation and
credential-retirement preconditions land. The header's "sole unresolved gap" framing, introduced when
the base-retarget finding was fresh, never accounted for the verdict-store gap that had already been
named since much earlier in this document's history. (2) A "went stale once" vs. "went stale twice"
inconsistency between two mentions of the same round-count-drift event within 25 lines of each other,
plus a matching inconsistency in `INDEX.md`. **Fixed**: the Status line and revision note now name
**both** residual gaps explicitly (base-retarget TOCTOU and verdict-store forgery), and the
stale-count self-reference is corrected to "twice" consistently in both files. This is the
document's own signature failure mode — a correct fix that doesn't fully account for something
already true elsewhere in the document — recurring one further time, in the one section meant to be
the trustworthy summary of everything else.

Both Claude and GPT code-reviewer verdicts are now current against this document's actual final
content, matching this repo's non-negotiable review discipline for trust-boundary/credential-scoping
changes before commit.

## Rollback

This design is proxy/broker infrastructure sitting in front of GitHub, not a GitHub-side control
itself (unlike `git-level-hard-backstop-design.md`'s ruleset, which this design's own scope stays
deliberately independent of — see §5's separation from `main-attestation-backstop`'s activation
state). Rollback here means reverting to today's status quo, not disabling something new on GitHub's
side:

- **Disable the proxy/broker path**: point agent sessions' `gh`/`git` traffic back at the ambient
  keyring credential directly (undoing whatever env-injection/transport wiring the implementation
  ticket builds). This is the fail-*open* direction relative to this design's own posture — it
  restores today's single-credential status quo (§1), not a hardened state, so it should be treated
  as an explicit, visible, user-directed action (e.g. a proxy outage blocking legitimate urgent work)
  and not something routine automation reaches for on its own.
- **No GitHub-side state to unwind**: this design creates no ruleset, no branch-protection rule, no
  workflow file — the credential(s) it proposes creating (§2, §7) are the only durable artifact, and
  revoking/deleting a fine-grained PAT via GitHub's own settings is the complete teardown for those.
- **Partial rollback — corrected after a GPT code-review co-gate pass caught this bullet
  reintroducing the exact flaw the merge-endpoint fix above closes**: an earlier version of this
  bullet suggested reverting only the broker component while keeping the proxy, routing
  denied/`--admin` requests back to today's `approve_admin_merge` marker-file flow. **This is not a
  safe intermediate state and must not be presented as one**: that marker-file flow is exactly the
  CLI-flag-based, same-OS-user-forgeable mechanism §3/§4 establish doesn't work — it can't
  distinguish `--admin` from plain merge calls (the proxy-level finding above applies equally to a
  reverted, non-proxied `approve_admin_merge` flow, since neither observes anything the CLI-level
  `--admin` flag would have added), and the marker file itself has no authentication (§4's named
  residual gap). The proxy and broker are separable as *infrastructure* (§3, §4, §7), but any
  intermediate state must still route every merge and every admin-capable operation through *some*
  authorization mechanism outside the agent's own trust domain — reverting to the old marker-file
  flow does not qualify, and doing so should be understood as reopening unapproved merge/admin access,
  not as a lighter-weight version of this design's actual protection.
