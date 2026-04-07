import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  detectDomains,
  detectDomainsWithFallback,
  parseCustomDomains,
  getAllDomainNames,
} from './domain-presets.js';

// ── detectDomains (keyword detection, unchanged behaviour) ────────────────────

describe('detectDomains', () => {
  it('detects engineering domain from code and bug keywords', () => {
    const domains = detectDomains('I have a bug in my code, can you debug it?');
    expect(domains).toContain('engineering');
  });

  it('detects marketing domain from campaign and conversion keywords', () => {
    const domains = detectDomains(
      'How do I improve our marketing campaign conversion rates?',
    );
    expect(domains).toContain('marketing');
  });

  it('detects strategy domain from roadmap and prioritize keywords', () => {
    const domains = detectDomains(
      'Help me prioritize the roadmap items for Q2 strategy.',
    );
    expect(domains).toContain('strategy');
  });

  it('detects study domain from exam and homework keywords', () => {
    const domains = detectDomains(
      'I have an exam tomorrow and need help with homework problems.',
    );
    expect(domains).toContain('study');
  });

  it('detects writing domain from essay and draft keywords', () => {
    const domains = detectDomains(
      'Can you help me draft an essay and edit it?',
    );
    expect(domains).toContain('writing');
  });

  it('returns empty array when no domain matches', () => {
    const domains = detectDomains('What is the weather like today?');
    expect(domains).toHaveLength(0);
  });

  it('returns empty array for empty string', () => {
    const domains = detectDomains('');
    expect(domains).toHaveLength(0);
  });

  it('detects multiple domains from a mixed prompt', () => {
    // This prompt hits both engineering and writing keywords
    const domains = detectDomains(
      'I need to write documentation (draft + essay style) for my API endpoints and backend code.',
    );
    expect(domains).toContain('engineering');
    expect(domains).toContain('writing');
  });

  it('requires at least 2 keyword hits (does not trigger on single keyword)', () => {
    // "code" alone is only 1 keyword for engineering — needs 2+
    const domains = detectDomains('I like code.');
    expect(domains).not.toContain('engineering');
  });

  it('is case-insensitive', () => {
    const domains = detectDomains('DEBUG my CODE please, there is a BUG');
    expect(domains).toContain('engineering');
  });
});

// ── parseCustomDomains ────────────────────────────────────────────────────────

describe('parseCustomDomains', () => {
  afterEach(() => {
    delete process.env.DEUS_CUSTOM_DOMAINS;
  });

  it('returns empty array when env var is not set', () => {
    delete process.env.DEUS_CUSTOM_DOMAINS;
    expect(parseCustomDomains()).toEqual([]);
  });

  it('returns empty array for empty string', () => {
    process.env.DEUS_CUSTOM_DOMAINS = '';
    expect(parseCustomDomains()).toEqual([]);
  });

  it('parses a single custom domain', () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal';
    expect(parseCustomDomains()).toEqual(['legal']);
  });

  it('parses multiple custom domains', () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal, finance, health';
    expect(parseCustomDomains()).toEqual(['legal', 'finance', 'health']);
  });

  it('normalises to lowercase', () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'Legal,Finance';
    const result = parseCustomDomains();
    expect(result).toContain('legal');
    expect(result).toContain('finance');
  });

  it('filters out blank entries', () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal,,finance,';
    const result = parseCustomDomains();
    expect(result).toEqual(['legal', 'finance']);
  });
});

// ── getAllDomainNames ─────────────────────────────────────────────────────────

describe('getAllDomainNames', () => {
  afterEach(() => {
    delete process.env.DEUS_CUSTOM_DOMAINS;
  });

  it('includes all 5 built-in domains', () => {
    delete process.env.DEUS_CUSTOM_DOMAINS;
    const names = getAllDomainNames();
    expect(names).toContain('engineering');
    expect(names).toContain('marketing');
    expect(names).toContain('strategy');
    expect(names).toContain('study');
    expect(names).toContain('writing');
  });

  it('appends custom domains to built-ins', () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal,finance';
    const names = getAllDomainNames();
    expect(names).toContain('engineering');
    expect(names).toContain('legal');
    expect(names).toContain('finance');
  });
});

