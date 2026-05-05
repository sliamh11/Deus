import type { RuntimeCapabilities } from './types.js';
import {
  ContainerRuntime,
  type ContainerRuntimeDeps,
} from './container-backend.js';

const OPENAI_CAPABILITIES: RuntimeCapabilities = {
  shell: true,
  filesystem: true,
  web: true,
  multimodal: true,
  handoffs: false,
  persistent_sessions: true,
  tool_streaming: false,
};

export function createOpenAIRuntime(
  deps: ContainerRuntimeDeps,
): ContainerRuntime {
  return new ContainerRuntime('openai', OPENAI_CAPABILITIES, deps);
}
