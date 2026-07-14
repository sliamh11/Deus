import { EventEmitter } from 'node:events';
import fs from 'node:fs';

import { createAgent, FakeToolCallingModel } from 'langchain';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createWardenGateMiddleware,
  invokeWardenGate,
  makeScratchEditTool,
  markScratchRepoShip,
  setupScratchRepo,
  teardownScratchRepo,
  type WardenGateDecision,
} from './lia395_warden_veto_walking_skeleton.js';

/** Minimal fake ChildProcess for invokeWardenGate's protocol-fault tests. */
function fakeChild() {
  const child = new EventEmitter() as EventEmitter & {
    stdin: EventEmitter & { write: (v: unknown) => boolean; end: () => void };
    stdout: EventEmitter;
    stderr: EventEmitter;
    kill: () => void;
  };
  child.stdin = Object.assign(new EventEmitter(), {
    write: () => true,
    end: () => {},
  });
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = vi.fn();
  return child;
}

const scratchRepos: string[] = [];
function newScratchRepo() {
  const repo = setupScratchRepo();
  scratchRepos.push(repo.scratchRepoRoot);
  return repo;
}

afterEach(() => {
  while (scratchRepos.length) {
    const dir = scratchRepos.pop();
    if (dir) teardownScratchRepo(dir);
  }
});

describe('invokeWardenGate — real gate, scratch repo', () => {
  it('denies with the real gate reason when no marker is present', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    const decision = await invokeWardenGate(
      'plan-review-gate',
      {
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      },
      scratchRepoRoot,
    );
    expect(decision.decision).toBe('deny');
    expect((decision as { reason: string }).reason.length).toBeGreaterThan(0);
  });

  it('allows after the scratch repo is marked SHIP via the real mark subcommand', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    expect(() => markScratchRepoShip(scratchRepoRoot)).not.toThrow();
    const decision = await invokeWardenGate(
      'plan-review-gate',
      {
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      },
      scratchRepoRoot,
    );
    expect(decision).toEqual<WardenGateDecision>({ decision: 'allow' });
  });
});

describe('invokeWardenGate — subprocess/protocol fail-closed', () => {
  it('hard-errors on nonzero exit with empty stdout', async () => {
    const spawnFn = vi.fn(() => {
      const child = fakeChild();
      queueMicrotask(() => child.emit('close', 1, null));
      return child as never;
    });
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', { spawnFn }),
    ).rejects.toThrow(/exited with code 1/);
  });

  it('hard-errors on spawn failure', async () => {
    const spawnFn = vi.fn(() => {
      const child = fakeChild();
      queueMicrotask(() => child.emit('error', new Error('ENOENT')));
      return child as never;
    });
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', { spawnFn }),
    ).rejects.toThrow(/failed to spawn/);
  });

  it('hard-errors on a signal-terminated child (distinct from nonzero exit)', async () => {
    const spawnFn = vi.fn(() => {
      const child = fakeChild();
      queueMicrotask(() => child.emit('close', null, 'SIGKILL'));
      return child as never;
    });
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', { spawnFn }),
    ).rejects.toThrow(/killed by signal SIGKILL/);
  });

  it('hard-errors on timeout instead of waiting the production duration', async () => {
    const spawnFn = vi.fn(() => fakeChild() as never); // never emits close/error
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', {
        spawnFn,
        timeoutMs: 50,
      }),
    ).rejects.toThrow(/timed out after 50ms/);
  });

  it('hard-errors on malformed (non-JSON) stdout', async () => {
    const spawnFn = vi.fn(() => {
      const child = fakeChild();
      queueMicrotask(() => {
        child.stdout.emit('data', Buffer.from('not json'));
        child.emit('close', 0, null);
      });
      return child as never;
    });
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', { spawnFn }),
    ).rejects.toThrow(/non-empty, non-JSON stdout/);
  });

  it('hard-errors on a deny shape with a missing/empty reason (never allow, never empty-reason deny)', async () => {
    const spawnFn = vi.fn(() => {
      const child = fakeChild();
      queueMicrotask(() => {
        child.stdout.emit(
          'data',
          Buffer.from(
            JSON.stringify({
              hookSpecificOutput: {
                hookEventName: 'PreToolUse',
                permissionDecision: 'deny',
                permissionDecisionReason: '',
              },
            }),
          ),
        );
        child.emit('close', 0, null);
      });
      return child as never;
    });
    await expect(
      invokeWardenGate('plan-review-gate', {}, '/tmp/whatever', { spawnFn }),
    ).rejects.toThrow(/unrecognized stdout shape/);
  });
});

