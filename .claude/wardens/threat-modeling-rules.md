# Threat Modeling Rules -- Wardens/threat-modeler

> Applied architecture-level before implementing security-sensitive features.
> STRIDE categories: S=Spoofing, T=Tampering, R=Repudiation, I=Information Disclosure, D=Denial of Service, E=Elevation of Privilege.
> Depth references: `threat-modeling-checklists.md` (read on every invocation for per-category checks).

## auth-surface-documented
**Severity:** blocking
**Applies when:** Design adds or modifies authentication or session management.
**Check:** Are all authentication entry points enumerated? Is the session lifecycle (create/validate/revoke/expire) fully specified?
**Rule:** Every auth surface must be explicitly described before implementation. Implicit or inherited auth ("it uses the existing session") is insufficient -- describe what "existing session" does and when it expires.
**Cite:** OWASP Authentication Cheat Sheet; STRIDE-Spoofing

## trust-boundaries-explicit
**Severity:** blocking
**Applies when:** Design involves two or more services, processes, or actors exchanging data.
**Check:** Does the design name every trust boundary? Are callers on the untrusted side always validated before data is processed?
**Rule:** Trust boundaries must be explicit, not implied. Data crossing a boundary from an untrusted caller must be validated/authenticated at that boundary, not assumed clean.
**Cite:** STRIDE-Tampering; OWASP API Security Top 10 -- API1 (Broken Object Level Authorization)

## credential-lifecycle
**Severity:** blocking
**Applies when:** Design handles credentials, API keys, OAuth tokens, JWTs, or webhook secrets.
**Check:** Does the design specify: storage mechanism (not plaintext), transmission (encrypted in transit), rotation policy, and revocation path?
**Rule:** Credential lifecycle (issue, use, rotate, revoke) must be fully specified. Designs that only specify issuance are incomplete.
**Cite:** OWASP Cryptographic Storage Cheat Sheet; STRIDE-Information Disclosure

## least-privilege
**Severity:** blocking
**Applies when:** Design grants permissions, roles, or scopes to an actor.
**Check:** Does each actor receive only the permissions required for its specific function? Are admin/elevated scopes justified?
**Rule:** Least-privilege: no actor should hold broader permissions than its narrowest use case requires. Designs that use broad scopes "for convenience" are non-compliant.
**Cite:** STRIDE-Elevation of Privilege; OWASP Access Control Cheat Sheet

## external-api-inputs
**Severity:** blocking
**Applies when:** Design receives data from an external API or webhook.
**Check:** Is there a validation step between receipt and use? Are all external inputs treated as untrusted?
**Rule:** External API responses and webhook payloads are untrusted by default. The design must include an explicit validation/schema-check step before data enters any internal system.
**Cite:** STRIDE-Tampering; OWASP API Security -- API8 (Security Misconfiguration)

## sensitive-data-flow
**Severity:** warning
**Applies when:** Design stores, transmits, or logs PII, health data, financial data, or credentials.
**Check:** Is sensitive data encrypted at rest and in transit? Does any logging path capture sensitive fields?
**Rule:** Sensitive data must be encrypted at rest and in transit. Log statements must never capture raw sensitive fields -- mask or omit.
**Cite:** STRIDE-Information Disclosure; GDPR data minimization principle

## dos-surface
**Severity:** warning
**Applies when:** Design exposes a public or semi-public endpoint, or a resource-intensive operation.
**Check:** Is there a rate-limit, quota, or backpressure mechanism? Can a single caller exhaust the resource?
**Rule:** Resource-intensive or public-facing operations need explicit throttling. "We'll add it later" is not acceptable for features that touch external inputs.
**Cite:** STRIDE-Denial of Service; OWASP API Security -- API4 (Unrestricted Resource Consumption)

## repudiation-logging
**Severity:** warning
**Applies when:** Design involves financial operations, privilege changes, or irreversible actions.
**Check:** Does the design include an audit log of who performed the action, with what input, at what time?
**Rule:** Irreversible or high-stakes actions require an audit trail. Design must specify what is logged and where it is stored.
**Cite:** STRIDE-Repudiation; PCI-DSS Req 10

## scope-not-code-review
**Severity:** informational
**Applies when:** Always.
**Check:** Are any findings code-level (injection in a specific function, missing sanitization in a specific call)?
**Rule:** Route code-level findings to code-reviewer. This review covers system design, not implementation.
**Cite:** Warden scope boundary (threat-modeler vs code-reviewer)
