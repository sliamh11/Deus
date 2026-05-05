import { resolveAgentRuntime } from './resolve.js';
import type { AgentRuntime, AgentRuntimeId } from './types.js';
import type { RegisteredGroup, ScheduledTask } from '../types.js';

export class RuntimeRegistry {
  private backends = new Map<AgentRuntimeId, AgentRuntime>();

  register(backend: AgentRuntime): void {
    this.backends.set(backend.name(), backend);
  }

  get(name: AgentRuntimeId): AgentRuntime {
    const backend = this.backends.get(name);
    if (!backend) {
      throw new Error(
        `No backend registered for "${name}". Available: ${[...this.backends.keys()].join(', ')}`,
      );
    }
    return backend;
  }

  has(name: AgentRuntimeId): boolean {
    return this.backends.has(name);
  }

  list(): AgentRuntimeId[] {
    return [...this.backends.keys()];
  }

  resolve(group: RegisteredGroup, task?: ScheduledTask): AgentRuntime {
    const name = resolveAgentRuntime(group, task);
    return this.get(name);
  }
}

export function initRuntimeRegistry(): RuntimeRegistry {
  return new RuntimeRegistry();
}
