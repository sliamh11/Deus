# Hermes guardrails via OPA — install, operate, roll back

See `docs/decisions/opa-warden-attestations-v1.md` for the full design rationale. This is the
operational how-to.

## Requirements

- OPA **1.19.0+** (`brew install opa`) — the Rego policy uses the `if`-keyword rule form (Rego v1
  syntax, requires OPA ≥0.59).
- Hermes Agent with `--accept-hooks` support (any recent install).
- macOS with `launchd` (the daemon install steps below are macOS-specific; Linux/systemd
  packaging is out of scope for v1).

## Install

1. Seed a generation-0 ledger (OPA loads this file at startup — it must exist first):
   ```bash
   python3 -c "
   from pathlib import Path
   import json, sys
   sys.path.insert(0, 'scripts')
   from warden_policy.attestation_store import _empty_document
   ledger = Path.home() / '.config' / 'deus' / 'guardrails' / 'attestations-v1.json'
   ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   if not ledger.exists():
       ledger.write_text(json.dumps(_empty_document(), indent=2, sort_keys=True))
       ledger.chmod(0o600)
   "
   ```
2. Render the launchd template (`launchd/com.deus.warden-opa.plist`) — substitute
   `{{PROJECT_ROOT}}` (this repo's absolute path), `{{GUARDRAILS_HOME}}`
   (`~/.config/deus/guardrails`), `{{HOME}}` — and install:
   ```bash
   cp <rendered-plist> ~/Library/LaunchAgents/com.deus.warden-opa.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deus.warden-opa.plist
   launchctl kickstart -k gui/$(id -u)/com.deus.warden-opa
   curl -fsS 'http://127.0.0.1:8181/health?plugins'
   ```
3. Install the periodic self-heal job (LIA-533) -- re-syncs the disk ledger to OPA every 5
   minutes if they've drifted (e.g. a transient PUT failure inside a write, or OPA restarting
   with a stale on-disk snapshot). Without this, a divergence is permanent until someone
   manually runs `sync` (see Troubleshooting). Run this from your main checkout, not a worktree
   -- the plist bakes in the invoking directory as `PROJECT_ROOT` (same convention as
   `install_hermes_procedure_recheck_launchd.py`), so a removed worktree silently breaks it:
   ```bash
   python3 scripts/install_warden_opa_sync_launchd.py
   ```
4. Add the Hermes hooks (personal `~/.hermes/config.yaml`, never repo-committed). Four
   independent gates, each its own hook entry (three under `matcher: "terminal"`; the new
   plan-review gate under its own `matcher: "write_file|patch"` since it fires on file writes,
   not shell commands) — a distinct `command` string is a distinct, independently-invoked
   registration — none of the four scripts calls or depends on another:
   ```yaml
   hooks:
     pre_tool_call:
       - matcher: "terminal"
         command: "python3 <PROJECT_ROOT>/scripts/hermes_warden_gate.py"
         timeout: 3
       - matcher: "terminal"
         command: "python3 <PROJECT_ROOT>/scripts/hermes_ai_eng_warden_gate.py"
         timeout: 3
       - matcher: "terminal"
         command: "python3 <PROJECT_ROOT>/scripts/hermes_verification_gate.py"
         timeout: 3
       - matcher: "write_file|patch"
         command: "python3 <PROJECT_ROOT>/scripts/hermes_plan_review_gate.py"
         timeout: 3
   ```
   This is the full target set once every gate below is enabled — a given
   `~/.hermes/config.yaml` only needs entries for the gates actually enabled for at least one
   repo, so a real config may legitimately carry fewer than four (e.g. just the `terminal`
   entry plus the `write_file|patch` one, with the ai-eng-warden/verification-gate `terminal`
   entries added later only once those gates are actually used).

   Adding the entry above is necessary but **not sufficient** to activate a hook — Hermes
   still won't dispatch it until its exact `(event, command)` pair is separately approved into
   the allowlist (next paragraph). Do **not** set `hooks_auto_accept: true` globally — approve
   each command explicitly so Hermes records it in `~/.hermes/shell-hooks-allowlist.json`. The
   allowlist keys on the `(event, command)` **pair**, not the command string alone, so
   approving one script's hook does not implicitly approve another's — start a new `hermes`
   session: every configured hook that is not yet allowlisted prompts once at startup,
   regardless of its `matcher`.
   **A non-interactive/gateway caller runs with `accept_hooks=False` by default and never sees
   that TTY prompt at all — the hook then silently never registers (a logged warning only, no
   error raised) and every matching call proceeds completely ungated, with nothing at
   write-time to indicate it.** For unattended activation, pass `--accept-hooks` on the
   invoking `hermes` command, or set `hooks_auto_accept: true` and accept its blanket-approval
   tradeoff. Either way, always confirm real registration afterward with `hermes hooks list`
   (prints `✓ allowed` / `✗ not allowlisted`) and `hermes hooks doctor` (prints
   `✓ allowlisted (approved ...)`) — a `doctor` line without `✓ allowlisted` did not activate no
   matter how confidently the config file reads, and `doctor` also flags a stale approval if the
   script's mtime moved since it was approved.
   Worst-case cumulative latency for one `git commit` is sequential-additive across however many
   of the three `terminal`-matcher hooks are enrolled, each up to its own `timeout:` (3s default)
   — up to ~9s worst case with all three enrolled and slow; inherent to Hermes's own sequential
   hook-dispatch loop. The `write_file|patch`-matcher plan-review gate matches a different tool,
   not `terminal` — it's the same `pre_tool_call` event, so it does not add to that `terminal`-call
   latency chain, and instead adds its own separate up-to-3s `timeout:` on each qualifying
   write/patch call.
