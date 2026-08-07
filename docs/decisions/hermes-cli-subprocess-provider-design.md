---
name: Route Hermes's Claude/Codex calls through safe CLI-subprocess providers (LIA-525) — design + one immediately-actionable config fix
description: >
  Investigates Hermes's actual current model-provider wiring for its Claude
  and Codex legs, finds one leg already has a safe subprocess-spawn runtime
  that's simply off by default (Codex) and one leg has no safe path at all
  yet (Claude), and designs the fix for both.
type: decision
tags: [hermes, wardens, billing, oauth, security, lia-525]
date: 2026-08-07
---

# Route Hermes's Claude/Codex calls through safe CLI-subprocess providers

**Date:** 2026-08-07
**Status:** Design + evidence for both legs. Codex leg: an immediately-actionable config-only fix,
specified but NOT applied in this pass — see §2.1 for why. Claude leg: needs new Hermes source,
design-only — see §2.2 for why.
**Scope:** `~/.hermes/config.yaml` (the user's live Hermes config, not a git repo), and
(follow-up, not this pass) a new provider module in `~/.hermes/hermes-agent`
(`agent/claude_code_acp_client.py` or similar), modeled directly on the existing
`agent/copilot_acp_client.py`.
**Related:** LIA-454 (the CLI-subprocess pattern this ticket asks to reuse — spawn the real,
unmodified CLI binary rather than extracting its OAuth token for a separate HTTP client). **Two
citation errors — found together in one review round, corrected in the next**: an earlier draft cited
`docs/decisions/deus-native-h1-production-wiring-design.md` and
`Session-Logs/2026-08-02/hermes-setup-and-judge-provider-design.md` as if they were committed to
`main`/verified-absent respectively — both claims were wrong, in different directions. The LIA-454
design doc is real and committed, but on the unmerged `lia-454-h1-production-wiring` branch, not
`main` (confirmed: `git log lia-454-h1-production-wiring -- docs/decisions/deus-native-h1-production-wiring-design.md`
→ two real commits, `1bb9e854`/`d41e194d`; confirmed separately that `main` itself doesn't have it:
`git show main:docs/decisions/deus-native-h1-production-wiring-design.md` → "does not exist"). The
Session-Logs file was first (wrongly) cited as citable repo history, then (also wrongly, on
correction) claimed not to exist at all — it genuinely exists, in the vault, at
`Second Brain/Deus/Session-Logs/2026-08-02/hermes-setup-and-judge-provider-design.md` (confirmed
via direct `find`), and is actually the *stronger* source for this document's ToS-risk framing: it
contains its own live-fetched primary-source verification ("Fetched `code.claude.com/docs/en/legal-and-compliance`
live, 2026-08-02," with a verbatim policy quote), not a claim borrowed from elsewhere. §1.1 below
cites that vault session log directly for the Roo Code/OAuth-restriction claim, rather than routing
through the unmerged LIA-454 branch for it. The Zed `claude-code-via-acp` ACP-bridge claim (§2.2)
is still sourced through the LIA-454 branch's own citation, not independently re-fetched by this
document — flagged there specifically, not blended in as independently-verified fact.

## 1. What this design found, that the ticket didn't already know

The ticket's framing assumed both the Claude and Codex legs need the *same* fix (build a
CLI-subprocess provider from scratch, matching LIA-454's pattern). That's true for Claude. It's
**not** true for Codex — Hermes already ships a genuine CLI-subprocess runtime for Codex,
`codex_app_server`, and the actual gap there is that it's off by default, not that it's missing.

### 1.1 The Claude leg: confirmed risky, no safe alternative exists in Hermes today

`~/.hermes/hermes-agent/plugins/model-providers/anthropic/__init__.py` (read directly):

```python
anthropic = AnthropicProfile(
    name="anthropic",
    aliases=("claude", "claude-oauth", "claude-code"),
    api_mode="anthropic_messages",
    env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    ...
)
```

This is a raw HTTP client (`fetch_models` calls `https://api.anthropic.com/v1/models` directly)
authenticated via `CLAUDE_CODE_OAUTH_TOKEN` — the same "extract another CLI's OAuth token and feed
it to a separate HTTP client" shape the vault session log
(`Second Brain/Deus/Session-Logs/2026-08-02/hermes-setup-and-judge-provider-design.md`) documents,
with its own live-fetched primary source, as the reason Anthropic's OAuth policy restricts these
tokens to native apps and as the mechanism behind Roo Code's GitHub archival. Confirmed live: the
user's actual `~/.hermes/config.yaml` sets `model: {default: claude-sonnet-5, provider: anthropic}`
— this raw-HTTP path is genuinely the live, active configuration today, not a hypothetical.

`providers/base.py`'s own module docstring confirms *why* there's no simple config flip available
here the way there is for Codex: "Provider profiles are DECLARATIVE... They do NOT own client
construction, credential rotation, or streaming." A provider profile just describes auth/endpoint
metadata; the actual HTTP-vs-subprocess transport logic lives in `agent/transports/*.py`, keyed by
`api_mode`. There is no `api_mode` value anywhere in this codebase that spawns a Claude Code CLI
subprocess — confirmed via `grep -rln "claude_code_app_server\|claude.*app.server"` across
`agent/`, `hermes_cli/`, `providers/`, `plugins/`: zero hits. This gap is real, not assumed.

### 1.2 The Codex leg: a safe path already exists, it's just off by default

`agent/transports/codex_app_server.py` (read directly, lines 1-15, 71-142): a genuine
`subprocess.Popen([codex_bin, "app-server"] + args, ...)` spawn of the real `codex` binary,
speaking newline-delimited JSON-RPC 2.0 over stdio — the same shape LIA-454 designed for Deus's own
CLI-subprocess transport, and (per LIA-454's own citation, not independently re-fetched here — see
the Verification section's provenance note) the same shape Zed's own Claude-Code-via-ACP
integration uses. Uses
`hermes_subprocess_env(inherit_credentials=True)` — the spawned `codex` process authenticates as
itself (its own `codex login` session or `OPENAI_API_KEY`), not via a token Hermes extracted and
injects into a raw HTTP call.

Selection is governed by `model.openai_runtime` (`hermes_cli/codex_runtime_switch.py`):
`VALID_RUNTIMES = ("auto", "codex_app_server")`, **default `"auto"`** — which resolves to the raw
HTTP `codex_responses` api_mode (OAuth-token-extraction), not the subprocess runtime. There's
already a `/codex-runtime on` slash command (and CLI/gateway equivalents) built specifically to
flip this. Confirmed live: the user's `~/.hermes/config.yaml` has no `model.openai_runtime` key at
all — so it's sitting on the risky default today, for both:

1. The user's own interactive Codex-model usage (if selected), and
2. The `wardens` preset's `reference_models: [{provider: openai-codex, model: gpt-5.6-sol}, ...]`
   entry — the GPT co-gate leg Hermes uses for its own plan-review/code-review ensemble, mirroring
   `~/deus`'s own warden co-gate shape per the config's own comments.

**A real discrepancy found in the live config's own comments, worth flagging plainly**: the config
states the `openai-codex` wardens leg runs "the same subscription mechanism
`scripts/codex_warden.py`'s own gpt backend already uses in `~/deus`." That's not accurate as
written. `~/deus`'s own `codex_warden.py`/`dispatch-oracle-author.sh` genuinely spawn `codex exec`
as a real subprocess (confirmed this session: `dispatch-oracle-author.sh`'s literal
`codex exec -m gpt-5.6-sol "$PROMPT"` line) — the safe pattern. Hermes's `provider: openai-codex`
here is the raw-HTTP-plus-OAuth-extraction path (`agent/transports/codex.py`'s `codex_responses`
api_mode), a different and riskier mechanism than the one the comment claims parity with. Not a
new problem this design creates — an existing config comment that overstates safety, corrected
here so it isn't relied on as evidence of already-being-safe.

## 2. The fix

### 2.1 Codex leg — config-only, no Hermes source change, NOT applied in this pass

Set `model.openai_runtime: codex_app_server` in `~/.hermes/config.yaml`. This is the entire fix —
the safe runtime already exists and ships in the version of Hermes currently installed
(confirmed: `agent/transports/codex_app_server.py` present, `MIN_CODEX_VERSION = (0, 125, 0)`
requirement stated inline). No code change, no new dependency beyond the `codex` CLI the user
already has installed and authenticated (`hermes_cli/doctor.py`'s `_safe_which("codex")` check
already exists as a doctor probe).

**Why this is named here but not executed** — a narrower, more direct reason than a first draft
of this document gave (found underspecified by review: an earlier version leaned on the LIA-522
self-hosted-runner precedent, which doesn't actually transfer cleanly — flipping one config key is
genuinely lower-stakes and more reversible than registering persistent GitHub Actions
infrastructure on a public repo, and citing that precedent alone overstated the parallel). The
real reason is simpler and still fully controlling: this is a live behavior change to the user's
interactive, daily-driver tool, made by a background session with no live user watching this
particular action — `.claude/rules/core-behavioral-rules.md`'s "Never execute without explicit
user approval. Wait to be told" applies directly and unconditionally here, independent of how
reversible the change is. Reversibility is a reason the fix is *safe to recommend*, not a license
to *apply it unapproved*. So: specified precisely enough to apply in one command (below), left for
the user to approve — inline, if they're reading this now, or in a future session — rather than
executed silently mid-roadmap.

Recommended command, for the record: edit `~/.hermes/config.yaml`'s `model:` block to add
`openai_runtime: codex_app_server`, or run `/codex-runtime on` from within a live Hermes session
(the built-in toggle, which also runs `check_codex_binary_ok()` first and reports the result).

### 2.2 Claude leg — new code, modeled directly on an existing sibling pattern, design only

No existing `api_mode` spawns a Claude Code CLI subprocess. The closest real precedent in this
same codebase is `agent/copilot_acp_client.py` — not a hypothetical pattern to invent, an actual
~200+-line module already shipped and used in production for GitHub Copilot's CLI, which:

1. Resolves the binary + args from env vars with sane defaults (`_resolve_command()`,
   `_resolve_args()` — `HERMES_COPILOT_ACP_COMMAND`/`COPILOT_CLI_PATH` env override, `"copilot"`
   fallback; `HERMES_COPILOT_ACP_ARGS` override, `["--acp", "--stdio"]` fallback).
2. Spawns via `subprocess` with `hermes_subprocess_env(inherit_credentials=True)` — same
   Tier-1-secret-stripping-but-provider-creds-flow-through helper `codex_app_server.py` uses.
3. Speaks ACP (Agent Client Protocol) over stdio, converts responses into the OpenAI-chat shape
   the rest of Hermes's pipeline expects.
4. Is registered as a `ProviderProfile` with `api_mode="chat_completions"` (routes through the
   *existing* transport, since ACP's output is normalized to look like a normal chat completion)
   and `auth_type="external_process"`.

The natural Claude equivalent, mirroring this file-for-file rather than designing a new shape from
scratch:

- New module, e.g. `agent/claude_code_acp_client.py`, spawning `claude-code-acp` — a real,
  published npm package (`claude-code-acp@0.1.1`, confirmed via `npx` resolution against the
  public registry; not installed on this machine, a genuine new dependency to add, similar in
  spirit to `copilot`'s own CLI being a prerequisite for `copilot_acp_client.py`) — LIA-454's
  design doc (committed on the unmerged `lia-454-h1-production-wiring` branch, not `main`)
  documents this as the same ACP bridge Zed's own Claude Code integration uses
  (`zed.dev/blog/claude-code-via-acp`, per that document's own citation — not independently
  re-fetched by this design), not a bespoke or unofficial wrapper.
- New `ProviderProfile` entry (a `plugins/model-providers/claude-code-acp/` directory mirroring
  `plugins/model-providers/copilot-acp/`'s two-file shape: `plugin.yaml` + `__init__.py`).
- Resolvable via the same env-var-override-with-fallback convention:
  `HERMES_CLAUDE_ACP_COMMAND`/`CLAUDE_CODE_ACP_PATH` overriding a `"claude-code-acp"` default,
  `HERMES_CLAUDE_ACP_ARGS` overriding whatever flag `claude-code-acp` needs to speak ACP over
  stdio (needs confirming against the package's own CLI surface — not verified in this pass, since
  the package isn't installed here; flagged as the first thing implementation must check, not
  assumed to mirror Copilot's `--acp --stdio` exactly).
- Config selection: a new `model.provider: claude-code-acp` value (or an analogous
  `model.anthropic_runtime` toggle, matching Codex's `openai_runtime` naming convention for
  consistency) replacing the current `model.provider: anthropic`.

**Why this stays design-only in this pass, not implemented**: the package isn't installed on this
machine (a real prerequisite gap, not a formality), the exact ACP invocation flags need confirming
against the real `claude-code-acp` CLI surface (not guessable from the Copilot sibling alone),
implementation is real, testable Hermes-source work that needs a live install-and-verify cycle
this pass didn't do, and — per this session's own Cross-Repo Worktree Handling discipline
(`.claude/rules/orchestration-rules.md`) — none of `~/deus`'s automated review gates provide real
protection for cross-repo commits into `~/.hermes/hermes-agent`, so implementation there needs the
full manual plan-review → implement → code-review → ai-eng-warden cycle self-imposed, better done
as its own dedicated pass than folded into this investigation.

## 3. What this design explicitly does NOT do

- Does not modify `~/.hermes/config.yaml` (the Codex-leg fix is specified, not applied — see §2.1).
- Does not write any new Hermes source (the Claude-leg fix is designed, not implemented — see §2.2).
- Does not install `claude-code-acp` or verify its actual CLI flag surface.
- Does not touch `~/.hermes/hermes-agent`'s live checkout at all — a scratch worktree
  (`~/.hermes/hermes-agent-worktrees/lia525-cli-subprocess-provider`, off a freshly-fetched
  `origin/main`) was used for read-only investigation and removed after; no commits were made
  there.
- Does not change Hermes's `wardens` preset's GPT co-gate leg's underlying mechanism beyond naming
  the same `model.openai_runtime: codex_app_server` fix as applicable to it too (the preset's
  `reference_models` entry uses `provider: openai-codex`, which is governed by the same runtime
  toggle as the interactive default model).

## 4. Not yet started, and why

- Applying the `model.openai_runtime: codex_app_server` config change — needs the user's explicit
  go-ahead since it changes live interactive-tool behavior, not a reviewable git commit.
- Installing `claude-code-acp` and confirming its real ACP invocation flags.
- Writing `agent/claude_code_acp_client.py` + the `plugins/model-providers/claude-code-acp/`
  profile, mirroring `copilot_acp_client.py`'s shape.
- Full review cycle (plan-reviewer, code-reviewer, ai-eng-warden — this touches LLM provider
  wiring and credential handling, squarely ai-eng-warden's remit) for the Claude-leg implementation
  once written, self-imposed manually per the Cross-Repo Worktree Handling discipline since no
  automated gate covers a `~/.hermes/hermes-agent` commit.
- Verifying, post-implementation, that the `wardens` preset's co-gate ensemble still produces
  equivalent-quality verdicts after both legs move off raw-HTTP OAuth-extraction — a real
  regression risk worth checking, not assumed safe by construction.

## Verification

Directly checked by this session, not assumed: the `anthropic`/`openai-codex`/`copilot-acp`
provider profile source (`plugins/model-providers/*/`, read in full), `providers/base.py`'s own
declarative-profile docstring, `agent/transports/codex_app_server.py`'s actual `subprocess.Popen`
call and its `inherit_credentials=True` comment, `hermes_cli/codex_runtime_switch.py`'s
`VALID_RUNTIMES`/default resolution, `agent/copilot_acp_client.py`'s real command-resolution and
spawn logic, the user's actual live `~/.hermes/config.yaml` (read-only), `claude-code-acp`'s
existence as a real published npm package (via `npx` registry resolution, package not installed),
that LIA-454's design doc IS committed but on the unmerged `lia-454-h1-production-wiring` branch
(`git log lia-454-h1-production-wiring -- docs/decisions/deus-native-h1-production-wiring-design.md`
→ commits `1bb9e854`/`d41e194d`; confirmed separately absent from `main`), and that the vault
session log `Second Brain/Deus/Session-Logs/2026-08-02/hermes-setup-and-judge-provider-design.md`
genuinely exists (confirmed via direct `find` after an earlier round wrongly claimed it didn't).
**One exception, flagged rather than blended in**: the Zed `claude-code-via-acp` ACP-bridge fact
(§2.2) is sourced through LIA-454's own citation, not independently re-fetched by this document —
the Roo Code/OAuth-policy claim (§1.1) no longer carries that caveat, since it's now cited from the
vault session log's own live-fetched primary source instead. The investigation worktree was
created from a freshly-fetched `origin/main` (`git -C ~/.hermes/hermes-agent fetch origin` showed
1534 files of upstream drift from the stale local checkout — a real gap, not skipped) rather than
the potentially-stale local `main`, and was removed after use (confirmed via `git worktree list`
showing only the pre-existing, differently-owned `lia521-pre-outbound-send` entry remaining). Two
distinct citation errors were found together in one review round and corrected in the next (§1.1/§2.2's
provenance, both directions — a file wrongly cited as committed-to-main, then a different file
wrongly cited as nonexistent) — recorded here rather than smoothed over, matching this design's own
practice on every other correction it documents.
