/**
 * Container Runner for Deus
 * Spawns agent execution in containers and handles IPC
 *
 * Mount assembly lives in container-mounter.ts.
 */
import { ChildProcess, execFile, spawn } from 'child_process';
import fs from 'fs';
import path from 'path';

import {
  AgentRuntimeId,
  RuntimeSession,
  defaultSession,
} from './agent-runtimes/types.js';
import {
  CONTAINER_IMAGE,
  CONTAINER_MAX_OUTPUT_SIZE,
  CONTAINER_TIMEOUT,
  CONTEXT_AUTO_COMPACT_PCT,
  CONTEXT_WARN_PCT,
  CREDENTIAL_PROXY_PORT,
  DEUS_CONTEXT_FILE_MAX_CHARS,
  DEUS_OPENAI_MODEL,
  IDLE_TIMEOUT,
  LLAMA_CPP_AGENT_MODEL,
  LLAMA_CPP_MODEL,
  LLAMA_CPP_PORT,
  TIMEZONE,
  TOOL_PROXY_PORT,
} from './config.js';
import {
  getOrCreateGroupToken,
  getOrCreateScopedToken,
} from './group-tokens.js';
import { resolveGroupFolderPath, resolveGroupIpcPath } from './group-folder.js';
import { logger } from './logger.js';

function redactContainerArgs(args: string[]): string {
  return args
    .join(' ')
    .replace(/DEUS_PROXY_TOKEN=[0-9a-f]+/g, 'DEUS_PROXY_TOKEN=[REDACTED]')
    .replace(/LINEAR_API_KEY=\S+/g, 'LINEAR_API_KEY=[REDACTED]');
}
import {
  CONTAINER_HOST_GATEWAY,
  CONTAINER_RUNTIME_BIN,
  hostGatewayArgs,
  readonlyMountArgs,
} from './container-runtime.js';
import { forceKillProcess } from './platform.js';
import { detectAuthMode } from './credential-proxy.js';
import { buildVolumeMounts } from './container-mounter.js';
import { RegisteredGroup } from './types.js';
import { detectDomainsWithFallback } from './domain-presets.js';
import {
  getActivePrompt,
  getReflections,
  logInteraction,
  type ToolCall,
} from './evolution-client.js';
import { estimateTokens } from './token-counter.js';
import { detectUserSignal } from './user-signal.js';
import { getProjectById } from './db.js';
import {
  ContainerOutputSchema,
  OUTPUT_START_MARKER,
  OUTPUT_END_MARKER,
} from './ipc-protocol.js';
import type {
  ContainerOutput,
  ContainerInput,
  ContextStats,
  CompactionEvent,
} from './ipc-protocol.js';
export type {
  ContainerOutput,
  ContainerInput,
  ContextStats,
  CompactionEvent,
} from './ipc-protocol.js';

function containsCodeBlock(text: string | null): boolean {
  if (!text) return false;
  return /```[\s\S]{10,}?```/.test(text);
}

/**
 * Prefix of the error string produced when a container is reaped by the hard
 * timeout (the resolve site below). Exported so downstream routing can tell an
 * infrastructure timeout apart from a genuine agent failure (LIA-168) without
 * brittle ad-hoc string matching.
 */
export const CONTAINER_TIMEOUT_ERROR_PREFIX = 'Container timed out after';

// LIA-315 Phase 2: host-side authoritative allowlist for a webhook (publicIngress)
// run's curated tools. The host is the trust boundary — it mints the scoped proxy
// token and exports DEUS_CURATED_TOOLS — so it must bound BOTH by this set, not by
// the raw config. A malformed config naming a sensitive tool (e.g. mcp__deus__*)
// must not end up in the token scope the tool-proxy authorizes against.
//
// SYNC-REQUIRED: mirror of SAFE_CURATED in container/agent-runner/src/allowed-tools.ts
// (host and container build as isolated packages — no shared module, cf. LIA-223).
// Exact names only (the token scope is matched exactly); MCP-glob curated tools are
// deferred to Phase 4. Exported so a host-side test can assert byte-equality with the
// container copy (safe-curated-sync.test.ts) — the automated guard against silent drift.
export const SAFE_CURATED = new Set<string>([
  'Read',
  'Glob',
  'Grep',
  'WebSearch',
  'WebFetch',
]);

