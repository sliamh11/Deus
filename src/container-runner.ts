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
  AgentBackendName,
  BackendSessionRef,
  defaultSessionRef,
} from './agent-backends/types.js';
import {
  CONTAINER_IMAGE,
  CONTAINER_MAX_OUTPUT_SIZE,
  CONTAINER_TIMEOUT,
  CREDENTIAL_PROXY_PORT,
  DEUS_CONTEXT_FILE_MAX_CHARS,
  DEUS_OPENAI_MODEL,
  IDLE_TIMEOUT,
  TIMEZONE,
} from './config.js';
import { resolveGroupFolderPath, resolveGroupIpcPath } from './group-folder.js';
import { logger } from './logger.js';
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
import { getReflections, logInteraction } from './evolution-client.js';
import { estimateTokens } from './token-counter.js';
import { detectUserSignal } from './user-signal.js';
import { getProjectById } from './db.js';

// SYNC-REQUIRED: Duplicated in container/agent-runner/src/index.ts.
// Cannot be shared via import — agent-runner is a separate package inside an isolated container.
const OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---';
const OUTPUT_END_MARKER = '---DEUS_OUTPUT_END---';

export interface ContainerInput {
  prompt: string;
  backend?: AgentBackendName;
  sessionId?: string;
  sessionRef?: BackendSessionRef;
  groupFolder: string;
  chatJid: string;
  isControlGroup: boolean;
  isScheduledTask?: boolean;
  assistantName?: string;
  imageAttachments?: Array<{ relativePath: string; mediaType: string }>;
  projectHint?: string;
}

export interface ContainerOutput {
  status: 'success' | 'error';
  result: string | null;
  newSessionRef?: BackendSessionRef;
  newSessionId?: string;
  error?: string;
}

function buildContainerArgs(
  mounts: ReturnType<typeof buildVolumeMounts>,
  containerName: string,
  backend: AgentBackendName,
  group?: RegisteredGroup,
): string[] {
  const args: string[] = ['run', '-i', '--rm', '--name', containerName];

  // Pass host timezone so container's local time matches the user's
  args.push('-e', `TZ=${TIMEZONE}`);
  if (DEUS_CONTEXT_FILE_MAX_CHARS) {
    args.push(
      '-e',
      `DEUS_CONTEXT_FILE_MAX_CHARS=${DEUS_CONTEXT_FILE_MAX_CHARS}`,
    );
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

export async function runContainerAgent(
  group: RegisteredGroup,
  input: ContainerInput,
  onProcess: (proc: ChildProcess, containerName: string) => void,
  onOutput?: (output: ContainerOutput) => Promise<void>,
): Promise<ContainerOutput> {
  const startTime = Date.now();

  // Pre-dispatch: inject relevant reflections from the evolution loop.
  // getReflections returns '' when evolution is disabled or nothing is found —
  // no tokens are added in that case.
  const reflections = await getReflections(input.prompt, input.groupFolder);
  if (reflections.block) {
    input = { ...input, prompt: `${reflections.block}\n\n${input.prompt}` };
  }

  // Detect domain tags for evolution loop metadata (no prompt injection).
  // detectDomainsWithFallback: fast keyword path first; if no keywords match,
  // falls back to a Gemini LLM call bounded to 3 s. Never throws.
  const userSignal = detectUserSignal(input.prompt);
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

  const mounts = buildVolumeMounts(group, input.isControlGroup);
  const safeName = group.folder.replace(/[^a-zA-Z0-9-]/g, '-');
  const containerName = `deus-${safeName}-${Date.now()}`;
  const containerArgs = buildContainerArgs(
    mounts,
    containerName,
    input.backend || 'claude',
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
      containerArgs: containerArgs.join(' '),
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
    let newSessionRef: BackendSessionRef | undefined;
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
            const parsed: ContainerOutput = JSON.parse(jsonStr);
            if (parsed.newSessionRef) {
              newSessionRef = parsed.newSessionRef;
            }
            if (parsed.newSessionId) {
              newSessionId = parsed.newSessionId;
            }
            hadStreamingOutput = true;
            // Activity detected — reset the hard timeout
            resetTimeout();
            // Call onOutput for all markers (including null results)
            // so idle timers start even for "silent" query completions.
            outputChain = outputChain.then(() => onOutput(parsed));
          } catch (err) {
            logger.warn(
              { group: group.name, error: err },
              'Failed to parse streamed output chunk',
            );
          }
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
              'Graceful stop failed, force killing',
            );
            forceKillProcess(container.pid!);
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

    // Interaction ID for evolution logging (stable per container run)
    const interactionId = `${group.folder}-${startTime}`;

    container.on('close', (code) => {
      clearTimeout(timeout);
      const duration = Date.now() - startTime;

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
          outputChain.then(() => {
            resolve({
              status: 'success',
              result: null,
              newSessionRef:
                newSessionRef ??
                (newSessionId
                  ? defaultSessionRef(newSessionId, input.backend || 'claude')
                  : undefined),
              newSessionId,
            });
          });
          return;
        }

        logger.error(
          { group: group.name, containerName, duration, code },
          'Container timed out with no output',
        );

        resolve({
          status: 'error',
          result: null,
          error: `Container timed out after ${configTimeout}ms`,
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
          containerArgs.join(' '),
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
        logger.error(
          {
            group: group.name,
            code,
            duration,
            stderr,
            stdout,
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
        outputChain.then(() => {
          logger.info(
            { group: group.name, duration, newSessionId },
            'Container completed (streaming mode)',
          );
          // Post-dispatch: log interaction for evolution loop (fire-and-forget)
          logInteraction({
            id: interactionId,
            prompt: input.prompt,
            response: null,
            groupFolder: group.folder,
            latencyMs: duration,
            sessionId: input.sessionRef?.session_id,
            domainPresets: domains.length > 0 ? domains : undefined,
            userSignal: userSignal ?? undefined,
            retrievedReflectionIds:
              reflections.reflectionIds.length > 0
                ? reflections.reflectionIds
                : undefined,
            contextTokens: estimateTokens(input.prompt),
          });
          resolve({
            status: 'success',
            result: null,
            newSessionRef:
              newSessionRef ??
              (newSessionId
                ? defaultSessionRef(newSessionId, input.backend || 'claude')
                : undefined),
            newSessionId,
          });
        });
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

        const output: ContainerOutput = JSON.parse(jsonLine);

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
        logInteraction({
          id: interactionId,
          prompt: input.prompt,
          response: output.result,
          groupFolder: group.folder,
          latencyMs: duration,
          sessionId:
            input.sessionRef?.session_id ??
            output.newSessionRef?.session_id ??
            output.newSessionId,
          domainPresets: domains.length > 0 ? domains : undefined,
          userSignal: userSignal ?? undefined,
          retrievedReflectionIds:
            reflections.reflectionIds.length > 0
              ? reflections.reflectionIds
              : undefined,
          contextTokens: estimateTokens(input.prompt),
        });

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
        { group: group.name, containerName, error: err },
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
