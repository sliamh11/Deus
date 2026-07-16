/**
 * Server tests for LIA-428 / G1 — request validation, turn serialization,
 * NDJSON event delivery, status/close routes, and the db-backed session
 * store adapter. Real ephemeral HTTP server on 127.0.0.1, fake runtime,
 * temp discovery paths. (The auth/discovery security boundary itself is
 * pinned by deus-native-chat-server.oracle.test.ts.)
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import type {
  AgentRuntime,
  RunContext,
  RuntimeEventSink,
  RuntimeSession,
} from '../agent-runtimes/types.js';
import { _initTestDatabase, getSession } from '../db.js';
import {
  NATIVE_CHAT_PROTOCOL_VERSION,
  CLI_CHAT_GROUP_FOLDER,
  type NativeChatSessionStore,
} from './deus-native-chat.js';
import {
  startNativeChatServer,
  createDbSessionStore,
  type NativeChatServerHandle,
} from './deus-native-chat-server.js';

const MINTED_ID = '33333333-3333-4333-8333-333333333333';

function memoryStore(): NativeChatSessionStore {
  const rows = new Map<string, RuntimeSession>();
  return {
    get: (groupFolder, backend) => rows.get(`${groupFolder} ${backend}`),
    set: (groupFolder, session) =>
      rows.set(`${groupFolder} ${session.backend}`, session),
  };
}

interface FakeRuntimeControls {
  runtime: AgentRuntime;
  turnCalls: Array<{ prompt: string; cwd: string | undefined }>;
  /** When set, runTurn blocks until released (for the concurrency test). */
  block?: Promise<void>;
}

function fakeRuntime(): FakeRuntimeControls {
  const controls: FakeRuntimeControls = {
    turnCalls: [],
    runtime: {
      name: () => 'deus-native' as const,
      capabilities: () => ({
        shell: false,
        filesystem: false,
        web: true,
        multimodal: false,
        handoffs: false,
        persistent_sessions: true,
        tool_streaming: false,
      }),
      startOrResume: async () => ({
        backend: 'deus-native' as const,
        session_id: '',
      }),
      runTurn: async (
        runContext: RunContext,
        _sessionRef: RuntimeSession,
        eventSink: RuntimeEventSink,
      ) => {
        controls.turnCalls.push({
          prompt: runContext.prompt,
          cwd: runContext.cwd,
        });
        if (controls.block) await controls.block;
        await eventSink({
          type: 'tool_call',
          name: 'web_search',
          arguments: { query: 'test query' },
        });
        await eventSink({ type: 'output_text', text: 'the answer' });
        await eventSink({ type: 'turn_complete' });
        return {
          status: 'success' as const,
          result: 'the answer',
          sessionRef: {
            backend: 'deus-native' as const,
            session_id: MINTED_ID,
          },
        };
      },
      close: async () => {},
    },
  };
  return controls;
}

let tmpDir: string;
let handle: NativeChatServerHandle | undefined;
let controls: FakeRuntimeControls;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'native-chat-server-'));
  controls = fakeRuntime();
});

