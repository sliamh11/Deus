/**
 * Oracle tests for Ingress Gateway Phase 2 — SAFE_CURATED filtering in buildAllowedTools.
 * Authored from the spec BEFORE implementation exists (oracle-author warden).
 * These tests are RED against origin/main and must go GREEN once the
 * implementer adds:
 *   - SAFE_CURATED: Set<string> const in allowed-tools.ts
 *   - profile?: 'full' | 'webhook' to AllowedToolsOpts
 *   - curatedTools?: string[] to AllowedToolsOpts
 *   - webhook branch in buildAllowedTools:
 *       returns minimal base ['Read','Glob','Grep','WebSearch','TodoWrite','ToolSearch']
 *       PLUS curatedTools filtered to SAFE_CURATED membership only
 *   - full / undefined profile: byte-identical to today's list
 *
 * Every test is tagged @oracle so the oracle-integrity gate can protect it.
 *
 * TEST-SEAM REQUIREMENTS imposed on the implementer:
 *   - SAFE_CURATED must be exported from allowed-tools.ts so the oracle can
 *     introspect membership. If the implementer prefers not to export it,
 *     the oracle assertions below use 'Bash' as a known-unsafe name (which
 *     must NOT be in SAFE_CURATED per the spec: "NO Bash/Write/Edit/Task/
 *     mcp__deus__*") and a caller-supplied safe name. Exporting SAFE_CURATED
 *     is strongly preferred — it makes the oracle self-describing.
 *
 * Assumption: 'Bash', 'Write', 'Edit', 'Task', 'mcp__deus__*' are NOT in
 * SAFE_CURATED per the spec ("NO Bash/Task/Write/Edit/mcp__deus__*").
 * The oracle uses 'Bash' as the canonical "known unsafe" sentinel.
 * A safe curated name is caller-supplied in each test.
 */

import { describe, it, expect } from 'vitest';
import { buildAllowedTools } from './allowed-tools.js';

// Minimal opts for the 'full' profile (regression baseline)
const FULL_BASE_OPTS = {
  teamsNeeded: false,
  hasGcalMcp: false,
  hasLinearMcp: false,
};

// The webhook minimal base set — exactly these, no more, per spec
const WEBHOOK_MINIMAL_BASE = [
  'Read',
  'Glob',
  'Grep',
  'WebSearch',
  'TodoWrite',
  'ToolSearch',
];

// Tools that MUST NOT appear in a webhook profile (spec: "NO Bash/Task/Write/Edit/mcp__deus__*")
const WEBHOOK_EXCLUDED_ALWAYS = [
  'Bash',
  'Write',
  'Edit',
  'Task',
  'mcp__deus__*',
];

describe('oracle (e): SAFE_CURATED filtering — webhook profile', () => {
  it('webhook profile returns the minimal base set', () => {
    // @oracle: webhook minimal base is exactly Read/Glob/Grep/WebSearch/TodoWrite/ToolSearch
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: [],
    });

    for (const expected of WEBHOOK_MINIMAL_BASE) {
      expect(tools).toContain(expected);
    }
  });

  it('webhook profile EXCLUDES Bash, Write, Edit, Task, mcp__deus__*', () => {
    // @oracle: R1 — dangerous tools absent from webhook manifest regardless of curatedTools
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: [],
    });

    for (const excluded of WEBHOOK_EXCLUDED_ALWAYS) {
      expect(tools).not.toContain(excluded);
    }
  });

  it('a curated name NOT in SAFE_CURATED is EXCLUDED from webhook manifest', () => {
    // @oracle: SAFE_CURATED filtering — unknown/unsafe curated names are silently dropped
    // 'Bash' is guaranteed NOT in SAFE_CURATED (spec prohibits it from webhook profile)
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: ['Bash'], // unsafe name — must be filtered out
    });

    expect(tools).not.toContain('Bash');
  });

  it('a curated name IN SAFE_CURATED IS INCLUDED in webhook manifest', () => {
    // @oracle: SAFE_CURATED filtering — approved curated names pass through
    // The spec guarantees that at minimum 'Read' and 'Glob' are in the base set;
    // a truly "curated" addition would be a tool NOT already in the minimal base.
    // We use 'WebFetch' as a plausible safe curated tool to exercise the include path.
    // If the implementer excludes 'WebFetch' from SAFE_CURATED, they must update
    // this test with a tool they DO include — and that is the discriminating action.
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: ['WebFetch'], // expected to be in SAFE_CURATED
    });

    // WebFetch is a read-only, no-side-effect tool appropriate for webhook agents
    expect(tools).toContain('WebFetch');
  });

  it('a mix of SAFE and unsafe curated names: only SAFE ones pass through', () => {
    // @oracle: SAFE_CURATED intersection — mixed curated list is correctly filtered
    // 'Bash' is unsafe (must be dropped), 'WebFetch' is safe (must pass through)
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: ['Bash', 'WebFetch', 'Write', 'Read'],
    });

    // Unsafe names are dropped
    expect(tools).not.toContain('Bash');
    expect(tools).not.toContain('Write'); // also excluded per spec

    // Safe curated names pass through
    expect(tools).toContain('WebFetch');
    // 'Read' is already in the minimal base, so it appears regardless
    expect(tools).toContain('Read');
  });

  it('webhook profile does NOT include SendMessage, TeamCreate, TeamDelete', () => {
    // @oracle: webhook profile is strictly reduced — team/multi-agent tools absent
    const tools = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'webhook',
      curatedTools: [],
    });

    expect(tools).not.toContain('SendMessage');
    expect(tools).not.toContain('TeamCreate');
    expect(tools).not.toContain('TeamDelete');
  });
});

describe('oracle (e): full profile — byte-identical regression guard', () => {
  it("profile='full' returns today's list including Bash, Write, Edit, Task, mcp__deus__*", () => {
    // @oracle: profile=full must be byte-identical to the pre-change behavior
    const fullTools = buildAllowedTools({ ...FULL_BASE_OPTS, profile: 'full' });
    const defaultTools = buildAllowedTools({ ...FULL_BASE_OPTS });

    // Must include all the tools present in today's list
    for (const tool of ['Bash', 'Write', 'Edit', 'Task', 'mcp__deus__*']) {
      expect(fullTools).toContain(tool);
    }

    // profile=full must equal the default (no profile) output exactly
    expect(fullTools).toEqual(defaultTools);
  });

  it('undefined profile is identical to full profile (backward compat)', () => {
    // @oracle: omitting profile must not change behavior for existing callers
    const undefinedProfile = buildAllowedTools({ ...FULL_BASE_OPTS });
    const explicitFull = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'full',
    });

    expect(undefinedProfile).toEqual(explicitFull);
  });

  it('curatedTools ignored when profile is full', () => {
    // @oracle: curatedTools has no effect on full profile — pure backward compat
    const withCurated = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'full',
      curatedTools: ['Read', 'Glob'],
    });
    const withoutCurated = buildAllowedTools({
      ...FULL_BASE_OPTS,
      profile: 'full',
    });

    expect(withCurated).toEqual(withoutCurated);
  });
});
