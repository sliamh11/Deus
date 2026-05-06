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
      if (output.result) {
        await eventSink({ type: 'output_text', text: output.result });
      }
      if (output.newSessionRef || output.newSessionId) {
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
        ...(runContext.imageInputs?.length && {
          imageAttachments: runContext.imageInputs,
        }),
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
      result: output.result,
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