// Exported as a test seam (LIA-315 Phase 2 @oracle byte-identity + isolation tests).
export function buildContainerArgs(
  mounts: ReturnType<typeof buildVolumeMounts>,
  containerName: string,
  backend: AgentRuntimeId,
  interactionId: string,
  group?: RegisteredGroup,
): string[] {
  const args: string[] = ['run', '-i', '--rm', '--name', containerName];

  // LIA-315 Phase 2: webhook-originated (publicIngress) groups run reduced-privilege:
  // a SCOPED proxy token (curated tools only), no raw secrets, and the webhook tool
  // profile. A normal group keeps the unscoped token + full env (byte-identical).
  const isPublicIngress = group?.containerConfig?.publicIngress === true;
  // Bound the curated set by the host SAFE_CURATED allowlist BEFORE it reaches
  // either the token scope or the container — the raw config is untrusted input.
  const curatedTools = (group?.containerConfig?.curatedTools ?? []).filter(
    (t) => SAFE_CURATED.has(t),
  );

  // R1 (LIA-315) fail-closed: the reduced-privilege tool manifest is enforced
  // only on the Claude path (buildAllowedTools). The openai/llama-cpp backends
  // branch before it and run their own toolset, so a publicIngress run on a
  // non-Claude backend would NOT be privilege-reduced. Refuse to launch rather
  // than silently downgrade isolation. (Phase 2 is dormant — no publicIngress
  // group exists yet — but this guards the Phase-4 dispatch wire against a
  // misconfigured backend.)
  if (isPublicIngress && backend !== 'claude') {
    throw new Error(
      `publicIngress group "${group?.folder}" requires the 'claude' backend ` +
        `(reduced-privilege profile is claude-only); refusing to launch on '${backend}'`,
    );
  }

  // Pass host timezone so container's local time matches the user's
  args.push('-e', `TZ=${TIMEZONE}`);
  const proxyToken =
    isPublicIngress && group
      ? getOrCreateScopedToken(group.folder, new Set(curatedTools))
      : getOrCreateGroupToken(group?.folder);
  args.push('-e', `DEUS_PROXY_TOKEN=${proxyToken}`);
  // LIA-154: exact per-dispatch join key for the in-container tool-call capture
  // hook → tool-calls/<id>.jsonl → readToolCalls() back on the host.
  args.push('-e', `DEUS_INTERACTION_ID=${interactionId}`);
  // Tool proxy URL — containers call host CLIs through this endpoint.
  // Uses CONTAINER_HOST_GATEWAY so the URL resolves to the host from inside the container.
  args.push(
    '-e',
    `DEUS_TOOL_PROXY_URL=http://${CONTAINER_HOST_GATEWAY}:${TOOL_PROXY_PORT}`,
  );
  if (DEUS_CONTEXT_FILE_MAX_CHARS) {
    args.push(
      '-e',
      `DEUS_CONTEXT_FILE_MAX_CHARS=${DEUS_CONTEXT_FILE_MAX_CHARS}`,
    );
  }
  args.push(
    '-e',
    `DEUS_CONTEXT_WARN_PCT=${CONTEXT_WARN_PCT}`,
    '-e',
    `DEUS_CONTEXT_AUTO_COMPACT_PCT=${CONTEXT_AUTO_COMPACT_PCT}`,
  );
  // Forward the memory-injection dedup kill-switch (LIA-355) — the container
  // env is enumerated, not inherited, so without this the in-container
  // dedup toggle would read undefined and could never be turned off.
  if (process.env.DEUS_MEMORY_DEDUP) {
    args.push('-e', `DEUS_MEMORY_DEDUP=${process.env.DEUS_MEMORY_DEDUP}`); // LIA-355
  }

  // R2 (LIA-315): never inject raw secrets into a webhook (publicIngress) container.
  // Curated actions that need Linear run host-brokered through the tool-proxy with
  // the credential on the host, gated by the scoped token.
  if (!isPublicIngress) {
    const linearKey =
      process.env.LINEAR_API_KEY || process.env.LINEAR_API_TOKEN;
    if (linearKey) {
      if (/^[A-Za-z0-9_-]+$/.test(linearKey)) {
        args.push('-e', `LINEAR_API_KEY=${linearKey}`);
      } else {
        logger.warn(
          'LINEAR_API_KEY contains invalid characters; Linear MCP disabled for this container',
        );
      }
    }
  } else {
    // R1 (LIA-315): tell the in-container agent-runner to build the reduced-
    // privilege tool manifest, and which curated tools it may request.
    args.push('-e', 'DEUS_TOOL_PROFILE=webhook');
    args.push('-e', `DEUS_CURATED_TOOLS=${curatedTools.join(',')}`);
  }

  // Inject per-channel memory privacy allowlist if configured
  if (group?.containerConfig?.memoryPrivacy?.length) {
    args.push(
      '-e',
      `DEUS_MEMORY_PRIVACY=${group.containerConfig.memoryPrivacy.join(',')}`,
    );
  }

  if (backend === 'openai') {
    args.push(
      '-e',
      `OPENAI_BASE_URL=http://${CONTAINER_HOST_GATEWAY}:${CREDENTIAL_PROXY_PORT}/openai`,
    );
    args.push('-e', 'OPENAI_API_KEY=placeholder');
    if (DEUS_OPENAI_MODEL) {
      args.push('-e', `DEUS_OPENAI_MODEL=${DEUS_OPENAI_MODEL}`);
    }
  } else if (backend === 'llama-cpp') {
    // llama-server runs on the host and has no auth — no credential-proxy
    // hop needed. The container talks to the host gateway directly. We
    // deliberately use LLAMA_CPP_* env names (NOT OPENAI_*) so config does
    // not co-mingle if the user has both backends configured. The container
    // driver reads LLAMA_CPP_BASE_URL etc. directly.
    args.push(
      '-e',
      `LLAMA_CPP_BASE_URL=http://${CONTAINER_HOST_GATEWAY}:${LLAMA_CPP_PORT}/v1`,
    );
    args.push('-e', 'LLAMA_CPP_API_KEY=placeholder');
    // Phase 3 (post-PR #461): inject both LLAMA_CPP_AGENT_MODEL (per-surface)
    // and LLAMA_CPP_MODEL (catch-all). Approach A — the container backend
    // reads LLAMA_CPP_AGENT_MODEL with fallback to LLAMA_CPP_MODEL, then to
    // empty (router-mode auto-pick). Both injected for back-compat safety.
    if (LLAMA_CPP_AGENT_MODEL) {
      args.push('-e', `LLAMA_CPP_AGENT_MODEL=${LLAMA_CPP_AGENT_MODEL}`);
    }
    if (LLAMA_CPP_MODEL) {
      args.push('-e', `LLAMA_CPP_MODEL=${LLAMA_CPP_MODEL}`);
    }
  } else {
    // Route API traffic through the credential proxy (containers never see real secrets)
    args.push(
      '-e',
      `ANTHROPIC_BASE_URL=http://${CONTAINER_HOST_GATEWAY}:${CREDENTIAL_PROXY_PORT}`,
    );

    // Mirror the host's auth method with a placeholder value.
    // API key mode: SDK sends x-api-key, proxy replaces with real key.
    // OAuth mode:   Placeholder .credentials.json is written into the group's
    //               session .claude/ dir by container-mounter.ts. The SDK reads
    //               it, sends Bearer placeholder, and the proxy swaps with the
    //               real token. No separate mount needed (avoids Docker conflicts
    //               with the overlapping /home/node/.claude bind mount).
    const authMode = detectAuthMode();
    if (authMode === 'api-key') {
      args.push('-e', 'ANTHROPIC_API_KEY=placeholder');
    }
  }

  // Runtime-specific args for host gateway resolution
  args.push(...hostGatewayArgs());

  // Run as host user so bind-mounted files are accessible.
  // Skip when running as root (uid 0), as the container's node user (uid 1000),
  // or when getuid is unavailable (native Windows without WSL).
  const hostUid = process.getuid?.();
  const hostGid = process.getgid?.();
  if (hostUid != null && hostUid !== 0 && hostUid !== 1000) {
    args.push('--user', `${hostUid}:${hostGid}`);
    args.push('-e', 'HOME=/home/node');
  }

  for (const mount of mounts) {
    if (mount.readonly) {
      args.push(...readonlyMountArgs(mount.hostPath, mount.containerPath));
    } else {
      args.push('-v', `${mount.hostPath}:${mount.containerPath}`);
    }
  }

  args.push(CONTAINER_IMAGE);

  return args;
}

