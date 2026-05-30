# AI Development Rules

These rules apply to any AI agent (Claude Code, Deus, or other) developing in this repository. They are machine-readable directives, not suggestions.

## Branch Workflow

1. **Always create a feature branch before making changes.** Never commit directly to `main`.
2. Branch naming: `feat/...`, `fix/...`, `docs/...`, `refactor/...`, `chore/...`, `ci/...`, `test/...`, `perf/...`
3. Verify the working tree is clean before creating a branch (`git status`).
4. After implementation: run `npm run build` and `npm test`. Both must pass before committing.
5. Before pushing, run `npm run drift-check` to catch pattern drift early. The `pre-push` hook (`.husky/pre-push`) runs this automatically against the merge-base of your branch and `origin/main`.
6. Create a PR targeting `main`. Wait for CI to pass before requesting merge.

## PR Scope and Squashing

Each PR must contain a **single squashed commit** when merged. If your branch has multiple commits, they will be squashed on merge. If two commits are fundamentally different in scope or purpose, split them into separate PRs instead.

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) strictly. Commits that don't match this format will be rejected by the commit-msg hook and CI.

Format: `type(scope): description`

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`

Scope is required (warning if missing). Use the module name: `channels`, `ipc`, `orchestration`, `container`, `skills`, `startup-gate`, `evolution`, `eval`, `memory`, `ci`, etc.

Examples:
- `feat(skills): add Gmail skill template`
- `fix(ipc): handle missing container response`
- `chore(deps): bump vitest to 4.1`

Breaking changes: add `BREAKING CHANGE:` in the commit footer. This triggers a major version bump.

## Tests

Every source code change must include unit tests. No exceptions. Tests live alongside source files (`*.test.ts`) or in `scripts/tests/` for Python.

Run tests: `npm test` (TypeScript) or `python3 -m pytest scripts/tests/` (Python).

## Skill Contributions

Skills live in `.claude/skills/`. A skill PR must only touch files inside `.claude/skills/` and optionally `docs/` or `README.md`. A CI check will reject PRs that modify both skill files and core source files.

What to commit in a skill PR:
- `SKILL.md` — documentation and instructions (required)
- `agent.ts` — container-side MCP tools (if the skill adds agent capabilities)

What stays local (never committed):
- `host.ts` / `host.js` — private host-side implementation
- `scripts/` — subprocess scripts
- `node_modules/`, `package-lock.json` — local dependencies

If your skill requires changes to core source code (`src/`, `container/agent-runner/`, `package.json`), open a separate PR for the core changes first.

## Source Code Changes

Source code PRs are accepted for: bug fixes, security fixes, simplifications, performance improvements.

New features and enhancements should be skills, not source code changes. If you believe a feature genuinely cannot be a skill, explain why in the PR description.

CODEOWNERS requires maintainer review for all source code changes.

## Security

- Never commit credentials, API keys, tokens, or secrets. Not even in test files.
- New credentials go in `.env.example` with descriptive comments.
- Design every change as if the repo is public (it is).
- Audit security implications before committing — especially for IPC, container mounts, and authentication code.

## Architecture Decisions

Before modifying `eval/`, `src/startup-gate.ts`, `src/checks.ts`, `setup/`, or `scripts/memory_indexer.py`: read `docs/decisions/INDEX.md` first. The index is short. Past decisions have non-obvious constraints that have caused regressions when ignored.

## Cross-Platform

All source code must work on macOS, Linux, and Windows. See `docs/CROSS_PLATFORM.md` for rules. Key points:
- Use `path.join()` or `path.resolve()`, never string concatenation for paths
- Use `fileURLToPath()` / `pathToFileURL()`, never `.replace('file://', '')` or `new URL('file://' + path)`
- Use `os.tmpdir()`, never hardcoded `/tmp`; use `os.devNull`, never `/dev/null`
- Use platform-aware process kill (see `killProcess()` in `remote-control.ts`), never bare `.kill('SIGKILL')`
- Automated pattern detection runs in `src/cross-platform.test.ts` on all CI platforms
- Test with `npx vitest run` on the target platform

## MCP Channel Servers

When creating or modifying MCP channel servers (`packages/mcp-*/`):

- **Always declare `capabilities: { logging: {} }`** when constructing `McpServer`. Without it, `sendLoggingMessage()` silently drops all notifications — messages will appear to connect but never reach the host process. This is by SDK design, not a bug.

```typescript
// CORRECT — notifications will reach the host
const server = new McpServer(
  { name: '@deus-ai/my-channel', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

// WRONG — sendLoggingMessage() silently does nothing
const server = new McpServer({ name: '@deus-ai/my-channel', version: '1.0.0' });
```

- When the host process uses `sendLoggingMessage` as the real-time message delivery path (as all Deus channels do via `server-base.ts`), a missing capability means **total silent message loss** — no errors, no warnings, no logs.

## Deploy and Service Restart

After any change to `src/`, `setup/`, or `packages/`:

1. **Always run `npm run build` before restarting the service.** The service runs `node dist/index.js`, not the TypeScript source. A merged PR or in-session edit does NOT auto-rebuild.
2. **Verify the running binary matches your changes.** Compare `stat dist/index.js` modification time against service startup log timestamp.
3. **Never write rotating credentials to `.env`.** OAuth tokens and short-lived keys must be read dynamically at request time from their source file (e.g., `credentials.json`). `.env` is for static secrets only.

## Debugging

When diagnosing a silent failure in the message pipeline or any multi-stage system:

1. **Establish basic facts first.** Before hypothesizing, check the simplest observable state: `SELECT MAX(timestamp) FROM messages` (are messages stored?), `pm2 logs --nostream` / `tail logs/deus.log` (is the service running?). This takes 10 seconds and prevents exploring wrong hypotheses.
2. **Instrument boundaries, don't reason about code.** Add one log line at each stage boundary, send one test input, read the logs. This definitively locates the break in 2 minutes. Reading code to guess where the failure is takes longer and misses runtime-only issues (like SDK capability gates).
3. **Read SDK source for silent failures.** When a framework function appears to fail silently, read its implementation in `node_modules/`. A 3-line function with a capability gate is found in 30 seconds — faster than tracing the entire pipeline.
4. **Use `info` level for temporary debug logs.** The service default is `info`. Using `debug` means your logs won't appear and you waste a restart cycle.

### Message Pipeline Stages

For reference, the message delivery pipeline is:

```
Channel (WhatsApp/Telegram) → MCP child process (messages.upsert / bot.on)
  → sendLoggingMessage() [requires logging capability]
  → Host MCP adapter (setNotificationHandler)
  → onMessage callback (sender allowlist check)
  → storeMessage() [SQLite]
  → Message polling loop (getNewMessages)
  → Trigger check (@Deus for non-main groups)
  → Container spawn (processGroupMessages)
```

Each `→` is a potential silent drop point. When debugging, instrument the boundaries between stages.

## Preventing Facades (built-but-not-wired)

A **facade** is code that is implemented, tested, and merged but never reachable in the runtime path — a feature flag that is never turned on, an exported module nothing calls, an MCP tool that is never registered. The LIA-133 audit found ~9 of these. The common root cause: **every facade had passing unit tests.** Green tests prove a unit behaves; they do not prove it is connected. Three layers defend against new facades accruing, each catching a different failure window:

**Layer 1 — connectivity rule (commit-time, judgment-based).** When a diff adds a new `DEUS_*` flag gate, a new integration/entry-point module, or a new top-level runtime module, the author must show the producer→consumer edge (a non-test caller, a live registration, or the flag read on a live path) **or** cite a deferred-wire Linear issue. Enforced by the `code-reviewer` warden (`.claude/wardens/code-review-rules.md`, rule `connectivity-wiring`, severity `warning`); a claim-time advisory companion lives in `verification-rules.md` (`wire-reachability`).

**Layer 2 — flag-lint (merge-time, deterministic).** *(Ships in PR2, not yet active.)* A CI job (`.github/workflows/validate-pr.yml` → `scripts/ci/flag_lint.py`) scans the PR diff for **net-new** `DEUS_*` flag gates (a flag name absent from the base ref). Each net-new flag must cite a Linear issue (`LIA-NNN` or `#NNN`) in an adjacent comment, or carry a `dev-only` escape comment, or CI fails. This is the load-bearing block — it cannot be rationalized away in review. Comment syntax for the cite/escape is accepted in `#`, `//`, and `/* */` forms. To make it truly unbypassable it must be added to branch-protection required checks (a one-time repo-admin action).

**Layer 3 — hybrid codegraph+grep orphan-sweep (drift-time, local).** *(Ships in PR3 / fast-follow.)* `scripts/maintenance/orphan_sweep.py` reports exported functions/classes/methods in a curated set of integration modules that look like facades, flagging a symbol only when BOTH the local codegraph index shows no non-test caller AND `git grep` finds no cross-file use of its name (dual-evidence corroboration). The grep arm is required because codegraph's caller graph has silent edge gaps (e.g. a factory called at `index.ts` can show zero codegraph callers) — a codegraph-only sweep would false-positive on live entry points. **This layer is macOS-local only and does NOT run on Linux or in CI:** it reads the gitignored `.codegraph/codegraph.db` from the local codegraph daemon and is scheduled via launchd (generated by `--print-launchd`). Contributors on other platforms have neither the daemon nor the schedule; it is a host-side drift sweep, not a portable gate.

**Rejected alternative — blanket zero-caller check.** A naive "flag any exported symbol with zero callers" check was rejected: it floods false positives. Flag-gated code is reachable (the flag just isn't on yet), MCP tools and channel handlers are entry points reached by dispatch (not by a static caller edge), and CLI subcommands are invoked by argv. The three layers above are deliberately scoped to *new* flags and *curated* modules with a citation escape, trading completeness for a usable false-positive rate. Full rationale: `docs/decisions/facade-prevention-mechanism.md`.

## What Not To Do

- Don't manually edit `CHANGELOG.md` — it's auto-generated by release-please
- Don't bump the version in `package.json` — release-please handles this
- Don't add features as source code changes — use skills
- Don't modify files outside the scope of your change
- Don't skip pre-commit hooks with `--no-verify`
- Don't force-push to shared branches
