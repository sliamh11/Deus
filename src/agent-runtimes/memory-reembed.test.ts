/**
 * Hermetic subprocess-boundary tests for edit-triggered memory re-embedding
 * (LIA-417 / D3). The child process is fully mocked: no Python, vault,
 * memory-tree database, or embedding provider is touched.
 */

import { EventEmitter } from 'events';
import path from 'path';

import { beforeEach, describe, expect, it, vi } from 'vitest';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));

vi.mock('child_process', () => ({ spawn: spawnMock }));

const {
  triggerMemoryReembed,
  MEMORY_REEMBED_HOOK_PATH,
  MEMORY_REEMBED_TIMEOUT_MS,
} = await import('./memory-reembed.js');
const { PROJECT_ROOT } = await import('../config.js');
const { PYTHON_BIN } = await import('../platform.js');

class FakeStdin extends EventEmitter {
  written = '';
  ended = false;

  end(chunk?: string): void {
    if (chunk !== undefined) this.written += chunk;
    this.ended = true;
  }
}

class FakeChildProcess extends EventEmitter {
  stdin = new FakeStdin();
}

const REQUEST = {
  toolName: 'Edit' as const,
  filePath: '/workspace/vault/example.md',
};

describe('triggerMemoryReembed — process launch and payload contract', () => {
  let child: FakeChildProcess;

  beforeEach(() => {
    spawnMock.mockReset();
    child = new FakeChildProcess();
    spawnMock.mockReturnValue(child);
  });

  it('launches PYTHON_BIN directly on memory_tree_hook.py from the repo root with the generous five-second bound', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);

    expect(spawnMock).toHaveBeenCalledTimes(1);
    expect(spawnMock).toHaveBeenCalledWith(
      PYTHON_BIN,
      [MEMORY_REEMBED_HOOK_PATH],
      {
        cwd: PROJECT_ROOT,
        stdio: ['pipe', 'ignore', 'ignore'],
        timeout: MEMORY_REEMBED_TIMEOUT_MS,
      },
    );
    expect(MEMORY_REEMBED_HOOK_PATH).toBe(
      path.join(PROJECT_ROOT, 'scripts', 'memory_tree_hook.py'),
    );
    expect(MEMORY_REEMBED_TIMEOUT_MS).toBe(5_000);

    child.emit('close', 0);
    await expect(resultPromise).resolves.toBeUndefined();
  });

  it('writes exactly one canonical PostToolUse JSON payload and closes stdin', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);

    expect(child.stdin.written).toBe(
      JSON.stringify({
        hook_event_name: 'PostToolUse',
        tool_name: 'Edit',
        tool_input: {
          file_path: '/workspace/vault/example.md',
        },
      }),
    );
    expect(child.stdin.ended).toBe(true);

    child.emit('close', 0);
    await expect(resultPromise).resolves.toBeUndefined();
  });
});

describe('triggerMemoryReembed — fail-open terminal paths', () => {
  let child: FakeChildProcess;

  beforeEach(() => {
    spawnMock.mockReset();
    child = new FakeChildProcess();
    spawnMock.mockReturnValue(child);
  });

  it('resolves on a non-zero exit', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);
    child.emit('close', 1);
    await expect(resultPromise).resolves.toBeUndefined();
  });

  it('resolves on a timeout/null close code', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);
    child.emit('close', null);
    await expect(resultPromise).resolves.toBeUndefined();
  });

  it('resolves on a child error', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);
    child.emit('error', new Error('spawn python3 ENOENT'));
    await expect(resultPromise).resolves.toBeUndefined();
  });

  it('resolves on a synchronous spawn throw', async () => {
    spawnMock.mockImplementation(() => {
      throw new Error('spawn failed synchronously');
    });
    await expect(triggerMemoryReembed(REQUEST)).resolves.toBeUndefined();
  });

  it('resolves on stdin EPIPE', async () => {
    const resultPromise = triggerMemoryReembed(REQUEST);
    child.stdin.emit('error', new Error('write EPIPE'));
    await expect(resultPromise).resolves.toBeUndefined();
  });

  it('multiple terminal events settle the adapter only once', async () => {
    const onSettled = vi.fn();
    const resultPromise = triggerMemoryReembed(REQUEST).then(onSettled);

    child.emit('error', new Error('spawn failure'));
    child.stdin.emit('error', new Error('write EPIPE'));
    child.emit('close', 1);

    await resultPromise;
    expect(onSettled).toHaveBeenCalledTimes(1);
  });
});
