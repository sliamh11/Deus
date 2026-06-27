/**
 * LIA-127 blocker #9 — real-container E2E for MultiAgentOrchestrator.
 *
 * @manual-only  NOT a vitest test; never runs in CI (no `.test`/`.spec` name, and
 *   it lives outside `src/` so `tsc` never compiles it). Requires docker, the
 *   `deus-agent:latest` image, and a real Claude subscription
 *   (`~/.claude/.credentials.json`).
 * @platform macOS/Linux.
 *
 * Runs `MultiAgentOrchestrator.dispatch()` against a REAL `ContainerRuntime` to
 * verify the container lifecycle. The highest-risk unverified item is the
 * `_close` one-shot sentinel (src/multi-agent/orchestrator.ts:301-312): if it
 * misfires, each subagent idles `IDLE_TIMEOUT` (30 min default, config.ts:198)
 * before `runTurn` resolves. Mock tests cannot catch this.
 *
 * Hermetic — ZERO collision with the live `com.deus` service:
 *   - own credential proxy on CREDENTIAL_PROXY_PORT (default 3099, NOT live 3001);
 *     tokens are in-memory only (group-tokens.ts:10-11) so the same process must
 *     mint + validate — reusing the live proxy would 401.
 *   - dead TOOL_PROXY_PORT (default 3098) so containers never reach the live tool proxy.
 *   - DEUS_VAULT_PATH → a throwaway temp dir (no live-vault mount, resolveVaultPath
 *     reads the env first, container-mounter.ts:45).
 *   - IDLE_TIMEOUT clamped to 90s so a broken `_close` FAILS in ~90s, not a 30-min hang.
 *   - disposable non-control group (no projectId → MOUNT_ALLOWLIST never consulted);
 *     cwd-derived dirs (GROUPS_DIR/DATA_DIR/STORE_DIR, config.ts:54-55) isolated by
 *     running from a dedicated worktree, NOT the live ~/deus checkout.
 *   - DEUS_MULTI_AGENT stays unset — dispatch() is called directly; the flag is
 *     irrelevant to this layer.
 *
 * Run from a dedicated git worktree (NOT ~/deus):
 *   CREDENTIAL_PROXY_PORT=3099 TOOL_PROXY_PORT=3098 IDLE_TIMEOUT=90000 \
 *     DEUS_VAULT_PATH=$(mktemp -d) npx tsx scripts/ma-real-e2e.ts
 */

import { execSync } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import type { Server } from 'node:http';

import type { RegisteredGroup } from '../src/types.js';
import type {
  SubagentTask,
  OrchestratorResult,
} from '../src/multi-agent/types.js';
import type { ContainerRuntimeDeps } from '../src/agent-runtimes/container-backend.js';
import type { VolumeMount } from '../src/container-mounter.js';

// --- Env defaults: MUST be set before the dynamic imports in main() ---------
// config.ts reads CREDENTIAL_PROXY_PORT / TOOL_PROXY_PORT / IDLE_TIMEOUT as
// module-load constants. setDefault runs at top-level (before main()), and every
// config-dependent module is dynamically imported inside main(), so the env is
// in place first. DEUS_VAULT_PATH is read at call-time but set here too.
function setDefault(key: string, value: string): void {
  if (!process.env[key]) process.env[key] = value;
}
setDefault('CREDENTIAL_PROXY_PORT', '3099');
setDefault('TOOL_PROXY_PORT', '3098');
setDefault('IDLE_TIMEOUT', '90000');
if (!process.env.DEUS_VAULT_PATH) {
  process.env.DEUS_VAULT_PATH = fs.mkdtempSync(
    path.join(os.tmpdir(), 'ma-e2e-vault-'),
  );
}
// This layer must never observe the live multi-agent routing flag.
delete process.env.DEUS_MULTI_AGENT;

const TEMP_VAULT = path.resolve(process.env.DEUS_VAULT_PATH);
const LIVE_GROUPS = path.join(os.homedir(), 'deus', 'groups');
const CREDS_DIR = path.join(os.homedir(), '.claude');
const TIMING_THRESHOLD_MS = 60_000; // < this ⇒ `_close` fired (idle wall is 90s)

/**
 * Abort a preflight gate. Used only BEFORE any container/proxy is allocated, so
 * the sole resource to reclaim is TEMP_VAULT (created at module load) — `finally`
 * does NOT run after `process.exit()`, so clean it here explicitly.
 */
function abortEarly(msg: string): never {
  console.error(msg);
  try {
    fs.rmSync(TEMP_VAULT, { recursive: true, force: true });
  } catch {
    /* best-effort */
  }
  process.exit(1);
}

