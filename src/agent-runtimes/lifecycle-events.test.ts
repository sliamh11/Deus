import fs from 'fs';
import os from 'os';
import path from 'path';

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  createAgent,
  tool,
  FakeToolCallingModel,
  SystemMessage,
  HumanMessage,
} from 'langchain';
import { ChatAnthropic } from '@langchain/anthropic';

import {
  loadSessionOpenContext,
  buildPromptLifecycleHook,
  type PromptEventRecord,
} from './lifecycle-events.js';
import { loadVaultContext } from './vault-context.js';
import type { VaultContextResult } from './vault-context.js';
import { defaultSession } from './types.js';
import type {
  RuntimeEvent,
  RuntimeSession,
  RunContext,
  RuntimeEventSink,
  RunResult,
} from './types.js';
import type { ContainerRuntimeDeps } from './container-backend.js';
import { RuntimeRegistry } from './registry.js';

// ---------------------------------------------------------------------------
// Shared mutable harness (hoisted so the module mocks below can reach it).
// - groupsDir: per-test temp `groups/` fixture root, so no test ever reads or
//   writes the repo's real groups/ directory.
// - checkpointerDbPath: per-test temp SQLite file for the REAL SqliteSaver
//   behind B4's checkpointer mock (see the './checkpointer.js' mock below).
// - makeModel: per-test factory for the scripted model that replaces the
//   (never-invoked) real ChatAnthropic inside runTurn's createAgent call.
// - tools: what the mocked buildSafeTools returns (the echo tool for
//   multi-cycle turns, [] otherwise).
// - captured: one entry per wrapModelCall firing, recorded by an INNERMOST
//   capture middleware appended after prompt-lifecycle — so it observes the
//   exact ModelRequest the injection produced (or didn't).
// ---------------------------------------------------------------------------
const harness = vi.hoisted(() => ({
  groupsDir: '',
  checkpointerDbPath: '',
  makeModel: null as null | (() => unknown),
  tools: [] as unknown[],
  captured: [] as Array<{ systemMessage: unknown; systemPrompt: unknown }>,
  // LIA-416 (D2): what the mocked vault facade returns for a CONTROL-group
  // call. undefined => "eligible, vault unavailable, no content" (the
  // hermetic default — real config/vault/DB/subprocess must never be touched
  // from this file). Non-control calls always get the real skip shape.
  vaultContent: undefined as string | undefined,
}));

