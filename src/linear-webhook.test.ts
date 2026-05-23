import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import path from 'path';
import { _initTestDatabase } from './db.js';
import {
  insertWebhookEvent,
  getLastCompletedGateRun,
  updateWebhookEventStatus,
  upsertGateComment,
  getGateCommentId,
} from './db.js';
import { loadGateSpecs } from './linear-gate-specs.js';
import { extractFrontmatter } from './linear-dispatcher.js';
import {
  parseEnrichment,
  parseVerdict,
  parseRatings,
  mergeEnrichment,
  stripEnrichmentSection,
  computeScopeLabelChanges,
} from './linear-webhook.js';

beforeEach(() => {
  _initTestDatabase();
});

describe('webhook event dedup', () => {
  it('inserts first event, rejects duplicate', () => {
    const event = {
      event_key: 'issue1:state-a:state-b:1234567890',
      issue_id: 'issue1',
      gate_to: 'Ready for Agent',
      from_state_id: 'state-a',
      to_state_id: 'state-b',
      webhook_ts: '2026-05-22T00:00:00.000Z',
    };

    expect(insertWebhookEvent(event)).toBe(true);
    expect(insertWebhookEvent(event)).toBe(false);
  });
});

describe('webhook event status tracking', () => {
  it('tracks pending → running → done lifecycle', () => {
    const event = {
      event_key: 'lifecycle-test',
      issue_id: 'issue2',
      gate_to: 'In Review',
      from_state_id: 'state-c',
      to_state_id: 'state-d',
      webhook_ts: '2026-05-22T00:00:00.000Z',
    };
    insertWebhookEvent(event);

    updateWebhookEventStatus('lifecycle-test', 'running');
    updateWebhookEventStatus('lifecycle-test', 'done', { verdict: 'SHIP' });

    const last = getLastCompletedGateRun('issue2', 'In Review');
    expect(last).toBeDefined();
    expect(last!.verdict).toBe('SHIP');
  });
});

describe('cooldown check', () => {
  it('returns undefined when no completed runs exist', () => {
    const result = getLastCompletedGateRun('nonexistent', 'Done');
    expect(result).toBeUndefined();
  });

  it('returns latest completed run', () => {
    const base = {
      issue_id: 'issue3',
      gate_to: 'Done',
      from_state_id: 'state-e',
      to_state_id: 'state-f',
      webhook_ts: '2026-05-22T00:00:00.000Z',
    };

    insertWebhookEvent({ ...base, event_key: 'run1' });
    updateWebhookEventStatus('run1', 'running');
    updateWebhookEventStatus('run1', 'done', { verdict: 'REVISE' });

    insertWebhookEvent({ ...base, event_key: 'run2' });
    updateWebhookEventStatus('run2', 'running');
    updateWebhookEventStatus('run2', 'done', { verdict: 'SHIP' });

    const last = getLastCompletedGateRun('issue3', 'Done');
    expect(last!.verdict).toBe('SHIP');
  });
});

describe('gate comment upsert', () => {
  it('creates and updates gate comment tracking', () => {
    expect(getGateCommentId('issue4', 'In Review')).toBeUndefined();

    upsertGateComment('issue4', 'In Review', 'comment-1');
    expect(getGateCommentId('issue4', 'In Review')).toBe('comment-1');

    upsertGateComment('issue4', 'In Review', 'comment-2');
    expect(getGateCommentId('issue4', 'In Review')).toBe('comment-2');
  });
});

