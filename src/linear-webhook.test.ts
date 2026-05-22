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
