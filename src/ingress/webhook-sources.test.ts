// Regression coverage for loadWebhookSources beyond the blind @oracle suite.
// Locks the fail-closed targetGroupFolder validation surfaced by the GPT
// code-reviewer co-gate: an invalid folder must throw at LOAD time, so it never
// produces a live /hook route that 202s while no sandbox group can be registered.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadWebhookSources } from './webhook-sources.js';

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'webhook-sources-regression-'));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

async function write(sources: unknown): Promise<string> {
  const p = join(dir, 'sources.json');
  await writeFile(p, JSON.stringify(sources), 'utf8');
  return p;
}

function base(over: Record<string, unknown> = {}) {
  return {
    name: 'github',
    hmacHeader: 'x-hub-signature-256',
    hmacSecret: 'secret',
    replayStrategy: 'none',
    targetGroupFolder: 'webhook-sandbox',
    ...over,
  };
}

describe('loadWebhookSources — targetGroupFolder is validated at load (fail-closed)', () => {
  it('throws on a folder with a path separator', async () => {
    const p = await write([base({ targetGroupFolder: 'a/b' })]);
    expect(() => loadWebhookSources(p)).toThrow();
  });

  it('throws on a folder with traversal / illegal chars', async () => {
    const p = await write([base({ targetGroupFolder: '../escape' })]);
    expect(() => loadWebhookSources(p)).toThrow();
  });

  it('throws on a folder with a leading separator char', async () => {
    const p = await write([base({ targetGroupFolder: '_leading' })]);
    expect(() => loadWebhookSources(p)).toThrow();
  });

  it('accepts a well-formed folder', async () => {
    const p = await write([
      base({ targetGroupFolder: 'webhook-sandbox-github' }),
    ]);
    const sources = loadWebhookSources(p);
    expect(sources).toHaveLength(1);
    expect(sources[0]!.targetGroupFolder).toBe('webhook-sandbox-github');
  });
});