describe('loadGateSpecs', () => {
  const tmpDir = path.join(process.cwd(), '.test-gate-specs-tmp');

  beforeEach(() => {
    fs.mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('loads gate specs keyed by gate_to', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'test-gate.md'),
      `---
name: test-gate
gate_to: "Ready for Agent"
allowed_from: ["Todo"]
mode: advise
fallback: SHIP
cooldown_minutes: 30
model: sonnet
---

Check the issue.`,
    );

    const specs = loadGateSpecs(tmpDir);
    expect(specs.size).toBe(1);
    expect(specs.has('Ready for Agent')).toBe(true);

    const spec = specs.get('Ready for Agent')!;
    expect(spec.name).toBe('test-gate');
    expect(spec.allowedFrom).toEqual(['Todo']);
    expect(spec.mode).toBe('advise');
    expect(spec.fallback).toBe('SHIP');
    expect(spec.cooldownMinutes).toBe(30);
    expect(spec.content).toBe('Check the issue.');
  });

  it('skips files without gate_to', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'no-gate.md'),
      `---
name: no-gate
---

Not a gate.`,
    );

    const specs = loadGateSpecs(tmpDir);
    expect(specs.size).toBe(0);
  });

  it('defaults mode to advise and fallback to SHIP', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'minimal.md'),
      `---
gate_to: "Done"
---

Minimal gate.`,
    );

    const specs = loadGateSpecs(tmpDir);
    const spec = specs.get('Done')!;
    expect(spec.mode).toBe('advise');
    expect(spec.fallback).toBe('SHIP');
    expect(spec.cooldownMinutes).toBe(60);
  });
});

describe('parseEnrichment', () => {
  it('extracts body between Enrichment and Verdict', () => {
    const output = `## Enrichment

## Scope

**Problem**: Fix the login bug.

**Acceptance criteria**:
- [ ] Login works

## Verdict: SHIP

All checks pass.`;
    const result = parseEnrichment(output);
    expect(result).toContain('## Scope');
    expect(result).toContain('Fix the login bug');
    expect(result).toContain('- [ ] Login works');
    expect(result).not.toContain('Verdict');
  });

  it('returns null when no Enrichment section', () => {
    const output = `## Verdict: SHIP\n\nAll checks pass.`;
    expect(parseEnrichment(output)).toBeNull();
  });

  it('preserves multiline content with code blocks', () => {
    const output = `## Enrichment

## Scope

\`\`\`typescript
const x = 1;
\`\`\`

**Plan**: step 1

## Verdict: SHIP

Done.`;
    const result = parseEnrichment(output);
    expect(result).toContain('```typescript');
    expect(result).toContain('const x = 1;');
  });
});

describe('mergeEnrichment', () => {
  it('appends block when no existing markers', () => {
    const result = mergeEnrichment(
      'Original description.',
      'test-gate',
      'New scope content',
    );
    expect(result).toContain('Original description.');
    expect(result).toContain('<!-- gate:test-gate:start -->');
    expect(result).toContain('New scope content');
    expect(result).toContain('<!-- gate:test-gate:end -->');
  });

  it('replaces existing block preserving surrounding content', () => {
    const existing = `Before.

<!-- gate:test-gate:start -->
Old content
<!-- gate:test-gate:end -->

After.`;
    const result = mergeEnrichment(existing, 'test-gate', 'Updated content');
    expect(result).toContain('Before.');
    expect(result).toContain('After.');
    expect(result).toContain('Updated content');
    expect(result).not.toContain('Old content');
  });

  it('creates block as entire description when empty', () => {
    const result = mergeEnrichment('', 'test-gate', 'Content');
    expect(result).toBe(
      '<!-- gate:test-gate:start -->\nContent\n<!-- gate:test-gate:end -->',
    );
  });
});

describe('stripEnrichmentSection', () => {
  it('removes enrichment block, preserves verdict', () => {
    const output = `## Enrichment

Some scope content here.

## Verdict: SHIP

Checklist:
- [x] All good`;
    const result = stripEnrichmentSection(output);
    expect(result).not.toContain('Some scope content');
    expect(result).toContain('## Verdict: SHIP');
    expect(result).toContain('All good');
  });
});

describe('parseRatings', () => {
  it('extracts effort and complexity from enrichment', () => {
    const enrichment = `## Scope

**Ratings**:
- Effort: 3 -- medium multi-file
- Complexity: 2 -- some edge cases
- Impact: 4 -- significant capability gain`;
    const ratings = parseRatings(enrichment);
    expect(ratings.effort).toBe(3);
    expect(ratings.complexity).toBe(2);
  });

  it('returns undefined for missing ratings', () => {
    const ratings = parseRatings('No ratings here.');
    expect(ratings.effort).toBeUndefined();
    expect(ratings.complexity).toBeUndefined();
  });
});

