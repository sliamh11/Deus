/**
 * Independent oracle for the wardens middleware gate wiring.
 *
 * Derived from the ticket contract before any production implementation was
 * present. The oracle observes the documented subprocess boundary through a
 * temporary `python3` executable on PATH; it does not assume which Node child-
 * process API the implementation uses. The fake CLI always exits 0, matching
 * codex_warden_hooks.py's load-bearing contract that allow vs deny is carried
 * by stdout JSON, never by the process exit status.
 *
 * RED before implementation: the current wardens placeholder never invokes
 * codex_warden_hooks.py, always delegates to the tool handler, and records
 * only `allow`. Consequently the block-path non-delegation assertions and the
 * CLI-capture assertions fail for behavioral reasons.
 */

import { execFileSync } from 'node:child_process';
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';

import { ToolMessage } from '@langchain/core/messages';
import { createAgent, FakeToolCallingModel, tool } from 'langchain';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { buildMiddlewareStack } from './middleware-stack.js';

const WORKTREE_ROOT = process.cwd();
const COMMON_GIT_DIR = execFileSync(
  'git',
  ['rev-parse', '--path-format=absolute', '--git-common-dir'],
  { cwd: WORKTREE_ROOT, encoding: 'utf8' },
).trim();
const REPO_ROOT = dirname(COMMON_GIT_DIR);
const WARDEN_SCRIPT = join(REPO_ROOT, 'scripts', 'codex_warden_hooks.py');

const PLAN_BEHAVIOR = 'plan-review-gate';
const COMMIT_BEHAVIORS = [
  'code-review-gate',
  'ai-eng-gate',
  'verification-gate',
] as const;

const PATCH_ARGS = {
  patch:
    '*** Begin Patch\n*** Add File: oracle-sentinel\n+blocked\n*** End Patch',
};
const COMMIT_ARGS = {
  command: 'git commit -m "oracle sentinel commit"',
};

interface CapturedInvocation {
  argv: string[];
  event: {
    cwd?: unknown;
    tool_name?: unknown;
    tool_input?: unknown;
  };
}

let sandboxDir = '';
let captureDir = '';

beforeEach(async () => {
  sandboxDir = await mkdtemp(join(tmpdir(), 'deus-warden-oracle-'));
  const binDir = join(sandboxDir, 'bin');
  captureDir = join(sandboxDir, 'capture');
  await mkdir(binDir);
  await mkdir(captureDir);

  const fakePython = join(binDir, 'python3');
  await writeFile(
    fakePython,
    `#!/bin/sh
set -eu
capture_dir="\${WARDEN_ORACLE_CAPTURE_DIR:?}"
behavior="\${3:-missing-behavior}"
prefix="$capture_dir/$behavior"
printf '%s\\n' "$@" > "$prefix.argv"
cat > "$prefix.stdin"
if [ "$behavior" = "\${WARDEN_ORACLE_BLOCK_BEHAVIOR:-}" ]; then
  printf '%s\\n' "\${WARDEN_ORACLE_BLOCK_JSON:?}"
fi
exit 0
`,
    'utf8',
  );
  await chmod(fakePython, 0o755);

  vi.stubEnv('PATH', `${binDir}${delimiter}${process.env.PATH ?? ''}`);
  vi.stubEnv('WARDEN_ORACLE_CAPTURE_DIR', captureDir);
  vi.stubEnv('WARDEN_ORACLE_BLOCK_BEHAVIOR', '');
  vi.stubEnv('WARDEN_ORACLE_BLOCK_JSON', '');
});

afterEach(async () => {
  vi.unstubAllEnvs();
  await rm(sandboxDir, { recursive: true, force: true });
});

function scriptedModelCalling(
  toolName: string,
  args: Record<string, unknown>,
): FakeToolCallingModel {
  return new FakeToolCallingModel({
    toolCalls: [[{ name: toolName, args, id: 'warden_oracle_call_1' }], []],
  });
}

