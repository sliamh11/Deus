import type { BackendCapabilities } from './types.js';
import {
  ContainerBackend,
  type ContainerBackendDeps,
} from './container-backend.js';

const CLAUDE_CAPABILITIES: BackendCapabilities = {
  shell: true,
  filesystem: true,
  web: true,
  multimodal: true,
  handoffs: false,
  persistent_sessions: true,
  tool_streaming: true,
};

export function createClaudeBackend(
  deps: ContainerBackendDeps,
): ContainerBackend {
  return new ContainerBackend('claude', CLAUDE_CAPABILITIES, deps);
}
