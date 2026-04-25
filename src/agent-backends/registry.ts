import { resolveAgentBackend } from './resolve.js';
import type { AgentBackend, AgentBackendName } from './types.js';
import type { RegisteredGroup, ScheduledTask } from '../types.js';

export class BackendRegistry {
  private backends = new Map<AgentBackendName, AgentBackend>();

  register(backend: AgentBackend): void {
    this.backends.set(backend.name(), backend);
  }

  get(name: AgentBackendName): AgentBackend {
    const backend = this.backends.get(name);
    if (!backend) {
      throw new Error(
        `No backend registered for "${name}". Available: ${[...this.backends.keys()].join(', ')}`,
      );
    }
    return backend;
  }

  has(name: AgentBackendName): boolean {
    return this.backends.has(name);
  }

  list(): AgentBackendName[] {
    return [...this.backends.keys()];
  }

  resolve(group: RegisteredGroup, task?: ScheduledTask): AgentBackend {
    const name = resolveAgentBackend(group, task);
    return this.get(name);
  }
}

let globalRegistry: BackendRegistry | undefined;

export function initBackendRegistry(): BackendRegistry {
  globalRegistry = new BackendRegistry();
  return globalRegistry;
}
