/**
 * Oracle tests for Ingress Phase 4 — Part A: loadWebhookSources()
 *
 * Authored from the SPEC (LIA-315 Phase 4 contract), BEFORE any implementation
 * exists (oracle-author warden). The module under test
 * (`./webhook-sources.js`) DOES NOT EXIST YET, so every import in this file
 * will fail at resolution — all tests are RED by import-compile failure until
 * the implementer ships `src/ingress/webhook-sources.ts` to the exact contract
 * described here.
 *
 * Independence: written blind to any implementation. Every expected value traces
 * to the spec, not to chosen code. This file must not be weakened after the
 * implementation ships — the @oracle tags are the commit-side integrity signal.
 *
 * Determinism: filesystem interactions use os.tmpdir() scratch dirs, created
 * fresh per test (beforeEach) and cleaned up (afterEach). No wall-clock usage.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// The module under test — DOES NOT EXIST YET. Import failure = RED (expected).
import { loadWebhookSources, type WebhookSource } from './webhook-sources.js';

// ─── Scratch directory per test ───────────────────────────────────────────────

let dir: string;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'oracle-webhook-sources-'));
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

// ─── Helpers ──────────────────────────────────────────────────────────────────

function sourcePath(filename = 'sources.json'): string {
  return join(dir, filename);
}

async function writeSources(
  sources: unknown,
  filename = 'sources.json',
): Promise<string> {
  const p = sourcePath(filename);
  await writeFile(p, JSON.stringify(sources), 'utf8');
  return p;
}

/** Minimal valid source — satisfies every field constraint. */
function validSource(
  over: Partial<{
    name: string;
    hmacHeader: string;
    hmacSecret: string;
    replayStrategy: string;
    targetGroupFolder: string;
  }> = {},
): unknown {
  return {
    name: over.name ?? 'github',
    hmacHeader: over.hmacHeader ?? 'x-hub-signature-256',
    hmacSecret: over.hmacSecret ?? 'secret-abc123',
    replayStrategy: over.replayStrategy ?? 'none',
    targetGroupFolder: over.targetGroupFolder ?? 'webhook-sandbox-github',
  };
}

// =============================================================================
// CASE A1 — valid file → typed array of WebhookSource
// =============================================================================
describe('@oracle loadWebhookSources — valid file returns typed source array', () => {
  it('@oracle returns a non-empty typed array for a well-formed sources file', async () => {
    // @oracle: spec A — valid file → array of typed sources
    const p = await writeSources([
      validSource({ name: 'github', targetGroupFolder: 'sandbox-github' }),
      validSource({ name: 'stripe', targetGroupFolder: 'sandbox-stripe' }),
    ]);

    const sources: WebhookSource[] = loadWebhookSources(p);

    expect(Array.isArray(sources)).toBe(true);
    expect(sources).toHaveLength(2);
    expect(sources[0]!.name).toBe('github');
    expect(sources[1]!.name).toBe('stripe');
  });

  it('@oracle each returned element has the name and targetGroupFolder fields', async () => {
    // @oracle: spec A — returned objects are typed WebhookSource with required fields
    const p = await writeSources([
      validSource({ name: 'github', targetGroupFolder: 'sandbox-github' }),
    ]);

    const sources: WebhookSource[] = loadWebhookSources(p);

    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatchObject({
      name: 'github',
      targetGroupFolder: 'sandbox-github',
    });
  });
});

// =============================================================================
// CASE A2 — missing file → returns [] (does NOT throw)
// =============================================================================
describe('@oracle loadWebhookSources — missing file returns empty array', () => {
  it('@oracle returns [] when the path does not exist (never throws)', () => {
    // @oracle: spec A — missing file → [] (NOT throw); absent config = no sources, not an error
    const nonexistent = join(dir, 'does-not-exist.json');

    let result: unknown;
    expect(() => {
      result = loadWebhookSources(nonexistent);
    }).not.toThrow();

    expect(Array.isArray(result)).toBe(true);
    expect(result as unknown[]).toHaveLength(0);
  });
});

