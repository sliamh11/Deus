# LIA-428 — Build `deus` CLI chat on `deus-native`

## Goal and constraints

Add a new `deus chat` subcommand that opens an interactive terminal conversation backed specifically by the registered `deus-native` `AgentRuntime`. Preserve the existing meanings of bare `deus`, `deus claude`, `deus codex`, and `deus openai`; they continue to launch the existing development CLIs. The new chat must survive at least two turns, persist and resume its backend-scoped LangGraph session across CLI processes, render normalized human-facing output rather than `RuntimeEvent`/transport frames, close cleanly, and expose the backend and session id through `/status`.

This is a source-code feature rather than a skill because it must enter the host-owned `AgentRuntime`, credential-proxy, database-session, and CLI-launcher paths. The implementation must follow the backend-neutral UX rules in `AI_AGENT_GUIDELINES.md`: present as Deus, keep provider details out of ordinary chat output, preserve backend-scoped session semantics, and never move credentials into the client process.

## Decisions

### 1. CLI entry point: additive `deus chat`

Add a `chat)` dispatch arm to `deus-cmd.sh` near the existing compiled-TypeScript subcommands (`pipeline`, `solution`, and `tui`, currently around lines 1507–1530). It will execute `node "$SCRIPT_DIR/dist/cli/deus-native-chat-client.js"` without changing directory, so the client retains the user's original `process.cwd()` for `RunContext.cwd`. Update the usage block around lines 1730–1766. Mirror the same `chat` dispatch and help text in `deus-cmd.ps1` (its command switch starts around line 490) because repo-owned CLI commands are a cross-backend/cross-platform contract.

The command is named `deus chat`, not bare `deus` and not a new package `bin`, because:

- bare `deus` and the `claude`/`codex` prefixes already have stable, unrelated meanings in `deus-cmd.sh` (prefix handling at lines 29–44 and the home launch arm at lines 788+);
- `package.json` has no installed `bin` and the live command is the symlinked shell/PowerShell launcher, so adding an npm-only entry point would not satisfy “from the `deus` CLI”;
- an options-object seam on the TypeScript client lets G2 add model-selection flags and G3 add a plan-mode toggle later without changing this dispatch shape.

`deus chat` resumes the one recorded `deus-native` CLI thread by default. No new-session/session-picker command is part of LIA-428.

### 2. Architecture: thin client; runtime remains in the daemon

Do **not** instantiate `DeusNativeRuntime`, `startCredentialProxy`, or `startToolProxy` in the short-lived CLI process. `src/index.ts:101–166` proves that the daemon initializes the database, starts the sole credential proxy, creates the shared in-memory group-token map indirectly used by the runtime, and registers `deus-native` with its live dependencies. A second process would mint a token in a different `src/group-tokens.ts` map, which the daemon proxy would reject, while starting a second proxy on fixed port 3001 would collide with the daemon. Booting the full host just to chat would also duplicate channels, schedulers, webhooks, and IPC watchers.

Instead, add a small daemon-owned, loopback-only chat server after the runtime registry is initialized. On every daemon start it will:

1. generate a cryptographically random bearer token;
2. listen on `127.0.0.1` with port `0` (an OS-assigned ephemeral port, so no new fixed-port/config/startup-collision surface is created);
3. atomically write `{ version, pid, host, port, token }` to a discovery file under `CONFIG_DIR`, with user-only permissions (`0600`) and no token logging;
4. accept only the exact versioned chat routes and require a constant-time bearer-token match, following the security pattern already used by `src/odysseus-server.ts:124–139, 300–335`;
5. remove only its own discovery record and close the server during the existing `src/index.ts:168–176` graceful shutdown path.

The client reads that discovery record and sends authenticated loopback requests. A missing/stale record, refused connection, protocol-version mismatch, or authentication failure becomes a concise “Deus service/chat endpoint is unavailable; rebuild/restart the service” message and a non-zero exit. There is deliberately no unsafe fallback that starts another proxy or reads provider credentials.

The transport uses one request per turn and a newline-delimited JSON response internally, so normalized events can be delivered incrementally when a runtime provides them. Protocol JSON is an implementation detail and is never written directly to stdout/stderr. `deus-native` currently reports `tool_streaming: false` and emits its answer after buffered `agent.invoke()` processing (`deus-native-backend.ts:360–492`), so LIA-428 must not claim token-level streaming that does not exist today; the transport and renderer will still consume incremental events correctly when the runtime begins emitting them.

