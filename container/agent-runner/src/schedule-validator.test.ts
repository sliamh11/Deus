import { describe, expect, it } from 'vitest';

import { validateSchedule } from './schedule-validator.js';

describe('validateSchedule', () => {
  // cron tests
  it('accepts a valid cron expression', () => {
    const result = validateSchedule({
      schedule_type: 'cron',
      schedule_value: '0 9 * * *',
    });
    expect(result.ok).toBe(true);
  });

  it('accepts a valid every-N-minutes cron expression', () => {
    const result = validateSchedule({
      schedule_type: 'cron',
      schedule_value: '*/5 * * * *',
    });
    expect(result.ok).toBe(true);
  });

  it('rejects an invalid cron expression', () => {
    const result = validateSchedule({
      schedule_type: 'cron',
      schedule_value: 'not-a-cron',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid cron/);
  });

  it('rejects an empty cron expression', () => {
    const result = validateSchedule({
      schedule_type: 'cron',
      schedule_value: '',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid cron/);
  });

  // interval tests
  it('accepts a valid positive interval', () => {
    const result = validateSchedule({
      schedule_type: 'interval',
      schedule_value: '300000',
    });
    expect(result.ok).toBe(true);
  });

  it('rejects a non-numeric interval', () => {
    const result = validateSchedule({
      schedule_type: 'interval',
      schedule_value: 'abc',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid interval/);
  });

  it('rejects a negative interval', () => {
    const result = validateSchedule({
      schedule_type: 'interval',
      schedule_value: '-1',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid interval/);
  });

  it('rejects a zero interval', () => {
    const result = validateSchedule({
      schedule_type: 'interval',
      schedule_value: '0',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid interval/);
  });

  // once tests
  it('accepts a valid local timestamp', () => {
    const result = validateSchedule({
      schedule_type: 'once',
      schedule_value: '2026-02-01T15:30:00',
    });
    expect(result.ok).toBe(true);
  });

  it('rejects a UTC timestamp with Z suffix', () => {
    const result = validateSchedule({
      schedule_type: 'once',
      schedule_value: '2026-02-01T15:30:00Z',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/local time/);
  });

  it('rejects a timestamp with +HH:MM offset', () => {
    const result = validateSchedule({
      schedule_type: 'once',
      schedule_value: '2026-02-01T15:30:00+03:00',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/local time/);
  });

  it('rejects an unparseable timestamp string', () => {
    const result = validateSchedule({
      schedule_type: 'once',
      schedule_value: 'not-a-date',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Invalid timestamp/);
  });

  it('preserves optional target_group_jid in success value', () => {
    const result = validateSchedule({
      schedule_type: 'cron',
      schedule_value: '0 9 * * *',
      target_group_jid: 'test-jid@g.us',
    });
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.target_group_jid).toBe('test-jid@g.us');
  });
});