/**
 * Read this dispatch's structured tool calls from the in-container capture log
 * (LIA-154). The hook writes one PER-INTERACTION file at
 * `<logsDir>/tool-calls/<interactionId>.jsonl`, so each read is bounded to this
 * dispatch (no unbounded single-file scan). Best-effort: a missing file (no
 * tools used) or a torn/partial line is skipped, never thrown — capture must
 * not affect the response pipeline.
 */
export function readToolCalls(
  logsDir: string,
  interactionId: string,
): ToolCall[] {
  const out: ToolCall[] = [];
  // MUST stay byte-identical to the container hook's safeInteractionId()
  // (tool-call-log.ts) so both resolve the same per-interaction file.
  const safeId = interactionId.replace(/[^A-Za-z0-9._-]/g, '_');
  try {
    const raw = fs.readFileSync(
      path.join(logsDir, 'tool-calls', `${safeId}.jsonl`),
      'utf8',
    );
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.parse(line) as ToolCall);
      } catch {
        // skip a malformed/torn line, keep the rest
      }
    }
  } catch {
    // no file = no tools used this dispatch
  }
  return out;
}

/**
 * Read this dispatch's offered tool manifest from the in-container capture file
 * (LIA-154). Best-effort: missing/malformed → []. The safeId transform MUST stay
 * byte-identical to the container's safe-interaction-id.ts (else they resolve
 * different files and capture silently drops).
 */