let passCount = 0;
let failCount = 0;
function check(name: string, ok: boolean, detail = ''): void {
  const tag = ok ? 'PASS' : 'FAIL';
  if (ok) passCount++;
  else failCount++;
  console.log(`  ${tag}  ${name}${detail ? ` — ${detail}` : ''}`);
}

function section(title: string): void {
  console.log(`\n=== ${title} ===`);
}

async function main(): Promise<void> {
  // Dynamic imports — env defaults above are in place before config.ts loads.
  // Import the COMPILED dist (plain JS, types erased) rather than src/*.ts:
  // tsx transpiles each .ts file in isolation, which trips on type-only imports
  // written as value imports in the src import chain (e.g. auth-providers).
  // dist is built from the same worktree source (`npx tsc`) before running.
  const {
    GROUPS_DIR,
    ASSISTANT_NAME,
    CREDENTIAL_PROXY_PORT,
    TOOL_PROXY_PORT,
    CONTAINER_IMAGE,
    IDLE_TIMEOUT,
  } = await import('../dist/config.js');
  const { ensureContainerRuntimeRunning } =
    await import('../dist/container-runtime.js');
  const { startCredentialProxy } = await import('../dist/credential-proxy.js');
  const { getOrCreateGroupToken } = await import('../dist/group-tokens.js');
  const { resolveGroupFolderPath } = await import('../dist/group-folder.js');
  const { buildVolumeMounts } = await import('../dist/container-mounter.js');
  const { initRuntimeRegistry } =
    await import('../dist/agent-runtimes/registry.js');
  const { createClaudeRuntime } =
    await import('../dist/agent-runtimes/claude-backend.js');
  const { MultiAgentOrchestrator } =
    await import('../dist/multi-agent/orchestrator.js');
  const { formatMultiAgentResult } =
    await import('../dist/multi-agent/message-bridge.js');

  const WORKTREE_ROOT = path.resolve(process.cwd());
  let server: Server | undefined;
  const trackedProcs: ChildProcess[] = [];

  // The disposable group — non-control, no projectId.
  const group: RegisteredGroup = {
    name: 'MA E2E Smoke',
    folder: 'ma-e2e-smoke',
    trigger: '',
    added_at: new Date(0).toISOString(),
    isControlGroup: false,
  };
  const groupDir = resolveGroupFolderPath(group.folder);

  section('Config');
  console.log(`  cwd (worktree)        ${WORKTREE_ROOT}`);
  console.log(`  GROUPS_DIR            ${GROUPS_DIR}`);
  console.log(`  group folder          ${groupDir}`);
  console.log(`  temp vault            ${TEMP_VAULT}`);
  console.log(`  CREDENTIAL_PROXY_PORT ${CREDENTIAL_PROXY_PORT}`);
  console.log(`  TOOL_PROXY_PORT       ${TOOL_PROXY_PORT} (dead)`);
  console.log(`  IDLE_TIMEOUT          ${IDLE_TIMEOUT} ms`);
  console.log(`  CONTAINER_IMAGE       ${CONTAINER_IMAGE}`);

  try {
    // ---- Preflight gates (abort before any container launches) -------------
    section('Preflight');

    // 1. Refuse to run from a primary checkout — it would mount the LIVE
    //    groups/global RO and collide with the live service. Two guards:
    //    (a) path-independent: cwd must be a LINKED git worktree (git-dir differs
    //        from git-common-dir), so this holds for any clone location, not just
    //        ~/deus; (b) belt-and-suspenders: GROUPS_DIR must not be the live one.
    try {
      const gitDir = execSync('git rev-parse --git-dir', {
        encoding: 'utf8',
        cwd: WORKTREE_ROOT,
      }).trim();
      const commonDir = execSync('git rev-parse --git-common-dir', {
        encoding: 'utf8',
        cwd: WORKTREE_ROOT,
      }).trim();
      if (
        path.resolve(WORKTREE_ROOT, gitDir) ===
        path.resolve(WORKTREE_ROOT, commonDir)
      ) {
        abortEarly(
          'ABORT: cwd is a primary checkout, not a linked git worktree. Run from a ' +
            'dedicated worktree (git worktree add ...).',
        );
      }
    } catch {
      // Not resolvable as a git worktree — fall through to the path check below.
    }
    if (path.resolve(GROUPS_DIR) === LIVE_GROUPS) {
      abortEarly(
        `ABORT: GROUPS_DIR resolves to the live ${LIVE_GROUPS}. Run from a dedicated ` +
          `worktree, not the live ~/deus checkout.`,
      );
    }
    check(
      'cwd is a linked worktree, not the live ~/deus checkout',
      true,
      GROUPS_DIR,
    );

    // 2. Docker up + image present.
    ensureContainerRuntimeRunning();
    check('container runtime running', true);
    try {
      execSync(`docker image inspect ${CONTAINER_IMAGE}`, { stdio: 'ignore' });
      check(`image ${CONTAINER_IMAGE} present`, true);
    } catch {
      abortEarly(
        `ABORT: image ${CONTAINER_IMAGE} missing. Build it first: ./container/build.sh`,
      );
    }

    // 3. Pre-launch MOUNT AUDIT — call buildVolumeMounts directly (NOT the
    //    post-spawn container-runner log) so this is a genuine abort point.
    //    Allowed host paths: the worktree subtree, the temp vault, ~/.claude
    //    (creds/session, RO), and /dev/null (sensitive-file shadows). Anything
    //    else (live vault, ~/deus/groups, ...) ⇒ abort.
    const within = (root: string, p: string): boolean =>
      p === root || p.startsWith(root + path.sep);
    const allowed = (hostPath: string): boolean => {
      const r = path.resolve(hostPath);
      return (
        r === path.resolve(os.devNull) ||
        r === '/dev/null' ||
        within(WORKTREE_ROOT, r) ||
        within(TEMP_VAULT, r) ||
        within(CREDS_DIR, r)
      );
    };
    // 'multi-agent-t1' mirrors the ipcRunKey the orchestrator uses (chatJid =
    // `multi-agent-${task.id}`, orchestrator.ts:276,285). Per-task ipcRunKeys
    // (t2/t3/t4) only vary the IPC subdir, and every IPC/session mount is rooted
    // at the cwd-derived DATA_DIR ⇒ always inside the worktree allowlist. So this
    // single representative audit bounds every runtime-added mount; the worktree
    // isolation (not the exact key) is what makes that sound.
    const auditMounts: VolumeMount[] = buildVolumeMounts(
      group,
      false,
      undefined,
      'multi-agent-t1',
    );
    console.log('  assembled mounts for the disposable group:');
    for (const m of auditMounts) {
      console.log(`    ${m.readonly ? 'ro' : 'rw'}  ${m.hostPath}`);
    }
    const offending = auditMounts.filter((m) => !allowed(m.hostPath));
    if (offending.length > 0) {
      for (const m of offending)
        console.error(`  offending mount: ${m.hostPath}`);
      abortEarly('ABORT: mount audit found host paths outside the allowlist.');
    }
    check(
      'mount audit — no live host path mounted',
      true,
      `${auditMounts.length} mounts`,
    );

    // ---- Setup: proxy, registry, group, token ------------------------------
    section('Setup');
    server = await startCredentialProxy(CREDENTIAL_PROXY_PORT, '127.0.0.1');
    check(
      'credential proxy started',
      true,
      `127.0.0.1:${CREDENTIAL_PROXY_PORT}`,
    );

    const deps: ContainerRuntimeDeps = {
      resolveGroup: (folder: string) =>
        folder === group.folder ? group : undefined,
      assistantName: ASSISTANT_NAME,
      registerProcess: (
        _chatJid: string,
        proc: ChildProcess,
        _containerName: string,
        _groupFolder: string,
      ) => {
        trackedProcs.push(proc);
      },
    };
    const registry = initRuntimeRegistry();
    registry.register(createClaudeRuntime(deps));
    check(
      'registry resolves a ContainerRuntime',
      registry.resolve(group).name() === 'claude',
    );

    fs.mkdirSync(groupDir, { recursive: true });
    const token = getOrCreateGroupToken(group.folder);
    check(
      'group token minted (in-proc, validated by our proxy)',
      token.length > 0,
    );

    // ---- Tasks -------------------------------------------------------------
    const mk = (
      id: string,
      prompt: string,
      contextFrom?: string[],
    ): SubagentTask => ({
      id,
      role: 'smoke-test agent',
      goal: 'complete a trivial smoke check with no tools',
      backstory: '',
      prompt,
      mode: 'read',
      contextFrom,
    });
    const PONG =
      'Reply with the single word: pong. Do not use any tools or read any files.';
    const t1 = mk('t1', PONG);
    const t2 = mk('t2', PONG);
    const t4 = mk(
      't4',
      'You cannot complete this task. Report it as blocked with the reason: smoke. Do not use any tools.',
    );
    const t3 = mk(
      't3',
      'Acknowledge the context above in one short sentence. Do not use any tools.',
      ['t1'],
    );

    const orchestrator = new MultiAgentOrchestrator(registry);

    // ---- Phase A: single-task per-container `_close` evidence ---------------
    section('Phase A — single container (`_close` timing)');
    const a0 = Date.now();
    const resultA: OrchestratorResult = await orchestrator.dispatch(
      [t1],
      group,
    );
    const elapsedA = Date.now() - a0;
    console.log(
      `  elapsedA = ${elapsedA} ms (idle wall is ${IDLE_TIMEOUT} ms)`,
    );
    check(
      'Phase A: single container exited via `_close` (not idle timeout)',
      elapsedA < TIMING_THRESHOLD_MS,
      `${elapsedA} ms < ${TIMING_THRESHOLD_MS} ms`,
    );
    check(
      'Phase A: task DONE with deliverable',
      resultA.results[0]?.status === 'DONE' &&
        resultA.results[0].output.trim().length > 0,
      `status=${resultA.results[0]?.status}`,
    );

    // ---- Phase B: tiering + blocked propagation + aggregation --------------
    section('Phase B — tiers [t1,t2,t4] then [t3], + blocked task');
    const tasksB = [t1, t2, t3, t4];
    const b0 = Date.now();
    const resultB: OrchestratorResult = await orchestrator.dispatch(
      tasksB,
      group,
    );
    const elapsedB = Date.now() - b0;
    console.log(`  elapsedB = ${elapsedB} ms`);
    // results are positionally aligned to tasksB (message-bridge.ts:112 note).
    const byId = new Map(tasksB.map((t, i) => [t.id, resultB.results[i]]));
    check(
      'Phase B: completed via `_close` (not idle timeout)',
      elapsedB < TIMING_THRESHOLD_MS,
      `${elapsedB} ms < ${TIMING_THRESHOLD_MS} ms`,
    );
    check(
      'Phase B: t1 DONE',
      byId.get('t1')?.status === 'DONE',
      `status=${byId.get('t1')?.status}`,
    );
    check(
      'Phase B: t2 DONE',
      byId.get('t2')?.status === 'DONE',
      `status=${byId.get('t2')?.status}`,
    );
    check(
      'Phase B: t3 DONE (dependent tier, prior-output injected)',
      byId.get('t3')?.status === 'DONE' &&
        (byId.get('t3')?.output.trim().length ?? 0) > 0,
      `status=${byId.get('t3')?.status}`,
    );
    // t4's BLOCKED status comes from the agent emitting a `[STATUS:BLOCKED:...]`
    // marker, parsed by parseStatusMarker (orchestrator.ts:99-102). If the agent
    // paraphrases instead of using the marker vocabulary this fails-as-FAIL (never
    // a false-PASS) — a refactor of that parser would surface here, not hide.
    check(
      'Phase B: t4 BLOCKED (blocked-status propagated)',
      byId.get('t4')?.status === 'BLOCKED',
      `status=${byId.get('t4')?.status}`,
    );
    check(
      "Phase B: aggregate status 'partial' (3 done + 1 blocked)",
      resultB.status === 'partial',
      `status=${resultB.status}`,
    );

    // ---- `<internal>` leak check at both layers ----------------------------
    section('Output hygiene');
    const rawHasInternal = resultB.results.some((r) =>
      r.output.includes('<internal>'),
    );
    const formatted = formatMultiAgentResult(resultB, tasksB);
    check('no `<internal>` in raw results', !rawHasInternal);
    check(
      'no `<internal>` in formatted reply',
      !formatted.includes('<internal>'),
    );
    console.log('\n  --- formatted reply (evidence) ---');
    console.log(
      formatted
        .split('\n')
        .map((l) => `  | ${l}`)
        .join('\n'),
    );
  } finally {
    // ---- Teardown (always) -------------------------------------------------
    section('Teardown');
    for (const proc of trackedProcs) {
      try {
        if (proc.exitCode === null && proc.signalCode === null)
          proc.kill('SIGKILL');
      } catch {
        /* best-effort */
      }
    }
    if (server) {
      await new Promise<void>((resolve) => server!.close(() => resolve()));
      console.log('  proxy closed');
    }
    for (const dir of [groupDir, TEMP_VAULT]) {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        /* best-effort */
      }
    }
    console.log('  temp dirs removed');
  }

  // ---- Verdict -------------------------------------------------------------
  section('Verdict');
  console.log(`  ${passCount} passed, ${failCount} failed`);
  process.exit(failCount > 0 ? 1 : 0);
}

main().catch((err) => {
  console.error('\nHARNESS ERROR:', err);
  process.exit(1);
});
