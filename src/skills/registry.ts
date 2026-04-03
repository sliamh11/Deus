/**
 * Skill IPC handler registry.
 *
 * Skills register handlers that process IPC task messages from containers.
 * Handlers are tried in registration order when the core switch-case doesn't
 * match. Each handler returns true if it handled the message, false otherwise.
 *
 * This enables community-contributed skill templates (committed) and private
 * user-specific implementations (local-only) to extend Deus without modifying
 * any tracked files.
 */

import { IpcDeps } from '../ipc.js';

export type SkillIpcHandler = (
  data: Record<string, unknown>,
  sourceGroup: string,
  isControlGroup: boolean,
  deps: IpcDeps,
) => Promise<boolean>;

const registry = new Map<string, SkillIpcHandler>();

export function registerSkillIpcHandler(
  name: string,
  handler: SkillIpcHandler,
): void {
  registry.set(name, handler);
}

export function getSkillIpcHandlers(): Map<string, SkillIpcHandler> {
  return registry;
}

export function getRegisteredSkillNames(): string[] {
  return [...registry.keys()];
}