5. Enroll a repo. Code-review, ai-eng-warden, verification-gate, and plan-review are four
   independent, additive on/off switches — enabling one does not enable the others:
   ```bash
   python3 scripts/warden_attest.py enroll --repo /path/to/repo
   python3 scripts/warden_attest.py enable-ai-eng-warden --repo /path/to/repo
   python3 scripts/warden_attest.py enable-verification-gate --repo /path/to/repo
   python3 scripts/warden_attest.py enable-plan-review --repo /path/to/repo
   ```
   A repo with none of these enabled is completely unaffected by any guardrail — every commit
   form works normally, including any of the repo's own git hooks. `enable-plan-review` is the
   *last* of three things that must all be true before writes are actually gated for a repo,
   not the only one: (1) the `write_file|patch` config entry from step 4 must be present, (2)
   that hook's `(event, command)` pair must be genuinely **allowlisted** (step 4's TTY
   approval, or `--accept-hooks`/`hooks_auto_accept` — confirm with `hermes hooks doctor`,
   since a non-interactive/gateway session silently never registers it otherwise, no error,
   just a logged warning), and only then does (3) `enable-plan-review` make the
   already-registered hook start gating writes for this repo. Skip any one of the three and
   writes proceed completely ungated with no visible warning at write time — see Daily use
   below for the command that authorizes writes once all three are true, and the two named
   limitations (relative-path calls, session-attestation TTL) before relying on it in a real
   session. To turn plan-review back off for one repo without touching the other three
   switches: `python3 scripts/warden_attest.py disable-plan-review --repo /path/to/repo`.

## Daily use

```bash
# after code review, issue a SHIP for the current staged tree:
python3 scripts/warden_attest.py issue --repo <path> --verdict SHIP \
    --reviewer-id 'code-reviewer@claude-sonnet-5' --reason '...'

# ai-eng-warden: --backend hermes is REQUIRED (a fixed, self-identifying backend id --
# omitting it, or any other value, is a usage error, not a silent misroute):
python3 scripts/warden_attest.py issue --repo <path> --gate ai-eng-warden --backend hermes \
    --verdict SHIP --reviewer-id 'ai-eng-warden@hermes' --reason '...'

# verification-gate: latest-indexed, single-verdict -- no --backend flag:
python3 scripts/warden_attest.py issue --repo <path> --gate verification-gate \
    --verdict SHIP --reviewer-id 'verification-gate@hermes' --reason '...'
```

```bash
# plan-review: session-bound, not tree-bound -- authorizes WRITES for a Hermes session
# (--session-id), not a git commit. Required once a repo has enable-plan-review on, or every
# write_file/patch call is blocked. Re-issue after plan_review_ttl_seconds (default 7200s)
# elapses, or the attestation expires mid-session and writes silently re-block.
#
# <hermes-session-id>: `hermes -q ...` (one-shot automation) prints it to stderr as
# `session_id: <id>` when the run ends (e.g. 20260101_120000_abc123); inside an interactive
# `hermes chat` session, the default (non-compact) startup welcome banner already shows
# `Session: <id>`, or get it via `/title` (prints `Session ID: <id>`) or the exit summary --
# `--compact` mode / a narrow (<80 col) terminal omits the startup banner's session line, so
# those two are the fallbacks there. To find a recent session's id afterward, run
# `hermes sessions list` (its `ID` column uses the same value):
python3 scripts/warden_attest.py plan-review --repo <path> --session-id <hermes-session-id> \
    --verdict SHIP --reviewer-id 'plan-reviewer@hermes' --reason '...'
```

Two named v1 limitations (`docs/decisions/opa-warden-attestations-v1.md`, "Phase 3 — Hermes
plan-review gate" section, the bold-labeled "Named v1 limitation" and "Session-attestation TTL"
passages) worth knowing before relying on plan-review in a real session:
- **Relative-path (including `~`) `write_file`/`patch` calls are always blocked (no override)** once
  the gate resolves the call's `cwd` to a repo that is itself plan-review-enrolled
  (`scripts/hermes_plan_review_gate.py:261-275`) — a real usability cost given how commonly
  coding agents emit relative paths. Use absolute paths for writes in an enrolled repo. Named
  v1 residual gap: when `cwd` isn't inside an enrolled repo at all (e.g. not a git repo, or a
  repo that hasn't run `enable-plan-review`), this half of the gate imposes no block on the
  relative-path call — matching the pre-existing, already-shipped ungated behavior for that
  case, not a regression.