### 3. Stable chat seam for G2/G3

**Design:** use an Adapter (`NativeChatSessionStore`) to isolate the existing `db.getSession`/`db.setSession` persistence API, a Facade/Controller to keep readline/HTTP concerns separate from `AgentRuntime` turn orchestration, and an anti-corruption/normalization boundary that converts `RuntimeEvent` into terminal-safe display events. These are structural seams, not algorithmic machinery: the controller reads and caches one backend-scoped session row, so there is no collection-processing or algorithmic-complexity tradeoff.

`src/cli/deus-native-chat.ts` will expose a testable controller rather than embedding readline, HTTP, database access, and runtime calls in one function:

```ts
export interface DeusNativeChatOptions {
  cwd: string;
  resume: true;
}

export interface NativeChatSessionStore {
  get(groupFolder: string, backend: AgentRuntimeId): RuntimeSession | undefined;
  set(groupFolder: string, session: RuntimeSession): void;
}

export function createDeusNativeChatController(deps: {
  runtime: AgentRuntime;
  sessions: NativeChatSessionStore;
}): DeusNativeChatController;
```

The controller takes a single `DeusNativeChatOptions` object and builds `RunContext` in one place. G2 can extend that object with its model selection and translate it into the runtime-supported model configuration; G3 can add a typed mode field and translate it into the runtime contract. LIA-428 will not add placeholder `model`, `planMode`, or arbitrary client-controlled `backendConfig` fields. This prevents premature UX and avoids exposing security-sensitive runtime configuration merely for future-proofing.

Use fixed internal identity constants such as `CLI_CHAT_GROUP_FOLDER = "deus-native-cli"` and `CLI_CHAT_JID = "cli:deus-native"`. The group folder is synthetic and unregistered; `DeusNativeRuntime.runTurn` only uses `resolveGroup` for the `publicIngress` refusal (`deus-native-backend.ts:253–262`), so `undefined` follows the safe non-public-ingress path. The synthetic identity also prevents the CLI session from overwriting a channel group's backend-scoped session row. `isControlGroup` remains `false`; this ticket does not grant the terminal client control-group privileges.

Add the same top-of-file non-goals style used by `deus-native-backend.ts:1–70` to the chat controller and server modules. The comment must name the deferred G2 model-selection UX and G3 plan-mode toggle explicitly and warn contributors not to add them in this ticket.

## Files and responsibilities

### New source files

- `src/cli/deus-native-chat.ts`
  - Own `DeusNativeChatOptions`, fixed CLI identity constants, the injectable session-store interface, `createDeusNativeChatController`, and the normalized display-event type.
  - Implement `start()`, `runTurn(prompt, options)`, `status()`, and `close()` around the real `AgentRuntime` contract.
  - Convert every `RuntimeEvent` variant into a bounded, UI-safe display event; never expose the runtime union itself to the terminal module.
  - Keep per-turn/session state in the controller only as a cache of the authoritative SQLite row; do not create a second persistence mechanism.

- `src/cli/deus-native-chat-server.ts`
  - Start the authenticated ephemeral loopback server inside the daemon, manage the discovery file, validate method/path/body/version/auth, cap request size, and serialize a single in-flight turn for the fixed CLI session (reject a competing turn rather than interleave prompts in one LangGraph thread).
  - Resolve `registry.get("deus-native")` explicitly rather than `registry.resolve(...)`; `deus chat` is this ticket's native surface and must not silently follow the channel/global default backend.
  - Adapt `db.getSession`/`db.setSession` to `NativeChatSessionStore`, stream the controller's normalized display events, expose status, and call controller close on the close route.
  - Export a server factory that listens on an injected/ephemeral port for hermetic tests; keep process exit and logging out of the core handler.

- `src/cli/deus-native-chat-client.ts`
  - Be the compiled executable invoked by the shell/PowerShell launchers.
  - Parse only LIA-428's current options, construct a `DeusNativeChatOptions` object with `cwd`, open a readline loop, and send non-empty prompts sequentially.
  - Recognize local commands `/status`, `/exit`, and `/quit`. `/status` fetches diagnostic state; `/exit`, `/quit`, EOF/Ctrl-D, and SIGINT stop accepting input, make a best-effort authenticated close request, restore terminal state, and exit without clearing the stored session.
  - Render only normalized display events and terminal prompts; never log request/response objects, bearer tokens, `RuntimeEvent.type`, `sessionRef`, or NDJSON framing.

