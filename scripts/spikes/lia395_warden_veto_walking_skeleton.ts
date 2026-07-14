/**
 * Spike (LIA-395 / A2): can LangChain JS's real `wrapToolCall` middleware
 * contract enforce an UNMODIFIED Deus warden gate (`scripts/codex_warden_hooks.py
 * run plan-review-gate`) — a denied call never reaches its handler, an allowed
 * call reaches it exactly once, and the model sees the gate's actual feedback
 * as a `ToolMessage`?
 *
 * This is a host-side-only spike, same convention as A1
 * (lia394_langchain_walking_skeleton.ts). Unlike A1, none of A2's core proof
 * needs a live model call — `invokeWardenGate`/`createWardenGateMiddleware`
 * are proven against the real (unmodified) gate script and a synthetic tool
 * in a throwaway scratch git repo, and the model-visible-feedback transport
 * is proven deterministically via LangChain's `FakeToolCallingModel`. See the
 * paired write-up (lia395_warden_veto_walking_skeleton.md) for the full
 * question/method/verdict.
 *
 * Cross-platform: PYTHON_BIN resolved via src/platform.ts, no shell-outs
 * beyond spawning the Python gate script itself.
 */

import { execFileSync } from 'node:child_process';
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ToolMessage } from '@langchain/core/messages';
import { tool, type StructuredTool } from '@langchain/core/tools';
import {
  createAgent,
  createMiddleware,
  FakeToolCallingModel,
  type AgentMiddleware,
  type ToolCallRequest,
} from 'langchain';

import { PYTHON_BIN } from '../../src/platform.js';

const WARDEN_SCRIPT = fileURLToPath(
  new URL('../../scripts/codex_warden_hooks.py', import.meta.url),
);
const DEFAULT_TIMEOUT_MS = 15_000;

// ── invokeWardenGate ────────────────────────────────────────────────────────

export interface WardenGateOptions {
  /** Injectable seam for tests — defaults to the real node:child_process.spawn. */
  spawnFn?: typeof spawn;
  /** Bounded wait before treating the gate subprocess as hung. */
  timeoutMs?: number;
}

export type WardenGateDecision =
  { decision: 'allow' } | { decision: 'deny'; reason: string };

/**
 * Fail-closed FOR SUBPROCESS/PROTOCOL FAILURES ONLY. The unmodified gate
 * script retains its own pre-existing fail-open paths (malformed stdin
 * becomes `{}`, failed git invocations return `None` and are subsequently
 * treated as permissive) — this wrapper cannot and does not paper over
 * those; they are a residual limitation of the reused, unmodified gate
 * script, recorded in the write-up rather than "fixed" here (fixing them
 * would be new scope requiring its own review, not part of proving
 * wrapToolCall).
 */
export async function invokeWardenGate(
  gateName: string,
  event: Record<string, unknown>,
  repoRoot: string,
  opts: WardenGateOptions = {},
): Promise<WardenGateDecision> {
  const spawnFn = opts.spawnFn ?? spawn;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  return new Promise((resolve, reject) => {
    const child: ChildProcess = spawnFn(PYTHON_BIN, [
      WARDEN_SCRIPT,
      'run',
      gateName,
      '--repo-root',
      repoRoot,
    ]);

    let settled = false;
    let stdout = '';
    let stderr = '';
    let timer: ReturnType<typeof setTimeout> | undefined;

    const settle = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      fn();
    };

    timer = setTimeout(() => {
      settle(() => {
        child.kill();
        reject(
          new Error(
            `invokeWardenGate: gate "${gateName}" timed out after ${timeoutMs}ms`,
          ),
        );
      });
    }, timeoutMs);

    // Broken-pipe/EPIPE on stdin surfaces via the eventual close/error event
    // below — swallow here only to avoid an unhandled 'error' crash on the
    // stream itself.
    child.stdin?.on('error', () => {});

    child.stdout?.on('data', (chunk: Buffer) => {
      stdout += chunk.toString('utf8');
    });
    child.stderr?.on('data', (chunk: Buffer) => {
      stderr += chunk.toString('utf8');
    });

    child.on('error', (err: Error) => {
      settle(() => {
        reject(
          new Error(
            `invokeWardenGate: failed to spawn gate "${gateName}": ${err.message}`,
          ),
        );
      });
    });

    child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
      settle(() => {
        if (signal) {
          reject(
            new Error(
              `invokeWardenGate: gate "${gateName}" was killed by signal ${signal}`,
            ),
          );
          return;
        }
        if (code !== 0) {
          reject(
            new Error(
              `invokeWardenGate: gate "${gateName}" exited with code ${code}. stderr: ${stderr}`,
            ),
          );
          return;
        }
        const trimmed = stdout.trim();
        if (trimmed === '') {
          resolve({ decision: 'allow' });
          return;
        }
        let parsed: unknown;
        try {
          parsed = JSON.parse(trimmed);
        } catch {
          reject(
            new Error(
              `invokeWardenGate: gate "${gateName}" produced non-empty, non-JSON stdout: ${trimmed}`,
            ),
          );
          return;
        }
        const hookOutput = (parsed as Record<string, unknown> | null)
          ?.hookSpecificOutput as Record<string, unknown> | undefined;
        const reason = hookOutput?.permissionDecisionReason;
        const isValidDeny =
          hookOutput?.hookEventName === 'PreToolUse' &&
          hookOutput?.permissionDecision === 'deny' &&
          typeof reason === 'string' &&
          reason.trim() !== '';
        if (!isValidDeny) {
          reject(
            new Error(
              `invokeWardenGate: gate "${gateName}" produced an unrecognized stdout shape: ${trimmed}`,
            ),
          );
          return;
        }
        resolve({ decision: 'deny', reason: reason as string });
      });
    });

    child.stdin?.write(JSON.stringify(event));
    child.stdin?.end();
  });
}

