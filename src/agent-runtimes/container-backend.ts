import type { ChildProcess } from 'child_process';

import type {
  AgentRuntime,
  AgentRuntimeId,
  RuntimeCapabilities,
  RuntimeSession,
  RunContext,
  RunResult,
  RuntimeEventSink,
} from './types.js';
import { defaultSession } from './types.js';
import {
  type ContainerOutput,
  runContainerAgent,
} from '../container-runner.js';
import type { RegisteredGroup } from '../types.js';
import { resolveAgentEffort } from './resolve.js';

export interface ContainerRuntimeDeps {
  resolveGroup: (groupFolder: string) => RegisteredGroup | undefined;
  assistantName: string;
  registerProcess: (
    chatJid: string,
    proc: ChildProcess,
    containerName: string,
    groupFolder: string,
  ) => void;
}

export class ContainerRuntime implements AgentRuntime {
  constructor(
    private backendName: AgentRuntimeId,
    private caps: RuntimeCapabilities,
    private deps: ContainerRuntimeDeps,
  ) {}

  name(): AgentRuntimeId {
    return this.backendName;
  }

  capabilities(): RuntimeCapabilities {
    return this.caps;
  }

  async startOrResume(_runContext: RunContext): Promise<RuntimeSession> {
    return defaultSession('', this.backendName);
  }

  async runTurn(
    runContext: RunContext,
    sessionRef: RuntimeSession,
    eventSink: RuntimeEventSink,
  ): Promise<RunResult> {
    const group = this.deps.resolveGroup(runContext.groupFolder);
    if (!group) {
      return {
        status: 'error',
        result: null,
        error: `Group not found: ${runContext.groupFolder}`,
      };
    }

    const onOutput = async (output: ContainerOutput) => {
      // Transient streaming variants (Claude backend + stream flag only). These
      // are fire-and-forget side events; they carry no terminal/session state.
      if (output.status === 'partial') {
        if (output.delta)
          await eventSink({ type: 'output_text', text: output.delta });
        return;
      }
      if (output.status === 'activity') {
        if (output.text)
          await eventSink({ type: 'activity', text: output.text });
        return;
      }
      // Terminal markers (success/error). Suppress the final answer emission when
      // it already went out as `partial` deltas (`streamed`), to avoid duplication.
      if (output.result && !output.streamed) {
        await eventSink({ type: 'output_text', text: output.result });
      }
      if (
        (output.newSessionRef || output.newSessionId) &&
        output.status !== 'error'
      ) {
        const ref =
          output.newSessionRef ??
          defaultSession(output.newSessionId!, this.backendName);
        await eventSink({ type: 'session', sessionRef: ref });
      }
      if (output.status === 'error' && output.error) {
        await eventSink({ type: 'error', error: output.error });
      }
      if (output.status === 'success') {
        await eventSink({ type: 'turn_complete' });
      }
    };

    const hasSession = sessionRef.session_id !== '';
    const effort = runContext.effort ?? resolveAgentEffort(group);
    const output = await runContainerAgent(
      group,
      {
        prompt: runContext.prompt,
        backend: this.backendName,
        sessionId: hasSession ? sessionRef.session_id : undefined,
        sessionRef: hasSession ? sessionRef : undefined,
        groupFolder: runContext.groupFolder,
        chatJid: runContext.chatJid,
        isControlGroup: runContext.isControlGroup,
        isScheduledTask: runContext.isScheduledTask,
        assistantName: this.deps.assistantName,
        effort,
        ...(runContext.imageInputs?.length && {
          imageAttachments: runContext.imageInputs,
        }),
        ...(runContext.worktreePath && {
          worktreePath: runContext.worktreePath,
        }),
        ...(runContext.ipcRunKey && {
          ipcRunKey: runContext.ipcRunKey,
        }),
        ...(runContext.stream && { stream: true }),
      },
      (proc, containerName) =>
        this.deps.registerProcess(
          runContext.chatJid,
          proc,
          containerName,
          runContext.groupFolder,
        ),
      onOutput,
    );

    return {
      status: output.status === 'error' ? 'error' : 'success',
      result: output.result ?? null,
      sessionRef:
        output.newSessionRef ??
        (output.newSessionId
          ? defaultSession(output.newSessionId, this.backendName)
          : undefined),
      error: output.error,
    };
  }

  async close(_sessionRef: RuntimeSession): Promise<void> {
    // Session cleanup handled by host via db.clearSession()
  }
}
