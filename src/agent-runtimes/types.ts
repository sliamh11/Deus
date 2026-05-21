import type { AgentEffortLevel } from '../types.js';
import type { ToolBroker } from '../tool-broker/types.js';

export type AgentRuntimeId = 'claude' | 'openai' | 'llama-cpp';

// Canonical accepted-value gate for AgentRuntimeId. Defined here (next to
// the type) so both `src/config.ts` and `src/ipc.ts` can import it without
// creating a circular dependency (`ipc.ts` already depends on `config.ts`).
export function parseAgentBackend(value: unknown): AgentRuntimeId | undefined {
  return value === 'claude' || value === 'openai' || value === 'llama-cpp'
    ? value
    : undefined;
}

export interface RuntimeCapabilities {
  shell: boolean;
  filesystem: boolean;
  web: boolean;
  multimodal: boolean;
  handoffs: boolean;
  persistent_sessions: boolean;
  tool_streaming: boolean;
}

export interface RuntimeSession {
  backend: AgentRuntimeId;
  session_id: string;
  resume_cursor?: string;
  metadata_json?: string;
}

export interface RunContext {
  prompt: string;
  cwd?: string;
  groupFolder: string;
  chatJid: string;
  isControlGroup: boolean;
  isScheduledTask?: boolean;
  effort?: AgentEffortLevel;
  backendConfig?: Record<string, unknown>;
  imageInputs?: Array<{ relativePath: string; mediaType: string }>;
  toolBroker?: ToolBroker;
}

export type RuntimeEvent =
  | { type: 'output_text'; text: string }
  | { type: 'tool_call'; name: string; arguments: Record<string, unknown> }
  | { type: 'session'; sessionRef: RuntimeSession }
  | { type: 'turn_complete' }
  | { type: 'error'; error: string };

export type RuntimeEventSink = (event: RuntimeEvent) => void | Promise<void>;

export interface RunResult {
  status: 'success' | 'error';
  result: string | null;
  sessionRef?: RuntimeSession;
  error?: string;
}

export interface AgentRuntime {
  name(): AgentRuntimeId;
  capabilities(): RuntimeCapabilities;
  startOrResume(runContext: RunContext): Promise<RuntimeSession>;
  runTurn(
    runContext: RunContext,
    sessionRef: RuntimeSession,
    eventSink: RuntimeEventSink,
  ): Promise<RunResult>;
  close(sessionRef: RuntimeSession): Promise<void>;
}

export function defaultSession(
  sessionId: string,
  backend: AgentRuntimeId = 'claude',
): RuntimeSession {
  return {
    backend,
    session_id: sessionId,
  };
}
