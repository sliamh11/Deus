import { DEFAULT_AGENT_RUNTIME } from '../config.js';
import type { RegisteredGroup, ScheduledTask } from '../types.js';
import type { AgentRuntimeId } from './types.js';

export function resolveAgentRuntime(
  group: RegisteredGroup,
  task?: ScheduledTask,
): AgentRuntimeId {
  if (task?.agent_backend) return task.agent_backend;
  if (group.containerConfig?.agentBackend) {
    return group.containerConfig.agentBackend;
  }
  return DEFAULT_AGENT_RUNTIME;
}
