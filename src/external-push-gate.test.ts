import { beforeEach, describe, expect, it } from 'vitest';

import {
  _initTestDatabase,
  createProject,
  getProjectById,
  getRegisteredGroupByFolder,
  setProjectAllowExternalPush,
  setRegisteredGroup,
} from './db.js';
import { SENSITIVE_FILE_PATTERNS } from './project-registry.js';
import { externalPushDenialReason, isPushOrMergeTool } from './tool-proxy.js';
import type { ProjectConfig, RegisteredGroup } from './types.js';

function group(
  overrides: Partial<RegisteredGroup> & { folder: string },
): RegisteredGroup {
  return {
    name: overrides.folder,
    trigger: '!deus',
    added_at: '2026-06-04T00:00:00Z',
    ...overrides,
  };
}

function project(id: string, allow?: boolean): ProjectConfig {
  return {
    id,
    name: id,
    path: `/host/${id}`,
    type: null,
    readonly: false,
    allow_external_push: allow,
    created_at: '2026-06-04T00:00:00Z',
  };
}

describe('isPushOrMergeTool', () => {
  it('flags the dedicated push tool unconditionally', () => {
    expect(isPushOrMergeTool('deus-git-push', [])).toBe(true);
    expect(isPushOrMergeTool('deus-git-push', ['--force'])).toBe(true);
  });

  it('flags gh pr merge / pr create / merge alias by subcommand position', () => {
    expect(isPushOrMergeTool('gh', ['pr', 'merge', '294', '--squash'])).toBe(
      true,
    );
    expect(isPushOrMergeTool('gh', ['pr', 'create', '--fill'])).toBe(true);
    expect(isPushOrMergeTool('gh', ['merge'])).toBe(true);
    // global flags before the subcommand are skipped (value-consuming -R/--repo)
    expect(isPushOrMergeTool('gh', ['-R', 'o/r', 'pr', 'merge', '1'])).toBe(
      true,
    );
    expect(isPushOrMergeTool('gh', ['--repo', 'o/r', 'pr', 'merge'])).toBe(
      true,
    );
    // equals-form flag is one token (does NOT consume the next as a value)
    expect(isPushOrMergeTool('gh', ['--hostname=h', 'pr', 'merge'])).toBe(true);
  });

  it('does NOT flag non-push gh subcommands (incl. pr close, which is not a merge)', () => {
    expect(isPushOrMergeTool('gh', ['pr', 'view', '294'])).toBe(false);
    expect(isPushOrMergeTool('gh', ['pr', 'list'])).toBe(false);
    expect(isPushOrMergeTool('gh', ['pr', 'close', '294', '--merge'])).toBe(
      false,
    );
    expect(isPushOrMergeTool('gh', ['issue', 'create'])).toBe(false);
  });

  it('does not flag unrelated tools', () => {
    expect(isPushOrMergeTool('ripgrep', ['pr', 'merge'])).toBe(false);
  });
});

describe('externalPushDenialReason (db-backed)', () => {
  beforeEach(() => {
    _initTestDatabase();
  });

  it('allows the home/control project (no projectId)', () => {
    setRegisteredGroup(
      'home@jid',
      group({ folder: 'main', isControlGroup: true }),
    );
    expect(externalPushDenialReason('main')).toBeNull();
  });

  it('blocks an external project that is not allowlisted', () => {
    createProject(project('projA'));
    setRegisteredGroup(
      'extA@jid',
      group({ folder: 'extproj', projectId: 'projA' }),
    );
    const reason = externalPushDenialReason('extproj');
    expect(reason).toContain('projA');
    expect(reason).toContain('blocked');
  });

  it('allows an external project once allowlisted', () => {
    createProject(project('projB'));
    setRegisteredGroup(
      'extB@jid',
      group({ folder: 'extallow', projectId: 'projB' }),
    );
    expect(externalPushDenialReason('extallow')).not.toBeNull(); // blocked by default
    setProjectAllowExternalPush('projB', true);
    expect(externalPushDenialReason('extallow')).toBeNull(); // now allowed
  });

  it('fail-closed: denies an unregistered group folder', () => {
    expect(externalPushDenialReason('ghost')).toContain('not registered');
  });

  it('fail-closed: denies when no group folder resolved (no token)', () => {
    expect(externalPushDenialReason(null)).toContain('blocked');
  });
});

describe('projects.allow_external_push persistence', () => {
  beforeEach(() => {
    _initTestDatabase();
  });

  it('round-trips through createProject/getProjectById and defaults to false', () => {
    createProject(project('p-default'));
    expect(getProjectById('p-default')?.allow_external_push).toBe(false);

    createProject(project('p-true', true));
    expect(getProjectById('p-true')?.allow_external_push).toBe(true);
  });

  it('setProjectAllowExternalPush flips the flag', () => {
    createProject(project('p-flip'));
    expect(getProjectById('p-flip')?.allow_external_push).toBe(false);
    setProjectAllowExternalPush('p-flip', true);
    expect(getProjectById('p-flip')?.allow_external_push).toBe(true);
    setProjectAllowExternalPush('p-flip', false);
    expect(getProjectById('p-flip')?.allow_external_push).toBe(false);
  });

  it('getRegisteredGroupByFolder resolves the group + projectId', () => {
    createProject(project('p-g'));
    setRegisteredGroup('g@jid', group({ folder: 'gfolder', projectId: 'p-g' }));
    const g = getRegisteredGroupByFolder('gfolder');
    expect(g?.projectId).toBe('p-g');
    expect(g?.jid).toBe('g@jid');
    expect(getRegisteredGroupByFolder('nope')).toBeUndefined();
  });
});

describe('SENSITIVE_FILE_PATTERNS', () => {
  it('shadows git credential stores', () => {
    expect(SENSITIVE_FILE_PATTERNS).toContain('.git-credentials');
    expect(SENSITIVE_FILE_PATTERNS).toContain('.netrc');
  });
});