// ── detectDomainsWithFallback ────────────────────────────────────────────────

// Mock child_process.execFile so tests never spawn real Python subprocesses.
vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return {
    ...actual,
    execFile: vi.fn(),
  };
});

import { execFile } from 'child_process';

const mockExecFile = vi.mocked(execFile);

/**
 * Helper: make execFile call its callback with (null, stdout, '').
 */
function mockLLMResponse(stdout: string): void {
  mockExecFile.mockImplementationOnce(
    (_cmd: unknown, _args: unknown, _opts: unknown, callback: unknown) => {
      const cb = callback as (
        err: null,
        stdout: string,
        stderr: string,
      ) => void;
      // Simulate async callback
      setTimeout(() => cb(null, stdout, ''), 0);
      return { kill: vi.fn() } as unknown as ReturnType<typeof execFile>;
    },
  );
}

/**
 * Helper: make execFile call its callback with an error (simulates timeout or failure).
 */
function mockLLMError(): void {
  mockExecFile.mockImplementationOnce(
    (_cmd: unknown, _args: unknown, _opts: unknown, callback: unknown) => {
      const cb = callback as (
        err: Error,
        stdout: string,
        stderr: string,
      ) => void;
      setTimeout(() => cb(new Error('subprocess failed'), '', ''), 0);
      return { kill: vi.fn() } as unknown as ReturnType<typeof execFile>;
    },
  );
}

describe('detectDomainsWithFallback', () => {
  beforeEach(() => {
    mockExecFile.mockReset();
    delete process.env.DEUS_CUSTOM_DOMAINS;
  });

  afterEach(() => {
    delete process.env.DEUS_CUSTOM_DOMAINS;
  });

  it('returns keyword result immediately without calling LLM when keywords match', async () => {
    const result = await detectDomainsWithFallback(
      'I have a bug in my code, need to debug it.',
    );
    expect(result).toContain('engineering');
    // execFile must not have been called — pure keyword path
    expect(mockExecFile).not.toHaveBeenCalled();
  });

  it('calls LLM fallback when keyword detection returns empty', async () => {
    mockLLMResponse('["writing"]');
    const result = await detectDomainsWithFallback(
      'What is the weather today?',
    );
    expect(mockExecFile).toHaveBeenCalledOnce();
    expect(result).toEqual(['writing']);
  });

  it('returns [] when LLM fallback returns empty JSON array', async () => {
    mockLLMResponse('[]');
    const result = await detectDomainsWithFallback(
      'Random off-topic question.',
    );
    expect(result).toEqual([]);
  });

  it('returns [] gracefully when LLM subprocess errors', async () => {
    mockLLMError();
    const result = await detectDomainsWithFallback('Something unparseable.');
    expect(result).toEqual([]);
  });

  it('returns [] gracefully when LLM response is malformed JSON', async () => {
    mockLLMResponse('not valid json');
    const result = await detectDomainsWithFallback('Something ambiguous.');
    expect(result).toEqual([]);
  });

  it('filters LLM response to known domains only', async () => {
    // LLM returns a mix of valid and unknown domain names
    mockLLMResponse('["writing", "quantum_physics", "engineering"]');
    const result = await detectDomainsWithFallback(
      'Something with partial domain hints.',
    );
    // Only valid domains survive validation (quantum_physics is not in the list)
    expect(result).toContain('writing');
    expect(result).toContain('engineering');
    expect(result).not.toContain('quantum_physics');
  });

  it('includes custom domains in the LLM call and accepts them in results', async () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal';
    mockLLMResponse('["legal"]');
    const result = await detectDomainsWithFallback(
      'Something about law and contracts.',
    );
    expect(result).toEqual(['legal']);
  });

  it('returns keyword result for hot path even when custom domains are set', async () => {
    process.env.DEUS_CUSTOM_DOMAINS = 'legal';
    const result = await detectDomainsWithFallback(
      'Debug my code, there is a bug.',
    );
    expect(result).toContain('engineering');
    // No LLM call — keywords matched
    expect(mockExecFile).not.toHaveBeenCalled();
  });
});
