import { describe, it, expect, beforeEach } from 'vitest';

import {
  registerSkillIpcHandler,
  getSkillIpcHandlers,
  getRegisteredSkillNames,
  SkillIpcHandler,
} from './registry.js';

// The registry is module-level state, so we test additively
describe('skill IPC registry', () => {
  const noopHandler: SkillIpcHandler = async () => false;

  it('registers and retrieves a handler', () => {
    registerSkillIpcHandler('test-skill', noopHandler);
    const handlers = getSkillIpcHandlers();
    expect(handlers.has('test-skill')).toBe(true);
    expect(handlers.get('test-skill')).toBe(noopHandler);
  });

  it('lists registered skill names', () => {
    registerSkillIpcHandler('another-skill', noopHandler);
    const names = getRegisteredSkillNames();
    expect(names).toContain('test-skill');
    expect(names).toContain('another-skill');
  });

  it('overwrites handler on re-register', () => {
    const newHandler: SkillIpcHandler = async () => true;
    registerSkillIpcHandler('test-skill', newHandler);
    const handlers = getSkillIpcHandlers();
    expect(handlers.get('test-skill')).toBe(newHandler);
  });
});