### Modified runtime/CLI files

- `src/index.ts`
  - Start the native chat server after `registry.register(createDeusNativeRuntime(...))` at current lines 147–166, when the database, credential proxy, shared group-token state, queue dependencies, and registry are all live.
  - Retain the returned server handle, close it/remove its discovery record in the existing signal shutdown function, and include startup failures in the daemon's normal fatal startup handling. Do not start any duplicate proxy or a second runtime registry.

- `deus-cmd.sh`
  - Add the `deus chat` dispatch to the compiled client and update help. Preserve the original cwd and all existing prefix/home behavior.

- `deus-cmd.ps1`
  - Add the equivalent `chat` dispatch/help entry so the source feature remains buildable and reachable on the Windows launcher as required by `docs/CROSS_PLATFORM.md`.

### Documentation required by the repo update rule

- `AGENTS.md`
  - Add `deus chat` to the stable Commands and Skills list and add the native CLI controller/server entry point to the architecture table so later agents do not rediscover the transport.

- `docs/AGENT_DEUS_101.md`
  - Add the client → authenticated daemon endpoint → `deus-native` controller → runtime/session-store flow to the detailed entrypoint map.

- `docs/decisions/deus-native-cli-chat.md` and `docs/decisions/INDEX.md`
  - Record the thin-client decision, why a second in-process CLI runtime/proxy is invalid, the ephemeral authenticated loopback/discovery-file boundary, the fixed synthetic session identity, and rollback (remove launcher arms, server startup, and discovery file handling). This is an architectural entrypoint and credential-boundary decision, so it should not live only in code comments.

### Tests

- `src/cli/deus-native-chat.test.ts`
  - Controller and renderer unit tests with an injected fake runtime/session store.

- `src/cli/deus-native-chat-server.test.ts`
  - Real ephemeral HTTP server tests with temporary discovery paths and fake controller/runtime dependencies.

- `src/cli/deus-native-chat-client.test.ts`
  - Scripted input/output tests against a fake transport for two prompts, status, EOF/SIGINT-safe cleanup behavior, and framing-free output.

- `src/cli/deus-native-chat.integration.test.ts`
  - Reuse the hermetic fake-model/real-checkpointer approach from `src/agent-runtimes/deus-native-checkpointer-integration.test.ts` and the dependency mocking conventions from `src/agent-runtimes/deus-native-backend.test.ts`; do not make live provider/network calls.

- `scripts/tests/test_deus_cmd_native_chat.py`
  - Structural launcher test proving `deus chat` reaches the compiled client and that existing bare/prefixed branches remain present. Follow the existing `scripts/tests/test_deus_cmd_backend_prefix.py`/`test_deus_cmd_print_identity.py` style rather than invoking the live daemon.

If the PowerShell launcher has no comparable test harness, cover its dispatch text in the TypeScript/structural test or add a small platform-neutral source assertion alongside the shell assertion; do not require a live Windows service in CI.

## Turn and session lifecycle

For each prompt, `DeusNativeChatController.runTurn` performs this exact sequence:

1. Build a `RunContext` with the prompt, client cwd, fixed synthetic `groupFolder` and `chatJid`, `isControlGroup: false`, and `stream: true`. No G2/G3 fields are supplied.
2. Read `sessions.get(CLI_CHAT_GROUP_FOLDER, "deus-native")`, which the production adapter implements with `db.getSession(groupFolder, backend)` (`src/db.ts:757–789`). The explicit backend argument is load-bearing: sessions are backend-scoped and the CLI must never select the most-recent Claude/OpenAI row.
3. When no row exists, call `runtime.startOrResume(runContext)`. Today this returns `defaultSession("", "deus-native")` (`deus-native-backend.ts:225–232`); passing that empty id into `runTurn` lets the runtime mint the real `crypto.randomUUID()` LangGraph `thread_id` (`deus-native-backend.ts:264–304`). Do not mint a competing CLI id.
4. Call `runtime.runTurn(runContext, sessionRef, eventSink)`. Keep the returned/current `RuntimeSession` in memory for the next prompt in the same CLI process.
5. If the sink receives a `session` event, immediately validate that its backend is `deus-native`, update current state, and persist it with `db.setSession(CLI_CHAT_GROUP_FOLDER, sessionRef)`. This matches the canonical event handling in `message-orchestrator.ts:191–207`.
6. After `runTurn` returns success, validate and persist `RunResult.sessionRef` as well, matching the success path in `message-orchestrator.ts:268–271`. This second path is required today because `DeusNativeRuntime` echoes its minted/resumed id in `RunResult.sessionRef` (`deus-native-backend.ts:485–492`) and does not currently emit a `session` event.
7. On the second prompt, reuse that non-empty `RuntimeSession`; the id doubles as the checkpointer `thread_id`, so LangGraph loads the first exchange. `db.setSession`'s same-id path updates `last_used_at` rather than creating a duplicate active row (`src/db.ts:791+`).
8. On `/exit`, `/quit`, EOF, or SIGINT, the close route reloads the current `deus-native` row if necessary and calls `runtime.close(sessionRef)`. It does **not** call `db.clearSession`; `clearSession` soft-orphans rows (`src/db.ts:895+`) and would break resume. A later `deus chat` process repeats step 2, passes the stored id to `runTurn`, and resumes the same checkpointed message history.