afterEach(async () => {
  if (handle) {
    await handle.close();
    handle = undefined;
  }
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

async function startServer(
  sessions: NativeChatSessionStore = memoryStore(),
): Promise<NativeChatServerHandle> {
  handle = await startNativeChatServer({
    registry: {
      get: (id) => (id === 'deus-native' ? controls.runtime : undefined),
    },
    sessions,
    discoveryPath: path.join(tmpDir, 'native-chat.json'),
  });
  return handle;
}

function authHeaders(server: NativeChatServerHandle): Record<string, string> {
  return { authorization: `Bearer ${server.token}` };
}

function turnRequest(
  server: NativeChatServerHandle,
  body: unknown,
): Promise<Response> {
  return fetch(`http://${server.host}:${server.port}/v1/native-chat/turn`, {
    method: 'POST',
    headers: { ...authHeaders(server), 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

function validTurnBody(prompt = 'hello'): Record<string, unknown> {
  return { version: NATIVE_CHAT_PROTOCOL_VERSION, prompt, cwd: tmpDir };
}

describe('routing and validation', () => {
  it('404s unknown paths and 405s wrong methods on the exact routes', async () => {
    const server = await startServer();
    const base = `http://${server.host}:${server.port}`;

    const unknown = await fetch(`${base}/v1/other`, {
      headers: authHeaders(server),
    });
    expect(unknown.status).toBe(404);

    const getTurn = await fetch(`${base}/v1/native-chat/turn`, {
      headers: authHeaders(server),
    });
    expect(getTurn.status).toBe(405);

    const postStatus = await fetch(`${base}/v1/native-chat/status`, {
      method: 'POST',
      headers: authHeaders(server),
    });
    expect(postStatus.status).toBe(405);
  });

  it('rejects a protocol-version-mismatched turn body with 400', async () => {
    const server = await startServer();
    const res = await turnRequest(server, {
      version: NATIVE_CHAT_PROTOCOL_VERSION + 1,
      prompt: 'hi',
      cwd: tmpDir,
    });
    expect(res.status).toBe(400);
    expect(controls.turnCalls).toHaveLength(0);
  });

  it('rejects malformed JSON, missing prompt, and missing cwd with 400', async () => {
    const server = await startServer();

    expect((await turnRequest(server, 'not json at all')).status).toBe(400);
    expect(
      (
        await turnRequest(server, {
          version: NATIVE_CHAT_PROTOCOL_VERSION,
          cwd: tmpDir,
        })
      ).status,
    ).toBe(400);
    expect(
      (
        await turnRequest(server, {
          version: NATIVE_CHAT_PROTOCOL_VERSION,
          prompt: '   ',
          cwd: tmpDir,
        })
      ).status,
    ).toBe(400);
    expect(
      (
        await turnRequest(server, {
          version: NATIVE_CHAT_PROTOCOL_VERSION,
          prompt: 'hi',
        })
      ).status,
    ).toBe(400);
    expect(controls.turnCalls).toHaveLength(0);
  });

  it('rejects an oversized body with 413', async () => {
    const server = await startServer();
    const res = await turnRequest(server, {
      ...validTurnBody('x'.repeat(300 * 1024)),
    });
    expect(res.status).toBe(413);
    expect(controls.turnCalls).toHaveLength(0);
  });

  it('rejects a competing concurrent turn with 409 instead of interleaving the thread', async () => {
    const server = await startServer();
    let release: () => void = () => {};
    controls.block = new Promise((resolve) => {
      release = resolve;
    });

    const first = turnRequest(server, validTurnBody('one'));
    // Give the first request time to reach the runtime and block.
    await new Promise((r) => setTimeout(r, 50));
    const second = await turnRequest(server, validTurnBody('two'));
    expect(second.status).toBe(409);

    release();
    const firstRes = await first;
    expect(firstRes.status).toBe(200);
    expect(controls.turnCalls).toHaveLength(1);
  });
});

describe('turn NDJSON delivery', () => {
  it('streams normalized display events and a final done record; no RuntimeEvent discriminants', async () => {
    const server = await startServer();
    const res = await turnRequest(server, validTurnBody('hello there'));
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('application/x-ndjson');

    const text = await res.text();
    const lines = text.trim().split('\n');
    const records = lines.map(
      (line) => JSON.parse(line) as Record<string, unknown>,
    );

    expect(records.at(-1)).toEqual({ done: true, ok: true });
    const kinds = records.slice(0, -1).map((r) => r.kind);
    expect(kinds).toEqual(['tool_use', 'assistant_text', 'assistant_done']);

    // The internal runtime union never crosses the wire.
    expect(text).not.toContain('"type"');
    expect(text).not.toContain('output_text');
    expect(text).not.toContain('turn_complete');
    expect(text).not.toContain('sessionRef');
    expect(text).not.toContain(MINTED_ID);
  });

  it('forwards the client cwd into the RunContext', async () => {
    const server = await startServer();
    await turnRequest(server, validTurnBody('hello'));
    expect(controls.turnCalls[0]?.cwd).toBe(tmpDir);
  });
});

describe('status and close routes', () => {
  it('status exposes backend/session/state/output; close calls through without clearing the store', async () => {
    const sessions = memoryStore();
    const server = await startServer(sessions);
    const base = `http://${server.host}:${server.port}`;

    const before = (await (
      await fetch(`${base}/v1/native-chat/status`, {
        headers: authHeaders(server),
      })
    ).json()) as Record<string, unknown>;
    expect(before.backend).toBe('deus-native');
    expect(before.sessionId).toBeUndefined();
    expect(before.state).toBe('new');
    expect(before.output).toBe('buffered');

    await turnRequest(server, validTurnBody('hello'));

    const after = (await (
      await fetch(`${base}/v1/native-chat/status`, {
        headers: authHeaders(server),
      })
    ).json()) as Record<string, unknown>;
    expect(after.sessionId).toBe(MINTED_ID);

    const close = await fetch(`${base}/v1/native-chat/close`, {
      method: 'POST',
      headers: authHeaders(server),
    });
    expect(close.status).toBe(200);
    // The stored session survives the close — resume depends on it.
    expect(sessions.get(CLI_CHAT_GROUP_FOLDER, 'deus-native')?.session_id).toBe(
      MINTED_ID,
    );
  });

  it('reports "resumed" when a stored row predates the server', async () => {
    const sessions = memoryStore();
    sessions.set(CLI_CHAT_GROUP_FOLDER, {
      backend: 'deus-native',
      session_id: MINTED_ID,
    });
    const server = await startServer(sessions);
    const status = (await (
      await fetch(
        `http://${server.host}:${server.port}/v1/native-chat/status`,
        {
          headers: authHeaders(server),
        },
      )
    ).json()) as Record<string, unknown>;
    expect(status.state).toBe('resumed');
    expect(status.sessionId).toBe(MINTED_ID);
  });

  it('refuses to start when the deus-native runtime is not registered', async () => {
    await expect(
      startNativeChatServer({
        registry: { get: () => undefined },
        sessions: memoryStore(),
        discoveryPath: path.join(tmpDir, 'native-chat.json'),
      }),
    ).rejects.toThrow(/deus-native/);
  });
});

describe('createDbSessionStore adapter', () => {
  it('round-trips a backend-scoped row through the real db layer', () => {
    _initTestDatabase();
    const store = createDbSessionStore();

    expect(store.get(CLI_CHAT_GROUP_FOLDER, 'deus-native')).toBeUndefined();
    store.set(CLI_CHAT_GROUP_FOLDER, {
      backend: 'deus-native',
      session_id: MINTED_ID,
    });
    expect(store.get(CLI_CHAT_GROUP_FOLDER, 'deus-native')?.session_id).toBe(
      MINTED_ID,
    );
    // Backend scoping is load-bearing: no claude row exists for the folder.
    expect(getSession(CLI_CHAT_GROUP_FOLDER, 'claude')).toBeUndefined();

    // Same-id rewrite is a touch, not a duplicate row.
    store.set(CLI_CHAT_GROUP_FOLDER, {
      backend: 'deus-native',
      session_id: MINTED_ID,
    });
    expect(store.get(CLI_CHAT_GROUP_FOLDER, 'deus-native')?.session_id).toBe(
      MINTED_ID,
    );
  });
});
