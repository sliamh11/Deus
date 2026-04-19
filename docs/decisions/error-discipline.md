# ADR: Error & Async Discipline

**Date:** 2026-04-19
**Status:** Accepted (PR #1 of 10)
**Scope:** All source code in `src/`, `setup/`, `container/*/src/`, `packages/*`

## Context

A tree-sitter + LLM static-analysis pass over Deus (TrueCourse, 2026-04-19) surfaced 4256 violations, with 86 flagged critical. After triage the real findings collapse to one systemic root cause:

**Deus has no error taxonomy, no global error sinks, and no async-boundary discipline.**

Evidence:

- 118 `catch {}` (empty) + 59 `catch (e)` (untyped, stringified) = **177 silent swallow sites**. The author of each catch had no shared vocabulary for "retry this" vs "show to user" vs "shut down", so the safe default became "swallow".
- **Zero process-level error handlers across all 6 Node entry points.** A single unhandled rejection anywhere in telegram polling, WhatsApp reconnect, or a provider fallback chain crashes the daemon with no attribution.
- `main()` is called without `.catch()` in `container/agent-runner/src/index.ts:945` and `setup/index.ts:76` — classic top-level-await hazard.
- 19 uniform stream-connect findings across every channel package (discord, gcal, gmail, slack, telegram, whatsapp, x). Uniformity implies a missing base-class helper, not 19 coincidences.
- 25 Python `datetime.now()` without tzinfo in a codebase where the user is in Asia/Jerusalem — DST landmines for checkpoints, reminders, gcal.
- 16 HIGH floating-promise findings concentrated in long-running channel loops where the error would otherwise vanish into the event loop.

Patching each site individually doesn't prevent recurrence. The next developer (human or AI) will make the same decision the same way because the idiomatic primitives aren't there.

## Decision

Deus introduces a **four-class error taxonomy** and the supporting async/lifecycle primitives. Every throw site picks the class that answers the question *"what should the caller do?"* — not "what went wrong?".

### 1. Error taxonomy (this PR)

Four disjoint classes in `src/errors/index.ts`:

| Class            | Caller action                           | Example                                                  |
| ---------------- | --------------------------------------- | -------------------------------------------------------- |
| `RetryableError` | Retry with backoff                      | HTTP 5xx, `ECONNRESET`, SQLite `BUSY`, provider 429      |
| `UserError`      | Surface message to user, don't error-log | Bad command syntax, auth denied, missing required input  |
| `FatalError`     | Log + shut down this boundary           | Corrupt DB, missing required secret, invalid startup config |
| `DeusError`      | Base class — use only when none fits    | Unclassified wrap for third-party error                  |

Rules:

- **Every class preserves `cause`** via ES2022 `Error.cause`. The original stack is never lost.
- **Every class carries structured `context`** (arbitrary key-value). Logs are structured from the throw site, not reconstructed at the sink.
- **Secrets must not go into `context`.** The sink serializes it verbatim.
- **Subclasses are disjoint.** A `RetryableError` is not a `FatalError`. If a failure could be both, the caller's policy wins — author picks one and documents why.
- **Prototype chain is preserved** via `Object.setPrototypeOf` so `instanceof` works after transpilation.

### 2. Bootstrap harness (PR #2)

A single `bootstrap(mainFn, { name })` helper in `src/bootstrap.ts` that every entry point calls. It installs `process.on('uncaughtException' | 'unhandledRejection')`, wraps `mainFn()` with structured exit logging, and ensures a daemon crash never happens without attribution.

### 3. Async-boundary helpers (PR #4)

Three primitives in `src/async/` cover the floating-promise findings:

- `fireAndForget(promise, { name, onError })` — explicit "I intend to ignore this but log failures."
- `withTimeout(promise, ms, { name })` — enforces deadlines at I/O boundaries.
- `allSettledOrThrow(promises, { throwIf })` — parallel work where partial failure is a policy, not an accident.

Paired with ESLint `@typescript-eslint/no-floating-promises` set to **warn with a baseline**, so new code is held to the rule without a one-shot mega-migration.

### 4. Stream-error installer (PR #6)

`installStreamErrorLogger(emitter, source)` in `mcp-channel-core` lets every channel package wire error listeners with one line instead of 19 divergent implementations.

### 5. Timezone policy (PR #8)

Python: **all `datetime.now()` calls must pass `tz=timezone.utc`** (or an explicit `ZoneInfo`). ADR addendum + migration in one PR.

### 6. Process-exit ban (PR #7)

`process.exit` is banned in `packages/*` and `container/*/src/` via ESLint. Libraries raise; only the top-level bootstrap chooses exit codes.

### 7. TrueCourse diff-gate (PR #10)

TrueCourse is **not** a blocking always-on gate — the noise ratio on the full scan was too high. It runs per-PR in `--diff` mode: new criticals fail CI, new highs warn, the baseline is ignored. Quarterly full scans stay manual.

## Consequences

- **Callers have a vocabulary.** "I caught a `RetryableError`" means something; "I caught an `Error`" means nothing.
- **Every new error site is a typed decision.** The easy default (`catch {}`) is still easy but now the linter + reviewer see it.
- **Logs become structured.** `err.toJSON()` produces `{ name, message, context, cause }` consistently; pino/downstream sinks get predictable shape.
- **Daemon crashes get attribution.** PR #2 guarantees every uncaught rejection lands in a logger with the entry-point name.
- **Per-PR revertability.** Each of the 10 PRs can be reverted without undoing the others. PRs #1–4 (additions) deliver foundation value even if migration PRs #5–10 get deferred.
- **Bundle cost is negligible.** `src/errors/` is ~80 lines of plain TypeScript classes, zero dependencies.

## Alternatives Considered

1. **`verror` / `ts-error`** — external packages duplicate what ES2022 `Error.cause` + a small base class give us. Not worth the dep.
2. **Effect-TS `Effect.Error`** — excellent but requires adopting the Effect runtime for non-error code too. Out of scope for a daemon written in plain async/await.
3. **One `DeusError` with a `kind: 'retry' | 'fatal' | 'user'` field** — less type-safe than subclasses; `instanceof` narrowing is the idiomatic TS pattern.
4. **Error codes instead of classes** (Go-style) — loses stack + cause chain; awkward in async.
5. **Patch the 177 silent-swallow sites directly without a taxonomy** — the original proposal. Rejected: it prevents the current wave of bugs but the next developer reintroduces them because the idiom isn't there.

## Migration Plan

This ADR is PR #1 of a 10-PR series, **ordered by blast-radius ascending**:

| PR   | Scope                                          | Type                |
| ---- | ---------------------------------------------- | ------------------- |
| #1   | This ADR + `src/errors/` primitives            | Pure addition       |
| #2   | `src/bootstrap.ts` harness                     | Pure addition       |
| #3   | Wire bootstrap into 6 entry points             | Per-file commits    |
| #4   | `src/async/` helpers + ESLint warn             | Pure addition + lint |
| #5   | Migrate 16 floating-promise HIGHs              | Per-file commits    |
| #6   | Stream-error installer + 19 channel wire-ups   | Per-channel commits |
| #7   | ESLint ban `process.exit` in packages/container | Lint rule + 1 fix   |
| #8   | TZ policy addendum + 25 Python migrations      | ADR + migration     |
| #9   | Evolution SQL f-string cleanup                 | Per-file commits    |
| #10  | TrueCourse `--diff` CI gate                    | CI workflow         |

Each PR ships independently. If any downstream migration causes regressions, PRs #1–4 stand on their own.

## For future contributors

When you catch or throw an error in Deus:

1. **Throwing?** Pick the subclass that answers *what should the caller do?* Never use `throw new Error(...)` in new code.
2. **Catching?** Narrow with `instanceof` (or `isDeusError`) and handle each class. `catch (e) { log; throw; }` is fine — swallowing without classification is not.
3. **Wrapping a third-party error?** Pass it as `cause`. Never stringify it into the message.
4. **Awaiting a promise you don't plan to block on?** Use `fireAndForget` (PR #4). Don't let it float.

See `src/errors/index.ts` for the full API and `src/errors/index.test.ts` for examples.

## References

- ES2022 `Error.cause` — <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Error/cause>
- Node.js `unhandledRejection` — <https://nodejs.org/api/process.html#event-unhandledrejection>
- Joyent "Error Handling in Node.js" (still the canonical taxonomy reference)
- PR #101 — platform-abstraction-layer ADR (same "centralize the missing primitive" pattern)
- TrueCourse session log: `Session-Logs/2026-04-19/truecourse-scan-error-discipline-plan.md`