// Real langchain everywhere (createMiddleware, FakeToolCallingModel, tool,
// message classes) — only createAgent is wrapped, to (a) swap the model for
// the test's scripted FakeToolCallingModel and (b) append the capture
// middleware LAST (innermost), so it sees prompt-lifecycle's injected
// systemMessage exactly as the model boundary would.
vi.mock('langchain', async (importOriginal) => {
  const actual = await importOriginal<typeof import('langchain')>();
  const capture = actual.createMiddleware({
    name: 'b3-test-capture',
    wrapModelCall: (request, handler) => {
      harness.captured.push({
        systemMessage: request.systemMessage,
        systemPrompt: request.systemPrompt,
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

// Redirect group-folder resolution into the per-test temp fixture dir.
// loadSessionOpenContext derives groups/global as the group dir's SIBLING,
// so this one override covers both the group and global reads.
vi.mock('../group-folder.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../group-folder.js')>();
  const nodePath = await import('path');
  return {
    ...actual,
    resolveGroupFolderPath: (folder: string) =>
      nodePath.join(harness.groupsDir, folder),
  };
});

// B4 (LIA-404): a STATEFUL checkpointer mock — a REAL SqliteSaver routed to
// the per-test temp file — NOT the simple always-undefined stub
// deus-native-backend.test.ts uses. This file's assertions specifically test
// new-vs-resumed injection behavior, which under B4's redefined isNewSession
// signal (real checkpoint existence, not a session_id string check) only
// keeps testing what it claims to test if the mocked checkpointer GENUINELY
// tracks whether a thread_id has been seen before: an always-undefined stub
// would make every call look "new" and pass the resumed-turn assertions
// vacuously; a fixed tuple would break the new-session cases. Same mechanism
// as deus-native-checkpointer-integration.test.ts, reused.
vi.mock('./checkpointer.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./checkpointer.js')>();
  return {
    ...actual,
    getCheckpointer: () => actual.getCheckpointer(harness.checkpointerDbPath),
  };
});

// LIA-416 (D2): hermetic vault-context facade — real config/vault/DB/
// subprocess access must never happen from this file. A non-control call
// delegates to the REAL loadVaultContext, which exits on the
// non-control-group check before any I/O (verified: vault-context.ts's
// eligibility exits run strictly before readConfig/resolveVault/fs/spawn),
// so that branch stays genuinely hermetic. A control-group call returns
// harness.vaultContent instead of touching the real vault. Wrapped in
// `vi.fn` so tests can assert call counts directly (AC1/AC3/AC4).
vi.mock('./vault-context.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./vault-context.js')>();
  return {
    ...actual,
    loadVaultContext: vi.fn(
      async (runContext: RunContext): Promise<VaultContextResult> => {
        if (!runContext.isControlGroup) {
          return actual.loadVaultContext(runContext);
        }
        const hasContent = harness.vaultContent !== undefined;
        return {
          content: harness.vaultContent,
          record: {
            eligible: true,
            vaultAvailable: hasContent,
            contextLoaded: hasContent,
            loadedSections: hasContent ? (['vault-files'] as const) : [],
            loadedVaultFiles: [],
          },
        };
      },
    ),
  };
});

// Hermeticity mocks, mirroring deus-native-backend.test.ts's convention: no
// live auth-mode detection, no on-disk token mint, no real tool broker, and
// no tasks-snapshot writes from the scheduler path.
vi.mock('../credential-proxy.js', () => ({
  detectAuthMode: () => 'api-key' as const,
}));
vi.mock('../group-tokens.js', () => ({
  getOrCreateGroupToken: () => 'fake-proxy-token',
}));
vi.mock('./tool-broker-langchain-adapter.js', () => ({
  buildSafeTools: async () => harness.tools,
}));
vi.mock('../container-runner.js', () => ({
  writeTasksSnapshot: vi.fn(),
}));

const { createDeusNativeRuntime } = await import('./deus-native-backend.js');
// Resolves through the './checkpointer.js' mock above, which spreads the
// actual module — this is the REAL reset function.
const { _resetCheckpointerForTests } = await import('./checkpointer.js');

const stubDeps: ContainerRuntimeDeps = {
  resolveGroup: () => undefined,
  assistantName: 'Deus',
  registerProcess: () => {},
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function runCtx(groupFolder: string, isControlGroup = false): RunContext {
  return {
    prompt: 'hello there',
    groupFolder,
    chatJid: 'test@g.us',
    isControlGroup,
  };
}

function writeGroupClaudeMd(folder: string, content: string): void {
  const dir = path.join(harness.groupsDir, folder);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'CLAUDE.md'), content);
}

function writeGlobalClaudeMd(content: string): void {
  writeGroupClaudeMd('global', content);
}

/** Text content of a captured SystemMessage (or '' for non-string shapes). */
function msgText(message: unknown): string {
  const content = (message as { content?: unknown } | undefined)?.content;
  return typeof content === 'string' ? content : '';
}

// The same scripted tool-call turn shape middleware-stack.test.ts uses:
// first model cycle requests one echo_tool call, second cycle answers plainly
// — two wrapModelCall/beforeModel firings within ONE agent turn.
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

let checkpointerTempDir = '';

