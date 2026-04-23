import { describe, expect, it } from 'vitest';

import {
  parseOpenAISessionMetadata,
  shouldResumeOpenAIResponse,
} from './openai-backend.js';
import {
  buildRipgrepSearchArgs,
  getOpenAIToolDefinitions,
  normalizeTaskContextMode,
  resolveGroupAttachmentPath,
  resolvePublicWebTarget,
  resolveWorkspacePath,
  splitCommandArgs,
  validateScheduleInput,
} from './tool-broker.js';

describe('OpenAI backend safety helpers', () => {
  it('blocks workspace path traversal outside mounted roots', () => {
    expect(resolveWorkspacePath('/workspace/group', 'notes.md')).toBe(
      '/workspace/group/notes.md',
    );
    expect(() =>
      resolveWorkspacePath('/workspace/group', '../../etc/passwd'),
    ).toThrow(/escapes/);
  });

  it('keeps image attachments constrained to the group workspace', () => {
    expect(resolveGroupAttachmentPath('images/photo.png')).toBe(
      '/workspace/group/images/photo.png',
    );
    expect(() => resolveGroupAttachmentPath('../vault/CLAUDE.md')).toThrow(
      /escapes/,
    );
  });

  it('passes ripgrep patterns after -- so option-like input is literal', () => {
    expect(buildRipgrepSearchArgs('--pre=touch /tmp/should-not-run')).toEqual([
      '-n',
      '--',
      '--pre=touch /tmp/should-not-run',
      '.',
    ]);
  });

  it('splits agent-browser args without shell expansion', () => {
    expect(splitCommandArgs('open "two words" \\$HOME')).toEqual([
      'open',
      'two words',
      '$HOME',
    ]);
  });

  it('rejects local and private web_fetch targets before request time', async () => {
    await expect(resolvePublicWebTarget('http://localhost/')).rejects.toThrow(
      /local|internal/,
    );
    await expect(resolvePublicWebTarget('http://127.0.0.1/')).rejects.toThrow(
      /private/,
    );
    await expect(resolvePublicWebTarget('ftp://example.com/')).rejects.toThrow(
      /http/,
    );
  });

  it('uses Claude-compatible task schedule validation', () => {
    expect(validateScheduleInput('cron', '0 9 * * *')).toBeNull();
    expect(validateScheduleInput('interval', '300000')).toBeNull();
    expect(validateScheduleInput('once', '2026-02-01T15:30:00')).toBeNull();
    expect(validateScheduleInput('once', '2026-02-01T15:30:00Z')).toMatch(
      /local time/,
    );
    expect(validateScheduleInput('interval', '-1')).toMatch(/Invalid interval/);
  });

  it('defaults scheduled tasks to group context like Claude MCP tools', () => {
    expect(normalizeTaskContextMode(undefined)).toBe('group');
    expect(normalizeTaskContextMode('group')).toBe('group');
    expect(normalizeTaskContextMode('isolated')).toBe('isolated');
  });

  it('exposes sender on send_message for channel bot parity', () => {
    const sendMessage = getOpenAIToolDefinitions().find(
      (tool) => tool.name === 'send_message',
    );
    expect(sendMessage).toBeDefined();
    expect(sendMessage?.parameters.properties).toHaveProperty('sender');
  });

  it('stores compacted OpenAI continuity as Deus-owned metadata', () => {
    expect(
      parseOpenAISessionMetadata(
        JSON.stringify({
          compact_summary: 'User prefers concise updates.',
          compacted_at: '2026-04-23T00:00:00.000Z',
          base_response_id: 'resp_123',
        }),
      ),
    ).toEqual({
      compact_summary: 'User prefers concise updates.',
      compacted_at: '2026-04-23T00:00:00.000Z',
      base_response_id: 'resp_123',
    });
    expect(parseOpenAISessionMetadata('{bad')).toEqual({});
  });

  it('does not send synthetic compact session ids to Responses resume', () => {
    expect(shouldResumeOpenAIResponse('resp_123')).toBe(true);
    expect(shouldResumeOpenAIResponse('openai-compact-1')).toBe(false);
    expect(shouldResumeOpenAIResponse(undefined)).toBe(false);
  });
});