export function readAvailableTools(
  logsDir: string,
  interactionId: string,
): string[] {
  const safeId = interactionId.replace(/[^A-Za-z0-9._-]/g, '_');
  try {
    const raw = fs.readFileSync(
      path.join(logsDir, 'available-tools', `${safeId}.json`),
      'utf8',
    );
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((t): t is string => typeof t === 'string');
    }
  } catch {
    // no file / malformed = no manifest captured this dispatch
  }
  return [];
}

/**
 * Bound the streaming parse buffer (LIA-234). Exported for testing.
 *
 * Called once per stdout data event on the post-while-loop remainder, which is
 * at most ONE partial frame: the marker loop already consumed every complete
 * START..END pair, so a "multiple STARTs" remainder cannot occur. Without this,
 * sibling stdout/stderr accumulators are capped at CONTAINER_MAX_OUTPUT_SIZE but
 * parseBuffer was not — a torn frame (START, no END) or marker-free stdout noise
 * could grow the host heap for the container's full (idle-extendable) lifetime.
 *
 * No START present: keep only the last markerLen-1 bytes (the longest possible
 * split-START prefix), discarding marker-free noise. START present but over the
 * cap: torn frame, drop it — the cap is a strict `>` so the END can still arrive
 * in the same chunk as the boundary byte. Stateless, O(n) per call.
 */
export function boundParseBuffer(
  buffer: string,
  maxSize: number,
): { buffer: string; droppedTornFrame: boolean } {
  if (buffer.indexOf(OUTPUT_START_MARKER) === -1) {
    return {
      buffer:
        buffer.length >= OUTPUT_START_MARKER.length
          ? buffer.slice(-(OUTPUT_START_MARKER.length - 1))
          : buffer,
      droppedTornFrame: false,
    };
  }
  if (buffer.length > maxSize) {
    return { buffer: '', droppedTornFrame: true };
  }
  return { buffer, droppedTornFrame: false };
}

