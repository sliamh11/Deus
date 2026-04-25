export type ToolCapability =
  | 'filesystem'
  | 'shell'
  | 'web'
  | 'browser'
  | 'deus-ipc';

export interface ToolDescriptor {
  name: string;
  capability: ToolCapability;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface ToolCallRequest {
  name: string;
  arguments: Record<string, unknown>;
  context: {
    groupFolder: string;
    chatJid: string;
    cwd?: string;
  };
}

export interface ToolCallResult {
  ok: boolean;
  content?: unknown;
  error?: string;
}

export interface ToolBroker {
  listTools(): ToolDescriptor[];
  callTool(request: ToolCallRequest): Promise<ToolCallResult>;
}

export const CANONICAL_TOOL_CAPABILITIES: readonly ToolCapability[] = [
  'filesystem',
  'shell',
  'web',
  'browser',
  'deus-ipc',
];