describe('createWardenGateMiddleware — blocked/allowed paths, real gate', () => {
  it('blocked path: wrapped tool execute never called, denied ToolMessage carries the real reason', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    const wrapped = makeScratchEditTool(scratchFilePath);
    const executeSpy = vi.spyOn(wrapped, 'invoke');

    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      () => ({
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      }),
      scratchRepoRoot,
    );

    const agent = createAgent({
      model: new FakeToolCallingModel({
        toolCalls: [
          [{ name: 'edit_scratch', args: { content: 'hello' }, id: 'call_1' }],
          [],
        ],
      }),
      tools: [wrapped],
      middleware: [middleware],
    });

    const result = await agent.invoke({
      messages: [{ role: 'user', content: 'write hello to the scratch file' }],
    });

    expect(executeSpy).not.toHaveBeenCalled();
    expect(fs.existsSync(scratchFilePath)).toBe(false);
    const denyMessage = result.messages.find(
      (m) => (m as { name?: string }).name === 'edit_scratch',
    ) as { content?: unknown; tool_call_id?: string } | undefined;
    expect(denyMessage).toBeDefined();
    expect(String(denyMessage?.content).length).toBeGreaterThan(0);
    expect(denyMessage?.tool_call_id).toBe('call_1');
  });

  it('allowed path: wrapped tool execute called exactly once, file written', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    markScratchRepoShip(scratchRepoRoot);
    const wrapped = makeScratchEditTool(scratchFilePath);
    const executeSpy = vi.spyOn(wrapped, 'invoke');

    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      () => ({
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      }),
      scratchRepoRoot,
    );

    const agent = createAgent({
      model: new FakeToolCallingModel({
        toolCalls: [
          [{ name: 'edit_scratch', args: { content: 'hello' }, id: 'call_1' }],
          [],
        ],
      }),
      tools: [wrapped],
      middleware: [middleware],
    });

    await agent.invoke({
      messages: [{ role: 'user', content: 'write hello to the scratch file' }],
    });

    expect(executeSpy).toHaveBeenCalledTimes(1);
    expect(fs.readFileSync(scratchFilePath, 'utf8')).toBe('hello');
  });

  it('negative test: a forged extra path argument has zero effect on the write target', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    markScratchRepoShip(scratchRepoRoot);
    const forgedPath = `${scratchRepoRoot}/forged.txt`;
    const wrapped = makeScratchEditTool(scratchFilePath);

    // The tool's `additionalProperties: false` schema means LangChain's own
    // validation rejects a forged extra path/file_path property BEFORE the
    // call ever reaches our execute function — an even stronger proof of
    // "no argument surface for redirection" than merely ignoring it would
    // have been. Confirmed empirically (not assumed): this throws.
    await expect(
      wrapped.invoke({
        content: 'hello',
        path: forgedPath,
        file_path: forgedPath,
      } as never),
    ).rejects.toThrow();

    expect(fs.existsSync(forgedPath)).toBe(false);
    expect(fs.existsSync(scratchFilePath)).toBe(false);
  });

  it('missing/empty tool-call ID rejects before the gate is ever invoked', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    const wrapped = makeScratchEditTool(scratchFilePath);
    const executeSpy = vi.spyOn(wrapped, 'invoke');
    const invokeGate = vi.fn();

    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      () => ({
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      }),
      scratchRepoRoot,
      invokeGate,
    );
    const handler = vi.fn();
    const wrapToolCall = (
      middleware as unknown as {
        wrapToolCall: (req: unknown, h: unknown) => Promise<unknown>;
      }
    ).wrapToolCall;

    await expect(
      wrapToolCall(
        { toolCall: { name: 'edit_scratch', args: {}, id: '' } },
        handler,
      ),
    ).rejects.toThrow(/no tool-call ID/);

    expect(invokeGate).not.toHaveBeenCalled();
    expect(executeSpy).not.toHaveBeenCalled();
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('model-visible-feedback — FakeToolCallingModel, deterministic', () => {
  it('the model actually receives the denied ToolMessage on its second invocation', async () => {
    const { scratchRepoRoot, scratchFilePath } = newScratchRepo();
    const wrapped = makeScratchEditTool(scratchFilePath);
    const executeSpy = vi.spyOn(wrapped, 'invoke');
    const generateSpy = vi.spyOn(
      FakeToolCallingModel.prototype,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      '_generate' as any,
    );

    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      () => ({
        tool_name: 'Edit',
        tool_input: { file_path: scratchFilePath },
        cwd: scratchRepoRoot,
      }),
      scratchRepoRoot,
    );

    const agent = createAgent({
      model: new FakeToolCallingModel({
        toolCalls: [
          [{ name: 'edit_scratch', args: { content: 'hello' }, id: 'call_1' }],
          [],
        ],
      }),
      tools: [wrapped],
      middleware: [middleware],
    });

    const result = await agent.invoke({
      messages: [{ role: 'user', content: 'write hello to the scratch file' }],
    });

    expect(executeSpy).not.toHaveBeenCalled();
    expect(generateSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    const secondInvocationMessages = generateSpy.mock.calls[1]?.[0] as Array<{
      name?: string;
      content?: unknown;
      tool_call_id?: string;
    }>;
    const forwardedDenyMessage = secondInvocationMessages.find(
      (m) => m.name === 'edit_scratch',
    );
    expect(forwardedDenyMessage).toBeDefined();
    expect(forwardedDenyMessage?.tool_call_id).toBe('call_1');
    expect(String(forwardedDenyMessage?.content).length).toBeGreaterThan(0);

    const finalDenyMessage = result.messages.find(
      (m) => (m as { name?: string }).name === 'edit_scratch',
    ) as { content?: unknown } | undefined;
    expect(finalDenyMessage?.content).toBe(forwardedDenyMessage?.content);

    generateSpy.mockRestore();
  });
});
