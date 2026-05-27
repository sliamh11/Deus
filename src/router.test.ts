import { describe, expect, it } from 'vitest';

import {
  escapeXml,
  formatMessages,
  formatOutbound,
  stripInternalTags,
} from './router.js';

describe('escapeXml', () => {
  it('returns empty string for falsy input', () => {
    expect(escapeXml('')).toBe('');
  });

  it('escapes XML special characters', () => {
    expect(escapeXml('&')).toBe('&amp;');
    expect(escapeXml('<')).toBe('&lt;');
    expect(escapeXml('>')).toBe('&gt;');
    expect(escapeXml('"')).toBe('&quot;');
  });

  it('escapes multiple special chars in one string', () => {
    expect(escapeXml('<b>"Tom & Jerry"</b>')).toBe(
      '&lt;b&gt;&quot;Tom &amp; Jerry&quot;&lt;/b&gt;',
    );
  });

  it('leaves plain text unchanged', () => {
    expect(escapeXml('hello world')).toBe('hello world');
  });
});

describe('formatMessages', () => {
  it('produces header and empty messages block for empty array', () => {
    const result = formatMessages([], 'UTC');
    expect(result).toContain('<context timezone="UTC" />');
    expect(result).toContain('<messages>');
    expect(result).toContain('</messages>');
  });

  it('formats a single message with correct XML structure', () => {
    const result = formatMessages(
      [
        {
          id: '1',
          chat_jid: 'g@g.us',
          sender: 'alice@s.whatsapp.net',
          sender_name: 'Alice',
          content: 'hello',
          timestamp: '2026-01-15T12:00:00.000Z',
          is_from_me: false,
          is_bot_message: false,
        },
      ],
      'UTC',
    );
    expect(result).toContain('sender="Alice"');
    expect(result).toContain('>hello</message>');
  });

  it('escapes XML special chars in sender name', () => {
    const result = formatMessages(
      [
        {
          id: '1',
          chat_jid: 'g@g.us',
          sender: 'bob@s.whatsapp.net',
          sender_name: 'Tom & Jerry',
          content: 'hi',
          timestamp: '2026-01-15T12:00:00.000Z',
          is_from_me: false,
          is_bot_message: false,
        },
      ],
      'UTC',
    );
    expect(result).toContain('sender="Tom &amp; Jerry"');
  });
});

describe('stripInternalTags', () => {
  it('returns trimmed original when no tags present', () => {
    expect(stripInternalTags('  hello world  ')).toBe('hello world');
  });

  it('strips single internal block', () => {
    expect(stripInternalTags('before<internal>secret</internal>after')).toBe(
      'beforeafter',
    );
  });

  it('strips multiple and multiline internal blocks', () => {
    const input =
      'start<internal>\nline1\nline2\n</internal>middle<internal>x</internal>end';
    expect(stripInternalTags(input)).toBe('startmiddleend');
  });
});

describe('formatOutbound', () => {
  it('strips internal tags from output', () => {
    expect(formatOutbound('visible<internal>hidden</internal> text')).toBe(
      'visible text',
    );
  });

  it('returns empty string when only internal content', () => {
    expect(formatOutbound('<internal>all hidden</internal>')).toBe('');
  });
});