describe('parseVerdict', () => {
  it('extracts SHIP verdict', () => {
    expect(parseVerdict('## Verdict: SHIP\nDone.')).toBe('SHIP');
  });

  it('extracts REVISE verdict', () => {
    expect(parseVerdict('## Verdict: REVISE\nNeeds work.')).toBe('REVISE');
  });

  it('returns null when no verdict', () => {
    expect(parseVerdict('No verdict here.')).toBeNull();
  });
});

describe('loadGateSpecs with effort and fetchComments', () => {
  const tmpDir = path.join(process.cwd(), '.test-gate-enrichment-tmp');

  beforeEach(() => {
    fs.mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('parses effort and fetch_comments from frontmatter', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'enriching-gate.md'),
      `---
name: enriching-gate
gate_to: "Ready for Agent"
allowed_from: ["Todo"]
mode: advise
fallback: SHIP
cooldown_minutes: 30
effort: high
fetch_comments: true
---

Scope the issue.`,
    );

    const specs = loadGateSpecs(tmpDir);
    const spec = specs.get('Ready for Agent')!;
    expect(spec.effort).toBe('high');
    expect(spec.fetchComments).toBe(true);
  });

  it('defaults effort to undefined and fetchComments to false', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'basic-gate.md'),
      `---
gate_to: "Done"
---

Check.`,
    );

    const specs = loadGateSpecs(tmpDir);
    const spec = specs.get('Done')!;
    expect(spec.effort).toBeUndefined();
    expect(spec.fetchComments).toBe(false);
  });
});

describe('extractFrontmatter', () => {
  it('parses valid YAML frontmatter', () => {
    const result = extractFrontmatter(`---
key: value
---

Body text.`);
    expect(result.data).toEqual({ key: 'value' });
    expect(result.body.trim()).toBe('Body text.');
  });

  it('returns empty data for no frontmatter', () => {
    const result = extractFrontmatter('Just body text.');
    expect(result.data).toEqual({});
    expect(result.body).toBe('Just body text.');
  });

  it('handles invalid YAML gracefully', () => {
    const result = extractFrontmatter(`---
bad: yaml: content: here
---

Body.`);
    expect(result.data).toEqual({});
  });
});

describe('computeScopeLabelChanges', () => {
  const gateLabels = {
    scoped: 'label-scoped-id',
    revise: 'label-revise-id',
    effort: {},
    complexity: {},
  };

  it('adds scoped label on agent-readiness-gate SHIP with enrichment', () => {
    const result = computeScopeLabelChanges(
      'agent-readiness-gate',
      'SHIP',
      '## Scope\n\n**Problem**: fix the bug',
      gateLabels,
    );
    expect(result.addIds).toEqual(['label-scoped-id']);
    expect(result.removeIds).toEqual(['label-revise-id']);
  });

  it('no labels on agent-readiness-gate SHIP without enrichment', () => {
    const result = computeScopeLabelChanges(
      'agent-readiness-gate',
      'SHIP',
      undefined,
      gateLabels,
    );
    expect(result.addIds).toEqual([]);
    expect(result.removeIds).toEqual([]);
  });

  it('adds revise label on agent-readiness-gate REVISE', () => {
    const result = computeScopeLabelChanges(
      'agent-readiness-gate',
      'REVISE',
      undefined,
      gateLabels,
    );
    expect(result.addIds).toEqual(['label-revise-id']);
    expect(result.removeIds).toEqual(['label-scoped-id']);
  });

  it('no labels when verdict is undefined (crash path)', () => {
    const result = computeScopeLabelChanges(
      'agent-readiness-gate',
      undefined,
      undefined,
      gateLabels,
    );
    expect(result.addIds).toEqual([]);
    expect(result.removeIds).toEqual([]);
  });

  it('no labels for non-readiness gates even on SHIP with enrichment', () => {
    const result = computeScopeLabelChanges(
      'output-quality-gate',
      'SHIP',
      '## Scope\n\nsome enrichment',
      gateLabels,
    );
    expect(result.addIds).toEqual([]);
    expect(result.removeIds).toEqual([]);
  });
});
