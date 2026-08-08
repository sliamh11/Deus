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
4. Add the Hermes hooks (personal `~/.hermes/config.yaml`, never repo-committed). Three
   independent gates, each its own hook entry under the same `matcher: "terminal"` (a distinct
   `command` string is a distinct, independently-invoked registration — none of the three
   scripts calls or depends on another):
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
   ```
   Do **not** set `hooks_auto_accept: true` globally — approve each command explicitly so
   Hermes records it in `~/.hermes/shell-hooks-allowlist.json`. Verify with `hermes hooks list`
   and `hermes hooks doctor`. Worst-case cumulative latency for one `git commit` is
   sequential-additive across however many of the three hooks are enrolled, each up to its own
   `timeout:` (3s default) — up to ~9s worst case with all three enrolled and slow; inherent to
   Hermes's own sequential hook-dispatch loop.
5. Enroll a repo. Code-review, ai-eng-warden, and verification-gate are three independent,
   additive on/off switches — enabling one does not enable the others:
   ```bash
   python3 scripts/warden_attest.py enroll --repo /path/to/repo
   python3 scripts/warden_attest.py enable-ai-eng-warden --repo /path/to/repo
   python3 scripts/warden_attest.py enable-verification-gate --repo /path/to/repo
   ```
   A repo with none of these enabled is completely unaffected by any guardrail — every commit
   form works normally, including any of the repo's own git hooks.

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
  `hooks:` block from `~/.hermes/config.yaml`, remove the corresponding entry from
  `~/.hermes/shell-hooks-allowlist.json`.
- **Turn off for one repo only** (daemon and other repos unaffected):
  `python3 scripts/warden_attest.py unenroll --repo <path>`.
- **Turn off ai-eng-warden or verification-gate for one repo only** (independent of
  code-review's own `enabled` switch and of each other):
  `python3 scripts/warden_attest.py disable-ai-eng-warden --repo <path>` /
  `python3 scripts/warden_attest.py disable-verification-gate --repo <path>`.
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
opa test -v scripts/warden_policy/policy
python3 -m pytest scripts/warden_policy/tests -v
python3 -m pytest scripts/tests/test_warden_attest_reconcile.py -v
shellcheck scripts/start_warden_opa.sh
plutil -lint launchd/com.deus.warden-opa.plist
python3 -m py_compile scripts/install_warden_opa_sync_launchd.py
```
