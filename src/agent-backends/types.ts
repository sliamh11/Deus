import type { ToolBroker } from '../tool-broker/types.js';

export type AgentBackendName = 'claude' | 'openai' | 'ollama';

export interface BackendCapabilities {
  shell: boolean;
  filesystem: boolean;
  web: boolean;
  multimodal: boolean;
  handoffs: boolean;
  persistent_sessions: boolean;
  tool_streaming: boolean;
}

export interface BackendSessionRef {
  backend: AgentBackendName;
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
  backendConfig?: Record<string, unknown>;
  imageInputs?: Array<{ relativePath: string; mediaType: string }>;
  toolBroker?: ToolBroker;
}

export type RuntimeEvent =
  | { type: 'output_text'; text: string }
  | { type: 'tool_call'; name: string; arguments: Record<string, unknown> }
  | { type: 'session'; sessionRef: BackendSessionRef }
  | { type: 'error'; error: string };

export type RuntimeEventSink = (event: RuntimeEvent) => void | Promise<void>;

export interface RunResult {
  status: 'success' | 'error';
  result: string | null;
  sessionRef?: BackendSessionRef;
  error?: string;
}

export interface AgentBackend {
  name(): AgentBackendName;
  capabilities(): BackendCapabilities;
  startOrResume(runContext: RunContext): Promise<BackendSessionRef>;
  runTurn(
    runContext: RunContext,
    sessionRef: BackendSessionRef,
    eventSink: RuntimeEventSink,
  ): Promise<RunResult>;
  close(sessionRef: BackendSessionRef): Promise<void>;
}

export function defaultSessionRef(
  sessionId: string,
  backend: AgentBackendName = 'claude',
): BackendSessionRef {
  return {
    backend,
    session_id: sessionId,
  };
}
