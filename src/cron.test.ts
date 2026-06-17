import { describe, expect, it } from 'vitest';

import { parseCronExpression } from './cron.js';

describe('parseCronExpression', () => {
  it('throws on an empty string', () => {
    // The real cron-parser >= 5.5.0 gap: "" is silently parsed as "* * * * *"
    // (every minute). The .trim() guard plugs this so it throws instead (GH #788).
    expect(() => parseCronExpression('', 'UTC')).toThrow(/Invalid cron/);
  });

  it('throws on whitespace-only input', () => {
    // Caught by the .trim() guard. (The parser would also reject these — the guard
    // is belt-and-suspenders here; the only case the parser silently accepts is "".)
    expect(() => parseCronExpression('   ', 'UTC')).toThrow(/Invalid cron/);
    expect(() => parseCronExpression('\t', 'UTC')).toThrow(/Invalid cron/);
  });

  it('parses a valid cron and yields a usable next() time', () => {
    const expr = parseCronExpression('0 9 * * *', 'UTC');
    const next = expr.next().toISOString();
    expect(typeof next).toBe('string');
    expect(next).toMatch(/T09:00:00/);
  });

  it('still throws on other malformed input (delegates to the parser)', () => {
    expect(() => parseCronExpression('not a cron', 'UTC')).toThrow();
  });
});
