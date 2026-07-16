/**
 * B4 (LIA-404): end-to-end integration tests for deus-native's
 * checkpointer-backed session persistence, against a REAL SqliteSaver on a
 * per-test temp file and the REAL LangGraph graph-execution engine (real
 * createAgent, scripted FakeToolCallingModel — B2/B3's own precedent). A
 * mocked createAgent would make the checkpointer state meaningless.
 *
 * Isolation approach: this file tests deus-native-backend.ts (a DIFFERENT
 * module from checkpointer.ts), so it legitimately mocks './checkpointer.js'
 * CROSS-module — the same class of mock lifecycle-events.test.ts already
 * uses for group-folder.js, NOT the same-module self-mock that plan review
 * correctly rejected for checkpointer.test.ts itself. The mocked
 * getCheckpointer routes EVERY call (regardless of arguments) through the
 * REAL, parameter-injectable getCheckpointer at harness.checkpointerDbPath,
 * so runTurn's internal calls and this file's own direct getCheckpointer()
 * assertions all resolve to the same per-test temp database. The real
 * module's memoization is still active underneath the mock wrapper, hence
 * _resetCheckpointerForTests() in beforeEach.
 */

import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tool, FakeToolCallingModel } from 'langchain';

import { getCheckpointer } from './checkpointer.js';
import { defaultSession } from './types.js';
import type { RuntimeEvent, RunContext } from './types.js';
import type { ContainerRuntimeDeps } from './container-backend.js';
import { DISPATCH_NESTED_AGENT_TOOL_NAME } from './nested-dispatch-tool.js';

// ---------------------------------------------------------------------------
// Shared mutable harness (hoisted so the module mocks below can reach it) —
// lifecycle-events.test.ts's own established convention, extended with
// checkpointerDbPath for B4.
// ---------------------------------------------------------------------------
const harness = vi.hoisted(() => ({
  groupsDir: '',
  checkpointerDbPath: '',
  makeModel: null as null | (() => unknown),
  tools: [] as unknown[],
  captured: [] as Array<{ systemMessage: unknown; messages: unknown[] }>,
}));

// Real langchain everywhere — only createAgent is wrapped, to (a) swap the
// model for the test's scripted FakeToolCallingModel and (b) append a capture
// middleware LAST (innermost), recording the exact ModelRequest each model
// call receives (both the injected systemMessage and the full messages
// array the checkpointer-loaded state produced).
vi.mock('langchain', async (importOriginal) => {
  const actual = await importOriginal<typeof import('langchain')>();
  const capture = actual.createMiddleware({
    name: 'b4-test-capture',
    wrapModelCall: (request, handler) => {
      harness.captured.push({
        systemMessage: request.systemMessage,
        messages: [...request.messages],
      });
      return handler(request);
    },
  });
  return {
    ...actual,
    createAgent: (config: Parameters<typeof actual.createAgent>[0]) =>
      actual.createAgent({
        ...config,
        model: harness.makeModel
          ? (harness.makeModel() as typeof config.model)
          : config.model,
        middleware: [
          ...(config.middleware ?? []),
          capture,
        ] as typeof config.middleware,
      }),
  };
});

// The B4 cross-module checkpointer mock: route every getCheckpointer call
// through the REAL implementation pinned at the per-test temp file.
vi.mock('./checkpointer.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./checkpointer.js')>();
  return {
    ...actual,
    getCheckpointer: () => actual.getCheckpointer(harness.checkpointerDbPath),
  };
});

// Redirect group-folder resolution into the per-test temp fixture dir
// (loadSessionOpenContext derives groups/global as the group dir's sibling).
vi.mock('../group-folder.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../group-folder.js')>();
  const nodePath = await import('path');
  return {
    ...actual,
    resolveGroupFolderPath: (folder: string) =>
      nodePath.join(harness.groupsDir, folder),
  };
});

// Hermeticity mocks, mirroring lifecycle-events.test.ts's convention.
vi.mock('../credential-proxy.js', () => ({
  detectAuthMode: () => 'api-key' as const,
}));
vi.mock('../group-tokens.js', () => ({
  getOrCreateGroupToken: () => 'fake-proxy-token',
}));
vi.mock('./tool-broker-langchain-adapter.js', () => ({
  buildSafeTools: async () => harness.tools,
}));