If `runTurn` returns `status: "error"`, render a user-facing error and do not replace a valid stored session with an empty/foreign ref. If a session event was already persisted before a later failure, retain it: it remains the runtime's authoritative continuity marker. Transport disconnect after the daemon accepted a prompt does not cancel or roll back the host turn; if the turn finishes, its session result is persisted and the next invocation can resume, even if the departed client missed the output.

## Runtime-event and terminal rendering

The controller exhaustively switches on the `RuntimeEvent` union in `src/agent-runtimes/types.ts:62–88` and emits a smaller display-event union. The client exhaustively renders that display union:

- `output_text`: write assistant text as chat content, preserving incremental chunks and adding terminal separation only at turn completion. Never print `{ type: "output_text", ... }` or JSON-stringify the event.
- `tool_call`: render a compact feedback line such as `Using web_search…` or `Using web_fetch…`. Optionally include a single bounded, escaped summary of useful arguments (query/URL), but never dump arbitrary argument JSON; redact keys matching token/auth/secret/password and cap length. The current runtime emits tool invocations after its buffered invoke and has no tool-result event in the `RuntimeEvent` contract, so LIA-428 renders the available invocation feedback and does not invent tool-result semantics.
- `activity`: render the supplied human-readable text as a subdued progress line, bounded to prevent terminal flooding. Do not expose it as a reasoning/protocol object.
- `usage`: accumulate the latest provider/model/token values for diagnostics, but keep provider/model accounting out of ordinary assistant output. Undefined token counts remain “unavailable,” never fabricated as zero.
- `session`: persist/update controller state; do not print it during ordinary turns. `/status` is the intentional diagnostic surface.
- `turn_complete`: finish the assistant block, clear temporary activity state, and return to the prompt. Do not print an event name or sentinel.
- `error`: emit one sanitized `Error: …` line to stderr and retain detailed structured data only in daemon logs. Do not dump stack traces, response bodies, proxy tokens, or transport frames.

The internal daemon response may contain NDJSON records, but client tests must assert that output contains neither raw JSON/event discriminants (for example `{"type":`, `output_text`, `turn_complete`, or `sessionRef`) nor SSE/NDJSON delimiters. This directly enforces acceptance criterion 3 rather than relying on visual inspection.

## Diagnostics/status

Use `/status` as the explicit in-chat diagnostic command because it is discoverable during a session, does not overload the existing service-level `deus status` command on PowerShell, and keeps identifiers out of normal responses. The daemon status route returns structured internal data; the client renders only:

```text
Backend: deus-native
Session: <full session id, or “not started” before the first successful turn>
State:   resumed | new
Output:  buffered | streaming
```

“Resumed” means a backend-matching row was loaded from SQLite at controller start; “new” means no row existed then. `Output` derives from `runtime.capabilities().tool_streaming`/the actual event mode and must report the current buffered limitation honestly. The full id is shown because the AC explicitly requires session identifiers through diagnostics; the startup banner may show only a short resume notice and must direct the user to `/status` for the full values.

This exact text format and the compact tool-feedback line intentionally skip a throwaway visual-variant/reaction pass: they are minimal, plain functional terminal output with no visual hierarchy or high-cost design decision. Keep them unstyled beyond ordinary terminal separation; if G2/G3 later introduces interactive selection or mode affordances, those richer UX surfaces should receive their own taste pass.

## Acceptance-criterion mapping