beforeEach(() => {
  harness.groupsDir = fs.mkdtempSync(
    path.join(os.tmpdir(), 'b3-lifecycle-groups-'),
  );
  // B4: fresh temp checkpointer DB per test. The REAL module's memoization
  // is still active underneath the mock wrapper — the reset must run BEFORE
  // any test's runTurn call, or a later test would silently keep reusing the
  // previous test's (deleted) database file.
  checkpointerTempDir = fs.mkdtempSync(
    path.join(os.tmpdir(), 'b4-lifecycle-checkpointer-'),
  );
  harness.checkpointerDbPath = path.join(checkpointerTempDir, 'checkpoints.db');
  _resetCheckpointerForTests();
  harness.makeModel = singleCycleModel;
  harness.tools = [];
  harness.captured.length = 0;
  harness.vaultContent = undefined;
  vi.mocked(loadVaultContext).mockClear();
});

afterEach(() => {
  _resetCheckpointerForTests();
  fs.rmSync(harness.groupsDir, { recursive: true, force: true });
  fs.rmSync(checkpointerTempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Real-conversion-path test: proves the systemMessage-first shape survives
// @langchain/anthropic's REAL payload converter (the exact code that throws
// "System messages are only permitted as the first passed message" on a
// mid-array system message) — exercised entirely through the PUBLIC
// ChatAnthropic surface via its own createClient constructor option (the
// deep import of dist/utils/message_inputs.js is blocked by the package's
// strict `exports` map). No network call.
// ---------------------------------------------------------------------------
describe('session-open injection through ChatAnthropic’s real conversion path', () => {
  it('lands in the payload `system` field with no system-role entry inside `messages`', async () => {
    const captured: Array<Record<string, unknown>> = [];
    // message_outputs.js's anthropicResponseToChatMessages reads
    // response.content immediately, so an under-specified fake response
    // throws for the wrong reason -- pin the minimal valid shape.
    const FAKE_RESPONSE = { content: [{ type: 'text', text: 'ok' }] };
    const model = new ChatAnthropic({
      model: 'claude-opus-4-8',
      // createClient is typed `(options) => any` on ChatAnthropicInput, so
      // the fake client needs no cast — and providing it also bypasses the
      // constructor's "Anthropic API key not found" throw.
      createClient: () => ({
        messages: {
          create: async (req: unknown) => {
            captured.push(req as Record<string, unknown>);
            return FAKE_RESPONSE;
          },
        },
      }),
    });

    const sessionOpenMessage = 'GROUP RULES: CLAUDE.md\n\nAlways be terse.';
    // Exactly what AgentNode produces from wrapModelCall's systemMessage
    // field: the system message FIRST, then the conversation.
    await expect(
      model.invoke([
        new SystemMessage(sessionOpenMessage),
        new HumanMessage('hi'),
      ]),
    ).resolves.toBeDefined();

    expect(captured).toHaveLength(1);
    const request = captured[0];
    expect(request.system).toBe(sessionOpenMessage);
    const messages = request.messages as Array<{ role: string }>;
    expect(messages.length).toBeGreaterThan(0);
    expect(messages.some((m) => m.role === 'system')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// AC1/AC4 — literal once-per-session gating through REAL runTurn calls.
// ---------------------------------------------------------------------------
describe('runTurn session-open lifecycle (AC1/AC4)', () => {
  it('injects on a new session and NOT on the resumed turn, echoing the session id back (AC1 + AC4)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    writeGroupClaudeMd('intgroup', 'Group rule: always answer in haiku.');

    // Call 1 — a genuine new open (empty incoming session_id).
    const result1 = await runtime.runTurn(
      runCtx('intgroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result1.status).toBe('success');
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toContain(
        'Group rule: always answer in haiku.',
      );
    }
    const sessionId1 = result1.sessionRef?.session_id;
    expect(sessionId1).toBeTruthy();
    expect(result1.sessionRef?.backend).toBe('deus-native');

    // Call 2 — a genuine resume: the SAME non-empty id passed back in,
    // mirroring what message-orchestrator.ts would pass on a real second
    // turn once RunResult.sessionRef is persisted.
    harness.captured.length = 0;
    const result2 = await runtime.runTurn(
      runCtx('intgroup'),
      { backend: 'deus-native', session_id: sessionId1 as string },
      () => {},
    );
    expect(result2.status).toBe('success');
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      // Literal once-per-session gating: the session-open content must NOT
      // repeat on the resumed turn — the request keeps the agent's default
      // (empty) system message.
      expect(msgText(call.systemMessage)).toBe('');
      expect(msgText(call.systemMessage)).not.toContain('haiku');
    }
    // Echo-back-on-resume minting strategy: unchanged id.
    expect(result2.sessionRef?.session_id).toBe(sessionId1);
  });

  it('does not inject on a new session when no CLAUDE.md content exists (content-presence gating)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    // No group CLAUDE.md, no global CLAUDE.md anywhere in the fixture.

    const result = await runtime.runTurn(
      runCtx('emptygroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result.status).toBe('success');
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toBe('');
    }
    // The session marker is still minted — injection gating and session
    // bookkeeping are independent concerns.
    expect(result.sessionRef?.session_id).toMatch(UUID_PATTERN);
  });

  it('re-injects with a NEW session id after an idle reset empties the incoming ref (AC1/AC4 idle-reset regression)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    writeGroupClaudeMd('idlegroup', 'Idle-reset rule content.');

    // Turn 1: new open.
    const result1 = await runtime.runTurn(
      runCtx('idlegroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    const sessionId1 = result1.sessionRef?.session_id as string;
    expect(sessionId1).toMatch(UUID_PATTERN);

    // Turn 2: resumed — no injection (proven in the test above; here it
    // just advances the lifecycle).
    harness.captured.length = 0;
    await runtime.runTurn(
      runCtx('idlegroup'),
      { backend: 'deus-native', session_id: sessionId1 },
      () => {},
    );
    expect(harness.captured.every((c) => msgText(c.systemMessage) === '')).toBe(
      true,
    );

    // Turn 3: message-orchestrator.ts's idle reset cleared the persisted
    // session (sessionRef = undefined -> defaultSession('', ...)), so the
    // incoming id is empty again. The session-open message must inject
    // AGAIN, with a NEW, DIFFERENT id minted.
    harness.captured.length = 0;
    const result3 = await runtime.runTurn(
      runCtx('idlegroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toContain('Idle-reset rule content.');
    }
    const sessionId3 = result3.sessionRef?.session_id;
    expect(sessionId3).toMatch(UUID_PATTERN);
    expect(sessionId3).not.toBe(sessionId1);
  });

  it('keeps the session-open content on EVERY model call within one turn, including after a tool call (AC4 within-turn persistence)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    writeGroupClaudeMd('persistgroup', 'Within-turn persistence rule.');
    harness.tools = [ECHO_TOOL];
    harness.makeModel = toolCallThenAnswerModel;

    const result = await runtime.runTurn(
      runCtx('persistgroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result.status).toBe('success');

    // Two model-decision cycles: the tool-call decision AND the final
    // answer after the tool result. AgentNode resets the system message at
    // the start of each cycle, so BOTH firings must carry the injection —
    // a first-firing-only gate would leave the second one empty.
    expect(harness.captured).toHaveLength(2);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toContain(
        'Within-turn persistence rule.',
      );
    }
  });
});

// ---------------------------------------------------------------------------
// LIA-416 (D2) — literal AC1/AC3/AC4 for the vault-context surface, through
// REAL runTurn calls (the mocked loadVaultContext from './vault-context.js'
// above, driven by harness.vaultContent — never the real vault/config/DB/
// subprocess).
// ---------------------------------------------------------------------------
describe('runTurn vault-context session-open lifecycle (LIA-416 AC1/AC3/AC4)', () => {
  it('invokes the vault surface once on a new control-group session and includes its content before the model call (AC1)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.vaultContent = '=== VAULT: CLAUDE.md ===\nVault identity content.';

    const result = await runtime.runTurn(
      runCtx('vaultgroup', true),
      defaultSession('', 'deus-native'),
      () => {},
    );

    expect(result.status).toBe('success');
    expect(vi.mocked(loadVaultContext)).toHaveBeenCalledTimes(1);
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).toContain('Vault identity content.');
    }
  });

  it('does not re-invoke the vault surface or re-inject its content on the resumed turn (AC4)', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.vaultContent = '=== VAULT: CLAUDE.md ===\nVault identity content.';

    const result1 = await runtime.runTurn(
      runCtx('vaultgroup', true),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(vi.mocked(loadVaultContext)).toHaveBeenCalledTimes(1);
    const sessionId1 = result1.sessionRef?.session_id;
    expect(sessionId1).toBeTruthy();

    harness.captured.length = 0;
    const result2 = await runtime.runTurn(
      runCtx('vaultgroup', true),
      { backend: 'deus-native', session_id: sessionId1 as string },
      () => {},
    );

    expect(result2.status).toBe('success');
    // Literal once-per-session gating (AC3): the resumed turn must NOT call
    // the facade again.
    expect(vi.mocked(loadVaultContext)).toHaveBeenCalledTimes(1);
    expect(harness.captured.length).toBeGreaterThan(0);
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).not.toContain(
        'Vault identity content.',
      );
    }
  });

  it('delegates to the real (ineligible) facade for a non-control group and injects no vault content', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.vaultContent = '=== VAULT: CLAUDE.md ===\nVault identity content.';

    const result = await runtime.runTurn(
      runCtx('nonvaultgroup', false),
      defaultSession('', 'deus-native'),
      () => {},
    );

    expect(result.status).toBe('success');
    // The mock still records the call (it delegates to the real early-exit
    // for non-control groups), but the delegated real facade must report
    // ineligibility rather than the harness's fixture content leaking in.
    for (const call of harness.captured) {
      expect(msgText(call.systemMessage)).not.toContain(
        'Vault identity content.',
      );
    }
  });
});

// ---------------------------------------------------------------------------
// AC2 — prompt middleware fires (and records) once per submitted prompt /
// model-decision cycle, before the model call.
// ---------------------------------------------------------------------------
describe('prompt lifecycle observation (AC2)', () => {
  it('accumulates one PromptEventRecord per beforeModel firing', async () => {
    const records: PromptEventRecord[] = [];
    const hook = buildPromptLifecycleHook(undefined, records);

    harness.makeModel = null; // use the model passed to createAgent directly
    const agent = createAgent({
      model: toolCallThenAnswerModel(),
      tools: [ECHO_TOOL],
      middleware: [hook],
    });
    await agent.invoke({
      messages: [{ role: 'user', content: 'call the echo tool once' }],
    });

    // Two model cycles => two records — per-model-call-cycle cardinality,
    // not once-per-turn.
    expect(records).toHaveLength(2);
    expect(records[0].promptLength).toBe('call the echo tool once'.length);
    for (const record of records) {
      expect(typeof record.timestamp).toBe('number');
      expect(record.promptLength).toBeGreaterThanOrEqual(0);
    }
  });
});

// ---------------------------------------------------------------------------
// AC3 — turn-complete fires exactly once per successful runTurn (regression
// test for already-shipped B1 behavior, not new production code).
// ---------------------------------------------------------------------------
describe('turn-complete lifecycle event (AC3)', () => {
  it('emits exactly one turn_complete RuntimeEvent per successful runTurn call', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    harness.tools = [ECHO_TOOL];
    harness.makeModel = toolCallThenAnswerModel;

    const events: RuntimeEvent[] = [];
    const result = await runtime.runTurn(
      runCtx('turncompletegroup'),
      defaultSession('', 'deus-native'),
      (event) => {
        events.push(event);
      },
    );

    expect(result.status).toBe('success');
    expect(events.filter((e) => e.type === 'turn_complete')).toHaveLength(1);
    // The scripted tool call and the answer both surfaced, and the terminal
    // event ordering held (turn_complete is the LAST event).
    expect(events.some((e) => e.type === 'tool_call')).toBe(true);
    expect(events[events.length - 1]?.type).toBe('turn_complete');
  });
});

// ---------------------------------------------------------------------------
// AC5 — loadSessionOpenContext event-context availability against a temp
// fixture groups/ dir.
// ---------------------------------------------------------------------------
describe('loadSessionOpenContext (AC5)', () => {
  it('loads a present group CLAUDE.md and records it', async () => {
    writeGroupClaudeMd('groupa', 'Rule A content.');
    const { systemMessage, record } = await loadSessionOpenContext(
      runCtx('groupa'),
    );
    expect(systemMessage).toContain('Rule A content.');
    expect(record.sessionOpened).toBe(true);
    expect(record.groupClaudeMdLoaded).toBe(true);
    expect(record.globalClaudeMdLoaded).toBe(false);
    expect(typeof record.timestamp).toBe('number');
  });

  it('returns undefined cleanly (no throw) when no CLAUDE.md exists', async () => {
    const { systemMessage, record } = await loadSessionOpenContext(
      runCtx('missinggroup'),
    );
    expect(systemMessage).toBeUndefined();
    expect(record.groupClaudeMdLoaded).toBe(false);
    expect(record.globalClaudeMdLoaded).toBe(false);
  });

  it('includes global CLAUDE.md for a non-main group (matching the container path’s main-vs-other mounting distinction)', async () => {
    writeGroupClaudeMd('groupb', 'Group B rules.');
    writeGlobalClaudeMd('Global shared rules.');
    const { systemMessage, record } = await loadSessionOpenContext(
      runCtx('groupb'),
    );
    expect(systemMessage).toContain('Global shared rules.');
    expect(systemMessage).toContain('Group B rules.');
    expect(record.globalClaudeMdLoaded).toBe(true);
    expect(record.groupClaudeMdLoaded).toBe(true);
  });

  it('EXCLUDES global CLAUDE.md for the main/control group', async () => {
    writeGroupClaudeMd('maingroup', 'Main group rules.');
    writeGlobalClaudeMd('Global shared rules.');
    const { systemMessage, record } = await loadSessionOpenContext(
      runCtx('maingroup', true),
    );
    expect(systemMessage).toContain('Main group rules.');
    expect(systemMessage).not.toContain('Global shared rules.');
    expect(record.globalClaudeMdLoaded).toBe(false);
  });

  it('global-only content still produces a session-open message for a non-main group', async () => {
    writeGlobalClaudeMd('Global-only rules.');
    const { systemMessage, record } = await loadSessionOpenContext(
      runCtx('groupc'),
    );
    expect(systemMessage).toContain('Global-only rules.');
    expect(record.globalClaudeMdLoaded).toBe(true);
    expect(record.groupClaudeMdLoaded).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// db.setSession dedup regression — the session-ID minting logic in isolation
// (echo-back keeps db.setSession's same-id "touch, don't insert" dedup to one
// row per real session-open; mint-fresh only on a genuine new open).
// ---------------------------------------------------------------------------
describe('session-ID minting strategy (db.setSession dedup regression)', () => {
  it('mints a fresh, non-empty id for an empty incoming session_id', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    const result = await runtime.runTurn(
      runCtx('mintgroup'),
      defaultSession('', 'deus-native'),
      () => {},
    );
    expect(result.status).toBe('success');
    expect(result.sessionRef?.backend).toBe('deus-native');
    expect(result.sessionRef?.session_id).toMatch(UUID_PATTERN);
  });

  it('echoes a non-empty incoming session_id back EXACTLY', async () => {
    const runtime = createDeusNativeRuntime(stubDeps);
    const incoming: RuntimeSession = {
      backend: 'deus-native',
      session_id: 'existing-session-id-123',
    };
    const result = await runtime.runTurn(
      runCtx('mintgroup'),
      incoming,
      () => {},
    );
    expect(result.status).toBe('success');
    expect(result.sessionRef?.session_id).toBe('existing-session-id-123');
  });
});

// ---------------------------------------------------------------------------
// task-scheduler.ts isNewSession-equivalent lookup — proves the sessionRef
// deus-native-backend.ts reads is correct where task-scheduler.ts builds it,
// not just in message-orchestrator.ts. Drives the REAL scheduler loop with a
// stub deus-native runtime that captures the sessionRef passed to runTurn.
// ---------------------------------------------------------------------------
describe('task-scheduler session lookup for deus-native', () => {
  type RunTurnFn = (
    ctx: RunContext,
    session: RuntimeSession,
    sink: RuntimeEventSink,
  ) => Promise<RunResult>;

  function makeDeusNativeRegistry(runTurn: RunTurnFn): RuntimeRegistry {
    const registry = new RuntimeRegistry();
    registry.register({
      name: () => 'deus-native' as const,
      capabilities: () => ({
        shell: false,
        filesystem: false,
        web: true,
        multimodal: false,
        handoffs: false,
        persistent_sessions: false,
        tool_streaming: false,
      }),
      startOrResume: async () => ({
        backend: 'deus-native' as const,
        session_id: '',
      }),
      runTurn,
      close: async () => {},
    });
    return registry;
  }

  beforeEach(async () => {
    const { _initTestDatabase } = await import('../db.js');
    const { _resetSchedulerLoopForTests } =
      await import('../task-scheduler.js');
    _initTestDatabase();
    _resetSchedulerLoopForTests();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function runSchedulerOnce(options: {
    getSession?: (
      groupFolder: string,
      backend?: RuntimeSession['backend'],
    ) => RuntimeSession | undefined;
  }): Promise<RuntimeSession[]> {
    const { createTask } = await import('../db.js');
    const { startSchedulerLoop } = await import('../task-scheduler.js');

    createTask({
      id: 'task-deus-native',
      group_folder: 'schedgroup',
      chat_jid: 'sched@g.us',
      prompt: 'scheduled hello',
      schedule_type: 'once',
      schedule_value: '2026-01-01T00:00:00.000Z',
      context_mode: 'group',
      agent_backend: 'deus-native',
      next_run: new Date(Date.now() - 60_000).toISOString(),
      status: 'active',
      created_at: '2026-01-01T00:00:00.000Z',
    });

    const capturedSessions: RuntimeSession[] = [];
    const runTurn: RunTurnFn = async (_ctx, session, sink) => {
      capturedSessions.push(session);
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    startSchedulerLoop({
      registeredGroups: () => ({
        'sched@g.us': {
          name: 'Sched Group',
          folder: 'schedgroup',
          trigger: 'sched',
          added_at: '2026-01-01T00:00:00.000Z',
        },
      }),
      getSessions: () => ({}),
      getSession: options.getSession,
      registry: makeDeusNativeRegistry(runTurn),
      queue: {
        enqueueTask: (
          _jid: string,
          _taskId: string,
          fn: () => Promise<void>,
        ) => {
          void fn();
        },
        notifyIdle: () => {},
        closeStdin: () => {},
      } as never,
      sendMessage: async () => {},
    });
    await vi.advanceTimersByTimeAsync(10);
    return capturedSessions;
  }

  it('passes an EMPTY session_id when no same-backend session exists (isNewSession-equivalent: new)', async () => {
    const captured = await runSchedulerOnce({});
    expect(captured).toHaveLength(1);
    expect(captured[0].backend).toBe('deus-native');
    expect(captured[0].session_id).toBe('');
  });

  it('passes the persisted NON-EMPTY session_id when one exists (isNewSession-equivalent: resumed)', async () => {
    const captured = await runSchedulerOnce({
      getSession: () => ({
        backend: 'deus-native',
        session_id: 'persisted-deus-native-session',
      }),
    });
    expect(captured).toHaveLength(1);
    expect(captured[0].session_id).toBe('persisted-deus-native-session');
  });
});
