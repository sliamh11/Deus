# Deus Security Model

## Trust Model

| Entity | Trust Level | Rationale |
|--------|-------------|-----------|
| Main group | Trusted | Private self-chat, admin control |
| Non-main groups | Untrusted | Other users may be malicious |
| Container agents | Sandboxed | Isolated execution environment |
| WhatsApp messages | User input | Potential prompt injection |

## Security Boundaries

### 1. Container Isolation (Primary Boundary)

Agents execute in containers (lightweight Linux VMs), providing:
- **Process isolation** - Container processes cannot affect the host
- **Filesystem isolation** - Only explicitly mounted directories are visible
- **Non-root execution** - Runs as unprivileged `node` user (uid 1000)
- **Ephemeral containers** - Fresh environment per invocation (`--rm`)

This is the primary security boundary. Rather than relying on application-level permission checks, the attack surface is limited by what's mounted.

### 2. Mount Security

**External Allowlist** - Mount permissions stored at `~/.config/deus/mount-allowlist.json`, which is:
- Outside project root
- Never mounted into containers
- Cannot be modified by agents

**Default Blocked Patterns:**
```
.ssh, .gnupg, .aws, .azure, .gcloud, .kube, .docker,
credentials, .env, .netrc, .npmrc, id_rsa, id_ed25519,
private_key, .secret
```

**Protections:**
- Symlink resolution before validation (prevents traversal attacks)
- Container path validation (rejects `..` and absolute paths)
- `nonMainReadOnly` option forces read-only for non-main groups

**Read-Only Project Root:**

The main group's project root is mounted read-only. Writable paths the agent needs (group folder, IPC, `.claude/`) are mounted separately. This prevents the agent from modifying host application code (`src/`, `dist/`, `package.json`, etc.) which would bypass the sandbox entirely on next restart.

### 3. Session Isolation

Each group has isolated Claude sessions at `data/sessions/{group}/.claude/`:
- Groups cannot see other groups' conversation history
- Session data includes full message history and file contents read
- Prevents cross-group information disclosure

### 4. IPC Authorization

Messages and task operations are verified against group identity:

| Operation | Main Group | Non-Main Group |
|-----------|------------|----------------|
| Send message to own chat | ✓ | ✓ |
| Send message to other chats | ✓ | ✗ |
| Schedule task for self | ✓ | ✓ |
| Schedule task for others | ✓ | ✗ |
| View all tasks | ✓ | Own only |
| Manage other groups | ✓ | ✗ |

### 5. Credential Isolation (Credential Proxy)

Real API credentials **never enter containers**. Instead, the host runs an HTTP credential proxy that injects authentication headers transparently.

**How it works:**
1. Host starts a credential proxy on `CREDENTIAL_PROXY_PORT` (default: 3001)
2. Containers receive `ANTHROPIC_BASE_URL=http://host.docker.internal:<port>` and `ANTHROPIC_API_KEY=placeholder`
3. The SDK sends API requests to the proxy with the placeholder key
4. The proxy strips placeholder auth, injects real credentials (`x-api-key` or `Authorization: Bearer`), and forwards to `api.anthropic.com`
5. Agents cannot discover real credentials — not in environment, stdin, files, or `/proc`

**NOT Mounted:**
- WhatsApp session (`store/auth/`) - host only
- Mount allowlist - external, never mounted
- Any credentials matching blocked patterns
- `.env` is shadowed with `/dev/null` in the project root mount

### 6. Sensitive Local Files

The following local files contain sensitive data and are never committed to git:

| File | Contents | Protection |
|------|----------|------------|
| `store/auth/creds.json` | WhatsApp session credentials (encrypted by Baileys) | `.gitignore`, host-only (never mounted) |
| `store/messages.db` | Full message history across all groups | `.gitignore`, host-only |
| `.env` | API keys and channel tokens (GEMINI, Telegram, OAuth) | `.gitignore`, `umask 077` on write |
| `~/.config/deus/mount-allowlist.json` | Allowed mount paths | Outside project, never mounted |
| `data/sessions/*/` | Per-group Claude session state (full conversation history) | `.gitignore`, group-isolated mounts |

**Recommendations:**
- Back up `store/auth/` if you want to avoid WhatsApp re-authentication
- The `messages.db` file grows unbounded — consider periodic cleanup for long-running installations
- If you suspect credential compromise, delete `store/auth/` and re-authenticate with `/add-whatsapp`

## Privilege Comparison

| Capability | Main Group | Non-Main Group |
|------------|------------|----------------|
| Project root access | `/workspace/project` (ro) | None |
| Group folder | `/workspace/group` (rw) | `/workspace/group` (rw) |
| Global memory | Implicit via project | `/workspace/global` (ro) |
| Additional mounts | Configurable | Read-only unless allowed |
| Network access | Unrestricted | Unrestricted |
| MCP tools | All | All |

## Reporting Vulnerabilities

Report security vulnerabilities privately via [GitHub Security Advisories](https://github.com/sliamh11/Deus/security/advisories/new). Do not open a public issue for security bugs.

## Security Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        UNTRUSTED ZONE                             │
│  WhatsApp Messages (potentially malicious)                        │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Trigger check, input escaping
┌──────────────────────────────────────────────────────────────────┐
│                     HOST PROCESS (TRUSTED)                        │
│  • Message routing                                                │
│  • IPC authorization                                              │
│  • Mount validation (external allowlist)                          │
│  • Container lifecycle                                            │
│  • Credential proxy (injects auth headers)                       │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Explicit mounts only, no secrets
┌──────────────────────────────────────────────────────────────────┐
│                CONTAINER (ISOLATED/SANDBOXED)                     │
│  • Agent execution                                                │
│  • Bash commands (sandboxed)                                      │
│  • File operations (limited to mounts)                            │
│  • API calls routed through credential proxy                     │
│  • No real credentials in environment or filesystem              │
└──────────────────────────────────────────────────────────────────┘
```
