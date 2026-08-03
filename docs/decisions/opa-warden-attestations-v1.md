# ADR: OPA/Rego attestation ledger as the shared Warden policy substrate (v1: Hermes only)

**Date:** 2026-08-03
**Status:** Accepted
**Scope:** `scripts/warden_policy/`, `scripts/hermes_warden_gate.py`, `scripts/warden_attest.py`, `scripts/start_warden_opa.sh`, `launchd/com.deus.warden-opa.plist`
**Related:** [docs/HERMES_WARDEN_OPA.md](../HERMES_WARDEN_OPA.md), [hook-dispatch-system.md](hook-dispatch-system.md)

## Status

Accepted, v1 shipped (Hermes-only). Claude Code and git-level adoption are explicitly future work.

## Context

Deus's Wardens (`scripts/warden_hooks/`, `scripts/warden_review/`) enforce plan-review and
code-review gates for Claude-Code-driven development via `.claude/settings.json` hooks. Hermes
Agent (`~/.hermes/hermes-agent`) is a separate agent framework the user also runs, with its own
`pre_tool_call` hook primitive but no wired guardrails and no equivalent gate system.

Prior research (`Second Brain/Deus/Research/2026-08-03-hermes-native-vs-bridge-synthesis.md`)
evaluated bridging Deus's Claude-Code-shaped hook model into Hermes vs. building something native
to Hermes vs. a git-level backstop. It converged on: don't port the gate logic three times (once
per platform) — share a policy substrate instead. Open Policy Agent (OPA) + Rego was identified
and validated as a live proof-of-concept
(`Second Brain/Deus/Research/2026-08-03-opa-guardrails-poc/`) as the concrete technology: a
mature, widely-used policy engine (Kubernetes admission control, CI gates) already used in
production for exactly this "Policy Enforcement Point at the tool-call layer" shape.

## Decision

Build a v1 slice: a repo-committed Rego policy + Python attestation store, a fail-closed Hermes
`pre_tool_call` adapter, and a persistent local OPA daemon. Scope: Hermes's code-review gate
only. Claude Code's existing `verdict_store.py`/`.warden-verdicts.json` is left completely
untouched — a different, higher-blast-radius migration, explicitly out of scope here.

### Core mechanism

- **Subject binding**: attestations bind to `git write-tree` on the *staged index* — byte-identical
  to what a plain `git commit` produces (verifiable via `git rev-parse HEAD^{tree}`), not a
  synthetic snapshot of the whole working tree. This closes LIA-382's unbound-verdict problem
  (a SHIP authorizing any subsequent commit while HEAD stays the same) as a property of the
  Rego policy itself, not application code that has to remember to check it.
- **Repo identity**: `git-common-dir-sha256:<hex>` — a hash of the canonical absolute git
  common-dir path. Linked worktrees share an identity (avoids the LIA-446 bucket-mismatch class
  entirely); the hash keeps raw personal paths out of policy input, block messages, and logs.
