import { DEFAULT_AGENT_BACKEND } from '../config.js';
import type { RegisteredGroup, ScheduledTask } from '../types.js';
import type { AgentBackendName } from './types.js';

export function resolveAgentBackend(
  group: RegisteredGroup,
  task?: ScheduledTask,
): AgentBackendName {
  if (task?.agent_backend) return task.agent_backend;
  if (group.containerConfig?.agentBackend) {
    return group.containerConfig.agentBackend;
  }
  return DEFAULT_AGENT_BACKEND;
}
