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

  it('does NOT flag gh read subcommands (pr view/list)', () => {
    expect(isPushOrMergeTool('gh', ['pr', 'view', '294'])).toBe(false);
    expect(isPushOrMergeTool('gh', ['pr', 'list'])).toBe(false);
  });

  it('fail-closed: flags gh subcommands not in the read allowlist (pr close, issue create)', () => {
    // Tightened by LIA-361: the allowlist names only pr view|list|diff|status|
    // checks as safe, so pr close (historically allowed) and issue create fall
    // through to the default-gated branch.
    expect(isPushOrMergeTool('gh', ['pr', 'close', '294', '--merge'])).toBe(
      true,
    );
    expect(isPushOrMergeTool('gh', ['issue', 'create'])).toBe(true);
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

// Independent oracle (authored from the LIA-361 spec, blind to the
// implementation) — discriminating cases for the fail-closed allowlist gate.
describe('isPushOrMergeTool — @oracle fail-closed allowlist spec', () => {
  describe('non-gh tools', () => {
    it('@oracle: deus-git-push is always a push regardless of args', () => {
      expect(isPushOrMergeTool('deus-git-push', [])).toBe(true);
      expect(isPushOrMergeTool('deus-git-push', ['--dry-run'])).toBe(true);
    });

    it('@oracle: non-gh, non-push tools are always allowed (bash, cat)', () => {
      expect(isPushOrMergeTool('bash', ['-c', 'git push origin main'])).toBe(
        false,
      );
      expect(isPushOrMergeTool('cat', ['/etc/passwd'])).toBe(false);
      expect(isPushOrMergeTool('git', ['push', 'origin', 'main'])).toBe(false);
    });
  });

  describe('gh: read allowlist (FALSE)', () => {
    it('@oracle: gh pr view/list/diff/status/checks are reads', () => {
      expect(isPushOrMergeTool('gh', ['pr', 'view', '294'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['pr', 'list'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['pr', 'diff', '294'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['pr', 'status'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['pr', 'checks', '294'])).toBe(false);
    });

    it('@oracle: gh issue view/list/status are reads', () => {
      expect(isPushOrMergeTool('gh', ['issue', 'view', '1'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['issue', 'list'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['issue', 'status'])).toBe(false);
    });

    it('@oracle: gh repo/release/run/workflow view|list are reads', () => {
      expect(isPushOrMergeTool('gh', ['repo', 'view'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['repo', 'list'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['release', 'view'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['release', 'list'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['run', 'view', '123'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['run', 'list'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['workflow', 'view', 'ci'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['workflow', 'list'])).toBe(false);
    });

    it('@oracle: gh search code|commits|issues|prs|repos are reads', () => {
      expect(isPushOrMergeTool('gh', ['search', 'repos', 'foo'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['search', 'issues', '--author=x'])).toBe(
        false,
      );
      expect(isPushOrMergeTool('gh', ['search', 'code', 'foo'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['search', 'commits', 'foo'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['search', 'prs', 'foo'])).toBe(false);
    });

    it('@oracle: an unknown gh search target fails closed (no wildcard)', () => {
      expect(isPushOrMergeTool('gh', ['search', 'frobnicate'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['search'])).toBe(true);
    });

    it('@oracle: a value that literally reads "push" must not false-trigger', () => {
      // "push" appears as a flag VALUE, not a subcommand token — must stay a read.
      expect(isPushOrMergeTool('gh', ['pr', 'view', '--head', 'push'])).toBe(
        false,
      );
    });
  });

  describe('gh: fail-closed tightening (now TRUE)', () => {
    it('@oracle: gh pr close is now gated (intentional tightening)', () => {
      expect(isPushOrMergeTool('gh', ['pr', 'close', '294'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['pr', 'close', '294', '--merge'])).toBe(
        true,
      );
    });

    it('@oracle: mutating/credential/execution gh subcommands are gated', () => {
      expect(isPushOrMergeTool('gh', ['alias', 'set', 'x', 'y'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['alias', 'import', 'file'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['extension', 'install', 'x/y'])).toBe(
        true,
      );
      expect(isPushOrMergeTool('gh', ['extension', 'exec', 'x'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['secret', 'set', 'FOO'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['gist', 'create', 'file.txt'])).toBe(
        true,
      );
      expect(isPushOrMergeTool('gh', ['ssh-key', 'add', 'key.pub'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['gpg-key', 'add', 'key.asc'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['repo', 'delete', 'o/r'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['repo', 'create', 'x'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['release', 'create', 'v1'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['release', 'delete', 'v1'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['workflow', 'run', 'ci.yml'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['run', 'rerun', '123'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['run', 'cancel', '123'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['run', 'download', '123'])).toBe(true);
    });

    it('@oracle: unknown/typo gh subcommands default to gated (fail closed)', () => {
      expect(isPushOrMergeTool('gh', ['frobnicate', 'x'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['prr', 'view'])).toBe(true); // typo of "pr"
    });

    it('@oracle: -X/--method are position-skipping value-flags when finding the subcommand', () => {
      expect(isPushOrMergeTool('gh', ['-X', 'POST', 'pr', 'view', '1'])).toBe(
        false,
      );
      expect(isPushOrMergeTool('gh', ['-X', 'POST', 'pr', 'merge', '1'])).toBe(
        true,
      );
    });
  });

  describe('gh api: unambiguous GET (FALSE)', () => {
    it('@oracle: plain gh api reads with no method/body flags are allowed', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r/pulls'])).toBe(false);
      expect(isPushOrMergeTool('gh', ['api', '/user'])).toBe(false);
    });

    it('@oracle: -q/--jq output filters are NOT body flags, still a read', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-q', '.name'])).toBe(
        false,
      );
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--jq', '.name']),
      ).toBe(false);
    });
  });

  describe('gh api: mutating method flag (TRUE)', () => {
    it('@oracle: -X VERB (spaced) selects a non-GET verb', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-X', 'PUT'])).toBe(
        true,
      );
    });

    it('@oracle: -XVERB (glued) selects a non-GET verb', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-XPUT'])).toBe(true);
    });

    it('@oracle: --method=verb (lowercase) selects a non-GET verb, case-insensitively', () => {
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--method=put']),
      ).toBe(true);
    });

    it('@oracle: --method VERB (spaced) selects a non-GET verb', () => {
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--method', 'DELETE']),
      ).toBe(true);
    });

    it('@oracle: a dangling -X/--method with no following value fails closed to TRUE', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-X'])).toBe(true);
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '--method'])).toBe(
        true,
      );
    });

    it('@oracle: an explicit -X GET stays a read', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-X', 'GET'])).toBe(
        false,
      );
    });
  });

  describe('gh api: body/param flags (TRUE)', () => {
    it('@oracle: -f key=value (spaced) is a body flag', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-f', 'k=v'])).toBe(
        true,
      );
    });

    it('@oracle: -fkey=value (glued shorthand) is a body flag', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-fk=v'])).toBe(true);
    });

    it('@oracle: --input - (stdin body) is a body flag', () => {
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--input', '-']),
      ).toBe(true);
    });

    it('@oracle: -F/--field/--raw-field and their =-forms are all body flags', () => {
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '-F', 'k=v'])).toBe(
        true,
      );
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--field', 'k=v']),
      ).toBe(true);
      expect(isPushOrMergeTool('gh', ['api', 'repos/o/r', '--field=k=v'])).toBe(
        true,
      );
      expect(
        isPushOrMergeTool('gh', ['api', 'repos/o/r', '--raw-field', 'k=v']),
      ).toBe(true);
    });

    it('@oracle: gh api graphql -f query=... is gated because the body arrives via -f', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'graphql',
          '-f',
          'query=query { viewer { login } }',
        ]),
      ).toBe(true);
    });
  });

  describe('gh api: POSIX shorthand clustering (TRUE) — regression for the -i cluster bypass', () => {
    // gh api exposes the boolean shorthand -i/--include; pflag lets it cluster
    // in front of a value flag, so `-if k=v` == `-i -f k=v`. A string-prefix
    // check on the raw token (`-if`) would miss the hidden -f/-X/-H entirely.
    it('@oracle: -if k=v (clustered body flag) is gated', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-if', 'k=v'])).toBe(
        true,
      );
    });

    it('@oracle: -ifk=v (clustered + glued body flag) is gated', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-ifk=v'])).toBe(true);
    });

    it('@oracle: -iX PUT (clustered method flag) is gated', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-iX', 'PUT'])).toBe(
        true,
      );
    });

    it('@oracle: -iXPUT (clustered + glued method flag) is gated', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-iXPUT'])).toBe(true);
    });

    it('@oracle: -iH with a method-override header (clustered) is gated', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          '/user',
          '-iH',
          'X-HTTP-Method-Override: DELETE',
        ]),
      ).toBe(true);
    });

    it('@oracle: -i alone (boolean include, no value flag) stays a read', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-i'])).toBe(false);
    });

    it('@oracle: -iq .name (include + jq read filter) stays a read', () => {
      expect(isPushOrMergeTool('gh', ['api', '/user', '-iq', '.name'])).toBe(
        false,
      );
    });
  });

  describe('gh api: -H/--header method-override (TRUE)', () => {
    it('@oracle: -H VALUE (spaced) matching method-override is gated', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'repos/o/r',
          '-H',
          'X-HTTP-Method-Override: DELETE',
        ]),
      ).toBe(true);
    });

    it('@oracle: -HVALUE (glued) matching method-override is gated', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'repos/o/r',
          '-HX-HTTP-Method-Override:PATCH',
        ]),
      ).toBe(true);
    });

    it('@oracle: --header=VALUE (=-form) matching method-override is gated', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'repos/o/r',
          '--header=X-HTTP-Method-Override:PUT',
        ]),
      ).toBe(true);
    });

    it('@oracle: every -H occurrence is checked, not just the first', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'repos/o/r',
          '-H',
          'Accept: application/json',
          '-H',
          'X-HTTP-Method-Override: DELETE',
        ]),
      ).toBe(true);
    });

    it('@oracle: an ordinary header with no override match stays a read', () => {
      expect(
        isPushOrMergeTool('gh', [
          'api',
          'repos/o/r',
          '-H',
          'Accept: application/json',
        ]),
      ).toBe(false);
    });
  });
});