async function runToolCall(
  toolName: string,
  args: Record<string, unknown>,
  blockBehavior?: string,
) {
  const handlerSpy = vi.fn(async () => 'ORACLE_TOOL_EXECUTED');
  const spiedTool = tool(handlerSpy, {
    name: toolName,
    description: 'Hermetic tool double for the independent warden oracle.',
    schema: {
      type: 'object',
      properties: {
        command: { type: 'string' },
        patch: { type: 'string' },
        query: { type: 'string' },
      },
      additionalProperties: false,
    },
  });

  let reviseFeedback: string | undefined;
  if (blockBehavior) {
    reviseFeedback = `[${blockBehavior}] REVISE: independent oracle sentinel feedback`;
    vi.stubEnv('WARDEN_ORACLE_BLOCK_BEHAVIOR', blockBehavior);
    vi.stubEnv(
      'WARDEN_ORACLE_BLOCK_JSON',
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: reviseFeedback,
        },
      }),
    );
  }

  const { middleware, logs } = buildMiddlewareStack({
    permissions: false,
    memory: false,
    telemetry: false,
  });
  const agent = createAgent({
    model: scriptedModelCalling(toolName, args),
    tools: [spiedTool],
    middleware,
  });
  const result = await agent.invoke({
    messages: [{ role: 'user', content: 'exercise the warden boundary' }],
  });

  const toolMessage = (result as { messages: unknown[] }).messages.find(
    (message): message is ToolMessage =>
      ToolMessage.isInstance(message as never) &&
      (message as ToolMessage).tool_call_id === 'warden_oracle_call_1',
  );

  return { handlerSpy, logs, reviseFeedback, toolMessage };
}

async function capturedBehaviors(): Promise<string[]> {
  return (await readdir(captureDir))
    .filter((name) => name.endsWith('.stdin'))
    .map((name) => name.slice(0, -'.stdin'.length))
    .sort();
}

async function readInvocation(behavior: string): Promise<CapturedInvocation> {
  const argv = (await readFile(join(captureDir, `${behavior}.argv`), 'utf8'))
    .trimEnd()
    .split('\n');
  const event = JSON.parse(
    await readFile(join(captureDir, `${behavior}.stdin`), 'utf8'),
  ) as CapturedInvocation['event'];
  return { argv, event };
}

async function expectInvocation(
  behavior: string,
  toolName: string,
  toolInput: Record<string, unknown>,
): Promise<void> {
  const { argv, event } = await readInvocation(behavior);

  // CLI contract: python3 <script> run <behavior> --repo-root <repo-root> ...
  expect(argv.slice(0, 3)).toEqual([WARDEN_SCRIPT, 'run', behavior]);
  const repoRootFlag = argv.indexOf('--repo-root');
  expect(repoRootFlag).toBeGreaterThanOrEqual(0);
  expect(argv[repoRootFlag + 1]).toBe(REPO_ROOT);

  // Claude Code PreToolUse event contract consumed by all four runners.
  expect(event).toEqual({
    cwd: WORKTREE_ROOT,
    tool_name: toolName,
    tool_input: toolInput,
  });
}

function expectBlockedWithFeedback(
  result: Awaited<ReturnType<typeof runToolCall>>,
  toolName: string,
): void {
  expect(result.handlerSpy).not.toHaveBeenCalled();
  expect(result.toolMessage).toBeDefined();
  expect(result.toolMessage?.status).toBe('error');
  expect(result.toolMessage?.name).toBe(toolName);
  expect(result.reviseFeedback).toBeDefined();
  expect(String(result.toolMessage?.content)).toContain(
    result.reviseFeedback as string,
  );
  expect(result.logs.wardens).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ toolName, decision: 'deny' }),
    ]),
  );
}

