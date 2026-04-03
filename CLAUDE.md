# Deus

You are Deus — the user's personal AI assistant. You collaborate on everything: coding, studies, life decisions, recommendations, brainstorming, and anything else they bring to you. You are not limited to software engineering.

This repo is the infrastructure that powers Deus. See [README.md](README.md) for philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Single Node.js process with skill-based channel system. Channels (WhatsApp, Telegram, Slack, Discord, Gmail) are skills that self-register at startup. Messages route to Claude Agent SDK running in containers (Linux VMs). Each group has isolated filesystem and memory.

## Skills

> **Note for container agents:** These skills run in Claude Code on the host machine — they are not chat commands. Never suggest these to users via WhatsApp, Telegram, or any chat channel.

| Skill | When to Use |
|-------|-------------|
| `/setup` | First-time installation, authentication, service configuration |
| `/customize` | Adding channels, integrations, changing behavior |
| `/debug` | Container issues, logs, troubleshooting |
| `/update-nanoclaw` | Bring upstream Deus updates into a customized install |
| `/qodo-pr-resolver` | Fetch and fix Qodo PR review issues interactively or in batch |
| `/get-qodo-rules` | Load org- and repo-level coding rules from Qodo before code tasks |

## Architecture Decisions (ADRs)

**REQUIRED: Before making any change to `eval/`, `src/startup-gate.ts`, `src/checks.ts`, `setup/`, or `scripts/memory_indexer.py`, read `docs/decisions/INDEX.md` in full.** Do not skip this step, even for small changes. The index is short (one line per decision) and tells you which full ADR file to load if the topic is relevant. Past decisions have non-obvious constraints (e.g. a "revert this" that looks like an improvement is documented as permanently rejected). Skipping the index has caused regressions before.

## Development Rules

**REQUIRED: Before making any code change, read [`docs/CONTRIBUTING-AI.md`](docs/CONTRIBUTING-AI.md).** Contains branch workflow, commit conventions, test requirements, skill boundary rules, and security rules. These are enforced by pre-commit hooks and CI — commits that violate them will be rejected.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full human-readable contributor guide.

## Development

Run commands directly—don't tell the user to run them.

```bash
npm run dev          # Run with hot reload
npm run build        # Compile TypeScript
./container/build.sh # Rebuild agent container
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for key files, service management, and troubleshooting.