- **Attestation ledger**: append-only (`records` + a `latest` pointer), never overwrite-in-place —
  per the standing "never lose, overwrite, or downgrade data" rule, and because REVISE→SHIP
  history is itself worth keeping. Locked (`fcntl.flock`, sidecar lockfile, atomic `os.replace`),
  re-implementing the ~30-line primitive `scripts/warden_hooks/verdict_store.py` already proved
  out rather than importing it (that module is coupled to the 4000-line `codex_warden_hooks`
  entry via `bind_entry()` and can't be imported standalone).
- **OPA sync**: every mutation activates synchronously (`PUT` + generation read-back under the
  same exclusive lock) — no `--watch` (OPA's own docs note file-watching can silently drop
  updates across atomic replace, unacceptable when a stale snapshot could still show a
  superseded SHIP). The Hermes adapter reads `generation` under a **shared** lock on the same
  lockfile and sends it as `expected_generation` on every query, not just during an outage —
  detecting OPA serving a stale snapshot after a failed/ambiguous PUT even while OPA itself is
  perfectly reachable.
- **Command classifier** (`scripts/warden_policy/command_parser.py`): ALLOWLIST, not a blocklist.
  Supported commit forms require **both** `--no-verify` and `-c core.hooksPath=<empty-dir>`
  (note: `-c` is a git *global* option and must precede `commit`, not follow it). Both flags are
  required because `--no-verify` alone suppresses `pre-commit`/`commit-msg` but **not**
  `prepare-commit-msg` — an ordinary shell-script hook that can still mutate the index before the
  commit is created. Verified live against this actual repo's own `.husky/pre-commit` (runs `npx
  lint-staged`) and a synthetic `prepare-commit-msg` hook — both confirmed suppressed, tree
  landed exactly as attested.
- **Enrollment-before-form-validation ordering**: an unenrolled repo allows *any* commit form,
  unconditionally, before any flag/OPA check runs — an earlier design checked commit-form
  validity first, which would have blocked ordinary commits in unenrolled scratch repos.
- **Fail-closed shim**: Hermes's own hook engine fails **open** on script error, timeout, or
  malformed output. The adapter's internal state starts at *block* and only flips to allow after
  complete validation; every failure path (OPA unreachable, timeout, garbage response, unreadable
  ledger, git resolution error) emits Hermes's canonical block shape and exits 0.

### What this is, and isn't

v1 is a **strong workflow guardrail, not a security boundary**. OPA is unauthenticated on
loopback (any local process can forge attestations); the shim can't protect against the hook
file or `python3` being entirely absent (Hermes fails open before it would even run); and a
genuinely concurrent, hand-timed adversarial process racing to mutate the index between
authorization and the actual `git commit` is not defended against (the hooks-disabled fix closes
the *deterministic* hook-driven version of this gap, not a live race by a separate process).
Closing that fully requires a git-level enforcement point — a later, separate phase.

**Named residual gap, found by code review**: the classifier requires the literal token `git` as
the invoked program (`command_parser.py`'s `tokens[0] != "git"` check). An absolute-path
invocation (`/usr/bin/git commit ...`) or a shell alias/function (`git ci ...`) is not recognized
as commit-shaped at all, and is allowed immediately with no ledger/OPA involvement — a real,
named bypass, not just theoretical. This is deliberately not closed in v1: enumerating every
alternate invocation form (paths, aliases, shell functions, wrapper scripts) is an unbounded
problem with the same shape as the shell-compound evasion gap already scoped to the git-level
backstop. Treat it the same way — a coaching-layer limitation, not a v1 defect to chase.

### Cross-reference: not the same system as `hook-dispatch-system.md`

`docs/decisions/hook-dispatch-system.md` / `hook-dispatch-facade-correction.md` describe a
different, Deus-internal, backend-neutral (Claude/OpenAI container) hook dispatch facade. This
ADR's mechanism shares vocabulary ("hooks," "guardrails," "policy decision point") but not
architecture — do not conflate the two.

## Consequences

- OPA (`brew install opa`, v1.19.0+, Rego v1 `if`-keyword syntax) is now a dependency for anyone
  who wants Hermes-side enforcement. It is not required for Claude Code's existing Wardens.
- A new persistent local daemon (`com.deus.warden-opa`, launchd-managed) runs on the user's
  machine going forward, mirroring the existing `com.deus.llama-cpp` precedent.
- `scripts/warden_review/registry.py` is untouched — OPA is a policy decision point, not a
  `ModelReviewerBackend`. A later automated reviewer-dispatch phase would obtain reviewers
  through that existing registry and write verdicts through this new store.

## Alternatives considered

- **Deus-owned backend-neutral execution engine** (the original two-model synthesis's Option A):
  rejected as the *engine*; the useful part (a shared evidence contract) survives as this ADR's
  attestation schema.
- **Hand-rolled bespoke Hermes hook script** (no policy engine): rejected — would be a third
  reimplementation of gate logic (after Claude Code's and any future git-level one), with no
  shared decision layer other repos or backends could reuse.
- **`--no-verify` alone**: rejected after adversarial review — insufficient (see
  `prepare-commit-msg` finding above).
- **`opa run --watch`**: rejected — silent-miss failure mode unacceptable for this use case.

## Rollback

- Full: `launchctl bootout gui/$(id -u)/com.deus.warden-opa`, remove the `hooks:` entry from
  `~/.hermes/config.yaml`, remove the corresponding entry from
  `~/.hermes/shell-hooks-allowlist.json`.
- Per-repo (daemon stays running): `python3 scripts/warden_attest.py unenroll --repo <path>`.

## Verification

Full unit test suite: `opa test` (policy) + `pytest scripts/warden_policy/tests` (Python) — see
`docs/HERMES_WARDEN_OPA.md` for exact commands. Live end-to-end proof against a real Hermes
agent and a real OPA daemon: block (no attestation), allow (matching SHIP, tree verified via
`git show`/`rev-parse HEAD^{tree}`), freshness (stale SHIP denied for a new tree), blocked form
(`-a`), hook-suppression (real `pre-commit` + `prepare-commit-msg` hooks installed, both
confirmed not to fire), enrollment-ordering (a separate unenrolled repo's hook fires normally),
fail-closed (daemon stopped mid-session, commit blocked, `decisions.jsonl` shows
`fail_closed: true`), and recovery (daemon restarted, loads the durable ledger, previously
unactivated attestation honored with no reissuance needed).
