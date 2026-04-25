import type { BackendCapabilities } from './types.js';
import {
  ContainerBackend,
  type ContainerBackendDeps,
} from './container-backend.js';

const OPENAI_CAPABILITIES: BackendCapabilities = {
  shell: true,
  filesystem: true,
  web: true,
  multimodal: true,
  handoffs: false,
  persistent_sessions: true,
  tool_streaming: false,
};

export function createOpenAIBackend(
  deps: ContainerBackendDeps,
): ContainerBackend {
  return new ContainerBackend('openai', OPENAI_CAPABILITIES, deps);
}
