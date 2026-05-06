import type { RuntimeCapabilities } from './types.js';
import {
  ContainerRuntime,
  type ContainerRuntimeDeps,
} from './container-backend.js';

const CLAUDE_CAPABILITIES: RuntimeCapabilities = {
  shell: true,
  filesystem: true,
  web: true,
  multimodal: true,
  handoffs: false,
  persistent_sessions: true,
  tool_streaming: true,
};

export function createClaudeRuntime(
  deps: ContainerRuntimeDeps,
): ContainerRuntime {
  return new ContainerRuntime('claude', CLAUDE_CAPABILITIES, deps);
}
