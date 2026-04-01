import { describe, it, expect } from 'vitest';
import { detectDomains } from './domain-presets.js';

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
    const domains = detectDomains('I have an exam tomorrow and need help with homework problems.');
    expect(domains).toContain('study');
  });

  it('detects writing domain from essay and draft keywords', () => {
    const domains = detectDomains('Can you help me draft an essay and edit it?');
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