const { createDeusNativeRuntime } = await import('./deus-native-backend.js');
const { _resetCheckpointerForTests } = await import('./checkpointer.js');

const stubDeps: ContainerRuntimeDeps = {
  resolveGroup: () => undefined,
  assistantName: 'Deus',
  registerProcess: () => {},
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function runCtx(groupFolder: string, prompt = 'hello there'): RunContext {
  return {
    prompt,
    groupFolder,
    chatJid: 'test@g.us',
    isControlGroup: false,
  };
}

function writeGroupClaudeMd(folder: string, content: string): void {
  const dir = path.join(harness.groupsDir, folder);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'CLAUDE.md'), content);
}

/** Text content of a captured SystemMessage (or '' for non-string shapes). */
function msgText(message: unknown): string {
  const content = (message as { content?: unknown } | undefined)?.content;
  return typeof content === 'string' ? content : '';
}

/** All string contents from one captured ModelRequest's messages array. */
function messageTexts(messages: unknown[]): string[] {
  return messages
    .map((m) => (m as { content?: unknown }).content)
    .filter((c): c is string => typeof c === 'string');
}

const ECHO_TOOL = tool(
  async (args: { value: string }) => `echo:${args.value}`,
  {
    name: 'echo_tool',
    description: 'Echoes the provided value back.',
    schema: {
      type: 'object',
      properties: { value: { type: 'string' } },
      required: ['value'],
      additionalProperties: false,
    },
  },
);

function singleCycleModel(): FakeToolCallingModel {
  return new FakeToolCallingModel({ toolCalls: [[]] });
}

function toolCallThenAnswerModel(): FakeToolCallingModel {
  return new FakeToolCallingModel({
    toolCalls: [
      [{ name: 'echo_tool', args: { value: 'ping' }, id: 'call_1' }],
      [],
    ],
  });
}

// B6 (LIA-406): KNOWN, hardcoded usage values for the AC4 reconciliation
// test — the emitted 'usage' events must carry these EXACT numbers.
const KNOWN_USAGE = {
  input_tokens: 42,
  output_tokens: 17,
  total_tokens: 59,
};

/**
 * B6 (LIA-406): FakeToolCallingModel never populates usage_metadata (its
 * _generate builds a bare AIMessage — that pristine behavior IS the AC4
 * "absent usage_metadata" case below). This wrapper stamps KNOWN_USAGE onto
 * every generated AIMessage for the reconciliation cases. It patches the
 * INSTANCE (and recursively re-wraps bindTools' result) rather than
 * subclassing, because FakeToolCallingModel.bindTools hard-codes
 * `new FakeToolCallingModel(...)` — and createAgent always rebinds tools,
 * which would silently discard a subclass's _generate override.
 */
function withKnownUsage(model: FakeToolCallingModel): FakeToolCallingModel {
  const originalGenerate = model._generate.bind(model);
  model._generate = async (
    ...args: Parameters<FakeToolCallingModel['_generate']>
  ) => {
    const result = await originalGenerate(...args);
    (
      result.generations[0].message as {
        usage_metadata?: typeof KNOWN_USAGE;
      }
    ).usage_metadata = { ...KNOWN_USAGE };
    return result;
  };
  const originalBindTools = model.bindTools.bind(model);
  model.bindTools = (
    ...args: Parameters<FakeToolCallingModel['bindTools']>
  ) => {
    const bound = originalBindTools(...args);
    return bound instanceof FakeToolCallingModel
      ? withKnownUsage(bound)
      : bound;
  };
  return model;
}

let checkpointerTempDir = '';

beforeEach(() => {
  harness.groupsDir = fs.mkdtempSync(
    path.join(os.tmpdir(), 'b4-integration-groups-'),
  );
  checkpointerTempDir = fs.mkdtempSync(
    path.join(os.tmpdir(), 'b4-integration-checkpointer-'),
  );
  harness.checkpointerDbPath = path.join(checkpointerTempDir, 'checkpoints.db');
  // The REAL module's memoization is still active underneath the mock
  // wrapper — without this reset, this test would silently reuse the
  // PREVIOUS test's (deleted) database file.
  _resetCheckpointerForTests();
  harness.makeModel = singleCycleModel;
  harness.tools = [];
  harness.captured.length = 0;
});