export async function runContainerAgent(
  group: RegisteredGroup,
  input: ContainerInput,
  onProcess: (proc: ChildProcess, containerName: string) => void,
  onOutput?: (output: ContainerOutput) => Promise<void>,
): Promise<ContainerOutput> {
  const startTime = Date.now();
  // Interaction ID for evolution logging (stable per container run). Computed
  // here (not just before logging) so it can be threaded into the container env
  // for the LIA-154 tool-call capture join.
  const interactionId = `${group.folder}-${startTime}`;

  // Detect user signal BEFORE reflections prepend — reflections inflate prompt
  // length past MAX_SIGNAL_LENGTH, causing false-null on short feedback messages.
  const userSignal = detectUserSignal(input.prompt);

  // Pre-dispatch: inject relevant reflections from the evolution loop.
  // getReflections returns '' when evolution is disabled or nothing is found —
  // no tokens are added in that case.
  const reflections = await getReflections(input.prompt, input.groupFolder);
  if (reflections.block) {
    input = { ...input, prompt: `${reflections.block}\n\n${input.prompt}` };
  }

  // LIA-131 Phase 2: inject the active DSPy-optimized prompt, composing WITH (not
  // replacing) the reflections block above — both self-improvement arms prepend at
  // this one fail-safe seam. Default OFF behind EVOLUTION_OPTIMIZED_PROMPTS; the
  // helper fails safe to '' so this is a no-op until an artifact is activated AND
  // the flag is flipped. Shadow only: "wired" is not "validated" — the qa
  // instruction was tuned against a DSPy Predict harness, so shadow deltas (logged
  // below) measure whether it actually transfers to the real agent.
  const optimizedPrompt = await getActivePrompt('qa');
  if (optimizedPrompt.block) {
    input = {
      ...input,
      prompt: `${optimizedPrompt.block}\n\n${input.prompt}`,
    };
    logger.info(
      {
        artifactId: optimizedPrompt.artifactId,
        baselineScore: optimizedPrompt.baselineScore,
        optimizedScore: optimizedPrompt.optimizedScore,
        delta:
          optimizedPrompt.optimizedScore != null &&
          optimizedPrompt.baselineScore != null
            ? optimizedPrompt.optimizedScore - optimizedPrompt.baselineScore
            : undefined,
        sampleCount: optimizedPrompt.sampleCount,
      },
      'evolution: injected optimized prompt (qa)',
    );
  }

  // Detect domain tags for evolution loop metadata (no prompt injection).
  // detectDomainsWithFallback: fast keyword path first; if no keywords match,
  // falls back to a Gemini LLM call bounded to 3 s. Never throws.
  const domains = await detectDomainsWithFallback(input.prompt);

  // Pre-dispatch: build project type hint if group has an associated project.
  // Placed on systemPrompt (session-stable) instead of per-turn user prompt so
  // it's sent once and doesn't repeat across turns in a resumed session.
  if (group.projectId) {
    const project = getProjectById(group.projectId);
    if (project?.type) {
      const parts = [project.type.language];
      if (project.type.framework) parts.push(project.type.framework);
      if (project.type.packageManager)
        parts.push(`pkg:${project.type.packageManager}`);
      if (project.type.testRunner)
        parts.push(`test:${project.type.testRunner}`);
      const hint = `[Project: ${project.name} (${parts.join(', ')}) at /workspace/project${project.readonly ? ' — READ-ONLY' : ''}]`;
      input = { ...input, projectHint: hint };
    } else if (project) {
      const hint = `[Project: ${project.name} at /workspace/project${project.readonly ? ' — READ-ONLY' : ''}]`;
      input = { ...input, projectHint: hint };
    }
  }

  const groupDir = resolveGroupFolderPath(group.folder);
  fs.mkdirSync(groupDir, { recursive: true });

  const mounts = buildVolumeMounts(
    group,
    input.isControlGroup,
    input.worktreePath,
    input.ipcRunKey,
  );
  const safeName = group.folder.replace(/[^a-zA-Z0-9-]/g, '-');
  const containerName = `deus-${safeName}-${Date.now()}`;
  const containerArgs = buildContainerArgs(
    mounts,
    containerName,
    input.backend || 'claude',
    interactionId,
    group,
  );

  logger.debug(
    {
      group: group.name,
      containerName,
      mounts: mounts.map(
        (m) =>
          `${m.hostPath} -> ${m.containerPath}${m.readonly ? ' (ro)' : ''}`,
      ),
      containerArgs: redactContainerArgs(containerArgs),
    },
    'Container mount configuration',
  );

  logger.info(
    {
      group: group.name,
      containerName,
      mountCount: mounts.length,
      isControlGroup: input.isControlGroup,
    },
    'Spawning container agent',
  );

  const logsDir = path.join(groupDir, 'logs');
  fs.mkdirSync(logsDir, { recursive: true });

  return new Promise((resolve) => {
    const container = spawn(CONTAINER_RUNTIME_BIN, containerArgs, {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    onProcess(container, containerName);

    let stdout = '';
    let stderr = '';
    let stdoutTruncated = false;
    let stderrTruncated = false;

    container.stdin.write(JSON.stringify(input));
    container.stdin.end();

    // Streaming output: parse OUTPUT_START/END marker pairs as they arrive
    let parseBuffer = '';
    let newSessionId: string | undefined;
    let newSessionRef: RuntimeSession | undefined;
    let outputChain = Promise.resolve();

    container.stdout.on('data', (data) => {
      const chunk = data.toString();

      // Always accumulate for logging
      if (!stdoutTruncated) {
        const remaining = CONTAINER_MAX_OUTPUT_SIZE - stdout.length;
        if (chunk.length > remaining) {
          stdout += chunk.slice(0, remaining);
          stdoutTruncated = true;
          logger.warn(
            { group: group.name, size: stdout.length },
            'Container stdout truncated due to size limit',
          );
        } else {
          stdout += chunk;
        }
      }

      // Stream-parse for output markers
      if (onOutput) {
        parseBuffer += chunk;
        let startIdx: number;
        while ((startIdx = parseBuffer.indexOf(OUTPUT_START_MARKER)) !== -1) {
          const endIdx = parseBuffer.indexOf(OUTPUT_END_MARKER, startIdx);
          if (endIdx === -1) break; // Incomplete pair, wait for more data

          const jsonStr = parseBuffer
            .slice(startIdx + OUTPUT_START_MARKER.length, endIdx)
            .trim();
          parseBuffer = parseBuffer.slice(endIdx + OUTPUT_END_MARKER.length);

          try {
            const parsed = ContainerOutputSchema.parse(JSON.parse(jsonStr));
            if (parsed.newSessionRef) {
              newSessionRef = parsed.newSessionRef;
            }
            if (parsed.newSessionId) {
              newSessionId = parsed.newSessionId;
            }
            hadStreamingOutput = true;
            lastOutputAt = Date.now();
            // Activity detected — reset the hard timeout
            resetTimeout();
            // Call onOutput for all markers (including null results)
            // so idle timers start even for "silent" query completions.
            // .catch keeps outputChain non-poisoning (LIA-212): a rejected
            // onOutput (e.g. a transient send failure) must not drop later
            // outputs or block the close-time resolve — log and continue.
            outputChain = outputChain
              .then(() => onOutput(parsed))
              .catch((err) => {
                logger.error(
                  { group: group.name, err },
                  'onOutput handler rejected; continuing stream',
                );
              });
          } catch (err) {
            logger.error(
              { group: group.name, err },
              'Failed to parse streamed output chunk',
            );
          }
        }

        // Bound the remainder so a torn frame or marker-free noise can't grow
        // the host heap unbounded over the container's lifetime (LIA-234).
        const beforeLen = parseBuffer.length;
        const bounded = boundParseBuffer(
          parseBuffer,
          CONTAINER_MAX_OUTPUT_SIZE,
        );
        parseBuffer = bounded.buffer;
        if (bounded.droppedTornFrame) {
          logger.warn(
            { group: group.name, parseBufferSize: beforeLen },
            'Container output parse buffer exceeded size limit; dropping torn frame',
          );
        }
      }
    });

    container.stderr.on('data', (data) => {
      const chunk = data.toString();
      const lines = chunk.trim().split('\n');
      for (const line of lines) {
        if (line) logger.debug({ container: group.folder }, line);
      }
      // Don't reset timeout on stderr — SDK writes debug logs continuously.
      // Timeout only resets on actual output (OUTPUT_MARKER in stdout).
      if (stderrTruncated) return;
      const remaining = CONTAINER_MAX_OUTPUT_SIZE - stderr.length;
      if (chunk.length > remaining) {
        stderr += chunk.slice(0, remaining);
        stderrTruncated = true;
        logger.warn(
          { group: group.name, size: stderr.length },
          'Container stderr truncated due to size limit',
        );
      } else {
        stderr += chunk;
      }
    });

    let timedOut = false;
    let hadStreamingOutput = false;
    // Epoch of the last real output marker — used as the latency anchor on the
    // reaped completion paths so an idle-then-reaped dispatch records its response
    // time, not the full (up to IDLE_TIMEOUT) container lifetime (LIA-196).
    let lastOutputAt = 0;
    const configTimeout = group.containerConfig?.timeout || CONTAINER_TIMEOUT;
    // Grace period: hard timeout must be at least IDLE_TIMEOUT + 30s so the
    // graceful _close sentinel has time to trigger before the hard kill fires.
    const timeoutMs = Math.max(configTimeout, IDLE_TIMEOUT + 30_000);

    const killOnTimeout = () => {
      timedOut = true;
      logger.error(
        { group: group.name, containerName },
        'Container timeout, stopping gracefully',
      );
      execFile(
        CONTAINER_RUNTIME_BIN,
        ['stop', '-t', '1', containerName],
        { timeout: 15000 },
        (err) => {
          if (err) {
            logger.warn(
              { group: group.name, containerName, err },
              'Graceful stop failed, force killing container',
            );
            // `docker kill` targets the runtime container; forceKillProcess only
            // reaps the orphaned `docker stop` CLI client, not the container.
            execFile(
              CONTAINER_RUNTIME_BIN,
              ['kill', containerName],
              { timeout: 15000 },
              (killErr) => {
                if (killErr) {
                  logger.error(
                    { group: group.name, containerName, err: killErr },
                    'Force kill failed — container may still be running',
                  );
                }
                if (container.pid != null) forceKillProcess(container.pid);
              },
            );
          }
        },
      );
    };

    let timeout = setTimeout(killOnTimeout, timeoutMs);

    // Reset the timeout whenever there's activity (streaming output)
    const resetTimeout = () => {
      clearTimeout(timeout);
      timeout = setTimeout(killOnTimeout, timeoutMs);
    };

    container.on('close', (code) => {
      clearTimeout(timeout);
      const duration = Date.now() - startTime;

      // Single logInteraction seam (LIA-196): every output-producing completion
      // path logs through this, so a malformed marker / unusual exit can no
      // longer silently drop the interaction.
      const logDispatch = (
        response: string | null,
        sessionId: string | undefined,
        latencyMs: number,
      ): void => {
        logInteraction({
          id: interactionId,
          prompt: input.prompt,
          response,
          groupFolder: group.folder,
          latencyMs,
          sessionId,
          domainPresets: domains.length > 0 ? domains : undefined,
          userSignal: userSignal ?? undefined,
          retrievedReflectionIds:
            reflections.reflectionIds.length > 0
              ? reflections.reflectionIds
              : undefined,
          contextTokens: estimateTokens(input.prompt),
          hasCode: containsCodeBlock(response),
          toolCalls: readToolCalls(logsDir, interactionId),
          availableTools: readAvailableTools(logsDir, interactionId),
        });
      };
      // Reaped paths (idle-after-output, non-zero-after-output) resolve long after
      // the response; anchor latency to the last output, not the container lifetime.
      const reapedLatencyMs = () =>
        (lastOutputAt > 0 ? lastOutputAt : Date.now()) - startTime;

      if (timedOut) {
        const ts = new Date().toISOString().replace(/[:.]/g, '-');
        const timeoutLog = path.join(logsDir, `container-${ts}.log`);
        fs.writeFileSync(
          timeoutLog,
          [
            `=== Container Run Log (TIMEOUT) ===`,
            `Timestamp: ${new Date().toISOString()}`,
            `Group: ${group.name}`,
            `Container: ${containerName}`,
            `Duration: ${duration}ms`,
            `Exit Code: ${code}`,
            `Had Streaming Output: ${hadStreamingOutput}`,
          ].join('\n'),
        );

        // Timeout after output = idle cleanup, not failure.
        // The agent already sent its response; this is just the
        // container being reaped after the idle period expired.
        if (hadStreamingOutput) {
          logger.info(
            { group: group.name, containerName, duration, code },
            'Container timed out after output (idle cleanup)',
          );
          // .then(settle, settle): settle the dispatch promise whether the
          // output chain fulfilled or rejected (LIA-212 defense-in-depth) —
          // this is the only resolve() on this path, so settle unconditionally.
          const settle = () => {
            // Produced output -> still log for the evolution loop (LIA-196).
            logDispatch(null, input.sessionRef?.session_id, reapedLatencyMs());
            resolve({
              status: 'success',
              result: null,
              newSessionRef:
                newSessionRef ??
                (newSessionId
                  ? defaultSession(newSessionId, input.backend || 'claude')
                  : undefined),
              newSessionId,
            });
          };
          outputChain.then(settle, settle);
          return;
        }

        logger.error(
          { group: group.name, containerName, duration, code },
          'Container timed out with no output',
        );

        resolve({
          status: 'error',
          result: null,
          error: `${CONTAINER_TIMEOUT_ERROR_PREFIX} ${configTimeout}ms`,
        });
        return;
      }

      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const logFile = path.join(logsDir, `container-${timestamp}.log`);
      const isVerbose =
        process.env.LOG_LEVEL === 'debug' || process.env.LOG_LEVEL === 'trace';

      const logLines = [
        `=== Container Run Log ===`,
        `Timestamp: ${new Date().toISOString()}`,
        `Group: ${group.name}`,
        `IsControlGroup: ${input.isControlGroup}`,
        `Duration: ${duration}ms`,
        `Exit Code: ${code}`,
        `Stdout Truncated: ${stdoutTruncated}`,
        `Stderr Truncated: ${stderrTruncated}`,
        ``,
      ];

      const isError = code !== 0;

      if (isVerbose || isError) {
        // On error, log input metadata only — not the full prompt.
        // Full input is only included at verbose level to avoid
        // persisting user conversation content on every non-zero exit.
        if (isVerbose) {
          logLines.push(`=== Input ===`, JSON.stringify(input, null, 2), ``);
        } else {
          logLines.push(
            `=== Input Summary ===`,
            `Prompt length: ${input.prompt.length} chars`,
            `Session ID: ${input.sessionRef?.session_id || input.sessionId || 'new'}`,
            ``,
          );
        }
        logLines.push(
          `=== Container Args ===`,
          redactContainerArgs(containerArgs),
          ``,
          `=== Mounts ===`,
          mounts
            .map(
              (m) =>
                `${m.hostPath} -> ${m.containerPath}${m.readonly ? ' (ro)' : ''}`,
            )
            .join('\n'),
          ``,
          `=== Stderr${stderrTruncated ? ' (TRUNCATED)' : ''} ===`,
          stderr,
          ``,
          `=== Stdout${stdoutTruncated ? ' (TRUNCATED)' : ''} ===`,
          stdout,
        );
      } else {
        logLines.push(
          `=== Input Summary ===`,
          `Prompt length: ${input.prompt.length} chars`,
          `Session ID: ${input.sessionRef?.session_id || input.sessionId || 'new'}`,
          ``,
          `=== Mounts ===`,
          mounts
            .map((m) => `${m.containerPath}${m.readonly ? ' (ro)' : ''}`)
            .join('\n'),
          ``,
        );
      }

      fs.writeFileSync(logFile, logLines.join('\n'));
      logger.debug({ logFile, verbose: isVerbose }, 'Container log written');

      if (code !== 0) {
        // Container killed externally after producing output — treat as success
        if (hadStreamingOutput && onOutput) {
          logger.info(
            { group: group.name, code, duration },
            'Container exited non-zero after output (treating as success)',
          );
          // .then(settle, settle): settle whether the output chain fulfilled or
          // rejected (LIA-212 defense-in-depth) — see the idle-cleanup seam.
          const settle = () => {
            // Produced output -> still log for the evolution loop (LIA-196).
            logDispatch(null, input.sessionRef?.session_id, reapedLatencyMs());
            resolve({
              status: 'success',
              result: null,
              newSessionRef:
                newSessionRef ??
                (newSessionId
                  ? defaultSession(newSessionId, input.backend || 'claude')
                  : undefined),
              newSessionId,
            });
          };
          outputChain.then(settle, settle);
          return;
        }

        logger.error(
          {
            group: group.name,
            containerId: containerName,
            exitCode: code,
            duration,
            stderr: stderr.slice(-2000),
            logFile,
          },
          'Container exited with error',
        );

        resolve({
          status: 'error',
          result: null,
          error: `Container exited with code ${code}: ${stderr.slice(-200)}`,
        });
        return;
      }

      // Streaming mode: wait for output chain to settle, return completion marker
      if (onOutput) {
        // .then(settle, settle): settle whether the output chain fulfilled or
        // rejected (LIA-212 defense-in-depth) — see the idle-cleanup seam.
        const settle = () => {
          logger.info(
            { group: group.name, duration, newSessionId },
            'Container completed (streaming mode)',
          );
          // Post-dispatch: log interaction for evolution loop (fire-and-forget)
          logDispatch(null, input.sessionRef?.session_id, duration);
          resolve({
            status: 'success',
            result: null,
            newSessionRef:
              newSessionRef ??
              (newSessionId
                ? defaultSession(newSessionId, input.backend || 'claude')
                : undefined),
            newSessionId,
          });
        };
        outputChain.then(settle, settle);
        return;
      }

      // Legacy mode: parse the last output marker pair from accumulated stdout
      try {
        // Extract JSON between sentinel markers for robust parsing
        const startIdx = stdout.indexOf(OUTPUT_START_MARKER);
        const endIdx = stdout.indexOf(OUTPUT_END_MARKER);

        let jsonLine: string;
        if (startIdx !== -1 && endIdx !== -1 && endIdx > startIdx) {
          jsonLine = stdout
            .slice(startIdx + OUTPUT_START_MARKER.length, endIdx)
            .trim();
        } else {
          // Fallback: last non-empty line (backwards compatibility)
          const lines = stdout.trim().split('\n');
          jsonLine = lines[lines.length - 1];
        }

        const output = ContainerOutputSchema.parse(JSON.parse(jsonLine));

        logger.info(
          {
            group: group.name,
            duration,
            status: output.status,
            hasResult: !!output.result,
          },
          'Container completed',
        );

        // Post-dispatch: log interaction for evolution loop (fire-and-forget)
        logDispatch(
          output.result ?? null,
          input.sessionRef?.session_id ??
            output.newSessionRef?.session_id ??
            output.newSessionId,
          duration,
        );

        resolve(output);
      } catch (err) {
        logger.error(
          {
            group: group.name,
            stdout,
            stderr,
            error: err,
          },
          'Failed to parse container output',
        );

        resolve({
          status: 'error',
          result: null,
          error: `Failed to parse container output: ${err instanceof Error ? err.message : String(err)}`,
        });
      }
    });

    container.on('error', (err) => {
      clearTimeout(timeout);
      logger.error(
        {
          group: group.name,
          containerId: containerName,
          exitCode: null,
          stderr: stderr.slice(-500),
          err,
        },
        'Container spawn error',
      );
      resolve({
        status: 'error',
        result: null,
        error: `Container spawn error: ${err.message}`,
      });
    });
  });
}

export function writeTasksSnapshot(
  groupFolder: string,
  isControlGroup: boolean,
  tasks: Array<{
    id: string;
    groupFolder: string;
    prompt: string;
    schedule_type: string;
    schedule_value: string;
    status: string;
    next_run: string | null;
  }>,
): void {
  // Write filtered tasks to the group's IPC directory
  const groupIpcDir = resolveGroupIpcPath(groupFolder);
  fs.mkdirSync(groupIpcDir, { recursive: true });

  // Main sees all tasks, others only see their own
  const filteredTasks = isControlGroup
    ? tasks
    : tasks.filter((t) => t.groupFolder === groupFolder);

  const tasksFile = path.join(groupIpcDir, 'current_tasks.json');
  fs.writeFileSync(tasksFile, JSON.stringify(filteredTasks, null, 2));
}

export interface AvailableGroup {
  jid: string;
  name: string;
  lastActivity: string;
  isRegistered: boolean;
}

/**
 * Write available groups snapshot for the container to read.
 * Only main group can see all available groups (for activation).
 * Non-main groups only see their own registration status.
 */
export function writeGroupsSnapshot(
  groupFolder: string,
  isControlGroup: boolean,
  groups: AvailableGroup[],
  _registeredJids: Set<string>,
): void {
  const groupIpcDir = resolveGroupIpcPath(groupFolder);
  fs.mkdirSync(groupIpcDir, { recursive: true });

  // Main sees all groups; others see nothing (they can't activate groups)
  const visibleGroups = isControlGroup ? groups : [];

  const groupsFile = path.join(groupIpcDir, 'available_groups.json');
  fs.writeFileSync(
    groupsFile,
    JSON.stringify(
      {
        groups: visibleGroups,
        lastSync: new Date().toISOString(),
      },
      null,
      2,
    ),
  );
}