// =============================================================================
// CASE A3 — duplicate `name` → THROWS (fail-closed)
// =============================================================================
describe('@oracle loadWebhookSources — duplicate name throws', () => {
  it('@oracle throws when two sources share the same name', async () => {
    // @oracle: spec A — duplicate name → THROWS; two routes to the same name is an operator config error
    const p = await writeSources([
      validSource({ name: 'github', targetGroupFolder: 'sandbox-a' }),
      validSource({ name: 'github', targetGroupFolder: 'sandbox-b' }),
    ]);

    expect(() => loadWebhookSources(p)).toThrow();
  });
});

// =============================================================================
// CASE A4 — duplicate `targetGroupFolder` → THROWS (fail-closed, R3 1:1)
// =============================================================================
describe('@oracle loadWebhookSources — duplicate targetGroupFolder throws', () => {
  it('@oracle throws when two sources share the same targetGroupFolder (R3 1:1 mapping)', async () => {
    // @oracle: spec A — duplicate targetGroupFolder → THROWS; one source per group (R3) must be enforced at load time
    const p = await writeSources([
      validSource({ name: 'github', targetGroupFolder: 'sandbox-shared' }),
      validSource({ name: 'stripe', targetGroupFolder: 'sandbox-shared' }),
    ]);

    expect(() => loadWebhookSources(p)).toThrow();
  });
});

// =============================================================================
// CASE A5 — invalid name characters → THROWS (must match /^[a-z0-9-]+$/)
// =============================================================================
describe('@oracle loadWebhookSources — invalid name format throws', () => {
  it('@oracle throws for a name with spaces ("Bad Name")', async () => {
    // @oracle: spec A — name must match /^[a-z0-9-]+$/; "Bad Name" contains a space → THROW
    const p = await writeSources([
      validSource({ name: 'Bad Name', targetGroupFolder: 'sandbox-bad' }),
    ]);

    expect(() => loadWebhookSources(p)).toThrow();
  });

  it('@oracle throws for a name that looks like a path traversal ("../x")', async () => {
    // @oracle: spec A — name must match /^[a-z0-9-]+$/; "../x" is invalid and a security risk → THROW
    const p = await writeSources([
      validSource({ name: '../x', targetGroupFolder: 'sandbox-traverse' }),
    ]);

    expect(() => loadWebhookSources(p)).toThrow();
  });

  it('@oracle throws for a name with uppercase letters ("GitHub")', async () => {
    // @oracle: spec A — name must match /^[a-z0-9-]+$/; uppercase is not in the allowed set → THROW
    const p = await writeSources([
      validSource({ name: 'GitHub', targetGroupFolder: 'sandbox-upper' }),
    ]);

    expect(() => loadWebhookSources(p)).toThrow();
  });
});

// =============================================================================
// CASE A6 — hmacSecret env interpolation ("<env:VAR>" → process.env.VAR)
// =============================================================================
describe('@oracle loadWebhookSources — hmacSecret env interpolation', () => {
  it('@oracle resolves "<env:WEBHOOK_TEST_SECRET_ORACLE>" from process.env, not stored literally', async () => {
    // @oracle: spec A — hmacSecret "<env:X>" is replaced by process.env[X] at load time;
    // the raw template string must NOT appear in the returned source
    const envKey = 'WEBHOOK_TEST_SECRET_ORACLE';
    const realSecret = 'resolved-secret-abc-xyz-123';
    process.env[envKey] = realSecret;

    try {
      const p = await writeSources([
        validSource({
          name: 'github',
          hmacSecret: `<env:${envKey}>`,
          targetGroupFolder: 'sandbox-github-env',
        }),
      ]);

      const sources: WebhookSource[] = loadWebhookSources(p);

      expect(sources).toHaveLength(1);
      // The resolved value must be the env var's content, not the literal template.
      expect((sources[0] as { hmacSecret?: string }).hmacSecret).toBe(
        realSecret,
      );
      // The raw template must not appear in the result.
      expect(JSON.stringify(sources)).not.toContain('<env:');
    } finally {
      delete process.env[envKey];
    }
  });
});