afterEach(() => {
  _resetCheckpointerForTests();
  fs.rmSync(harness.groupsDir, { recursive: true, force: true });
  fs.rmSync(checkpointerTempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// AC1 — a deus-native session row references real checkpointer state.
// ---------------------------------------------------------------------------
describe('new session (AC1: session row references checkpointer state)', () => {
  it('mints a fresh UUID and a checkpoint genuinely exists for that thread_id afterward', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);

    const result = await runtime.runTurn(
      runCtx('newgroup', 'first ever message'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result.status).toBe('success');
    const sessionId = result.sessionRef?.session_id;
    expect(sessionId).toMatch(UUID_PATTERN);
    expect(result.sessionRef?.backend).toBe('deus-native');

    // This getCheckpointer() call is this file's OWN import from
    // './checkpointer.js' — the SAME module path the vi.mock above
    // intercepts — so it resolves through the SAME mocked implementation
    // runTurn used (routed to harness.checkpointerDbPath regardless of
    // arguments), never the real, unmocked module.
    const tuple = await getCheckpointer().getTuple({
      configurable: { thread_id: sessionId as string },
    });
    // A checkpoint genuinely got WRITTEN (not just "the call succeeded"),
    // and it carries this turn's exchange.
    expect(tuple).toBeDefined();
    const channelMessages = (
      tuple?.checkpoint.channel_values as { messages?: unknown[] }
    ).messages;
    expect(Array.isArray(channelMessages)).toBe(true);
    const texts = messageTexts(channelMessages as unknown[]);
    expect(texts.some((t) => t.includes('first ever message'))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// AC2 — same-backend resume with REAL message continuity.
// ---------------------------------------------------------------------------
describe('same-backend resume (AC2: real conversation continuity)', () => {
  it('call 2 on the same session sees BOTH turns’ user messages in real accumulated state', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);

    const result1 = await runtime.runTurn(
      runCtx('resumegroup', 'remember the word: pomegranate'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result1.status).toBe('success');
    const sessionId = result1.sessionRef?.session_id as string;
    expect(sessionId).toMatch(UUID_PATTERN);

    harness.captured.length = 0;
    const result2 = await runtime.runTurn(
      runCtx('resumegroup', 'what did I ask you to remember?'),
      { backend: 'deus-native', session_id: sessionId },
      () => {},
    );
    expect(result2.status).toBe('success');
    expect(result2.sessionRef?.session_id).toBe(sessionId);

    // The exact messages array the model boundary saw on call 2 — must
    // contain BOTH call 1's and call 2's user messages, proving real
    // checkpointer-loaded accumulated state, not just "the call didn't
    // error".
    expect(harness.captured.length).toBeGreaterThan(0);
    const call2Texts = messageTexts(
      harness.captured[harness.captured.length - 1].messages,
    );
    expect(
      call2Texts.some((t) => t.includes('remember the word: pomegranate')),
    ).toBe(true);
    expect(
      call2Texts.some((t) => t.includes('what did I ask you to remember?')),
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Within-turn tool-call scoping regression (plan history item 1): a resumed
// turn must NOT re-emit tool_call events from earlier turns. This test
// directly falsifies the regression if the priorMessageCount slice is
// missing or wrong.
// ---------------------------------------------------------------------------
describe('tool-call event scoping on resume', () => {
  it('does not re-emit call 1’s tool call on the resumed call 2', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.tools = [ECHO_TOOL];
    harness.makeModel = toolCallThenAnswerModel;

    const events1: RuntimeEvent[] = [];
    const result1 = await runtime.runTurn(
      runCtx('toolgroup', 'call the echo tool'),
      defaultSession('', 'deus-native'),
      (event) => {
        events1.push(event);
      },
    );
    expect(result1.status).toBe('success');
    // Call 1's own tool call fired as expected.
    const toolEvents1 = events1.filter((e) => e.type === 'tool_call');
    expect(toolEvents1).toHaveLength(1);
    expect(toolEvents1[0]).toMatchObject({
      type: 'tool_call',
      name: 'echo_tool',
      arguments: { value: 'ping' },
    });

    // Call 2: same resumed thread, NO tool call this turn. The prior turn's
    // AIMessage (with its tool_calls) is still in the checkpointer-loaded
    // state — without the slice, the emission loop would re-emit it.
    harness.makeModel = singleCycleModel;
    const events2: RuntimeEvent[] = [];
    const result2 = await runtime.runTurn(
      runCtx('toolgroup', 'just answer plainly'),
      {
        backend: 'deus-native',
        session_id: result1.sessionRef?.session_id as string,
      },
      (event) => {
        events2.push(event);
      },
    );
    expect(result2.status).toBe('success');
    expect(events2.filter((e) => e.type === 'tool_call')).toHaveLength(0);
    // The turn still completed normally.
    expect(events2.some((e) => e.type === 'turn_complete')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// B6 (LIA-406) AC4 — usage-event reconciliation against KNOWN scripted
// values, driven through the REAL runTurn + real createAgent. Case A: exact
// token-count reconciliation (event AND RunResult.usage aggregate). Case B:
// an AIMessage with NO usage_metadata still emits a 'usage' event, with all
// token fields undefined and RunResult.usage omitted. Case C: multi-model-
// call turns sum the aggregate across every AIMessage that carried usage.
// ---------------------------------------------------------------------------
describe('usage events (B6 LIA-406: AC4 reconciliation)', () => {
  it('emits a usage event whose token counts equal the scripted usage_metadata exactly, and RunResult.usage matches', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.makeModel = () =>
      withKnownUsage(new FakeToolCallingModel({ toolCalls: [[]] }));

    const events: RuntimeEvent[] = [];
    const result = await runtime.runTurn(
      runCtx('usagegroup', 'count my tokens'),
      defaultSession('', 'deus-native'),
      (event) => {
        events.push(event);
      },
    );
    expect(result.status).toBe('success');

    // Exactly ONE model call this turn → exactly ONE usage event, carrying
    // the scripted numbers verbatim (a real reconciliation, not "an event
    // fired").
    const usageEvents = events.filter((e) => e.type === 'usage');
    expect(usageEvents).toHaveLength(1);
    expect(usageEvents[0]).toMatchObject({
      type: 'usage',
      sessionId: result.sessionRef?.session_id,
      provider: 'anthropic',
      inputTokens: KNOWN_USAGE.input_tokens,
      outputTokens: KNOWN_USAGE.output_tokens,
      totalTokens: KNOWN_USAGE.total_tokens,
    });
    // AC2: model is read off the ChatAnthropic instance's own .model field
    // at call time — assert it's a real, non-empty identifier without
    // re-hardcoding the tier literal here (the exact thing AC2 avoids).
    const usageEvent = usageEvents[0] as Extract<
      RuntimeEvent,
      { type: 'usage' }
    >;
    expect(typeof usageEvent.model).toBe('string');
    expect(usageEvent.model.length).toBeGreaterThan(0);

    // The turn-level aggregate reconciles to the same scripted numbers.
    expect(result.usage).toEqual({
      inputTokens: KNOWN_USAGE.input_tokens,
      outputTokens: KNOWN_USAGE.output_tokens,
      totalTokens: KNOWN_USAGE.total_tokens,
    });
  });

  it('still emits a usage event (with all token fields undefined) when the AIMessage has no usage_metadata, and omits RunResult.usage', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    // Pristine FakeToolCallingModel: its _generate never sets usage_metadata.
    harness.makeModel = singleCycleModel;

    const events: RuntimeEvent[] = [];
    const result = await runtime.runTurn(
      runCtx('nousagegroup', 'no usage reported'),
      defaultSession('', 'deus-native'),
      (event) => {
        events.push(event);
      },
    );
    expect(result.status).toBe('success');

    // The event STILL fires (AC1's unconditional guarantee) — absence is
    // represented explicitly as undefined counts, never fabricated zeros.
    const usageEvents = events.filter((e) => e.type === 'usage');
    expect(usageEvents).toHaveLength(1);
    const usageEvent = usageEvents[0] as Extract<
      RuntimeEvent,
      { type: 'usage' }
    >;
    expect(usageEvent.provider).toBe('anthropic');
    expect(usageEvent.sessionId).toBe(result.sessionRef?.session_id);
    expect(usageEvent.inputTokens).toBeUndefined();
    expect(usageEvent.outputTokens).toBeUndefined();
    expect(usageEvent.totalTokens).toBeUndefined();

    // No message in the turn carried usage → the aggregate is omitted
    // entirely, not a zero-object.
    expect(result.usage).toBeUndefined();
  });

  it('a tool-calling turn (two model calls) emits one usage event per AIMessage and sums the aggregate', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.tools = [ECHO_TOOL];
    harness.makeModel = () =>
      withKnownUsage(
        new FakeToolCallingModel({
          toolCalls: [
            [{ name: 'echo_tool', args: { value: 'ping' }, id: 'call_1' }],
            [],
          ],
        }),
      );

    const events: RuntimeEvent[] = [];
    const result = await runtime.runTurn(
      runCtx('multiusagegroup', 'call the echo tool'),
      defaultSession('', 'deus-native'),
      (event) => {
        events.push(event);
      },
    );
    expect(result.status).toBe('success');

    // Two model calls (agent-node → tool-node → agent-node) → two
    // AIMessages → two usage events, each carrying the scripted numbers.
    const usageEvents = events.filter((e) => e.type === 'usage');
    expect(usageEvents).toHaveLength(2);
    for (const event of usageEvents) {
      expect(event).toMatchObject({
        inputTokens: KNOWN_USAGE.input_tokens,
        outputTokens: KNOWN_USAGE.output_tokens,
        totalTokens: KNOWN_USAGE.total_tokens,
      });
    }

    // The aggregate is the SUM across both model calls.
    expect(result.usage).toEqual({
      inputTokens: KNOWN_USAGE.input_tokens * 2,
      outputTokens: KNOWN_USAGE.output_tokens * 2,
      totalTokens: KNOWN_USAGE.total_tokens * 2,
    });
  });
});

// ---------------------------------------------------------------------------
// B3-upgrade-path regression (plan history item 3): a NON-EMPTY, pre-existing
// session_id (a B3-era row that predates any checkpointer) with NO real
// checkpoint must still be treated as a NEW session for injection purposes,
// while the echo-vs-mint decision stays unaffected. This test directly
// falsifies the fix if isNewSession ever regresses to a string check.
// ---------------------------------------------------------------------------
describe('B3-upgrade-path regression (isNewSession = real checkpoint existence)', () => {
  it('injects CLAUDE.md for a non-empty pre-B4 session_id with no checkpoint, and echoes the SAME id back', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    writeGroupClaudeMd('upgradegroup', 'Upgrade-path rule content.');

    // Simulates a B3-era row: UUID-shaped, non-empty, never a real
    // checkpointer thread (nothing has ever been written for it in this
    // fresh per-test database).
    const preexistingId = crypto.randomUUID();
    const result = await runtime.runTurn(
      runCtx('upgradegroup'),
      { backend: 'deus-native', session_id: preexistingId },
      () => {},
    );
    expect(result.status).toBe('success');

    // Session-open CLAUDE.md content STILL injects — isNewSession is driven
    // by real checkpoint existence, not the non-empty string.
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toContain(
        'Upgrade-path rule content.',
      );
    }

    // The echo-vs-mint decision is unaffected by the fix: the DB row's
    // identity is preserved even though injection correctly re-fires.
    expect(result.sessionRef?.session_id).toBe(preexistingId);
  });
});

// ---------------------------------------------------------------------------
// AC3 — backend mismatch: switching backends creates a new row instead of
// rewriting the prior row. Verified here (not just asserted): db.setSession's
// dedup WHERE clause is backend-scoped (db.ts:806). db.test.ts already
// covers claude/openai backend isolation at the pure-db level ('keeps
// separate sessions for each backend in the same group', 'replaces the
// active session for one backend without touching the other') — this test
// adds the deus-native-specific case with a REAL runTurn-minted session id,
// referencing (not duplicating) that existing coverage.
// ---------------------------------------------------------------------------
describe('backend mismatch (AC3: switching backends never rewrites the deus-native row)', () => {
  it('a claude session save leaves the runTurn-minted deus-native row intact, both rows coexisting', async () => {
    const { _initTestDatabase, setSession, getSession } =
      await import('../db.js');
    _initTestDatabase();

    const runtime = createDeusNativeRuntime(stubDeps);
    const result1 = await runtime.runTurn(
      runCtx('mismatchgroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result1.status).toBe('success');
    const deusNativeId = result1.sessionRef?.session_id as string;
    expect(deusNativeId).toMatch(UUID_PATTERN);

    // Persist the deus-native session the way the orchestrator would.
    setSession('mismatchgroup', result1.sessionRef!);

    // Simulate the group switching backends.
    setSession('mismatchgroup', {
      backend: 'claude',
      session_id: 'some-claude-id',
    });

    // (a) deus-native's ORIGINAL session survives, unmodified/un-orphaned.
    expect(getSession('mismatchgroup', 'deus-native')?.session_id).toBe(
      deusNativeId,
    );
    // (b) the claude row coexists — neither rewrote the other.
    expect(getSession('mismatchgroup', 'claude')?.session_id).toBe(
      'some-claude-id',
    );
  });
});

// ---------------------------------------------------------------------------
// B5 (LIA-405) AC4 — session-row idempotency across a same-thread re-invoke:
// the ONE real idempotency property in today's deus-native path worth a
// frozen regression test (docs/decisions/deus-v2-replay-safety.md, Section 3).
// Invoking the SAME thread_id twice in a row with an already-existing
// session must leave exactly ONE row in `sessions` for (groupFolder,
// backend): setSession's dedup (db.ts:791-830) touches last_used_at instead
// of orphaning + inserting. Asserts the row's `id` (autoincrement primary
// key — `sessions` has no `created_at` column per db.ts:83-93) is unchanged
// and `last_used_at` advances, via the `_`-prefixed raw-row test accessor.
// ---------------------------------------------------------------------------
describe('same-thread re-invoke (B5 AC4: exactly one session row, id stable, last_used_at advances)', () => {
  it('a second runTurn + save on the same thread_id keeps a single un-orphaned row with the same id and a later last_used_at', async () => {
    const { _initTestDatabase, setSession, _getRawSessionRowsForTests } =
      await import('../db.js');
    _initTestDatabase();

    const runtime = createDeusNativeRuntime(stubDeps);

    // Turn 1: mint the session and persist it the way the orchestrator would.
    const result1 = await runtime.runTurn(
      runCtx('idempotentgroup', 'first turn'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result1.status).toBe('success');
    const sessionId = result1.sessionRef?.session_id as string;
    expect(sessionId).toMatch(UUID_PATTERN);
    setSession('idempotentgroup', result1.sessionRef!);

    const rowsAfterFirst = _getRawSessionRowsForTests(
      'idempotentgroup',
      'deus-native',
    );
    expect(rowsAfterFirst).toHaveLength(1);
    const firstRow = rowsAfterFirst[0];
    expect(firstRow.orphaned_at).toBeNull();
    expect(firstRow.last_used_at).not.toBeNull();

    // setSession stamps last_used_at with millisecond-resolution ISO
    // timestamps — without this gap, both saves could land on the SAME
    // millisecond and "advances" would be unfalsifiable.
    await new Promise((resolve) => setTimeout(resolve, 10));

    // Turn 2: same thread_id (already-existing session), then re-save.
    const result2 = await runtime.runTurn(
      runCtx('idempotentgroup', 'second turn'),
      { backend: 'deus-native', session_id: sessionId },
      () => {},
    );
    expect(result2.status).toBe('success');
    expect(result2.sessionRef?.session_id).toBe(sessionId);
    setSession('idempotentgroup', result2.sessionRef!);

    const rowsAfterSecond = _getRawSessionRowsForTests(
      'idempotentgroup',
      'deus-native',
    );
    // Exactly ONE row — the dedup path updated in place; no orphan + insert.
    expect(rowsAfterSecond).toHaveLength(1);
    const secondRow = rowsAfterSecond[0];
    // The autoincrement primary key is unchanged: same physical row.
    expect(secondRow.id).toBe(firstRow.id);
    expect(secondRow.session_id).toBe(sessionId);
    expect(secondRow.orphaned_at).toBeNull();
    // last_used_at advanced (ISO-8601 strings compare lexicographically).
    expect(secondRow.last_used_at! > firstRow.last_used_at!).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// B8 (LIA-408) — nested subagent dispatch: usage/session reconciliation.
// Dispatches a REAL nested-dispatch child (through the production
// dispatch_nested_agent tool `deus-native-backend.ts` now wires into every
// parent tool list) from a scripted parent tool call, and proves: the
// child's usage is attributed to the PARENT session id (never a separate
// child thread/session — children get no checkpointer at all), reconciled
// into the combined RunResult.usage even when the parent itself reports no
// usage, and the tool result visible to the parent carries traceable
// agent/model/provider/parent-session metadata (AC5). Only TWO createAgent
// graphs exist for this whole turn — the parent's (created once in
// deus-native-backend.ts's runTurn) and the ONE dispatched child's (created
// inside nested-dispatch.ts's dispatch() call) — so harness.makeModel is
// called exactly twice: once per createAgent() invocation, not once per
// individual model generation.
// ---------------------------------------------------------------------------
describe('nested dispatch (B8 LIA-408): parent-to-child usage and session reconciliation', () => {
  const CHILD_MODEL_ID = 'child-model-xyz';
  const CHILD_USAGE = { input_tokens: 7, output_tokens: 3, total_tokens: 10 };

  const DISPATCH_OUTPUT_CONTRACT = {
    name: 'dispatch-result',
    schema: {
      type: 'object',
      properties: { answer: { type: 'string', minLength: 1 } },
      required: ['answer'],
      additionalProperties: false,
    },
  };

  /** Stamps a fixed final-content string onto every generated AIMessage,
   *  surviving createAgent's internal bindTools rebind — same technique as
   *  nested-dispatch.test.ts's own modelReturning helper (createAgent always
   *  rebinds tools, and FakeToolCallingModel.bindTools constructs a fresh
   *  instance on rebind, silently discarding an un-decorated override). */
  function modelReturningContent(content: string): FakeToolCallingModel {
    const decorate = (model: FakeToolCallingModel): FakeToolCallingModel => {
      const originalGenerate = model._generate.bind(model);
      model._generate = async (
        ...args: Parameters<FakeToolCallingModel['_generate']>
      ) => {
        const result = await originalGenerate(...args);
        const generation = result.generations[0];
        if (generation) {
          generation.text = content;
          generation.message.content = content;
        }
        return result;
      };
      const originalBindTools = model.bindTools.bind(model);
      model.bindTools = (
        ...args: Parameters<FakeToolCallingModel['bindTools']>
      ) => {
        const bound = originalBindTools(...args);
        return bound instanceof FakeToolCallingModel ? decorate(bound) : bound;
      };
      return model;
    };
    return decorate(new FakeToolCallingModel({ toolCalls: [[]] }));
  }

  /** Stamps arbitrary usage_metadata (parametrized, unlike this file's own
   *  `withKnownUsage`, which is fixed to KNOWN_USAGE) onto every generated
   *  AIMessage, surviving bindTools rebind the same way. */
  function withUsage(
    model: FakeToolCallingModel,
    usage: {
      input_tokens: number;
      output_tokens: number;
      total_tokens: number;
    },
  ): FakeToolCallingModel {
    const originalGenerate = model._generate.bind(model);
    model._generate = async (
      ...args: Parameters<FakeToolCallingModel['_generate']>
    ) => {
      const result = await originalGenerate(...args);
      (
        result.generations[0].message as { usage_metadata?: typeof usage }
      ).usage_metadata = { ...usage };
      return result;
    };
    const originalBindTools = model.bindTools.bind(model);
    model.bindTools = (
      ...args: Parameters<FakeToolCallingModel['bindTools']>
    ) => {
      const bound = originalBindTools(...args);
      return bound instanceof FakeToolCallingModel
        ? withUsage(bound, usage)
        : bound;
    };
    return model;
  }

  /** The parent's model: one dispatch tool call, then a plain final answer
   *  — no usage_metadata scripted on either step, so the turn's aggregate
   *  usage (asserted below) can ONLY have come from the dispatched child. */
  function parentDispatchingModel(): FakeToolCallingModel {
    return new FakeToolCallingModel({
      toolCalls: [
        [
          {
            name: DISPATCH_NESTED_AGENT_TOOL_NAME,
            args: {
              agentId: 'helper',
              model: CHILD_MODEL_ID,
              prompt: 'help with the task',
              outputContract: DISPATCH_OUTPUT_CONTRACT,
            },
            id: 'dispatch_call_1',
          },
        ],
        [],
      ],
    });
  }

  it("folds a nested child dispatch's usage into RunResult.usage under the PARENT session id, and surfaces traceable metadata to the parent", async () => {
    const runtime = createDeusNativeRuntime(stubDeps);

    // Exactly two createAgent() graphs exist this turn: the parent's (1st
    // call) and the ONE dispatched child's (2nd call, made from inside the
    // dispatch_nested_agent tool's execution).
    let createAgentCallCount = 0;
    harness.makeModel = () => {
      createAgentCallCount += 1;
      if (createAgentCallCount === 2) {
        return withUsage(
          modelReturningContent(JSON.stringify({ answer: 'child says hi' })),
          CHILD_USAGE,
        );
      }
      return parentDispatchingModel();
    };

    const events: RuntimeEvent[] = [];
    const result = await runtime.runTurn(
      runCtx('dispatchgroup', 'please delegate this'),
      defaultSession('', 'deus-native'),
      (event) => {
        events.push(event);
      },
    );

    expect(result.status).toBe('success');
    const sessionId = result.sessionRef?.session_id as string;
    expect(sessionId).toMatch(UUID_PATTERN);

    // AC1/AC4/AC5: the dispatch fired against an independently selected
    // child model, and the child's usage event used the PARENT's session
    // id — a dispatched child never gets a session/thread of its own.
    const usageEvents = events.filter(
      (e): e is Extract<RuntimeEvent, { type: 'usage' }> => e.type === 'usage',
    );
    const childUsageEvent = usageEvents.find((e) => e.model === CHILD_MODEL_ID);
    expect(childUsageEvent).toBeDefined();
    expect(childUsageEvent?.sessionId).toBe(sessionId);
    expect(childUsageEvent?.provider).toBe('anthropic');
    expect(childUsageEvent).toMatchObject({
      inputTokens: CHILD_USAGE.input_tokens,
      outputTokens: CHILD_USAGE.output_tokens,
      totalTokens: CHILD_USAGE.total_tokens,
    });

    // The parent's own two model calls carried no scripted usage_metadata
    // (undefined token fields, per the unconditional-emit contract) — so
    // the turn-level aggregate is EXACTLY the child's contribution. This
    // directly falsifies the regression where a child's usage never
    // reaches RunResult.usage at all.
    expect(result.usage).toEqual({
      inputTokens: CHILD_USAGE.input_tokens,
      outputTokens: CHILD_USAGE.output_tokens,
      totalTokens: CHILD_USAGE.total_tokens,
    });

    // AC5: the tool result visible to the parent (embedded in the
    // checkpointed transcript's ToolMessage content) carries traceable
    // agent/model/provider/parent-session metadata — a failed OR successful
    // dispatch remains identifiable from the parent's own transcript.
    const tuple = await getCheckpointer().getTuple({
      configurable: { thread_id: sessionId },
    });
    const channelMessages = (
      tuple?.checkpoint.channel_values as { messages?: unknown[] }
    ).messages as unknown[];
    // The success-path content is wrapped in a `<nested-dispatch-output>`
    // prompt-injection boundary (nested-dispatch-tool.ts, added after
    // ai-eng-warden review — the child shares the parent's web_search/
    // web_fetch surface, so its validated output can carry untrusted
    // external content); the raw result JSON is the one line inside that
    // wrapper, never the whole message content string.
    const dispatchResultText = messageTexts(channelMessages).find((t) =>
      t.includes('"status":"success"'),
    );
    expect(dispatchResultText).toBeDefined();
    expect(dispatchResultText).toContain('<nested-dispatch-output');
    const jsonLine = (dispatchResultText as string)
      .split('\n')
      .find((line) => line.trim().startsWith('{'));
    expect(jsonLine).toBeDefined();
    const dispatchResultPayload = JSON.parse(jsonLine as string);
    expect(dispatchResultPayload).toMatchObject({
      status: 'success',
      output: { answer: 'child says hi' },
      metadata: {
        agentId: 'helper',
        model: CHILD_MODEL_ID,
        provider: 'anthropic',
        parentSessionId: sessionId,
      },
    });

    // Only ONE thread exists for this session — the dispatched child never
    // created a checkpoint/thread of its own (nested-dispatch.ts's child
    // `createAgent({model, tools, middleware})` call carries no
    // `checkpointer` field at all).
    expect(tuple).toBeDefined();
  });
});
