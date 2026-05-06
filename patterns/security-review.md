---
governs:
  - src/container-mounter.ts
  - src/credential-proxy.ts
  - src/ipc.ts
  - src/sender-allowlist.ts
  - src/mount-security.ts
last_verified: "2026-05-06" # auto-bump
test_tasks:
  - "Add a new mount in src/container-mounter.ts for per-group config files"
  - "Update src/mount-security.ts to permit reading a new credential path"
  - "Audit a new MCP tool for credential exposure via src/credential-proxy.ts"
  - "Add a new sender to src/sender-allowlist.ts for a specific channel"
---
# Pattern: security-review

## Trust model

| Entity | Trust level |
|--------|-------------|
| Main group | Trusted — admin control |
| Non-main groups | Untrusted — treat as hostile input |
| Container agents | Sandboxed — isolated execution |
| Channel messages | User input — potential prompt injection |

## Container isolation (primary boundary)

Each container run gets: process isolation, filesystem isolation (only explicitly mounted paths visible), non-root execution (uid 1000), and ephemeral environment (`--rm`). **The attack surface is limited by what's mounted, not by application-level permission checks.** Do not rely on code-level guards as the primary security mechanism.

## Session isolation

Each group has isolated Claude sessions at `data/sessions/{group}/.claude/`. Groups cannot see other groups' conversation history. Cross-group information disclosure is prevented at the filesystem level — not application-level.

## Mount security

- Mount allowlist lives at `~/.config/deus/mount-allowlist.json` — outside project root, never mounted into containers, cannot be modified by agents.
- **Symlinks resolved before validation** (`src/mount-security.ts`): the real path is checked against the allowlist, not the symlink. Prevents symlink-swap (TOCTOU) attacks where a valid path is replaced with a symlink to a sensitive file after the check passes.
- Container paths reject `..` and absolute paths before allowlist check.
- **`.env` is shadowed with `/dev/null`** in the project root mount (`src/container-mounter.ts`). This is a defence-in-depth measure — even if `.env` is somehow accessible in the mount, it reads as empty. Never remove this shadow mount.
- Project root mounted read-only for main group. Writable paths (group folder, IPC, `.claude/`) mounted separately.

## Credential isolation

Real credentials **never enter containers**. Host runs an HTTP proxy at `:3001`. Containers receive `ANTHROPIC_API_KEY=placeholder` and `ANTHROPIC_BASE_URL=http://host.docker.internal:<port>`. The proxy strips the placeholder, injects real credentials, and forwards to `api.anthropic.com`. Agents cannot discover real credentials from environment, stdin, files, or `/proc`.

Never committed / never mounted:
- `store/auth/` — WhatsApp session credentials
- `~/.config/deus/mount-allowlist.json`
- Any file matching blocked patterns (`.ssh`, `.env`, `credentials`, `private_key`, etc.)

## IPC authorization

| Operation | Main group | Non-main group |
|-----------|------------|----------------|
| Send to other chats | ✓ | ✗ |
| Schedule task for others | ✓ | ✗ |
| View all tasks | ✓ | Own only |
| Manage other groups | ✓ | ✗ |

Verify group identity before any privileged IPC operation.

## Sensitive local files (never committed)

| File | Contents |
|------|----------|
| `store/auth/creds.json` | WhatsApp session (encrypted) |
| `store/messages.db` | Full message history |
| `.env` | Static long-lived secrets (API keys, bot tokens) — never rotating credentials |
| `~/.config/deus/mount-allowlist.json` | Allowed mount paths |
| `data/sessions/*/` | Per-group Claude session state |

## Security audit checklist

```
[ ] No credentials in code, test files, or git history
[ ] New credentials have .env.example entry with descriptive comment
[ ] IPC operations verify group identity before privileged actions
[ ] Container mounts go through allowlist validation
[ ] .env shadow mount preserved — not removed from project root mount
[ ] No secrets in container environment or mounted paths
[ ] New mounts: does this expose anything outside the intended scope?
[ ] New IPC operations: does non-main group access need to be restricted?
[ ] Symlink paths resolved via mount-security.ts before allowlist check
```

## Extra doc

Load `docs/SECURITY.md` for the full architecture diagram and privilege comparison table.