| Acceptance criterion | Concrete implementation and proof |
| --- | --- |
| A user can start a deus-native chat from the `deus` CLI. | `deus-cmd.sh`/`deus-cmd.ps1` route `deus chat` to `src/cli/deus-native-chat-client.ts`; the daemon server resolves `registry.get("deus-native")`, whose live registration is in `src/index.ts:162–165`. Launcher/server tests prove the path is connected rather than a tested-but-unwired facade. |
| At least two consecutive turns in one session. | The readline loop remains open; `createDeusNativeChatController.runTurn` caches and reuses the first `RunResult.sessionRef`. The integration test sends two prompts and asserts call 2 receives the same UUID and its real checkpointer state contains both messages, following the existing checkpointer integration assertions around lines 282–328. |
| Model output and tool feedback are rendered without protocol framing. | The exhaustive runtime-event normalizer in `deus-native-chat.ts` and renderer in `deus-native-chat-client.ts` handle output/tool/activity/usage/session/complete/error separately. Renderer tests feed every variant and assert no raw event or transport JSON appears. |
| The user can exit cleanly and resume the recorded session. | `/exit`, `/quit`, EOF, and SIGINT call the daemon close route and restore readline without `clearSession`. `db.getSession(..., "deus-native")` reloads the stored row on a new client/controller. The integration test closes, reconstructs controller/client state, sends a third prompt, and proves the same checkpointer thread resumes. |
| Backend and session identifiers are available through diagnostics or status output. | `/status` calls the daemon status route and renders full `Backend: deus-native` and `Session: <id>` values; tests cover pre-first-turn and persisted/resumed states. |

## Test plan and verification

### Unit tests

1. Controller start with no row calls `startOrResume`, passes its empty native ref to `runTurn`, persists the result ref, and reports “new.”
2. A second `runTurn` on the same controller gets the first result's exact session id; reconstructing the controller over the same store loads that id and reports “resumed.”
3. Both the `session` event path and final `RunResult.sessionRef` path persist; same-id writes are harmless; foreign-backend/empty result refs are rejected rather than corrupting the native row.
4. `close()` calls the runtime with the current/persisted ref and never clears the store.
5. Every `RuntimeEvent` variant maps exhaustively to the intended display behavior; usage absence remains unknown; tool arguments are bounded/redacted.
6. Client loop handles two prompts, `/status`, `/exit`, EOF, and interrupt without overlapping requests or leaving the terminal prompt active.
7. Server rejects wrong method/path/auth/version, oversized/malformed bodies, and concurrent turns; it never logs/returns the bearer token; discovery-file cleanup cannot delete a successor daemon's record. Because this endpoint can drive the host-side agent loop, derive the missing-token, wrong-token, and stale-discovery-record rejection cases independently from the security/transport specification through the repo's `oracle-author` gate and tag those cases `@oracle`; the implementation author must not weaken or rewrite those assertions to match the handler. Ordinary implementer-authored tests remain appropriate for low-blast-radius method/version/body validation.

### Integration tests

1. Start the daemon server factory on an ephemeral port with a real `DeusNativeRuntime` wired to the hermetic model/checkpointer harness used by `deus-native-checkpointer-integration.test.ts`; use a temporary/in-memory database.
2. Client A sends turn 1 and turn 2. Assert the same runtime UUID is persisted and the second model boundary sees both user messages.
3. Client A closes. Client B reads the same stored native row, `/status` reports resumed/backend/id, and turn 3 reaches the same checkpointer thread.
4. Have the fake model produce a tool call. Assert the terminal sees readable tool feedback and assistant output, while raw `RuntimeEvent` and NDJSON strings never appear.
5. Run the launcher structural test to prove the live `deus chat` command reaches the compiled client and the existing bare/Claude/Codex branches are unchanged.

### Commands before claiming implementation complete

```bash
npx vitest run src/cli/deus-native-chat.test.ts src/cli/deus-native-chat-server.test.ts src/cli/deus-native-chat-client.test.ts src/cli/deus-native-chat.integration.test.ts
npx vitest run src/agent-runtimes/deus-native-backend.test.ts src/agent-runtimes/deus-native-checkpointer-integration.test.ts
python3 -m pytest scripts/tests/test_deus_cmd_native_chat.py scripts/tests/test_deus_cmd_backend_prefix.py scripts/tests/test_deus_cmd_print_identity.py
zsh -n deus-cmd.sh
npm run typecheck
npm run build
npm run lint
npm test
npm run drift-check
git diff --check
```

