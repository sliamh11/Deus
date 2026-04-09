---
governs:
  - src/container-mounter.ts
  - src/credential-proxy.ts
  - src/ipc.ts
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
- Symlinks resolved before validation (prevents traversal attacks).
- Container paths reject `..` and absolute paths.
- `.env` is shadowed with `/dev/null` in the project root mount.
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
| `.env` | API keys and channel tokens |
| `~/.config/deus/mount-allowlist.json` | Allowed mount paths |
| `data/sessions/*/` | Per-group Claude session state |

## Security audit checklist

```
[ ] No credentials in code, test files, or git history
[ ] New credentials have .env.example entry with descriptive comment
[ ] IPC operations verify group identity before privileged actions
[ ] Container mounts go through allowlist validation
[ ] No secrets in container environment or mounted paths
[ ] New mounts: does this expose anything outside the intended scope?
[ ] New IPC operations: does non-main group access need to be restricted?
```

## Extra doc

Load `docs/SECURITY.md` for the full architecture diagram and privilege comparison table.