// ── createWardenGateMiddleware ──────────────────────────────────────────────

/**
 * Real LangChain middleware via createMiddleware's wrapToolCall hook — NOT a
 * Decorator around .invoke() (that would prove a different mechanism than
 * the one LIA-395 names). `invokeGate` is an injectable seam defaulting to
 * the real invokeWardenGate: wrapToolCall closes over the module-local
 * binding directly, and a spy on the export would not reliably intercept
 * that internal call — tests that need to assert "the gate was never
 * called" inject a mock here instead of spying.
 */
export function createWardenGateMiddleware(
  gateName: string,
  toEvent: (request: ToolCallRequest) => Record<string, unknown>,
  repoRoot: string,
  invokeGate: typeof invokeWardenGate = invokeWardenGate,
): AgentMiddleware {
  return createMiddleware({
    name: 'WardenGate',
    wrapToolCall: async (request, handler) => {
      // Checked before any subprocess is spawned: ToolCallRequest.toolCall.id
      // is string | undefined, but ToolMessage.tool_call_id requires string.
      // Synthesizing an empty id with `?? ''` would compile but silently
      // break the model-visible-feedback transport this spike exists to
      // prove.
      const toolCallId = request.toolCall.id;
      if (!toolCallId?.trim()) {
        throw new Error('Warden-denied tool call has no tool-call ID');
      }
      const decision = await invokeGate(gateName, toEvent(request), repoRoot);
      if (decision.decision === 'deny') {
        return new ToolMessage({
          content: decision.reason,
          tool_call_id: toolCallId,
          name: request.toolCall.name,
          status: 'error',
        });
      }
      return handler(request);
    },
  });
}

// ── Synthetic scratch-repo test tool ────────────────────────────────────────

/**
 * Schema is `{ content: string }` only — deliberately NO path argument.
 * Closes over a fixed, server-chosen scratchFilePath at construction time,
 * so there is no argument surface for a model (real or fake) to redirect
 * the write anywhere else. A forged extra `path`/`file_path` property in the
 * call args is simply never read.
 */
export function makeScratchEditTool(scratchFilePath: string): StructuredTool {
  return tool(
    async (args: Record<string, unknown>) => {
      const content = typeof args.content === 'string' ? args.content : '';
      fs.writeFileSync(scratchFilePath, content, 'utf8');
      return JSON.stringify({ ok: true, path: scratchFilePath });
    },
    {
      name: 'edit_scratch',
      description:
        'Writes content to a fixed scratch file for the A2 warden-veto spike.',
      schema: {
        type: 'object',
        properties: { content: { type: 'string' } },
        required: ['content'],
        additionalProperties: false,
      },
    },
  );
}

// ── Scratch repo helpers ────────────────────────────────────────────────────

export function setupScratchRepo(): {
  scratchRepoRoot: string;
  scratchFilePath: string;
} {
  const scratchRepoRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), 'lia395-scratch-'),
  );
  execFileSync('git', ['init', '--quiet'], { cwd: scratchRepoRoot });
  return {
    scratchRepoRoot,
    scratchFilePath: path.join(scratchRepoRoot, 'scratch.txt'),
  };
}

