import { describe, it, expect, beforeEach, vi } from 'vitest';

// Mock the checks module entirely — startup-gate just orchestrates them
vi.mock('./checks.js', () => ({
  hasApiCredentials: vi.fn(() => false),
  hasGeminiApiKey: vi.fn(() => false),
  hasMemoryVault: vi.fn(() => ({ ok: false, path: null })),
  hasPythonDeps: vi.fn(() => ({ ok: false, missing: ['python3'] })),
  hasAnyChannelAuth: vi.fn(() => false),
  hasContainerImage: vi.fn(() => false),
  countRegisteredGroups: vi.fn(() => 0),
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import {
  hasApiCredentials,
  hasGeminiApiKey,
  hasMemoryVault,
  hasPythonDeps,
  hasAnyChannelAuth,
  hasContainerImage,
  countRegisteredGroups,
} from './checks.js';
import {
  runStartupChecks,
  registerStartupCheck,
  StartupCheck,
} from './startup-gate.js';

const mockHasApiCredentials = vi.mocked(hasApiCredentials);
const mockHasGeminiApiKey = vi.mocked(hasGeminiApiKey);
const mockHasMemoryVault = vi.mocked(hasMemoryVault);
const mockHasPythonDeps = vi.mocked(hasPythonDeps);
const mockHasAnyChannelAuth = vi.mocked(hasAnyChannelAuth);
const mockHasContainerImage = vi.mocked(hasContainerImage);
const mockCountRegisteredGroups = vi.mocked(countRegisteredGroups);

beforeEach(() => {
  // Reset all check mocks to failing defaults
  mockHasApiCredentials.mockReturnValue(false);
  mockHasGeminiApiKey.mockReturnValue(false);
  mockHasMemoryVault.mockReturnValue({ ok: false, path: null });
  mockHasPythonDeps.mockReturnValue({ ok: false, missing: ['python3'] });
  mockHasAnyChannelAuth.mockReturnValue(false);
  mockHasContainerImage.mockReturnValue(false);
  mockCountRegisteredGroups.mockReturnValue(0);
});

// ── runStartupChecks ──────────────────────────────────────────────────────

describe('runStartupChecks', () => {
  it('returns fatal when API credentials are missing', () => {
    mockHasApiCredentials.mockReturnValue(false);
    const report = runStartupChecks();
    const fatalNames = report.fatals.map((r) => r.name);
    expect(fatalNames).toContain('API credentials');
  });

  it('does not put API credentials in fatals when configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const passedNames = report.passed.map((r) => r.name);
    expect(passedNames).toContain('API credentials');
  });

  it('puts Memory vault in warnings when not configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: false, path: null });
    const report = runStartupChecks();
    const warnNames = report.warnings.map((r) => r.name);
    expect(warnNames).toContain('Memory vault');
  });

  it('puts Memory vault in passed when configured and exists', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: true, path: '/tmp/vault' });
    const report = runStartupChecks();
    const passedNames = report.passed.map((r) => r.name);
    expect(passedNames).toContain('Memory vault');
  });

  it('puts Gemini API key in suggestions when not configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Gemini API key');
  });

  it('puts Channels in suggestions when none configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Channels');
  });

  it('puts Registered groups in suggestions when none registered', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Registered groups');
  });

  it('returns all passed when everything is healthy', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasGeminiApiKey.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: true, path: '/tmp/vault' });
    mockHasPythonDeps.mockReturnValue({ ok: true, missing: [] });
    mockHasAnyChannelAuth.mockReturnValue(true);
    mockHasContainerImage.mockReturnValue(true);
    mockCountRegisteredGroups.mockReturnValue(2);

    const report = runStartupChecks();
    expect(report.fatals).toHaveLength(0);
    expect(report.warnings).toHaveLength(0);
    expect(report.suggestions).toHaveLength(0);
    expect(report.passed.length).toBeGreaterThan(0);
  });
});

// ── registerStartupCheck ──────────────────────────────────────────────────

describe('registerStartupCheck', () => {
  it('custom registered check appears in the report', () => {
    const customCheck: StartupCheck = {
      name: 'Custom test check',
      level: 'suggest',
      run: () => ({
        name: 'Custom test check',
        level: 'suggest',
        ok: false,
        hint: 'This is a test hint',
      }),
    };

    registerStartupCheck(customCheck);
    const report = runStartupChecks();

    const allResults = [
      ...report.fatals,
      ...report.warnings,
      ...report.suggestions,
      ...report.passed,
    ];
    const found = allResults.find((r) => r.name === 'Custom test check');
    expect(found).toBeDefined();
    expect(found!.hint).toBe('This is a test hint');
  });
});