On a machine with the configured Anthropic credential path and the service rebuilt/restarted, perform one manual smoke test: run `deus chat`, ask a tool-free question, ask a follow-up that depends on turn 1, run `/status`, exit, relaunch `deus chat`, and ask a resume-dependent question. Confirm daemon logs contain no discovery bearer token. Live-provider verification supplements but does not replace the hermetic tests.

`patterns/general-code.md` was read from the repo-root `patterns/` directory as selected by `.mex/ROUTER.md`. Its implementation requirements are reflected here: every new TypeScript source file has an alongside unit test, the architectural/credential-boundary change is documented through the ADR index, commits use `type(scope): description` Conventional Commit format (for example `feat(cli): add deus-native chat`), and `npm run drift-check` runs before push. Implementation must begin on a feature branch, remain one logical PR, and must not commit or push without the user-approved commit message required by `AGENTS.md`.

## Explicit non-goals

The new module comments and implementation must state these non-goals in the same prominent style as `deus-native-backend.ts`:

- No G2 model-selection UX: no `--model`, picker, model persistence, or provider switching.
- No G3 plan-mode toggle: no `--plan`, `/plan`, mode prompt, or permission-profile change.
- No change to bare `deus`, `deus claude`, `deus codex`, `deus openai`, `DEUS_CLI_AGENT`, or `DEUS_AGENT_BACKEND` behavior.
- No new backend/runtime interface, no changes to `DeusNativeRuntime` session-minting/checkpointer semantics, and no second database/session store.
- No expansion of the `deus-native` tool surface, shell/filesystem access, permission policy, control-group privilege, context loading, session compaction, or checkpoint cleanup.
- No invented tool-result event or fake token streaming; render only the normalized events the runtime contract supplies today.
- No multi-session picker, named conversations, `/new`, transcript export, history UI, or concurrent clients sharing one CLI thread.
- No reuse/change of the Odysseus `/v1/chat/completions` semantics: that endpoint intentionally uses fresh non-persisted sessions and a control group (`src/odysseus-server.ts:527+`), which conflicts with LIA-428's persisted synthetic CLI session.

## Risks and resolved/open questions

- **Credential-proxy ownership — resolved:** use the already-running daemon. A separate CLI runtime is invalid because group tokens are process-local and port 3001 is already owned.
- **CLI naming/compatibility — resolved:** additive `deus chat`; bare and prefixed commands remain unchanged.
- **Transport authorization — implementation risk:** loopback alone is insufficient because another local process can connect. Use a fresh high-entropy bearer token, constant-time comparison, user-only discovery file, no token logging, request caps, and exact routes. The implementation needs security review because this endpoint can drive the host-side agent loop; widening native tools later must re-evaluate this boundary.
- **Discovery-file races/staleness — implementation risk:** write atomically after listen, include pid/version/token, validate before use, and remove only a record still owned by the shutting-down server. A stale or mismatched record fails closed with an actionable error.
- **Interrupted clients — known behavior:** the host turn may complete and persist after the terminal disconnects. This is preferable to corrupting/checkpoint-cancelling a LangGraph turn; tell the user on reconnect that the persisted session was resumed.
- **Tool feedback granularity — contract limitation:** today's `RuntimeEvent` union exposes a tool invocation but not a separate tool-result event, and `deus-native` emits after buffered invoke. LIA-428 can truthfully show which tool was used, not live execution/result phases. Changing that contract belongs to a separately reviewed runtime ticket.
- **One global CLI thread — deliberate LIA-428 limit:** the fixed synthetic group folder yields one resumable native CLI session. Named/multiple sessions and cwd-scoped session keys are future UX decisions; silently keying by cwd now would make resume behavior surprising and constrain G2/G3.
- **Daemon availability/version skew — implementation risk:** the client cannot work safely without the daemon endpoint. Fail closed and tell the user to rebuild/restart; do not bootstrap a partial host or read credentials as a fallback.
- **Platform file permissions — verification item:** use `CONFIG_DIR`/`path.join` and Node's user-only create mode; verify POSIX `0600` in tests and document that Windows relies on the user's profile-directory ACL plus the random token. If review finds that insufficient, replace only the transport/discovery implementation with a same-user named-pipe ACL without changing the controller/client options seam.
- **Router/pattern compliance — resolved:** `.mex/ROUTER.md` resolves `patterns/general-code.md` relative to the repo root; that file is present and was read. Its alongside-unit-test, ADR-gate, Conventional Commit, one-logical-PR, and pre-push `npm run drift-check` requirements are captured in the file map, verification commands, and implementation-workflow note above.