export function teardownScratchRepo(scratchRepoRoot: string): void {
  fs.rmSync(scratchRepoRoot, { recursive: true, force: true });
}

export function markScratchRepoShip(scratchRepoRoot: string): void {
  execFileSync(
    PYTHON_BIN,
    [
      WARDEN_SCRIPT,
      'mark',
      'plan-reviewed',
      'SHIP',
      'test',
      '--repo-root',
      scratchRepoRoot,
    ],
    { cwd: scratchRepoRoot },
  );
}

function toEditEvent(scratchRepoRoot: string, scratchFilePath: string) {
  return (_request: ToolCallRequest): Record<string, unknown> => ({
    tool_name: 'Edit',
    tool_input: { file_path: scratchFilePath },
    cwd: scratchRepoRoot,
  });
}

// ── Direct-execution smoke run ──────────────────────────────────────────────

async function runBlockedPathDemo(): Promise<void> {
  const { scratchRepoRoot, scratchFilePath } = setupScratchRepo();
  try {
    const tool_ = makeScratchEditTool(scratchFilePath);
    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      toEditEvent(scratchRepoRoot, scratchFilePath),
      scratchRepoRoot,
    );
    const agent = createAgent({
      model: new FakeToolCallingModel({
        toolCalls: [
          [{ name: 'edit_scratch', args: { content: 'hello' }, id: 'call_1' }],
          [],
        ],
      }),
      tools: [tool_],
      middleware: [middleware],
    });
    const result = await agent.invoke({
      messages: [{ role: 'user', content: 'write hello to the scratch file' }],
    });
    const wroteFile = fs.existsSync(scratchFilePath);
    console.log(
      `Blocked path: file written = ${wroteFile} (expected false), ` +
        `messages = ${result.messages.length}`,
    );
    const denyMessage = result.messages.find(
      (m) => (m as { name?: string }).name === 'edit_scratch',
    ) as { content?: unknown } | undefined;
    console.log(
      `Deny message content: ${JSON.stringify(denyMessage?.content)}`,
    );
  } finally {
    teardownScratchRepo(scratchRepoRoot);
  }
}

async function runAllowedPathDemo(): Promise<void> {
  const { scratchRepoRoot, scratchFilePath } = setupScratchRepo();
  try {
    markScratchRepoShip(scratchRepoRoot);
    const tool_ = makeScratchEditTool(scratchFilePath);
    const middleware = createWardenGateMiddleware(
      'plan-review-gate',
      toEditEvent(scratchRepoRoot, scratchFilePath),
      scratchRepoRoot,
    );
    const agent = createAgent({
      model: new FakeToolCallingModel({
        toolCalls: [
          [{ name: 'edit_scratch', args: { content: 'hello' }, id: 'call_1' }],
          [],
        ],
      }),
      tools: [tool_],
      middleware: [middleware],
    });
    await agent.invoke({
      messages: [{ role: 'user', content: 'write hello to the scratch file' }],
    });
    const wroteFile = fs.existsSync(scratchFilePath);
    const content = wroteFile
      ? fs.readFileSync(scratchFilePath, 'utf8')
      : undefined;
    console.log(
      `Allowed path: file written = ${wroteFile} (expected true), content = ${JSON.stringify(content)}`,
    );
  } finally {
    teardownScratchRepo(scratchRepoRoot);
  }
}

async function main(): Promise<void> {
  console.log('=== A2 walking skeleton: blocked path ===');
  await runBlockedPathDemo();
  console.log('\n=== A2 walking skeleton: allowed path ===');
  await runAllowedPathDemo();
  console.log(
    '\nSee scripts/spikes/lia395_warden_veto_walking_skeleton.test.ts for the ' +
      'full assertion suite (deny/allow/model-visible-feedback/fail-closed cases).',
  );
}

// Only run when executed directly (not when imported by the unit tests).
// See lia394_langchain_walking_skeleton.ts for why this compares resolved
// filesystem paths rather than raw strings (Windows file:// vs backslash
// path mismatch).
const invokedDirectly =
  process.argv[1] !== undefined &&
  fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (invokedDirectly) {
  main().catch((err) => {
    console.error('A2 spike failed:', err instanceof Error ? err.message : err);
    process.exit(1);
  });
}
