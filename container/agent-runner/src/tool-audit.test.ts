import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'fs';
import {
  isAuditedTool,
  writeAuditEntry,
  generateToolUseId,
} from './tool-audit.js';

vi.mock('fs');

describe('tool-audit', () => {
  describe('isAuditedTool', () => {
    it('matches exact deus tools', () => {
      expect(isAuditedTool('mcp__deus__send_message')).toBe(true);
      expect(isAuditedTool('mcp__deus__schedule_task')).toBe(true);
      expect(isAuditedTool('mcp__deus__update_task')).toBe(true);
      expect(isAuditedTool('mcp__deus__delete_task')).toBe(true);
    });

    it('matches google calendar prefix', () => {
      expect(isAuditedTool('mcp__google_calendar__create_event')).toBe(true);
      expect(isAuditedTool('mcp__google_calendar__delete_event')).toBe(true);
    });

    it('matches gmail prefix', () => {
      expect(isAuditedTool('mcp__gmail__send_email')).toBe(true);
    });

    it('rejects non-audited tools', () => {
      expect(isAuditedTool('mcp__deus__list_tasks')).toBe(false);
      expect(isAuditedTool('Read')).toBe(false);
      expect(isAuditedTool('Bash')).toBe(false);
      expect(isAuditedTool('mcp__youtube__get_transcript')).toBe(false);
    });
  });

  describe('writeAuditEntry', () => {
    const env = process.env;

    beforeEach(() => {
      process.env = { ...env, DEUS_GROUP_FOLDER: 'test-group' };
      vi.clearAllMocks();
      vi.mocked(fs.mkdirSync).mockReturnValue(undefined);
      vi.mocked(fs.appendFileSync).mockReturnValue(undefined);
    });

    afterEach(() => {
      process.env = env;
    });

    it('writes JSONL entry with correct fields', () => {
      writeAuditEntry('mcp__deus__send_message', 'tu-123', { text: 'hello' });

      expect(fs.appendFileSync).toHaveBeenCalledOnce();
      const written = vi.mocked(fs.appendFileSync).mock.calls[0][1] as string;
      const entry = JSON.parse(written.trim());

      expect(entry.tool).toBe('mcp__deus__send_message');
      expect(entry.tool_use_id).toBe('tu-123');
      expect(entry.group).toBe('test-group');
      expect(entry.args_preview).toContain('hello');
      expect(entry.ts).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    });

    it('truncates args_preview to 500 chars', () => {
      const longArgs = { text: 'x'.repeat(1000) };
      writeAuditEntry('mcp__deus__send_message', 'tu-456', longArgs);

      const written = vi.mocked(fs.appendFileSync).mock.calls[0][1] as string;
      const entry = JSON.parse(written.trim());
      expect(entry.args_preview.length).toBeLessThanOrEqual(500);
    });

    it('uses unknown when DEUS_GROUP_FOLDER is not set', () => {
      delete process.env.DEUS_GROUP_FOLDER;
      writeAuditEntry('mcp__deus__send_message', 'tu-789', {});

      const written = vi.mocked(fs.appendFileSync).mock.calls[0][1] as string;
      const entry = JSON.parse(written.trim());
      expect(entry.group).toBe('unknown');
    });

    it('does nothing when DEUS_TOOL_AUDIT_LOG=0', () => {
      process.env.DEUS_TOOL_AUDIT_LOG = '0';
      writeAuditEntry('mcp__deus__send_message', 'tu-000', {});
      expect(fs.appendFileSync).not.toHaveBeenCalled();
    });

    it('does not throw on fs errors', () => {
      vi.mocked(fs.appendFileSync).mockImplementation(() => {
        throw new Error('disk full');
      });
      expect(() =>
        writeAuditEntry('mcp__deus__send_message', 'tu-err', {}),
      ).not.toThrow();
    });
  });

  describe('generateToolUseId', () => {
    it('returns a string with openai prefix', () => {
      const id = generateToolUseId();
      expect(id).toMatch(/^openai-\d+-\d+$/);
    });

    it('generates unique IDs on successive calls', () => {
      const ids = new Set(
        Array.from({ length: 100 }, () => generateToolUseId()),
      );
      expect(ids.size).toBe(100);
    });
  });
});
