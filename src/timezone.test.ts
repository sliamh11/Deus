import { describe, it, expect } from 'vitest';

import {
  formatLocalTime,
  formatLocalHHMM,
  formatLocalDateTime,
} from './timezone.js';

// --- formatLocalTime ---

describe('formatLocalTime', () => {
  it('converts UTC to local time display', () => {
    // 2026-02-04T18:30:00Z in America/New_York (EST, UTC-5) = 1:30 PM
    const result = formatLocalTime(
      '2026-02-04T18:30:00.000Z',
      'America/New_York',
    );
    expect(result).toContain('1:30');
    expect(result).toContain('PM');
    expect(result).toContain('Feb');
    expect(result).toContain('2026');
  });

  it('handles different timezones', () => {
    // Same UTC time should produce different local times
    const utc = '2026-06-15T12:00:00.000Z';
    const ny = formatLocalTime(utc, 'America/New_York');
    const tokyo = formatLocalTime(utc, 'Asia/Tokyo');
    // NY is UTC-4 in summer (EDT), Tokyo is UTC+9
    expect(ny).toContain('8:00');
    expect(tokyo).toContain('9:00');
  });
});

// --- formatLocalHHMM (LIA-124) ---

describe('formatLocalHHMM', () => {
  it('renders local 24-hour HH:MM for an explicit timezone', () => {
    // 00:18 UTC in Asia/Jerusalem (UTC+3 in June, IDT) = 03:18.
    expect(formatLocalHHMM('2026-05-23T00:18:00.000Z', 'Asia/Jerusalem')).toBe(
      '03:18',
    );
  });

  it('same instant differs by timezone', () => {
    const utc = '2026-06-15T12:00:00.000Z';
    expect(formatLocalHHMM(utc, 'America/New_York')).toBe('08:00'); // EDT -4
    expect(formatLocalHHMM(utc, 'Asia/Tokyo')).toBe('21:00'); // JST +9
    expect(formatLocalHHMM(utc, 'UTC')).toBe('12:00');
  });

  it('normalises midnight to 00:MM (never 24:MM)', () => {
    expect(formatLocalHHMM('2026-06-15T00:00:00.000Z', 'UTC')).toBe('00:00');
    // 15:00 UTC + 9h (Asia/Tokyo) = 24:00 = midnight → must render 00:00, not 24:00.
    expect(formatLocalHHMM('2026-06-15T15:00:00.000Z', 'Asia/Tokyo')).toBe(
      '00:00',
    );
  });
});

// --- formatLocalDateTime (LIA-124) ---

describe('formatLocalDateTime', () => {
  it('renders local YYYY-MM-DD HH:MM for an explicit timezone', () => {
    // 23:30 UTC in Asia/Tokyo (+9) rolls to the next calendar day.
    expect(formatLocalDateTime('2026-06-15T23:30:00.000Z', 'Asia/Tokyo')).toBe(
      '2026-06-16 08:30',
    );
  });

  it('matches the legacy slice shape for UTC (date+time, zero-padded)', () => {
    // The replaced code did `iso.slice(0,16).replace('T',' ')` — same shape in UTC.
    const iso = '2026-06-09T15:30:00.000Z';
    expect(formatLocalDateTime(iso, 'UTC')).toBe('2026-06-09 15:30');
    expect(formatLocalDateTime(iso, 'UTC')).toBe(
      iso.slice(0, 16).replace('T', ' '),
    );
  });
});
