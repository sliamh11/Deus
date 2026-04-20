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

### PR #6 addendum: TrueCourse `missing-error-event-handler` — false-positive inventory

The original plan framed PR #6 as *"stream-error installer + 19 channel wire-ups"* on the assumption that the 19 uniform findings pointed to a missing base-class helper. Source review found that assumption wrong: **0 of the 19 flagged sites are EventEmitter/Stream sites**. The rule's regex over `.connect()` and `createWriteStream()` matches more than the rule's intent.

Breakdown of the 19 findings:

| Category                                              | Count | Disposition                                                                                                                    |
| ----------------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------ |
| `provider.connect().catch(console.error)` in `packages/mcp-*/src/index.ts` auto-connect | 6     | **Fixed**: `console.error` → `logger.error({err, source: '<pkg>.auto-connect'})` with structured owner label               |
| `provider.connect()` in tool handler without try/catch | 2     | **Fixed**: wrap in try/catch, return `{ isError: true, content: [...] }` with owner attribution (whatsapp:83, channel-core:158) |
| `await channel.connect()` in `src/index.ts:229`       | 1     | **Fixed**: per-channel try/catch with `{err, channel}` log before rethrow; outer `main().catch()` still owns the exit path  |
| `server.connect(new StdioServerTransport())` in 7× `index.ts` | 7     | **False positive**: the SDK's `StdioServerTransport.start()` registers `this._stdin.on('error', this._onerror)` itself     |
| `client.connect(new StdioClientTransport())` in `src/channels/mcp-adapter.ts` + `container/agent-runner/src/ipc-mcp-stdio.ts` | 2     | **False positive**: the SDK's `StdioClientTransport` wires `process.stdin.on('error')`, `process.stdout.on('error')`, and `process.on('error')` internally |
| `pipeline(res.body, createWriteStream(...))` in `src/transcription.ts:71` | 1     | **False positive**: `stream/promises.pipeline()` propagates stream errors as a rejected Promise — `.on('error')` would be redundant |

**Total: 9 real fixes + 10 documented false positives = 19.** (The table above is 6+2+1+7+2+1 = 19 — the "2 false positives in 2 files" row covers mcp-adapter + ipc-mcp-stdio.)

Corollary rule added to `For future contributors` (below): static-analyzer "missing error handler" findings on `.connect()` must be cross-checked against the actual return type. If the call returns `Promise<T>` rather than an `EventEmitter`, the correct fix is structured `.catch()` or try/catch, not `.on('error')`.

No shared `installStreamErrorLogger` helper shipped — YAGNI. If a real EventEmitter site surfaces later, the helper can be added at that time.

### PR #7 addendum: when `process.exit` is OK

PR #7 adds an ESLint rule (`no-restricted-syntax`) banning `process.exit` in `packages/*/src/**/*.ts` and `container/*/src/**/*.ts`. The rule fires as an error. Legitimate exits are documented via `eslint-disable-next-line` with a rationale comment.

**Allowed categories:**

