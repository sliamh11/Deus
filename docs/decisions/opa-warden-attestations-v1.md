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

> **Partially superseded — see [Migration phases](#migration-phases-claude-code-side) below.**
> The `verdict_store.py`/`.warden-verdicts.json` half of that sentence still holds exactly as
> written: no phase to date modifies either. The broader "Claude Code is untouched" reading no
> longer does — Phase 1 adds a read-only, default-off shadow observer to
> `codex_warden_hooks.py`. No Claude Code gate consults an OPA answer; the migration this
> sentence declared out of scope remains out of scope.

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

**Fixed gap, found and closed during deep testing (2026-08-03, same day as initial ship)**: the
classifier validated commit-form *shape* (which flags are present) but never inspected flag
*values* for shell command-substitution syntax. Since Hermes's `terminal` tool runs commands
through a real persistent shell, a "supported" commit whose message/author/`-C`/`-c` value
contained a backtick or `$(` would execute arbitrary code as a side effect, independent of git
or the tree-attestation logic entirely — verified live: a real Hermes agent ran
`-m "safe $(touch /tmp/x)"`, classified as supported, and the target file was created (confirmed
via `ls`, not the agent's self-report). Five adversarial review rounds (GPT-5.6-sol) progressively
found the fix's scope too narrow (missed `-C`/`-c` values), the check placed too early (would
have misclassified unrelated non-git commands merely mentioning "commit"), and two distinct
shell line-continuation splice bypasses (`` `\`` + newline `` inside a quoted value, and the same
marker with the backslash already consumed by `shlex`'s own escape handling for an unquoted
value) — both verified live before and after the fix, not just unit-tested. The final fix
normalizes the *raw command string* for line continuations before tokenization (not individual
tokens afterward), then checks every token for the two markers once the command is already
confirmed to be a genuine, otherwise-fully-supported git-commit invocation. **Live re-proof**:
the fix was re-verified end-to-end against a real Hermes agent (gpt-5.6-sol via OpenAI Codex) —
the exact original exploit (`-m "safe $(touch /tmp/x)"`, which had succeeded and created the
target file against the pre-fix code) is now blocked, target file confirmed absent via `ls`; the
unquoted line-continuation bypass was re-run the same way and also confirmed blocked, target
file confirmed absent. Both re-proofs used a temporary hook redirect to the fix worktree, fully
reverted afterward (`~/.hermes/config.yaml` restored byte-identical, confirmed via `diff`). Also
verified at the unit level: 14 new regression tests (one per exploit variant found across all
six review rounds, including process substitution -- see below), full `scripts/warden_policy/`
suite (83 tests) passes with no regressions.

A closely related construct, process substitution (`<(`, `>(`), was found missing from the
marker set by code review after the above rounds -- the same vulnerability class (bash forks and
runs the enclosed command to set up the substitution as a side effect of word-splitting alone,
whether or not the resulting path is ever read), verified live the same way. Both markers are
now included.

**Fixed in a follow-up round** (found during continued deep testing after v1 shipped): the
`-c <key>` gap named above was confirmed as a real, live-exploitable bypass, and a second,
more severe gap in the same code was found by adversarial (GPT-backend) plan review while
fixing it.

1. *Unrestricted `-c <key>`.* `classify()` only checked WHETHER one of possibly several `-c`
   flags equaled `core.hooksPath` -- it never rejected a `-c` with a different key, and never
   rejected a duplicate `-c core.hooksPath=<different-value>`. **Live-verified**: ran
   `git -c core.hooksPath=/tmp/empty -c core.fsmonitor=<script> commit --no-verify -m safe`
   through a real Hermes agent's real terminal tool (gpt-5.6-sol via OpenAI Codex) against the
   deployed pre-fix daemon -- the configured script executed as a side effect of an otherwise
   "supported" commit (marker file created, confirmed via `ls`). A duplicate
   `-c core.hooksPath=<different-value>` is a related bypass: git's own config precedence takes
   the LAST value for a single-valued key, so a second override could silently redirect back to
   a real (dangerous) hooks directory even though `core.hooksPath` was technically "seen."

2. *Unvalidated `core.hooksPath` value* (found by GPT-backend plan review while fixing #1 above,
   not previously named as a gap). Even with the key restricted to exactly `core.hooksPath`,
   `classify()` never checked that the VALUE points to a real, empty, trusted directory.
   `--no-verify` does **not** suppress `prepare-commit-msg` -- **live-verified directly against
   real git** (a throwaway repo + a `prepare-commit-msg` hook that touches a marker file; `git -c
   core.hooksPath=<that-dir> commit --no-verify` exits 0 and the marker file is created). So an
   attacker- or agent-supplied non-empty `core.hooksPath` value defeats the entire reason the
   override exists, independent of the `-c`-key gap. This is a pre-existing gap present since
   v1 shipped (#1105), not introduced by this fix.

**The fix** (`command_parser.py`): the pre-`commit` `-c` handling now rejects (a) any second
`-c` of any kind (closing both the wrong-key and duplicate-hooksPath-value bypasses), and (b) a
`core.hooksPath` value that isn't an absolute, existing, currently-empty directory (via a new
`_is_valid_empty_hooks_dir()` helper, checked at the same point in `classify()` as the existing
shell-substitution-marker check -- after commit-shape and every other supported-form requirement
is already confirmed, so an unrelated non-commit command is never misclassified). Filesystem
errors (unreadable directory, path mutated mid-check) fail closed rather than raising.

**Live re-proof, post-fix**: both exploits above were re-run through a real Hermes agent
(gpt-5.6-sol) against the fixed code (via a temporary hook redirect to the fix worktree, fully
reverted afterward -- `~/.hermes/config.yaml` restored byte-identical, confirmed via `diff`,
same technique as the shell-substitution fix's re-proof above). Both are now blocked with the
new, specific messages; the marker files are confirmed absent via `ls`. A genuinely-supported
commit (real empty hooksPath dir, no injection) was also re-run the same way and still succeeds,
confirming no regression on the legitimate path.

**Verified at the unit level**: 12 new regression tests across two new test classes
(`TestDashCKeyAllowlist`, `TestHooksPathValueValidation`), full `scripts/warden_policy/` suite
(95 tests) passes with no regressions. Existing tests' hardcoded `/tmp/empty`/`/tmp/e` hooksPath
placeholders were converted to a real `tempfile.TemporaryDirectory()` fixture (not a fixed
namespaced path -- flagged by GPT-backend review as still collision-prone/foot-gun-prone even
if namespaced) since the classifier now validates the value against the real filesystem.

**A third gap, found by adversarial GPT code-review while reviewing the emptiness-validation
fix above** (not previously named): `classify()` validates the LITERAL string produced by
`shlex.split()` on the raw command, but Hermes's terminal tool later executes the SAME raw
string through a real shell, which performs parameter expansion (`$USER`, `${VAR}`) and
globbing (`*`) that `shlex.split()` does not. **Live-verified** via direct bash reproduction:
created a literal, empty decoy directory named `$USER` (dollar sign is a valid filename
character), pre-populated the REAL expansion target (`/tmp/<username>`) with a
`prepare-commit-msg` hook, ran `bash -c 'git -c core.hooksPath=/tmp/$USER commit --no-verify -m
test'` (exactly how Hermes's terminal tool executes a command) -- confirmed exit 0, and the hook
executed from the DIFFERENT (expanded) directory, not the literal one `classify()` validated.
Braced parameter expansion (`${VAR}`) was independently confirmed to behave identically to the
bare form in this argument position, and globbing (`/tmp/*`) was confirmed to expand when a
matching filesystem entry exists.

**Fix**: the hooksPath value must now match a strict character allowlist
(`[A-Za-z0-9_./-]+`, via `re.fullmatch` -- not `re.match` with `^`/`$` anchors, since Python's
`$` anchor allows a single trailing newline, which `fullmatch` correctly rejects, found by
adversarial plan review) before the existing emptiness check runs. `~`, `{`, `}` are rejected as
allowlist conservatism (consistent with the module's "new/obscure -- default to BLOCKED"
design) -- confirmed live NOT to expand in this specific argument position, so blocked on
general allowlist principle rather than as a demonstrated bypass (a distinction the review
process specifically caught being mislabeled in both directions across two rounds, and
corrected each time before landing).

**Live re-proof, post-fix**: the `$USER` exploit was re-run through a real Hermes agent
(gpt-5.6-sol) against the fixed code (same temporary-hook-redirect-then-revert methodology as
the other two gaps above) -- confirmed blocked with the new message, marker file confirmed
absent. A genuinely-supported commit (safe-character hooksPath, no injection) was re-run the
same way and still succeeds.

**Verified at the unit level**: 6 more regression tests (`TestHooksPathShellExpansionDivergence`),
full `scripts/warden_policy/` suite (101 tests) passes with no regressions.

**Pre-existing, not newly introduced**: the allowed charset still permits `..`, so
`-c core.hooksPath=/tmp/../some/other/empty/dir` passes the character allowlist -- this doesn't
expand attacker capability beyond "find or create some empty directory" (no shell-expansion
divergence involved, since no shell treats `..` specially; it resolves identically regardless of
which process reads it), which is already the accepted baseline threat model, not a new gap.

**Accepted, still-open residual (raised and explicitly scoped out during this round)**: a
genuinely concurrent process racing to write a hook file into the validated-empty directory
*after* `_is_valid_empty_hooks_dir()` approves it but *before* the real `git commit` reads the
hooks directory. This is the same category of risk this ADR's "What this is, and isn't" section
already names and defers ("a genuinely concurrent, hand-timed adversarial process racing to
mutate the index between authorization and the actual `git commit` is not defended against...
closing that fully requires a git-level enforcement point -- a later, separate phase") -- not
new scope-narrowing invented for this fix. The realistic threat model this guardrail defends
against is a Hermes agent (possibly prompt-injected, or simply careless) directly and
deterministically supplying a bad value -- which this fix closes -- not an independent attacker
process racing filesystem writes, which already implies independent code execution outside this
guardrail's threat model.

### Cross-reference: not the same system as `hook-dispatch-system.md`

`docs/decisions/hook-dispatch-system.md` / `hook-dispatch-facade-correction.md` describe a
different, Deus-internal, backend-neutral (Claude/OpenAI container) hook dispatch facade. This
ADR's mechanism shares vocabulary ("hooks," "guardrails," "policy decision point") but not
architecture — do not conflate the two.

## Migration phases (Claude-Code side)

The Decision section above scoped v1 to Hermes. Extending the same substrate to Claude Code's
own gates is being done in explicitly separated phases so each one can be reviewed and reverted
on its own. **No phase to date lets a Claude Code gate consult an OPA answer** — that cutover is
not designed yet and needs a human to read accumulated shadow data first. (Distinct from Phase 4
below, which lets the Rego layer itself answer an `attestation.verify` query for the separate,
designed-but-not-yet-implemented `attestation-verify` git-level check —
`git-level-hard-backstop-design.md`'s workflow, verify script, and `main-attestation-backstop`
ruleset are all still unwritten per its §5. No *Claude Code gate* consults that answer either, same
as every other phase here — this sentence's own claim stays true; LIA-534 tracks the still-open
gate-wiring half, LIA-530/Phase 4 below closes the separate Rego-cutover half.)

### Phase 0 — inert infra (PR #1118, `c287ec71`)

Additive Rego facts (`backend_verdict`, `backend_verdict_map`) over a new additive
`latest_by_backend` ledger index, plus an optional `backend=` parameter on
`AttestationStore.issue`. Zero callers on the Claude Code side; `hermes_warden_gate.py` gained
only the `"gate": "code-review"` field its own decision bodies now require.

### Phase 1 — read-only shadow observer (this section)

`scripts/warden_policy/cc_shadow.py` asks OPA what it *would* have decided, immediately after a
Claude Code gate has already decided, and appends one classified line to
`~/.config/deus/guardrails/logs/cc-shadow.jsonl`. Wired into `run_warden_backends_gate` (covering
code-review and ai-eng) and `run_verification_gate`. Off by default: `.claude/wardens/opa-shadow.json`
(gitignored, absent == off) with a `DEUS_OPA_SHADOW` env override.

The toggle file is **repo-level, not per-worktree** — it must live in the PRIMARY checkout's
`.claude/wardens/`, exactly like `config.json`, because the gates resolve `repo_root` to the
primary checkout. A copy inside a linked worktree is silently never read. Found during
natural-usage verification (a worktree-local toggle produced no observation from a real commit
gate); the same run demonstrates the mechanism, since the gate read `backends: [claude, gpt, glm]`
from the primary repo's `config.json` while that worktree has no `config.json` at all. The env
var is process-scoped and works from any cwd.

Three invariants, each with an executable test rather than a comment claiming it:

1. No gate outcome can depend on it — `observe()` returns `None`, every call site discards the
   result, and on blocking paths it runs *after* the decision has been written to stdout.
2. It never writes to stdout or stderr — `_block_pre_tool` uses stdout for the hook protocol.
3. It never writes to, or locks, the attestation ledger.

**Why Phase 1 has no write path** (the design's load-bearing decision, recorded so it is not
re-proposed): an earlier draft mirrored Claude Code verdicts into the ledger via
`issue(backend=...)`, reasoning that `latest` is untouched so Hermes cannot be affected. That
is true of the `latest` *index* and false of the *generation-coherence* property. `_mutate`
bumps `generation` and writes disk **before** its OPA PUT is confirmed, so a failed PUT leaves
disk at N+1 while OPA serves N; `supported` requires them equal, so the next Hermes-gated commit
in **any enrolled repo** falls through to the default deny and fail-closes. A separate write
flag, an OPA health pre-flight and a `sync()` retry lower the probability but do not restore the
"changes nothing" contract, so the write path was cut entirely (independent plan review, round 2).

Accepted consequence: with nothing writing `latest_by_backend`, essentially every observation
classifies as `no-attestation` or `opa-unreachable`. That is the expected shape of Phase 1 data,
not a defect — the value is in measuring OPA reachability and latency from the gate's own
process, generation coherence under real traffic, subject-resolution success on real events, and
the real legacy allow/block and TRIVIAL-bypass distributions. A Phase 2 write path needs its own
design (isolated ledger, or atomic persist-and-activate) and its own review.

**Gate-key vocabulary (found in code review; the single easiest thing to get wrong here).**
`latest` and `latest_by_backend` use *different* `gate` vocabularies, and conflating them reads
a permanently empty bucket:

| index | keyed by | example |
| --- | --- | --- |
| `latest` (Hermes's single-attestation path) | gate name | `code-review` |
| `latest_by_backend` (Phase 0's multi-backend index) | **role name** | `code-reviewer`, `ai-eng-warden` |

This is Phase 0's own established convention, not a Phase 1 invention — `attestation-v1.schema.json`'s
gate enum was widened to `["code-review", "code-reviewer", "ai-eng-warden"]` precisely to admit the
role names, `test_attestation_store.py` uses `gate="code-reviewer"` for every `latest_by_backend`
case, and `guardrails_test.rego`'s `backend_verdict` fixtures index under `code-reviewer` while its
`valid_ship` fixtures use `code-review`. An earlier Phase 1 draft sent `latest`'s vocabulary; every
mocked test still passed, and so did the live run, because with nothing writing the index an
always-empty bucket is indistinguishable from a correctly-empty one. Pinned now by
`test_cc_shadow.py::TestGateVocabularyRoundTrip`, which writes through the real `AttestationStore`,
evaluates the real `guardrails.rego` via `opa eval`, and asserts both that the correct key retrieves
the attestations *and* that the wrong key returns nothing (so the test stays discriminating).

`verification-gate` is deliberately absent from the schema enum: nothing has ever written a
verification attestation. Phase 1 only reads, so it queries an empty bucket harmlessly; a Phase 2
write path must widen the enum first.

Open item, recorded rather than skipped: the discriminating oracle for the gate-invariance
property was **self-authored by the implementer**, not produced independently via `oracle-author`
(the implementing agent had no dispatch capability). An independently authored oracle should be
run before any Phase 2 cutover.

### Phase 2 — isolated CC write path (LIA-527, implemented; gate wiring tracked separately as LIA-534)

This section designs, and (as of LIA-527) implements, the write path Phase 1 deliberately left
undesigned. Implemented: the six-site `AttestationStore` parameterization, `issue_if_newer`,
`scripts/warden_policy/cc_attestations.py`, and the CC-specific schema file. **Corrected (was:
"Not implemented here, and tracked separately: wiring `cc_attestations.enqueue_verdict` into any
Claude Code gate (LIA-534) and any Rego rule consulting `data.warden_cc_attestations` (LIA-530
item 2)" — the Rego half is now done):** the Rego rule consulting `data.warden_cc_attestations`
is implemented (LIA-530, "### Phase 4" below). Still not implemented here, and still tracked
separately: wiring `cc_attestations.enqueue_verdict` into any Claude Code gate (LIA-534) — so this
phase alone still does not change what any existing Claude Code gate does; the separate
`attestation-verify` git-level check (Phase 4 below) can now consult a CC-mirrored SHIP once
LIA-534 populates the ledger it reads.
The *cutover decision* (which gates, if any, start consulting OPA) still needs real shadow data
and is explicitly out of scope here — see "Not yet started" below.

**Chosen shape: an isolated document, not a fixed `_mutate` ordering.** Phase 1's own write-path
post-mortem (above) identified the root cause precisely: `_mutate` persists disk at
`generation` N+1 before its OPA PUT is confirmed, and `guardrails.rego`'s `supported` guard
requires `data.warden_attestations.generation == input.expected_generation` — so a failed PUT on
*any* write to that document fails closed for *every* enrolled repo's Hermes gate, including ones
having nothing to do with the write that failed. Reordering `_mutate` to persist only after PUT
confirmation was considered and rejected for this phase: it would rewrite the write transaction
that six-plus rounds of adversarial review already hardened for Hermes's real, field-proven path,
for a benefit (avoiding a redundant OPA document) that isolation achieves more cheaply and with
zero shared-state blast radius.

Instead: Claude-Code-authored mirrors go to a **second, wholly separate OPA data document**,
`data.warden_cc_attestations`, backed by its own on-disk ledger
(`~/.config/deus/guardrails/attestations-cc-v1.json`) and its own `generation` counter. A failed
PUT on this document can only ever desynchronize `warden_cc_attestations.generation` from its own
disk value — as of Phase 2 (this section), no Rego rule reads that path yet (Phase 2 adds none;
the Phase-4 attestation-verify cutover rule below, LIA-530, is the first and only one that does),
and
`warden_attestations.generation` (the value Hermes's `supported` guard actually checks) is never
touched by this write path at all, PROVIDED every OPA-bound write actually targets the isolated
document's own data path rather than the module-level default (see the six sites below — this is
not automatic and one of them is exactly the mechanism that would silently defeat it).

**Mechanism reuse, not reimplementation.** `AttestationStore`'s locking/atomic-write/PUT-readback
transaction is exactly what this write path needs — parameterize it rather than duplicate it:

```python
class AttestationStore:
    def __init__(
        self,
        ledger_path: Path,
        opa_base_url: str = OPA_DEFAULT_BASE_URL,
        *,
        document_key: str = "warden_attestations",      # new, defaults to today's behavior
        opa_data_path: str | None = None,                # new; defaults to f"/v1/data/{document_key}"
    ):
        ...
        self.document_key = document_key
        self.opa_data_path = opa_data_path or f"/v1/data/{document_key}"
```

Every site in `attestation_store.py` that hardcodes the literal string `warden_attestations` —
confirmed exhaustively via `grep -n "warden_attestations" scripts/warden_policy/attestation_store.py`
(unquoted; a narrower quoted-literal grep undercounts and was the root cause of an earlier
round's gap) — becomes `self.document_key`/`self.opa_data_path`-scoped together, as one
atomic change, not a partial subset. All six sites:

1. `OPA_DATA_PATH = "/v1/data/warden_attestations"` (:55) — the module constant `_put_and_readback`
   uses for both the PUT (`Request` built at :142, sent at :146) and the generation read-back
   (`Request` built at :151, sent at :153). This is the site that
   actually determines which OPA document a write lands in, regardless of what the on-disk
   ledger's own key is named — **the single most load-bearing site for the isolation guarantee**,
   and the one an earlier round's review caught missing. `_put_and_readback` must read
   `self.opa_data_path` here, not the module constant.
2. `_empty_document()`'s `"warden_attestations"` wrapper key (:61).
3. `_read_disk`'s schema-shape check, `if "warden_attestations" not in doc` (:113-114).
4. `_mutate` (:192) — `inner = doc["warden_attestations"]`. `enroll`, `unenroll`, and
   `issue()` all route through `_mutate`; `issue()` is what the whole Phase 2 write path is
   built on.
5. `sync()` (:270) — same `doc["warden_attestations"]` pattern, the retry path for a prior
   failed PUT.
6. `inspect()` (:277) — same pattern, the read-only listing path.

Missing any of these six would let a CC-authored write silently reach or report on
`data.warden_attestations` / the on-disk `warden_attestations` key instead of the isolated
`warden_cc_attestations` counterpart — reproducing the exact failure mode the "Chosen shape"
section above exists to rule out. Site #1 is the one that would do so most dangerously: even if
sites 2-6 correctly read/write an isolated on-disk ledger file, an unparameterized
`_put_and_readback` would still PUT that isolated document's contents to OPA's live
`/v1/data/warden_attestations` path — overwriting the real document `guardrails.rego`'s
`supported` guard reads and fail-closing every Hermes-enrolled repo immediately. All six must be
fixed together; none is optional.

Every existing call site (Hermes's, Phase 0's `backend=` parameter, `warden_attest.py`'s CLI)
passes no new argument and is byte-identical in behavior — this is additive, confirmed by keeping
`test_attestation_store.py`'s existing cases unparameterized and adding a new parameterized class
for the second document, including a regression test that asserts the isolated store's PUT target
is `/v1/data/warden_cc_attestations`, never `/v1/data/warden_attestations` (the concrete, testable
form of the guarantee site #1 above states in prose). A `CcAttestationStore` convenience wrapper
(or a `scripts/warden_policy/cc_attestations.py` module analogous to `cc_shadow.py`) instantiates
`AttestationStore(CC_LEDGER_PATH, document_key="warden_cc_attestations")` so call sites never
touch the constructor directly.

**Who writes, and when.** `run_warden_backends_gate` (code-reviewer, ai-eng-warden) and
`run_verification_gate` enqueue a job for `cc_store.issue_if_newer(backend=<backend>, gate=<role>,
..., queued_at=...)` (see "Ordering guard" below for why the plain `issue()` method is not what
the write path actually calls) once per backend verdict, **after** the legacy decision is already
finalized and returned/written — same
ordering discipline `cc_shadow.observe()` already established for Phase 1, and for the same
reason: this call's outcome (success or failure) must never be consulted by the gate that just
ran. A failed write here means "this SHIP wasn't mirrored to the CC ledger yet," never a block,
never a retry-on-the-critical-path. `verification-gate` is not yet in any schema's `gate` enum
(Phase 1 flagged this as a prerequisite for covering it at all) — Phase 2 satisfies that via a
**new**, CC-specific schema file (`attestation-cc-v1.schema.json`) that includes it from the
start, rather than widening the Hermes-facing `attestation-v1.schema.json`, so Hermes's own
schema contract is untouched by this phase.

**Bounding the synchronous call, and TRIVIAL handling** (found missing by the GPT co-gate review
of this design, round 1 — a real gap, not addressed by the "outcome is discarded" framing alone).
Two further rounds of Claude-backend review corrected the mechanism below: a first draft proposed
a daemon-thread-plus-`join(timeout=...)` approach, but `codex_warden_hooks.py`'s hook invocations
are short-lived, single-shot subprocesses (`main()` → `sys.exit(main())`) — when the join times
out and the process exits, CPython does not let a daemon thread finish; it is torn down with the
process almost every time, not "eventually completes independently" as that draft claimed. That
would make the whole write path silently produce almost no real mirrors, undermining the reason
Phase 2 exists (a CC ledger a future cutover can actually read).

Discarding the *result* of `cc_store.issue_if_newer()` is not sufficient on its own: it (like
`issue()`, the primitive it's layered on — see "Fix" below) runs under `_mutate`'s exclusive lock,
`fcntl.flock(fd, fcntl.LOCK_EX)` (`attestation_store.py:97`)
— a raw blocking syscall with **no timeout parameter at all** — and can spend up to 10s total
across the two already-bounded `urlopen(..., timeout=_OPA_TIMEOUT_SECONDS)` calls inside
`_put_and_readback` (the PUT at `:146`, the generation read-back at `:153`, each capped at 5s). A
bare `try/except Exception:` cannot bound wall-clock time — it only fires after the call returns —
and an in-process thread cannot outlive the hook's own short-lived subprocess.

**Concrete mechanism, adopting this codebase's own established pattern for exactly this problem**
(`session_end_hook.py`'s `_spawn_worker`/`_run_worker`, mirrored in `memory_tree_hook.py` — "work
that must survive past the hook's own short lifetime" is not new scope here, it is a solved
problem in this repo): the gate call site never calls the store's write method itself. It:

- (a) writes one small JSON job file — the exact `issue()` kwargs (`repo_id`, `gate`,
  `subject_key`, `verdict`, `issuer_kind`, `reviewer_id`, `reason`, `backend`) plus a `queued_at`
  timestamp, keyed by `job_id = str(uuid.uuid4())` (guarantees no collision between
  near-simultaneous gates for the same commit, or a REVISE round quickly followed by a SHIP
  re-run) — atomically (write-temp, `os.replace`, `chmod(0o600)`) into a queue directory
  (`~/.config/deus/guardrails/cc-write-queue/`, created with `mkdir(..., mode=0o700)` — both
  matching `AttestationStore`'s own ledger-directory and temp-file permissions exactly; the job
  file's `reason` field carries the same review text `AttestationStore.issue()` already accepts
  and stores unredacted in `records[...].reason`, so it gets no less protection in the queue than
  at rest in the ledger it is about to be written into);
- (b) spawns a **fully detached** subprocess via `subprocess.Popen([sys.executable,
  str(Path(__file__).resolve()), "--worker", job_id], start_new_session=True, stdin=DEVNULL,
  stdout=DEVNULL, stderr=DEVNULL)` (module path resolved the same way `session_end_hook.py:110`
  resolves its own — the worker re-invokes the `cc_attestations` module itself, not a separate
  script) — the exact `Popen` shape `_spawn_worker` already uses. `start_new_session=True` here
  protects specifically against a process-*group*-wide signal or a harness reaping the whole
  subprocess tree as a unit on hook-process exit (a plain parent exit alone never kills an
  already-forked child on POSIX regardless of session; the risk this flag closes is
  SIGHUP/session-teardown propagation and being caught up in the parent's own process-group
  cleanup, not "child dies because parent died") — and returns immediately, unconditionally, with
  **no `join`, no timeout, no wait of any kind**.

The detached worker process then does the actual `cc_store.issue_if_newer()` call on its own time, with no
hook-budget constraint at all, since it is no longer part of the hook's critical path. Concurrency
across independently-spawned workers is already safe by the same mechanism that protects Hermes's
own concurrent writers today: `_locked()`'s `fcntl.flock` is acquired on a physical lockfile path
opened independently by each process, which POSIX `flock` serializes correctly across *separate
processes*, not just threads within one — no new race is introduced by moving the call from an
in-process thread to a subprocess.

**Ordering guard — completion order is not enqueue order** (found by the required GPT code-review
co-gate on this design, a real gap `flock` mutual exclusion alone does not close). Mutual exclusion
prevents two workers from writing *concurrently*, but says nothing about which one writes *last* —
and `_apply`'s `latest[...] = record_id` (the line `issue()`'s inner apply function uses to update
the pointer `guardrails.rego` and any future consumer would read) unconditionally overwrites
whatever was there, with no ordering check. Detached workers acquire the lock in whatever order the
OS schedules them, which is not enqueue order: for the explicitly-supported "REVISE round quickly
followed by a SHIP re-run" sequence, if the SHIP worker's OPA round-trip happens to finish first and
the REVISE worker (queued earlier, still legitimately in flight) wins the lock second, `latest`
ends up pointing at the REVISE record even though SHIP is the true, newer verdict — a stale-overwrite
that misrepresents the CC ledger's own append-only, mirror-of-reality purpose. `queued_at` alone
(as specified above) does not prevent this: it timestamps enqueue, not the write that actually
lands, and nothing before this fix compared it against anything.

**Fix**: a CC-specific write method, `issue_if_newer(..., queued_at)`, layered on top of `issue()`'s
existing `_mutate`-based transaction rather than changing `issue()` itself (Hermes's own call sites
use plain `issue()`, untouched, byte-identical). Inside the same locked apply function — so the
check and the write are atomic with respect to every other writer, not a separate read-then-write
race of its own — it looks up the record currently pointed to by `latest[repo_id][gate][subject_key]`
(or `latest_by_backend[...][backend]` for the CC path's backend-scoped writes), and only advances
the pointer to the new record if no existing record for that key has a `queued_at` newer than this
job's own. The append-only `records` map still gains the new record unconditionally either way —
every attempted write stays part of the permanent audit trail, per this ledger's existing
never-edit-never-delete contract — only pointer advancement is guarded. A worker that loses this
comparison is not an error: it correctly recognizes its own job as superseded and exits normally
after seeing its write did not become `latest`.

Two further requirements this fix depends on, found by the same review round when checking whether
the fix as first stated could actually work in practice:

- **`queued_at` must be persisted onto the stored record, not just the (deleted-on-success) job
  file.** The CC-specific record schema (a variant of the `record` shape in
  `attestation-v1.schema.json`, scoped to the new `attestation-cc-v1.schema.json`) adds a
  `queued_at` field alongside the existing `issued_at` — otherwise a *later* `issue_if_newer` call
  has nothing durable to compare its own `queued_at` against once an earlier job's file is gone.
- **Clock source: `time.time_ns()` (wall-clock, nanosecond resolution) — not `monotonic_ns()`, and
  not the ledger's existing 1-second `time.strftime(...)` convention either.** An earlier draft of
  this fix specified `time.monotonic_ns()`, found wrong by the same GPT code-review co-gate that
  found the original ordering race: `queued_at` is *persisted to disk* and compared against
  writes from arbitrarily later processes, but `monotonic_ns()`'s epoch is boot-relative and resets
  on every reboot — a value written before a reboot is not just stale, it is not meaningfully
  comparable at all to one written after, and a lower post-reboot value could leave `latest`
  pointing at a genuinely obsolete pre-reboot record indefinitely (until uptime happens to exceed
  the old value). `time.time_ns()` is wall-clock (UTC epoch), so it survives reboots and stays
  comparable across the ledger's entire lifetime, while still getting nanosecond resolution — far
  finer than the existing `time.strftime(...)` convention's 1-second granularity, which alone would
  let two same-host re-runs enqueued within the same second tie. Ties (still possible, though far
  less likely at ns resolution than at 1-second resolution) are broken in favor of keeping whichever
  record is already `latest` unchanged, so a tie can never flip an established pointer. This is not
  claimed to be perfectly monotonic (wall clocks can rarely step backward under NTP correction,
  the same caveat every other timestamp already in this ledger — `issued_at`/`enrolled_at` — already
  carries), only reboot-durable and precise enough that a real correctness bug (the reboot-reset
  case) is not traded for a merely theoretical one already accepted elsewhere in this design.

The oracle (below) must assert this ordering property directly, including both new requirements
above, not merely that both writes eventually land.

**Deletion criterion, stated explicitly** (a first draft left this to inference and a wrong reading
would quietly reintroduce the same low-mirror-rate problem this redesign fixes): the job file is
deleted when `cc_store.issue_if_newer(...)` returns a `WriteResult` with `.ok is True` — disk persistence,
which `_mutate`'s own code comment states is "always true past this point" once `_write_disk_atomic`
returns without raising — **not** gated on `.activated` (OPA PUT+read-back success). A
persisted-but-not-activated write (OPA unreachable at write time — plausible early on, since the
shadow toggle went live with essentially zero traffic yet) is the ledger's normal "failed PUT"
state, which `sync()` already exists to retry on a later mutation; gating deletion on `.activated`
instead would leave every job attempted during an OPA outage stuck in the queue with no retry path
at all, a different flavor of the exact failure this redesign exists to avoid. `.ok` reflects disk
persistence of the append-only `records` entry, independent of whether `issue_if_newer` advanced
`latest`/`latest_by_backend` — a job that loses the ordering comparison above still persists (its
verdict is real and belongs in the audit trail) and its job file is still deleted on that same
`.ok is True`, exactly like a job that wins; "lost the ordering comparison" and "failed to persist"
are different outcomes and only the second one should ever leave a job file behind for the sweep.

**No worker self-ceiling needed, but a stale-job sweep is** — two related but distinct concerns,
handled differently. `session_end_hook.py`'s `_run_worker` arms a `threading.Timer`-based wall-clock
ceiling (LIA-235 pattern, "so a hung child can't leave an orphan") because its own worker's runtime
is not intrinsically bounded (it invokes `auto_compress.py`, an LLM-driven summarization step with
open-ended duration). This design's worker has no equivalent unbounded step: its entire body is one
`cc_store.issue_if_newer()` call, whose worst case is fully bounded by `AttestationStore` itself — up to 10s
across the two 5s-capped `urlopen` calls inside `_put_and_readback`, plus a `flock` acquisition that
is fast in practice (single-writer-at-a-time ledger, no long-held cross-process contention by
design) and, unlike the timer's actual target (a hung *userspace* loop), is released by the kernel
immediately on process death regardless of how the process ends. A self-imposed timer would be
redundant here, not missing.

The orphan-file risk `session_end_hook.py`'s belt-and-sweep pair actually guards against — a worker
killed (OOM, forced termination, an unhandled exception before the delete step) leaving its job file
on disk forever, since nothing else ever revisits it — is real for this design too and is not
covered by the "no ceiling needed" argument above; those are separate risks. Phase 2 needs the
sweep half of that pair, not the belt half: extend `scripts/maintenance.py`'s existing daily
registration (the same one that runs `compress_sweep.py`) with a lightweight `cc-write-queue` sweep
that deletes (and logs one line per deletion, surfacing in `logs/maintenance.log` for `/review-logs`,
matching `compress_sweep.py`'s own `MAX_ATTEMPTS`-exhausted logging convention) any `*.json` in the
queue dir older than 24h — no `.running`/`.failed` state machine or attempts counter needed, since
this design already chose single-best-effort-no-retry (below): a stale file is definitionally one
the worker never got to, or got to and crashed before deleting, and either way there is nothing left
to retry, only to reclaim.

This mechanism needs no retry queue or attempts counter the way `session_end_hook.py`'s fuller
worker does — a missed mirror costs one absent row of shadow-adjacent data, the same acceptable-loss
shape Phase 1 already accepts for its own classification gaps (`generation-unknown`,
`opa-unreachable`, etc.); a single best-effort attempt per queued job is sufficient scope. The
parent hook's own containment is now trivial: (a) and (b) above wrapped in a single broad
`try/except Exception: pass`, matching `cc_shadow.observe()`'s containment floor, purely against
the enqueue/spawn step itself ever raising — never against anything inside `AttestationStore`,
which remains untouched.

`TRIVIAL` verdicts need explicit, distinct handling: the legacy gate accepts a human `TRIVIAL`
bypass as satisfied, but `AttestationStore.issue()` rejects `TRIVIAL` as an invalid `verdict` value
outright (`verdict not in ("SHIP", "REVISE", "BLOCK", "COULD_NOT_RUN")`), and the proposed
CC-specific schema does not admit it either — an unhandled TRIVIAL commit would either raise inside
the mirror call (caught by the containment above, so not a gate-breaking bug, but silently
producing zero record) or need to be misrepresented as some other verdict. The write-path call site
must recognize `TRIVIAL` explicitly and skip enqueueing entirely for it — never enqueue a job with
an invalid verdict, and never invent a synthetic non-TRIVIAL verdict to satisfy the schema. The CC
ledger simply has no record for a TRIVIAL-bypassed commit, which is honest: it was never actually
reviewed by a backend, so mirroring "as if reviewed" would misrepresent Phase 1's own shadow-log
data this design is meant to eventually build on.

**What Phase 2 explicitly does NOT do:**
- No Rego rule consults `data.warden_cc_attestations`. Writing and reading are separate
  decisions; this section designs writing only. (Reading is added by Phase 4 below, LIA-530, a
  separate section and a separate review cycle — this bullet describes Phase 2's own scope only.)
- No Claude Code gate's pass/fail outcome changes. Identical invariant to Phase 1's #1
  ("no gate outcome can depend on it"), extended to cover write failures specifically.
- No change to `data.warden_attestations`, its schema, or any code path Hermes depends on —
  contingent on all six sites above being fixed together, not aspirational.

**Public interface** (named now specifically so the independent oracle below can target the real
future call site, not a stand-in — found necessary by GPT code-review co-gate: an oracle that
tests only `AttestationStore` directly, with no reference to the call-site module, can be
satisfied by a call site that gets the wrapping wrong even when the store primitive itself is
correct). `scripts/warden_policy/cc_attestations.py`, not yet implemented, exposes exactly two
functions the rest of this design already specifies the behavior of:

- `enqueue_verdict(*, repo_id, gate, subject_key, verdict, issuer_kind, reviewer_id, reason,
  backend, queue_dir: Path = QUEUE_DIR) -> None` — the hook call site's entry point (`queue_dir`
  defaults to the real module-level constant for production use, overridable so tests never touch
  the real queue directory, same rationale as `process_job`'s overrides below). Recognizes and
  skips `TRIVIAL` before doing anything else (never writes a job file for it); otherwise performs
  the atomic job-file write plus detached-`Popen` spawn described above. Wrapped in the broad
  `try/except Exception: pass` containment already specified; returns `None` unconditionally.
- `process_job(job_id: str, *, queue_dir: Path = QUEUE_DIR, ledger_path: Path = CC_LEDGER_PATH) ->
  bool` — the detached worker's entry point (what `--worker <job-id>` invokes, resolving the job
  file as `queue_dir / f"{job_id}.json"`). Reads the job file, calls `cc_store.issue_if_newer(...)`
  against a store built from `ledger_path`, and returns whether the job file should be deleted —
  `True` iff `WriteResult.ok is True`, per the deletion criterion above, regardless of `.activated`
  or of whether the ordering comparison was won or lost. `queue_dir`/`ledger_path` default to the
  real module-level constants for production use; both are overridable so tests never touch the
  real `~/.config/deus/guardrails/` paths. The `--worker` CLI wrapper (not specified further here
  — trivial glue) deletes the job file when this returns `True`.

**Independent oracle.** Per this ADR's own open item above and `.claude/wardens/plan-review-rules.md`'s
independent-oracle pattern, the discriminating test for this design is authored by the
`oracle-author` role (dispatched via `scripts/dispatch-oracle-author.sh`, which runs it on
GPT-5.6-Sol — genuine model diversity from whichever model eventually implements this, per the
2026-07-15 user decision recorded in that script) from this spec, blind to any implementation
(none exists yet) — `scripts/warden_policy/tests/test_cc_write_path_oracle.py`, with each
assertion tagged `# @oracle LIA-527: <spec point>`, extending (per-assertion rather than the
precedent's usual per-test-function granularity) this repo's existing convention
(`test_verdict_store_staleness.py`, `test_codex_warden_hooks.py`, `test_warden_review.py`). The
oracle brief must explicitly require tests asserting: the isolated store's OPA PUT target is
`/v1/data/warden_cc_attestations`, distinct from `/v1/data/warden_attestations` (the site-#1
guarantee above, made executable); `issue_if_newer`'s ordering guarantee on **both** paths CC
writes actually use — the backend-scoped `latest_by_backend[repo_id][gate][subject_key][backend]`
pointer (since CC writes always pass `backend=`, per "Who writes, and when," above) and, for
completeness, the plain `latest[repo_id][gate][subject_key]` pointer `issue_if_newer` also
supports — given two writes for the same key with different `queued_at` values applied to the
store in EITHER order, the relevant pointer ends up referencing the newer `queued_at`'s record,
never the older one, while `records` still contains both — sequential calls alone are not
sufficient here (found necessary by the same GPT co-gate: an implementation that reads the current
pointer BEFORE acquiring `_mutate`'s lock, then only conditionally mutates, can pass every
sequential ordering test while still racing under real concurrent detached workers), so the oracle
must ALSO exercise this with genuinely concurrent threads released together via a
`threading.Barrier`, repeated across enough trials to give a non-atomic implementation a real
chance to interleave (best-effort, not a formal proof — this property has no single deterministic
pre-implementation assertion); and, **against the `cc_attestations`
module named above, not a local stand-in** — `process_job` returns `True` for a persisted-but-not-
activated `WriteResult` and `False` only for a genuinely failed one (the deletion criterion, made
discriminating against the real call site rather than a proxy that could pass even if a future
implementation wraps the criterion wrong), and `enqueue_verdict` writes no job file at all for
`verdict="TRIVIAL"` (checkable by asserting the queue directory stays empty after the call, not
merely that `AttestationStore.issue()` itself rejects TRIVIAL when called directly). Since
`cc_attestations.py` does not exist yet, these two assertions are expected to fail with
`ImportError`/`ModuleNotFoundError` today — a stronger, more specific red than a stand-in
function's tautological pass, and the correct oracle shape for a module that hasn't been written:
it can only ever be satisfied by the real interface behaving correctly, never by a look-alike.
Every other assertion in the oracle (isolation, six-site coverage, ordering, legacy defaults)
targets `AttestationStore` directly, which is the correct target for those — `cc_attestations.py`
is a thin wrapper around it, not a reimplementation, so those properties belong at the store layer
regardless of the wrapper's own correctness. It is expected **red** until Phase 2 is actually
implemented; passing it is a precondition for treating the write path as done, not evidence it
already is.

**Not yet started, and why** *(historical — as of this design's 7th review round, before
LIA-527 implemented it; kept for the reasoning, superseded by the heading above)*: actual
implementation of `AttestationStore`'s parameterization (all six sites), the new `cc_attestations`
write/worker/sweep call sites, and the CC-specific schema file. Two independent reasons to hold
here rather than implement in the same pass as this design: (1) LIA-527 itself is the
lowest-urgency item in the roadmap by explicit prior decision — implementation should not compete
for attention with higher-priority work; (2) the *cutover* decision this write path exists to
eventually support needs real shadow-observer data first, and as of this design there is
essentially none — `~/.config/deus/guardrails/logs/cc-shadow.jsonl` holds 9 lines, all from
2026-08-04 pre-toggle dev testing; the toggle (LIA-520) was only switched on 2026-08-07, less than
two hours before this section was written, and has produced zero new observations since (no
commit gate has fired in that window). Implementing the write path itself is not blocked on that
data — only the cutover is — but there is no benefit to landing unused write-path code before the
data that would justify designing the cutover on top of it exists. Tracked as explicit follow-up
scope, not silently dropped.

**Still not yet started, current** — post-LIA-527: `cc_attestations.enqueue_verdict` wiring into
any Claude Code gate (LIA-534). **Corrected (was: "and the Rego cutover rule (LIA-530 item 2).
Both remain out of scope")**: the Rego cutover rule is now designed, reviewed, and implemented
(LIA-530, "### Phase 4" below) — it was out of scope for LIA-527 itself, and remains so, but is no
longer undone. LIA-534's gate-wiring is the one item in this bullet still not started.

**Scope check** (round-count-circuit-breaker, per `plan-review-rules.md`): after 7 review rounds,
re-confirming this is still the smallest design that satisfies LIA-527's ask — "design the write
path... get the independent oracle authored" — rather than scope creep accumulated round-by-round.
It is: every addition since round 4 traces directly to a real defect a reviewer found in the
previous draft (missing parameterization sites, an unsafe concurrency mechanism, unstated
deletion/uniqueness/cleanup semantics), not to speculative hardening. No implementation, cutover,
or Rego change has been added — those remain explicitly out of scope, unchanged since round 4.

### Phase 3 — Hermes plan-review gate (LIA-523, implemented)

Extends this substrate to a SECOND Hermes gate type: a `pre_tool_call` adapter for `write_file`/
`patch` (file-write-shaped tool calls), the pre-implementation half of "safely doing real
day-to-day development from Hermes" that Phase 0/1/2 (code-review only) never covered. Went
through 15 plan-review rounds (Claude) + 2 GPT co-gate rounds before implementation — recorded
here because several of the roadmap's own working assumptions turned out wrong on inspection,
and the corrections are load-bearing for anyone extending this gate further.

**Corrected assumption**: the roadmap that scoped this ticket assumed it would need LIA-521's
fork/monkeypatch pattern (`GatewayRunner._create_adapter` `__class__`-swap), reasoning by analogy
to LIA-521's `pre_outbound_send` spike. Wrong: Hermes already has a native, production
`pre_tool_call` hook (this is what `hermes_warden_gate.py` already uses for `terminal`). This
phase needed zero Hermes source changes — one new `~/.hermes/config.yaml` hook entry
(`matcher: "write_file|patch"`, separate from the existing `matcher: "terminal"` entry) plus new
code entirely in this repo (`scripts/hermes_plan_review_gate.py`, plus additive changes to
`attestation_store.py`, `guardrails.rego`, `attestation-v1.schema.json`, `warden_attest.py`).

**Session-bound subject, not git-tree-bound.** Nothing is staged pre-write, so code-review's
`git write-tree` binding doesn't apply. Follows Claude Code's own plan-review gate instead
(`codex_warden_hooks.py::run_plan_review_gate`'s `.plan-reviewed` marker): a plan-reviewer SHIP
approves a session's reviewed intent, not a diff snapshot (LIA-516 established this same
principle for Claude Code — diff-hash staleness deliberately disabled for this role).
`AttestationStore.issue()` gained a `kind` parameter (`"git-tree"` default, byte-identical to
every existing call site; `"session"` stores the raw `session_id` — an opaque token, nothing to
digest). `attestation-v1.schema.json`'s `subject` became an `if`/`then`/`else` conditional on
`kind` rather than a flat `const` shape.

**Enrollment: additive, not a restructure — deliberately reconciled with Phase 2's already-merged
design.** An early draft restructured `enforced_repos[repo_id]` into a nested `{"gates": {...}}`
shape; review found this would (a) have no real migration path since OPA evaluates raw JSON
directly, not through a Python abstraction, (b) require editing `guardrails.rego`'s three
existing `git.commit` decision bodies to call `enrolled("code-review")` instead of the bare
`enrolled` fact, and (c) directly conflict with this same ADR's Phase 2 section above, whose
oracle (`test_cc_write_path_oracle.py`) locks `enroll()`'s flat output shape as a forward-
compatibility baseline. Final design: a single new field, `enforced_repos[repo_id].plan_review_enabled`
(boolean, absent = off), via a new `AttestationStore.set_plan_review_enabled(repo_id, enabled)`
method — `enroll()`'s existing shape, the bare `enrolled` Rego fact, and all three `git.commit`
decision bodies stay byte-unchanged. A real, necessary fix surfaced during this work: `enroll()`'s
`_apply` was a wholesale dict-replace, which would have silently erased `plan_review_enabled` if
`enroll()` ran after `set_plan_review_enabled()` — changed to a merge (`setdefault` + per-key
assignment), verified to produce byte-identical output to the old code for the case Phase 2's
oracle actually exercises (a fresh `enroll()` call, nothing pre-existing to preserve).

**Repo-identity resolution — the hardest part, four discarded designs before landing on v1's
actual shape.** In order, and why each was rejected: (1) calling Hermes's own
`tools.file_tools._resolve_path_for_task` — its top two resolution tiers read in-memory,
per-process state (`terminal_tool.py`'s `_session_cwd`/`_task_env_overrides`) that doesn't survive
into the freshly-spawned gate subprocess; (2) reading `$TERMINAL_CWD` from the subprocess
environment alone — it is Hermes's own documented "global mutable timeshared between sessions,"
so a concurrent/child session could read a stale value belonging to a different session,
risking a false ALLOW; (3) a SessionDB (`hermes_state.py`'s `sessions` table) lookup — its
safety argument rested on a claim (plain CLI `-w` sessions never populate it) that was
subsequently found false (`run_agent.py::_ensure_db_session` does populate it for local
sessions) once someone actually re-checked the grep that produced the claim.

**Final v1 design — branch on path shape first, absolute paths only:**
- **Absolute** `write_file`/`patch` targets resolve their OWN precise `repo_id` (parent-directory
  walk to the nearest existing ancestor, then `git -C <dir> rev-parse ...` — identical mechanism
  to `git_subject.py::resolve_repo_id`) and are checked against THAT repo's own enrollment and
  attestation state. `payload["cwd"]` is never consulted in this branch, so there is nothing for
  it to diverge from.
- **Relative** targets (v1 cannot precisely resolve these — the reason this gate scope-cuts them
  at all) get only a coarse, best-effort gate-applicability pre-check via `payload["cwd"]` (the
  same mechanism `hermes_warden_gate.py` already uses for `terminal`) — used ONLY to decide "is
  this call even in scope," never to grant an attestation-based allow. If the guess looks
  enrolled, the call fails closed with an explicit message; if not, it falls back to today's
  pre-existing, already-shipped, ungated behavior (a named, accepted residual gap, not a
  regression). Tilde-prefixed paths are classified as relative and never expanded (Hermes's own
  tilde-expansion depends on a process-dependent `HOME` that can differ between gateway and
  interactive-CLI contexts — the same environment-dependent-resolution risk class as `$TERMINAL_CWD`).
- **Every target in a `patch` call is checked independently before any call-level decision** — a
  real false-ALLOW found by the GPT co-gate: an earlier draft let any relative target in a
  mixed-target patch downgrade the WHOLE call to the weak cwd-based check, letting an absolute
  target inside an enrolled-but-unattested repo be smuggled through via an irrelevant relative
  sibling. Fixed so the call blocks if either the absolute-targets' precise check OR the
  relative-targets' coarse check would block it. Cross-repo ambiguity (more than one distinct
  ENROLLED repo_id among a patch's absolute targets) fails closed; unenrolled repo_ids never
  count toward that check.

**Named v1 limitation**: relative-path (including tilde) `write_file`/`patch` calls are
unconditionally blocked once the repo looks plan-review-enrolled — a real usability cost given
how commonly coding agents emit relative paths, accepted because no repo-identity mechanism this
review could verify safe across Hermes's CLI/TUI/Desktop/ACP/gateway/subagent session types
existed after genuine, repeated attempts. Mitigated by the gate being opt-in per-repo
(`enable-plan-review`/`disable-plan-review`), not a global default. A `terminal` command can still
write files directly (`echo > file`) — LIA-522's territory (the git-level backstop), not this
gate's job.

**Session-attestation TTL**: `~/.hermes/config.yaml`'s `session_reset.mode: none` and
`agent.max_turns: 500` mean a session-bound attestation with no expiry could authorize writes for
an entire, potentially very long session. `guardrails.rego`'s new `valid_plan_review_ship` rule
requires `issued_at` within `plan_review_ttl_seconds` (default 7200, overridable via
`data.warden_attestations.config.plan_review_ttl_seconds`) — an expired attestation blocks
exactly like no attestation, never falls through to allow. Named residual gap: Hermes has no
native "start a new plan mid-session" event (unlike Claude Code's `/plan` or a fresh Plan-subagent
dispatch), so a session-scoped attestation stays valid for every write in that session (bounded by
the TTL) even if the actual plan changes mid-session.

**Independent oracle**: `scripts/warden_policy/tests/test_hermes_plan_review_gate_oracle.py`,
authored via `oracle-author` from the spec before `hermes_plan_review_gate.py` existed (confirmed
red for the right reason — `ModuleNotFoundError` — before any implementation was written), same
pattern as Phase 2's oracle above. 22 test cases, tagged `@oracle LIA-523:`, discriminating
specifically against the false-ALLOW/enrollment-ordering/tilde-expansion/ambiguity-scoping bug
classes named above. One fixture bug was found and fixed during implementation (the oracle's own
V4A MOVE-operation test helper used an invented two-line syntax that Hermes's real
`tools.patch_parser` doesn't recognize as a MOVE at all — corrected to the real single-line
`*** Move File: <src> -> <dst>` syntax after running the oracle against a real implementation and
discovering it silently never exercised the MOVE-dual-path property it claimed to).

**Hook activation is NOT just the config entry.** `agent/shell_hooks.py::register_from_config`
keys registration on the exact `(event, command)` pair against
`~/.hermes/shell-hooks-allowlist.json`; a non-interactive/gateway caller runs with
`accept_hooks=False` by default. The new command string (`hermes_plan_review_gate.py`, distinct
from the already-approved `hermes_warden_gate.py`) needs its own approval — either one interactive
CLI session to trigger and accept the TTY consent prompt once (persists to the shared allowlist,
covers every future session type), or `hooks_auto_accept: true` in `~/.hermes/config.yaml` for
unattended activation. Without this, the hook silently never registers — logged warning only, no
error — for gateway/non-TTY sessions specifically.

**Not yet done**: live end-to-end verification against a real Hermes session and a real OPA daemon
(allow/block/fail-closed/TTL-expiry/recovery, mirroring this ADR's own Verification section below)
— tracked as the final step before this phase is considered fully proven, same posture as every
other phase's live-verification requirement.

### Phase 4 — attestation-verify cutover (LIA-530)

Closes the second of `git-level-hard-backstop-design.md` §3.6's activation preconditions: a new
Rego decision block in `guardrails.rego`, under a dedicated `operation: "attestation.verify"`
value (never `git.commit`), that lets the git-level `attestation-verify` check consult
`data.warden_cc_attestations` for evidence of a Claude-Code-native `code-reviewer` SHIP —
something no phase before this one did (Phase 2's own text was explicit that reading was out of
scope; Phase 1's shadow observer never wrote a live gate outcome either). **Framing, stated
plainly and not softened**: once `main-attestation-backstop` is flipped to `enforcement: "active"`
(blocked on §3.6's still-open third precondition, LIA-534's gate-wiring — see that section), this
Rego block becomes the sole non-bypassable gate on `main` (`bypass_actors: []`,
`git-level-hard-backstop-design.md` §3.1). It went through five rounds of design-only plan-review
plus threat-model (two independent reviewer types, run repeatedly), then a further round of
implementation-plan review before landing, given the stakes of that framing.

**Two paths, one Hermes-authoritative.** `hermes_path_ok` (a fresh, `supported` OPA snapshot with
a genuine Hermes-native `code-review` SHIP) is checked first; only if Hermes has no opinion at all
does the CC-mirror path (`cc_path_ok`) get consulted. "No opinion at all" is deliberately an
existence check (`hermes_record_exists`, added late — see below), not a SHIP-specific one: a fresh
Hermes REVISE, BLOCK, or COULD_NOT_RUN for the same tree is just as authoritative as a SHIP, and
must never be silently overridden by a CC-mirrored claude SHIP for that same tree.

**The REVISE-override bug, and why the fix is an existence check, not a verdict check.** An
earlier draft gated the CC path on `not valid_ship` — "Hermes doesn't have a matching SHIP" — which
cannot distinguish "Hermes never reviewed this tree" from "Hermes reviewed it and said REVISE."
Two independent reviewers, run separately, found the same exploit: an explicit, fresh Hermes
REVISE or BLOCK on a tree could be silently overridden by a CC-mirrored claude SHIP for that same
tree, because the gate only asked "is there a SHIP," never "did Hermes already answer." The fix,
`hermes_record_exists`, checks record existence only — deliberately not re-using `valid_ship`'s
SHIP-specific re-checks — so the CC path is reachable only when Hermes genuinely has no record for
that subject at all. This is a DENY-favoring resolution, stated here explicitly rather than left
as the ambiguous "union" `git-level-hard-backstop-design.md` §3.6 originally hedged as one
possible shape: an explicit Hermes REVISE now always wins, regardless of what the CC ledger says —
the CC path is strictly a fallback for "Hermes has no opinion," never an override for "Hermes said
no."

**The mis-targeted-document exploit, and its discriminator.** `warden_attestations` and
`warden_cc_attestations` are schema-identical at the record level, so no field on its own
distinguishes a genuine CC-mirrored record from a Hermes-shaped record accidentally (or
adversarially) planted under the CC document's index — a real, demonstrated exploit against an
earlier draft. The actual discriminator, found on direct schema comparison rather than invented:
`attestation-cc-v1.schema.json` requires `queued_at` on every CC record (`issue_if_newer`, the
CC-only write path, sets it); `attestation-v1.schema.json` sets `additionalProperties: false` and
defines no `queued_at` field at all — Hermes's `issue()` never writes it. `valid_cc_mirrored_ship`
checks for its existence (Rego's `if` fails on undefined; the field is typed as an integer >= 0,
so no legitimate value, including 0, is falsy). This is a defense-in-depth backstop for Phase 2's
own most-scrutinized risk (its six-site write-target parameterization), not a replacement for it —
a shared risk, cross-referenced rather than assumed independently solved.

**A stale Hermes snapshot must deny, never silently downgrade to the weaker CC-only check.**
`cc_path_ok` requires `supported` — Hermes's own fresh-snapshot generation guard — explicitly, not
merely "no Hermes SHIP was found," which is also true when the snapshot is simply stale or
running an unrecognized contract version. Without this, a stale-Hermes-but-actually-SHIPped commit
could route through the single-backend CC path instead of denying outright, silently downgrading
the evidentiary bar the moment OPA served a stale read.

**The CC-cutover enrollment toggle: schema field exists, deliberately left unwired.**
`attestation-cc-v1.schema.json`'s `config.enforced_repos.<repo>.enabled` genuinely exists
(structural parity with `warden_attestations`), but no writer sets it — no CLI flag or call site
anywhere in `scripts/` calls `enroll()` against the CC document. Wiring one would be a real,
disproportionate-for-this-pass CLI/enrollment-flow feature this design does not need to be
correct. The real activation control for the whole mechanism stays one layer outside Rego:
`main-attestation-backstop`'s own `enforcement: "active"`/`"disabled"` state
(`git-level-hard-backstop-design.md` §3.6) — sufficient for this repo's single enrolled target
today. If a second repo ever needs independent CC-cutover control, wiring a writer for the
already-reserved schema field is a small, well-scoped follow-up, not a redesign — named explicitly
so it isn't quietly built as a drive-by inside an unrelated change later.

**Permanent, honest, by-design limitation: claude backend only.** The CC-mirror path checks the
native `claude` backend's own `code-reviewer` mirror, full stop — it does not verify `gpt`/`glm`
co-gate backend verdicts, and this is not a temporary scope cut. A commit the local strict-AND
gate (`.claude/wardens/config.json`'s `code-reviewer.backends`) actually BLOCKED on a real
non-claude REVISE still passes this check on a claude-only SHIP. Disclosed loudly, in both the
Rego comment block itself and the allow-reason string returned on that path ("claude backend only
— gpt/glm not verified, permanent limitation"), not left as an implied fact without its
consequence.

**Required, not advisory — a confirmed decision, not a hedge.** `git-level-hard-backstop-design.md`
commits to `bypass_actors: []` for the ruleset this check backs, meaning `attestation-verify` will
be a required, non-bypassable status check once activated — not an optional signal a human glances
at. The claude-only limitation above is a real, accepted gap in the sole gate on `main`, stated
plainly rather than softened by an "advisory" label that would misrepresent its actual operational
weight.

**Does not activate on its own.** Per `git-level-hard-backstop-design.md` §3.6 (corrected in the
same change as this section), landing this Rego rule clears only the SECOND of three activation
preconditions for `main-attestation-backstop`. The THIRD — `cc_attestations.enqueue_verdict`
actually wired into the real commit gates (`codex_warden_hooks.py`'s
`run_warden_backends_gate`/`run_verification_gate`) so the CC ledger this rule reads is genuinely
populated on real commits — is tracked separately as **LIA-534** and is explicitly not done yet.
Landing this section alone does not mean the CC-mirror path is reachable in production.

**Independent test coverage, verified by mutation testing, not inspection alone.**
`guardrails_test.rego` gained defense-in-depth tests for the new block (non-SHIP CC verdicts,
backend/gate/repo_id/schema_version field mismatches at both the document and record level, the
mis-targeted-document exploit and its positive control, the REVISE-override exploit and its
positive control, session-kind records, stale-Hermes-generation fall-through — both with and
without a genuine Hermes SHIP present, the CC-mirror path's own wrong-gate guard isolated from the
Hermes-native body's, the catch-all deny body's own wrong-gate guard isolated with no evidence
present at all, Hermes-native precedence when valid CC evidence ALSO genuinely coexists for the
same tree (pinned via an explicit `valid_cc_mirrored_ship` assertion, not just the resulting
`decision` — an earlier version of this same test asserted only `decision`, which is unaffected by
whether the coexisting CC evidence is real or the fixture is absent entirely, a round-2
code-review finding, fixed), and bidirectional isolation against both `git.commit` and
`file.write`). Verified by deleting each guard line in the new block one at a time and confirming
`opa test` catches it: of 31 guard lines, **28 are caught by a dedicated test** (two rounds found
real gaps and closed them — an earlier draft's CC-mirror gate guard and no-evidence-plus-wrong-gate
catch-all guard both initially survived deletion at green, closed by adding the isolating tests
named above).

**3 lines are not currently discriminated by any test, each a genuine, disclosed residual gap in
the test suite's precision (not a defect in the Rego block itself), and each individually
addressable if closing them is ever prioritized**: `hermes_record_exists`'s final existence check
(`data.warden_attestations.records[id]`) only matters for a dangling `latest` pointer — no fixture
constructs one, though one could; the deny body's literal `"allow": false` field is untested via
`not decision.allow`-style assertions specifically because Rego's `not` treats an absent key the
same as `false` (true of every pre-existing test in this file, not introduced here) — an explicit
`decision.allow == false` assertion would close it; and `valid_cc_mirrored_ship`'s `id := ...`
pointer lookup degrades gracefully, not dangerously, under deletion — Rego's own semantics for an
unbound index variable turn the lookup into an existential scan of every CC record instead of the
specific `latest_by_backend`-pointed one, which the current fixture set cannot yet distinguish
from the pointer-scoped behavior because every fixture's `latest_by_backend` entry and matching
record are mutually consistent — a fixture with a `latest_by_backend` pointer to one record ID
while a DIFFERENT, non-matching record also exists in `records` would close it.

**2 lines are proven, not merely observed, to be permanently non-discriminable by any possible
test — logically redundant by construction, not an untested gap**: `cc_supported`'s own
`input.contract_version == 1` check is an unreachable duplicate of `supported`'s identical check
(`cc_path_ok` already requires `supported` before `cc_supported` is ever reached, so this line can
never be the sole cause of a different outcome); and the CC-mirror decision body's own
`not hermes_path_ok` is provably redundant with `cc_path_ok`'s own `not hermes_record_exists` —
`valid_ship` and `hermes_record_exists` resolve the identical index lookup, so any tree where
`hermes_path_ok` is true necessarily has `hermes_record_exists` true too, which already makes
`cc_path_ok` false via its own guard before `not hermes_path_ok` is ever reached. Both are cheap,
intentional defense-in-depth, kept rather than removed — but a future reader should not mistake
either for load-bearing logic protecting against a real, reachable state.

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

- Full: `launchctl bootout gui/$(id -u)/com.deus.warden-opa`, remove the `hooks:` entries from
  `~/.hermes/config.yaml` (both `matcher: "terminal"` and, if Phase 3 is enabled,
  `matcher: "write_file|patch"`), remove the corresponding entries from
  `~/.hermes/shell-hooks-allowlist.json`.
- Per-repo, code-review gate (daemon stays running): `python3 scripts/warden_attest.py unenroll --repo <path>`.
- Per-repo, Phase 3 plan-review gate only: `python3 scripts/warden_attest.py disable-plan-review
  --repo <path>`, or remove just the `matcher: "write_file|patch"` hook entry to disable Phase 3
  everywhere at once without touching code-review enforcement — every Phase 3 change is additive,
  so this alone fully and immediately disables it.

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