describe('@oracle wardens middleware — every real gate can block before tool execution', () => {
  it('@oracle plan-review-gate blocks apply_patch and returns its REVISE reason to the model', async () => {
    // @oracle: plan-review runs at PreToolUse for apply_patch and may deny it.
    const result = await runToolCall('apply_patch', PATCH_ARGS, PLAN_BEHAVIOR);

    expectBlockedWithFeedback(result, 'apply_patch');
    expect(await capturedBehaviors()).toContain(PLAN_BEHAVIOR);
    await expectInvocation(PLAN_BEHAVIOR, 'apply_patch', PATCH_ARGS);
  });

  it.each(COMMIT_BEHAVIORS)(
    '@oracle %s blocks a Bash git-commit call and returns its REVISE reason to the model despite exit code 0',
    async (behavior) => {
      // @oracle: code-review, ai-eng, and verification are independent
      // PreToolUse gates on the Bash commit path; each can deny execution.
      const result = await runToolCall('Bash', COMMIT_ARGS, behavior);

      expectBlockedWithFeedback(result, 'Bash');
      expect(await capturedBehaviors()).toContain(behavior);
      await expectInvocation(behavior, 'Bash', COMMIT_ARGS);
    },
  );
});

describe('@oracle wardens middleware — empty stdout allows the protected action', () => {
  it('@oracle an allowed apply_patch reaches plan-review-gate, then delegates once', async () => {
    // @oracle: allow is represented by empty stdout, not a nonzero/zero code.
    const result = await runToolCall('apply_patch', PATCH_ARGS);

    expect(result.handlerSpy).toHaveBeenCalledTimes(1);
    expect(await capturedBehaviors()).toContain(PLAN_BEHAVIOR);
    expect(result.logs.wardens).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          toolName: 'apply_patch',
          decision: 'allow',
        }),
      ]),
    );
    await expectInvocation(PLAN_BEHAVIOR, 'apply_patch', PATCH_ARGS);
  });

  it('@oracle an allowed Bash git commit invokes all three commit gates, then delegates once', async () => {
    // @oracle: the Bash matcher carries code-review, ai-eng, and
    // verification; all three empty outputs are required before execution.
    const result = await runToolCall('Bash', COMMIT_ARGS);

    expect(result.handlerSpy).toHaveBeenCalledTimes(1);
    expect(await capturedBehaviors()).toEqual(
      expect.arrayContaining([...COMMIT_BEHAVIORS]),
    );
    expect(result.logs.wardens).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ toolName: 'Bash', decision: 'allow' }),
      ]),
    );
    for (const behavior of COMMIT_BEHAVIORS) {
      await expectInvocation(behavior, 'Bash', COMMIT_ARGS);
    }
  });
});

describe('@oracle Claude Code hook path remains installed for additive double enforcement', () => {
  it('@oracle .claude/settings.json still wires the same four behaviors to their PreToolUse matchers', async () => {
    // @oracle: the middleware path is additive; C5 retains Claude's existing
    // plan matcher and Bash commit matcher with their shim commands intact.
    const settings = JSON.parse(
      await readFile(join(WORKTREE_ROOT, '.claude', 'settings.json'), 'utf8'),
    ) as {
      hooks?: {
        PreToolUse?: Array<{
          matcher?: string;
          hooks?: Array<{
            type?: string;
            command?: string;
            timeout?: number;
          }>;
        }>;
      };
    };
    const preToolUse = settings.hooks?.PreToolUse ?? [];
    const expectedHook = (behavior: string) => ({
      type: 'command',
      command:
        `bash -c '"\${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/` +
        `warden-shim.sh" ${behavior}'`,
      timeout: 5,
    });

    expect(preToolUse).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          matcher: 'Write|Edit|MultiEdit|apply_patch|ExitPlanMode',
          hooks: expect.arrayContaining([expectedHook(PLAN_BEHAVIOR)]),
        }),
        expect.objectContaining({
          matcher: 'Bash',
          hooks: expect.arrayContaining(COMMIT_BEHAVIORS.map(expectedHook)),
        }),
      ]),
    );
  });
});
