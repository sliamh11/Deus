import type { RuntimeCapabilities } from './types.js';
import {
  ContainerRuntime,
  type ContainerRuntimeDeps,
} from './container-backend.js';

// Capabilities of the llama.cpp local backend.
// - web/multimodal: false. Default Gemma-3-1B GGUF is text-only.
// - handoffs: false. Same parity gap as OpenAI runtime today.
// - tool_streaming: false. Mirrors OpenAI runtime.
// - persistent_sessions: false. The container driver keeps the message
//   history in-memory across turns within a single container lifecycle,
//   but cross-restart resume is NOT supported in this PR — `sessionRef`
//   metadata is ignored on resume. Setting this to `false` prevents the
//   host from assuming a stored session id can be replayed.
const LLAMA_CPP_CAPABILITIES: RuntimeCapabilities = {
  shell: true,
  filesystem: true,
  web: false,
  multimodal: false,
  handoffs: false,
  persistent_sessions: false,
  tool_streaming: false,
};

export function createLlamaCppRuntime(
  deps: ContainerRuntimeDeps,
): ContainerRuntime {
  return new ContainerRuntime('llama-cpp', LLAMA_CPP_CAPABILITIES, deps);
}
