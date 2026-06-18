/**
 * LIA-315 Phase 2: automated sync guard for the SAFE_CURATED allowlist.
 *
 * SAFE_CURATED is hand-duplicated across the host (src/container-runner.ts —
 * authoritative; bounds the scoped token + DEUS_CURATED_TOOLS) and the container
 * (container/agent-runner/src/allowed-tools.ts — defense-in-depth; bounds the
 * offered manifest). The two packages build in isolation with disjoint tsc rootDirs
 * and cannot share a module (cf. LIA-223). A silent divergence — e.g. adding a tool
 * to one copy but not the other — would widen one enforcement layer without failing
 * any other test. This test fails CI the moment the two sets diverge.
 *
 * The container copy is loaded via a dynamic import with a non-literal specifier so
 * `tsc` does not try to compile it under the host's rootDir (TS6059); vitest resolves
 * it at runtime.
 */
import { describe, it, expect } from 'vitest';
import { SAFE_CURATED as HOST_SAFE_CURATED } from './container-runner.js';

const CONTAINER_ALLOWED_TOOLS =
  '../container/agent-runner/src/allowed-tools.js';

describe('SAFE_CURATED host/container sync (LIA-315)', () => {
  it('the host and container allowlists are byte-identical', async () => {
    const mod = await import(/* @vite-ignore */ CONTAINER_ALLOWED_TOOLS);
    const containerSet = mod.SAFE_CURATED as Set<string>;
    expect([...HOST_SAFE_CURATED].sort()).toEqual([...containerSet].sort());
  });
});