1. **The bootstrap harness itself** — `container/agent-runner/src/bootstrap.ts` (and the mirror in `src/bootstrap.ts`, which is not in the rule's file glob). The harness IS the one place that terminates the process on behalf of everyone else; exits here are exactly what the PR is promoting, not an escape hatch.

2. **MCP server suicide signal** — e.g. `packages/mcp-telegram/src/telegram.ts:399`. When a long-running channel loop hits unrecoverable state (e.g. `MAX_RECONNECT_RETRIES` polling failures in a row), the MCP server exits so the host orchestrator detects the dead process and can restart it. Throwing in these specific sites would only reach a `fireAndForget` log boundary and leave the MCP server alive but advertising tools that silently no-op.

3. **Short-lived CLI scripts** — `setup/*.ts`, `src/deus-listen.ts`. These are one-shot entry points, not long-lived daemons. `process.exit(N)` on a user-facing error is idiomatic and the existing TrueCourse hits here are false positives for this rule's intent. Not covered by the rule's file glob.

**Banned categories:**

1. **Anywhere inside `main()` that runs under bootstrap.** After PR #3 (#219), `agent-runner` and the main deus process both wrap their `main()` in `bootstrap()`. Any throw inside `main()` propagates to `bootstrap.ts:43 .catch → process.exit(exitCode)` with structured `[<name>]` attribution. Direct `process.exit` bypasses the harness and loses that attribution — the exact anti-pattern the error-discipline initiative exists to prevent. PR #7 converts the two remaining such sites (`container/agent-runner/src/index.ts:822, 1025`) from `process.exit(1)` to `throw err`.

2. **Library code generally.** A package or module should throw; the caller decides whether that's fatal. If you find yourself wanting `process.exit` in a file other than the three categories above, the correct question is "where is the upstream catcher, and why doesn't it exit on this error?" — not "how do I exit here?"

### PR #8 addendum: datetime-TZ policy (Python `scripts/`)

TrueCourse flagged 25 `datetime.now()` calls in `scripts/` (rule `bugs/deterministic/datetime-without-timezone`). Naive datetime is dangerous for two reasons in this codebase:

1. **DST in Asia/Jerusalem.** Twice a year a naive `datetime.now()` is ambiguous (fall-back hour) or non-existent (spring-forward hour). Comparisons across the boundary silently produce wrong answers.
2. **Mixed UTC + naive comparisons.** `datetime.fromtimestamp(file.st_mtime)` returns a naive datetime in *local* time, while every other datetime in the codebase is implicitly UTC (db timestamps, log timestamps, Unix epochs). Comparing the two raises `TypeError: can't compare offset-naive and offset-aware datetimes` if either side is later made tz-aware — and silently produces an off-by-(local-utc) timedelta if both stay naive.

The literal "blanket UTC" recommendation produces a worse bug for a user-facing tool: at 01:30 IST, `datetime.now(timezone.utc).strftime("%Y-%m-%d")` produces yesterday's date string. Liam reads daily-atom filenames, weekday checks, and the maintenance display in `ls`/stdout — they must match his calendar day.

**Policy: split by intent.**

| Intent | Helper | Examples |
|---|---|---|
| Internal timestamps, age comparisons against UTC st_mtime, ISO frontmatter | `utc_now()` | DB rows, retention cutoffs, last-review epochs |
| User-facing date/filename strings, weekday checks, day-grouped indexing | `local_now()` | `bak-YYYYMMDD-HHMMSS`, daily-atom buckets, `weekday() == 6` |

Both helpers live in `scripts/_time.py` and return tz-aware datetimes. `datetime.fromtimestamp(st_mtime, tz=timezone.utc)` must be used when reading file mtimes for comparison with `utc_now()`.

**Rule for new Python code in `scripts/`:** never write bare `datetime.now()`. Pick `utc_now()` or `local_now()` explicitly. The choice forces an answer to "is this a moment, or is this a calendar day?".

### PR #9 addendum: SQL injection surface (Python `evolution/`)

TrueCourse flagged 13 `security/deterministic/sql-injection` HIGHs in `evolution/db.py` + `evolution/storage/providers/sqlite.py`. Audit found two distinct categories: **structural-safe** sites that can't be parameterized, and **real risk** sites where blind kwargs / unvalidated ints would let a caller forge SQL.

**SQLite parameterization rules (the "why" behind the policy):**

1. **DDL cannot be parameterized.** `?` placeholders only work in DML expression positions. `CREATE VIRTUAL TABLE ... USING vec0(embedding float[?])` is a syntax error. Same for `ALTER TABLE foo ADD COLUMN ? ?`. When the schema demands an identifier or a type, you must interpolate.
2. **Identifiers (table/column names) cannot be parameterized.** `SELECT COUNT(*) FROM ?` is invalid. When you need a dynamic table or column name, interpolate from a closed allow-list (a literal tuple in code, or a regex-validated string from `sqlite_master`). Never accept identifier names from untrusted callers.
3. **Data values must be parameterized.** Anything that's user input or function argument data — strings, ints, floats — goes through `?` bound parameters. The exception is integer values inside SQLite expressions like `DATETIME('now', '-N days')` that don't accept placeholders, in which case explicit `int()` coercion + a sane clamp is mandatory before interpolation.

**Project conventions adopted in PR #9:**

- `# safe: <reason>` comment on f-string `.execute()` sites that are structurally parameterizable-impossible. Examples: vec0 dimension constants, ALTER TABLE column tuples from a literal list, WHERE clauses assembled from local literal-string fragments. The comment is for human reviewers — TrueCourse does not consume inline suppressions; closing the violation happens in the dashboard after merge.
- **Allow-list module constants** for kwarg-driven UPDATEs. `evolution/storage/providers/sqlite.py:_UPDATABLE_INTERACTION_COLS` enumerates the 5 columns callers actually write after initial insert. `update_interaction(**fields)` raises `ValueError` on unknown keys. Widening the set is intentional and visible in code review.
- **Identifier regex** (`_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")`) before interpolating a name from `sqlite_master` into DDL. Rejects payloads that survived a corrupted-DB or hostile-environment edge case.
- **Int-coerce + clamp** for time-window parameters that interpolate into `DATETIME('now', '-N days')`: `max(1, int(days))`. The clamp also prevents `days=0` from silently widening the window to all rows.

PR #11 audited the remaining 10 non-evolution `sql-injection` HIGHs in `scripts/bench/store.py`, `scripts/memory_indexer.py`, and `scripts/memory_tree.py`; all confirmed structural-safe (vec0 dims from module int constants, ALTER TABLE col/coltype from literal tuple-lists, WHERE clauses joined from local literal-string fragments, DELETE FROM iterating a hard-coded schema list). Annotated with `# safe: <reason>` per the PR #9 convention. SQL injection backlog from the TrueCourse 2026-04-19 scan is closed.

### Issue #218: bootstrap mirror discipline

PR #2 (#215) shipped `src/bootstrap.ts`. PR #3 (#219) wired it into the agent-runner entry point — but `container/agent-runner/` has its own `tsconfig.json` and `node_modules` and cannot import from `src/`. The harness was duplicated as `container/agent-runner/src/bootstrap.ts`, with the only divergence being the logger: pino + `FatalError` discrimination in the main process, plain `console.error` in the container (which has no pino dep). Issue #218 was opened to track extracting both copies into a shared `packages/bootstrap` workspace.

After investigation, **extraction was rejected** in favor of mechanical drift enforcement:

1. **Extraction needs a third logging abstraction.** The container copy was deliberately stripped of pino + `FatalError` to keep the runtime image small and avoid a `src/`-side dep. Sharing one harness requires either (a) the container takes on pino, (b) `src/` downgrades to `console.error` (loses the `FatalError` → `fatal` severity discrimination), or (c) introduce a `LogAdapter` interface and inject the logger. Option (c) is a bigger design call than the duplication itself.
2. **No workspace tooling.** The repo has no `workspaces` field in root `package.json`. Channel packages use `file:../mcp-channel-core` pseudo-deps that require manual pre-builds. There is no `src/` → `packages/` consumer relationship anywhere today; making `src/bootstrap.ts` consume `packages/bootstrap` would be the first such direction in the repo.

The actual risk — silent drift between the two copies — is closed mechanically by `python3 scripts/drift_check.py --bootstrap-mirror` (also runs as part of `--all` in CI). The check normalizes both files (strips JSDoc, single-line comments, blank lines, and `// MIRROR-IGNORE-START / MIRROR-IGNORE-END` blocks) and asserts byte-equal structural shape. Deliberately divergent regions — imports, logger call sites, and the `console.error` helper — are wrapped in `MIRROR-IGNORE` markers; everything else must mirror exactly. Adding a parameter, removing a function, or changing the control flow on one side now fails CI until the other side mirrors the change.

If a future PR resolves the logger divergence (e.g., a `LogAdapter` shipped in another initiative, or the container gains pino), the extraction becomes straightforward and the mirror check can be retired. Until then, two files + one mechanical check is the proportionate trade-off.

## For future contributors

When you catch or throw an error in Deus:

1. **Throwing?** Pick the subclass that answers *what should the caller do?* Never use `throw new Error(...)` in new code.
2. **Catching?** Narrow with `instanceof` (or `isDeusError`) and handle each class. `catch (e) { log; throw; }` is fine — swallowing without classification is not.
3. **Wrapping a third-party error?** Pass it as `cause`. Never stringify it into the message.
4. **Awaiting a promise you don't plan to block on?** Use `fireAndForget` (PR #4). Don't let it float.
5. **Static analyzer flagging a `.connect()` site as "missing error handler"?** Check the return type first. `Promise<T>` → structured `.catch()` or try/catch with an owner label (PR #6 pattern). `EventEmitter` → `.on('error', ...)`. SDK-internal transports (e.g. `StdioServerTransport`) wire their own stdin/stdout error listeners — leave them alone.
6. **Writing Python in `scripts/`?** Never `datetime.now()`. Use `utc_now()` for internal timestamps, `local_now()` for user-facing strings (PR #8). The naming forces you to answer "moment or calendar day?".
7. **Building SQL?** Always parameterize data values. Identifiers (table/column names) and DDL must come from a closed allow-list (a literal tuple in code) or pass through `_SAFE_IDENT` regex validation (PR #9). Annotate the unavoidable f-string `.execute()` sites with `# safe: <reason>`.

See `src/errors/index.ts` for the full API and `src/errors/index.test.ts` for examples.

## References

- ES2022 `Error.cause` — <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Error/cause>
- Node.js `unhandledRejection` — <https://nodejs.org/api/process.html#event-unhandledrejection>
- Joyent "Error Handling in Node.js" (still the canonical taxonomy reference)
- PR #101 — platform-abstraction-layer ADR (same "centralize the missing primitive" pattern)
- TrueCourse session log: `Session-Logs/2026-04-19/truecourse-scan-error-discipline-plan.md`
