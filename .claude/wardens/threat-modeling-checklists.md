# Threat Modeling Checklists — STRIDE Reference

> Read by `threat-modeler` on every invocation. Architecture-level only.
> Code-level patterns (regex, sanitization calls) belong to code-reviewer.
> Sources: OWASP Top 10, CWE Top 25, ASVS L1–L3.

## SPOOFING

- **OAuth flows:** Require `state` parameter (anti-CSRF), PKCE for public clients, exact-match redirect URI allowlist — no wildcards. [CWE-352, OWASP A07:2021]
- **JWT security:** Lock algorithm explicitly (reject `none`, reject symmetric when expecting asymmetric). Validate `aud`/`iss`/`exp` claims. Keys sourced from server config only — never from token headers (`jku`/`jwk`/`kid`). [CWE-347]
- **Password storage:** Must use a memory-hard KDF (argon2id, bcrypt, scrypt). Never MD5/SHA-family for passwords. [CWE-327, OWASP A02:2021]
- **Account enumeration:** Auth endpoints must return identical responses and timing for valid vs invalid credentials. [CWE-287]
- **WebAuthn/Passkeys:** `userVerification: required`, `rpID` scoped to exact domain, single-use challenges with server-side verification. [CWE-287]

## TAMPERING

- **Mass assignment:** Designs that accept user input into DB writes must specify an explicit field allowlist. Never pass raw request body directly to ORM/storage. [CWE-915]
- **Supply chain:** Lockfile committed and enforced in CI (`npm ci` not `npm install`). Review `postinstall` scripts for new dependencies. Scoped packages for private deps. [CWE-1357, OWASP A06:2021]
- **Webhook verification:** Incoming webhooks must verify HMAC/signature before processing. Reject on verification failure — never process optimistically. [CWE-345]
- **Path traversal:** Any operation that resolves user-supplied paths (file uploads, zip extraction, URL paths) must validate the resolved path stays within the intended directory. [CWE-22]

## REPUDIATION

- **Security event logging:** Log authn success/failure, privilege changes, and resource access with structured fields (who, what, when, from-where). Never embed raw user input into log strings. [CWE-117, CWE-778]
- **Audit trails:** Irreversible or high-stakes actions (financial, admin, deletion) require immutable audit records with actor identity, timestamp, and input snapshot. [PCI-DSS Req 10]

## INFORMATION DISCLOSURE

- **SSRF prevention:** Server-side URL fetches must validate against a domain allowlist AND block RFC-1918/link-local/loopback ranges. [CWE-918, OWASP A10:2021]
- **PII in transit:** PII must not appear in logs, URLs, query parameters, or error responses. Serialized objects must exclude sensitive fields (password hashes, tokens, internal IDs). [CWE-200, CWE-312]
- **Secrets management:** No hardcoded credentials. `.env` never committed. Pre-commit secret scanning. Source maps disabled in production. [CWE-798]
- **JWT payload minimization:** JWT payloads are base64-encoded (not encrypted). Never include passwords, PII, payment data, or internal secrets in claims. [CWE-312]
- **RAG/vector search:** User-scoped permission filters must be enforced at query time — a user's query must never return another user's documents. [CWE-862]

## DENIAL OF SERVICE

- **Tiered rate limiting:** Global limit (request ceiling), per-endpoint limits (tighter on auth/login), per-user limits (not just per-IP). Distributed deployments need shared state (Redis/equivalent). [CWE-400, CWE-770]
- **Request bounds:** Body size limits, request timeouts, connection limits per source. Designs exposing file upload or streaming endpoints must specify max sizes. [CWE-400]
- **Race conditions:** Financial operations, inventory, and redemption flows require atomic operations or row-level locking. Design must specify concurrency strategy. [CWE-362]
- **Pagination:** All collection/list endpoints must be paginated. No unbounded queries. [CWE-400]
- **Numeric validation:** User-supplied numeric values that control iteration, allocation, or recursion depth must have explicit bounds. [CWE-190]

## ELEVATION OF PRIVILEGE

- **IDOR:** Every resource access must verify ownership — the design must specify how `resource.owner == caller` is enforced, not just that auth is present. [CWE-862, OWASP A01:2021]
- **Session fixation:** Session ID must be regenerated after login, after role/privilege change, and after OAuth callback. [CWE-384]
- **Session security:** Session cookies require `httpOnly`, `secure`, `sameSite=strict`. Design must specify absolute timeout and inactivity timeout. Logout must destroy server-side session state. [CWE-614]
- **CSRF defense:** Designs with state-changing endpoints need layered defense: SameSite cookies + origin/referer validation + CSRF token (for legacy browser support). [CWE-352]
- **AI/LLM tool-calling:** Agent tool invocations require minimal permissions per tool, tool-name validation against an explicit allowlist, and per-call user authorization verification. MCP servers must be explicitly allowlisted. [CWE-269]
- **File uploads:** Files stored outside web root, filenames randomized server-side, executable extensions blocked. Design must specify storage location and access control. [CWE-434]
- **Database access:** Application DB user must have least-privilege grants (never root/superuser). Database ports never publicly accessible. [CWE-250]