- **Session attestations expire** after `plan_review_ttl_seconds` (default 7200s / 2h,
  overridable via `data.warden_attestations.config.plan_review_ttl_seconds`). A long session can
  silently re-block mid-flight once the TTL lapses — re-run the `plan-review` command above to
  refresh it.

```bash
# supported commit form (BOTH flags required, -c must precede `commit`):
git -c core.hooksPath=<any-empty-dir> commit --no-verify -m 'message'

# -C is supported: identity resolves from the -C target, not cwd (a safe, absolute or
# cwd-relative path only -- an unsafe value, e.g. containing shell metacharacters, blocks
# unconditionally):
git -C /path/to/repo -c core.hooksPath=<any-empty-dir> commit --no-verify -m 'message'

# check what the guardrail would decide for a given command, without running it:
python3 scripts/warden_attest.py check --repo <path> --command 'git commit -m x'

# inspect attestation history for a repo:
python3 scripts/warden_attest.py inspect --repo <path>
```

## Rollback

- **Turn off entirely**: `launchctl bootout gui/$(id -u)/com.deus.warden-opa`, remove the
  `hooks:` entries from `~/.hermes/config.yaml` (all `matcher: "terminal"` entries and, if
  the plan-review gate is enabled, the `matcher: "write_file|patch"` entry), remove the
  corresponding entries from `~/.hermes/shell-hooks-allowlist.json`. Takes effect on the
  next `hermes` start, not immediately — hooks are registered once at process startup with
  no unregister/reload path, so an already-running session keeps every hook it started with
  registered for its lifetime regardless of this edit.
- **Turn off for one repo only, code-review gate** (daemon and other repos unaffected):
  `python3 scripts/warden_attest.py unenroll --repo <path>`.
- **Turn off ai-eng-warden or verification-gate for one repo only** (independent of
  code-review's own `enabled` switch and of each other):
  `python3 scripts/warden_attest.py disable-ai-eng-warden --repo <path>` /
  `python3 scripts/warden_attest.py disable-verification-gate --repo <path>`.
- **Turn off the plan-review gate for one repo only** (independent of the other three
  switches; code-review/ai-eng-warden/verification-gate enforcement for that repo stays
  active): `python3 scripts/warden_attest.py disable-plan-review --repo <path>`.
- **Turn off the plan-review gate everywhere at once** (code-review/ai-eng-warden/
  verification-gate enforcement unaffected — the plan-review gate is purely additive over the
  other three): remove just the `matcher: "write_file|patch"` entry from
  `~/.hermes/config.yaml`. Same startup-only-registration caveat as above: takes effect on
  the next `hermes` start, not immediately — an already-running session keeps the hook
  registered for its lifetime. The corresponding `~/.hermes/shell-hooks-allowlist.json`
  entry can stay — it is inert without the matching config entry.
- **Turn off the periodic self-heal job only** (daemon and gate unaffected — you lose automatic
  drift recovery, manual `sync` still works): `python3
  scripts/install_warden_opa_sync_launchd.py --uninstall`, or directly `launchctl bootout
  gui/$(id -u)/com.deus.warden-opa-sync`.

## Troubleshooting

- **Commit blocked with "guardrails policy engine (OPA) unreachable"**: the daemon is down.
  `launchctl kickstart -k gui/$(id -u)/com.deus.warden-opa`, then check
  `~/.config/deus/guardrails/logs/warden-opa.error.log`.
- **Commit blocked with "persisted but not activated" after `issue`**: the disk write succeeded
  but OPA's PUT failed (daemon was briefly down, e.g.). If the periodic self-heal job (LIA-533,
  install step 3 above) is running, this self-corrects within 5 minutes automatically. For
  immediate recovery, run `python3 scripts/warden_attest.py sync` once the daemon is back up —
  no need to reissue the attestation. Check `launchctl list | grep warden-opa-sync` and
  `<PROJECT_ROOT>/logs/warden-opa-sync.log` to confirm the self-heal job is actually running.
- **Every decision logged to** `~/.config/deus/guardrails/logs/decisions.jsonl` (one JSON line
  per commit-shaped call) — redacted (hashed command, no raw repo paths or commit messages), but
  enough to answer "is this gate ever actually firing."

## Verification (running the test suite yourself)

```bash
opa fmt --fail scripts/warden_policy/policy
opa check --strict scripts/warden_policy/policy
# --ignore excludes the loose JSON Schema docs: opa test merges same-directory JSON files into one
# data tree, and the two schema files' conflicting top-level keys (e.g. $id) trigger a merge error
# otherwise (LIA-538). Neither schema file is consumed by the Rego policy itself.
opa test -v --ignore="*.schema.json" scripts/warden_policy/policy
python3 -m pytest scripts/warden_policy/tests -v
python3 -m pytest scripts/tests/test_warden_attest_reconcile.py -v
shellcheck scripts/start_warden_opa.sh
plutil -lint launchd/com.deus.warden-opa.plist
python3 -m py_compile scripts/install_warden_opa_sync_launchd.py
```
