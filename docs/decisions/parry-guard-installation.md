# parry-guard Installation (Host Claude Code Defense)

**Date:** 2026-05-05
**Status:** Accepted
**Scope:** Host Claude Code sessions (not container agents)
**Related:** [docs/SECURITY.md](../SECURITY.md), [tui-permission-bridge.md](tui-permission-bridge.md)

## Context

Deus processes untrusted user input from WhatsApp, Telegram, Slack, Discord,
and Gmail. The container isolation boundary (documented in `docs/SECURITY.md`)
protects the host from container agents. However, host-level Claude Code
sessions — used by the operator directly — have no equivalent runtime defense
against prompt injection in tool inputs and outputs.

This creates a two-layer defense need:

| Layer | Surface | Tool | Protects |
|-------|---------|------|----------|
| 1 | Channel ingestion | Pre-ingestion pattern scanner (orchestrator) | Container agents from malicious channel messages |
| 2 | Host Claude Code | parry-guard | Operator sessions from injected tool I/O |

Layer 1 runs inside the Deus host process and filters messages before they
reach container agents. Layer 2 runs as a Claude Code hook daemon and scans
tool inputs/outputs during interactive host sessions.

## Decision

Install **parry-guard** as the Layer 2 defense for host Claude Code sessions.

### What parry-guard provides

- **DeBERTa ML inference** — classifies tool inputs/outputs for prompt
  injection signals.
- **Tree-sitter AST exfiltration detection** — parses code in tool outputs for
  data exfiltration patterns.
- **Pattern matching** — fast-path regex rules for known injection signatures.
- **Persistent daemon** — runs as a background process with Unix socket IPC.
  First invocation starts the daemon and downloads HuggingFace models.
- **Claude Code hook integration** — hooks into `PreToolUse` and `PostToolUse`
  events.

### What it does NOT protect

- **Container agents** — containers are isolated and use their own pre-ingestion
  scanner. parry-guard runs on the host only.
- **Non-Claude backends** — hooks are Claude Code specific. Codex sessions are
  not covered (they have separate permission semantics).
- **Channel message content** — that is Layer 1's job.

### Installation

parry-guard is distributed via PyPI (uvx) and crates.io (cargo). The canonical
runtime invocation uses `uvx`, so installation via `uvx` is preferred for
consistency with the hook configuration.

```bash
# Preferred: uvx auto-installs into the uv tool cache
uvx parry-guard --version

# Alternative: Rust/cargo native install
cargo install parry-guard
```

First run downloads HuggingFace models (~500MB). Set `HF_TOKEN` if downloading
from gated repositories.

### Hook Configuration

Add the following to `~/.claude/settings.json` under the user's hooks:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "command": "uvx parry-guard hook",
        "timeout": 1000
      }
    ],
    "PostToolUse": [
      {
        "command": "uvx parry-guard hook",
        "timeout": 5000
      }
    ]
  }
}
```

**Timeout rationale:**
- `PreToolUse` at 1000ms — fast-path pattern matching on tool inputs; daemon
  already running.
- `PostToolUse` at 5000ms — ML inference on tool outputs; heavier analysis
  justified because the output is already produced and a few seconds of
  scanning is acceptable.

> **Do not modify `~/.claude/settings.json` from this PR.** The operator applies
> this configuration manually after running the setup script, or uses the
> `/update-config` skill.

### Setup Script

`scripts/setup-parry-guard.sh` automates pre-flight checks:

1. Verifies `uvx` or `cargo` is available.
2. Checks for an existing parry-guard installation.
3. Validates `HF_TOKEN` is set (warns if missing — may be needed for gated
   model downloads).
4. Runs a test scan to verify the daemon starts.
5. Prints the hook configuration snippet for manual application.

## Consequences

- Host Claude Code sessions gain ML-based prompt injection defense.
- First run has a one-time ~500MB model download cost.
- The daemon adds ~1-5 seconds of latency to tool calls (mostly on
  `PostToolUse`).
- Container agents are unaffected — their defense remains the pre-ingestion
  scanner.
- Codex/OpenAI sessions are not covered — this is Claude Code specific.
