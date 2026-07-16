/**
 * Hermetic subprocess-boundary tests for the memory retrieval adapter
 * (LIA-415 / D1). `child_process.spawn` is mocked — no real Python, vault,
 * or Ollama is touched — so what these tests pin down is the EXACT process
 * contract: interpreter, script path, cwd, timeout, stdin payload shape,
 * stdout extraction, and the fail-open empty/error paths (AC1/AC2/AC4).
 */

import { EventEmitter } from 'events';
import path from 'path';

import { describe, it, expect, vi, beforeEach } from 'vitest';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));

vi.mock('child_process', () => ({ spawn: spawnMock }));

const {
  retrieveMemoryContext,
  MEMORY_RETRIEVAL_HOOK_PATH,
  MEMORY_RETRIEVAL_TIMEOUT_MS,
} = await import('./memory-retrieval.js');
const { PROJECT_ROOT } = await import('../config.js');
const { PYTHON_BIN } = await import('../platform.js');

/** Minimal fake of the child-process surface the adapter touches. */
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
  stdout = new EventEmitter();
}

/** Generic fixtures only — no personal identifiers (repo-public rule). */
const REQUEST = {
  prompt: 'a generic question about the test fixture topic',
  sessionId: 'session-fixture-1',
};

const HOOK_OUTPUT_CONTEXT = '=== recalled fixture context ===';
const VALID_HOOK_OUTPUT = JSON.stringify({
  hookSpecificOutput: {
    hookEventName: 'UserPromptSubmit',
    additionalContext: HOOK_OUTPUT_CONTEXT,
  },
});

describe('retrieveMemoryContext — process launch contract (AC1/AC2)', () => {
  let child: FakeChildProcess;

  beforeEach(() => {
    spawnMock.mockReset();
    child = new FakeChildProcess();
    spawnMock.mockReturnValue(child);
  });

  it('launches PYTHON_BIN directly on the unchanged hook script, from the repo root, with the five-second bound', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);

    expect(spawnMock).toHaveBeenCalledTimes(1);
    expect(spawnMock).toHaveBeenCalledWith(
      PYTHON_BIN,
      [MEMORY_RETRIEVAL_HOOK_PATH],
      {
        cwd: PROJECT_ROOT,
        stdio: ['pipe', 'pipe', 'ignore'],
        timeout: MEMORY_RETRIEVAL_TIMEOUT_MS,
      },
    );
    // The exact script — not a copy, wrapper, or reimplementation.
    expect(MEMORY_RETRIEVAL_HOOK_PATH).toBe(
      path.join(PROJECT_ROOT, 'scripts', 'memory_retrieval_hook.py'),
    );
    expect(MEMORY_RETRIEVAL_TIMEOUT_MS).toBe(5_000);

    child.stdout.emit('data', VALID_HOOK_OUTPUT);
    child.emit('close', 0);
    await resultPromise;
  });

  it('writes exactly the hook stdin contract — a JSON object with prompt and session_id — and closes stdin', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);

    expect(child.stdin.written).toBe(
      JSON.stringify({
        prompt: REQUEST.prompt,
        session_id: REQUEST.sessionId,
      }),
    );
    expect(child.stdin.ended).toBe(true);

    child.emit('close', 0);
    await resultPromise;
  });

  it('extracts hookSpecificOutput.additionalContext from valid hook output', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    // Chunked stdout — the adapter must accumulate, not parse per-chunk.
    child.stdout.emit('data', VALID_HOOK_OUTPUT.slice(0, 10));
    child.stdout.emit('data', VALID_HOOK_OUTPUT.slice(10));
    child.emit('close', 0);
    await expect(resultPromise).resolves.toBe(HOOK_OUTPUT_CONTEXT);
  });
});

describe('retrieveMemoryContext — fail-open empty/error paths (AC4)', () => {
  let child: FakeChildProcess;

  beforeEach(() => {
    spawnMock.mockReset();
    child = new FakeChildProcess();
    spawnMock.mockReturnValue(child);
  });

  it('empty stdout (the hook prints nothing on short prompts / abstained recall) resolves ""', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.emit('close', 0);
    await expect(resultPromise).resolves.toBe('');
  });

  it('malformed (non-JSON) stdout resolves ""', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.stdout.emit('data', 'not json at all {');
    child.emit('close', 0);
    await expect(resultPromise).resolves.toBe('');
  });

  it('valid JSON missing additionalContext (or with a non-string value) resolves ""', async () => {
    const missing = retrieveMemoryContext(REQUEST);
    child.stdout.emit('data', JSON.stringify({ hookSpecificOutput: {} }));
    child.emit('close', 0);
    await expect(missing).resolves.toBe('');

    const secondChild = new FakeChildProcess();
    spawnMock.mockReturnValue(secondChild);
    const nonString = retrieveMemoryContext(REQUEST);
    secondChild.stdout.emit(
      'data',
      JSON.stringify({ hookSpecificOutput: { additionalContext: 42 } }),
    );
    secondChild.emit('close', 0);
    await expect(nonString).resolves.toBe('');
  });

  it('a non-zero exit code resolves "" even when stdout looks valid (process failure wins)', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.stdout.emit('data', VALID_HOOK_OUTPUT);
    child.emit('close', 1);
    await expect(resultPromise).resolves.toBe('');
  });

  it('a timeout kill (close with null code) resolves ""', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.emit('close', null);
    await expect(resultPromise).resolves.toBe('');
  });

  it('a child "error" event (interpreter not found) resolves "" without rejecting', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.emit('error', new Error('spawn python3 ENOENT'));
    await expect(resultPromise).resolves.toBe('');
  });

  it('a synchronous spawn throw resolves "" without rejecting', async () => {
    spawnMock.mockImplementation(() => {
      throw new Error('spawn failed synchronously');
    });
    await expect(retrieveMemoryContext(REQUEST)).resolves.toBe('');
  });

  it('an "error" followed by a later "close" still resolves exactly once (first signal wins)', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.emit('error', new Error('boom'));
    child.emit('close', 0);
    await expect(resultPromise).resolves.toBe('');
  });

  it('a stdin EPIPE (child died before consuming input) resolves ""', async () => {
    const resultPromise = retrieveMemoryContext(REQUEST);
    child.stdin.emit('error', new Error('write EPIPE'));
    await expect(resultPromise).resolves.toBe('');
  });
});
