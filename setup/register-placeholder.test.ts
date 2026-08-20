/**
 * Regression tests for #1165 — a falsy channelFormat left the literal
 * `{{CHANNEL_FORMAT}}` in a group's CLAUDE.md.
 *
 * Why this mattered more than a cosmetic leak: the generated file is read as
 * instructions by the agent, and `generateClaudeMdFromTemplate` early-returns
 * when the output already exists ("never overwrite customizations"). So a
 * corrupted file was PERMANENT — setup would never regenerate it and nothing
 * surfaced the problem. The fix refuses to write, which keeps the failure
 * recoverable: fix the input, re-run, and the normal path succeeds.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

vi.mock('../src/logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

const { generateClaudeMdFromTemplate } = await import('./register.js');

let tmp: string;

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-register-'));
});

function writeTemplate(body: string): string {
  const p = path.join(tmp, 'CLAUDE.md.template');
  fs.writeFileSync(p, body);
  return p;
}

describe('generateClaudeMdFromTemplate placeholder safety (#1165)', () => {
  it('refuses to write when channelFormat is empty', () => {
    const tpl = writeTemplate('Reply using {{CHANNEL_FORMAT}} formatting.\n');
    const out = path.join(tmp, 'groups', 'main', 'CLAUDE.md');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
      channelFormat: '',
    });

    expect(wrote).toBe(false);
    // The defect: previously this file was written containing the literal
    // placeholder, and could never be regenerated.
    expect(fs.existsSync(out)).toBe(false);
  });

  it('refuses to write when channelFormat is absent but the template needs it', () => {
    const tpl = writeTemplate('Reply using {{CHANNEL_FORMAT}} formatting.\n');
    const out = path.join(tmp, 'groups', 'main', 'CLAUDE.md');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
    });

    expect(wrote).toBe(false);
    expect(fs.existsSync(out)).toBe(false);
  });

  it('substitutes and writes normally when channelFormat is provided', () => {
    const tpl = writeTemplate(
      '{{ASSISTANT_NAME}} replies using {{CHANNEL_FORMAT}} formatting.\n',
    );
    const out = path.join(tmp, 'groups', 'main', 'CLAUDE.md');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
      channelFormat: 'WhatsApp',
    });

    expect(wrote).toBe(true);
    const written = fs.readFileSync(out, 'utf-8');
    expect(written).toBe('Deus replies using WhatsApp formatting.\n');
    expect(written).not.toContain('{{');
  });

  it('still generates a template that has no CHANNEL_FORMAT placeholder', () => {
    // The global template takes no channelFormat and contains no such
    // placeholder. A naive "require channelFormat" fix would have broken it.
    const tpl = writeTemplate('{{ASSISTANT_NAME}} is your assistant.\n');
    const out = path.join(tmp, 'groups', 'global', 'CLAUDE.md');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
    });

    expect(wrote).toBe(true);
    expect(fs.readFileSync(out, 'utf-8')).toBe('Deus is your assistant.\n');
  });

  it('catches any future unsubstituted placeholder, not just CHANNEL_FORMAT', () => {
    const tpl = writeTemplate('{{ASSISTANT_NAME}} and {{SOME_NEW_TOKEN}}.\n');
    const out = path.join(tmp, 'groups', 'main', 'CLAUDE.md');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
      channelFormat: 'WhatsApp',
    });

    expect(wrote).toBe(false);
    expect(fs.existsSync(out)).toBe(false);
  });

  it('leaves an existing CLAUDE.md untouched', () => {
    const tpl = writeTemplate('Reply using {{CHANNEL_FORMAT}} formatting.\n');
    const out = path.join(tmp, 'CLAUDE.md');
    fs.writeFileSync(out, 'customized by hand\n');

    const wrote = generateClaudeMdFromTemplate(tpl, out, {
      assistantName: 'Deus',
      channelFormat: 'WhatsApp',
    });

    expect(wrote).toBe(false);
    expect(fs.readFileSync(out, 'utf-8')).toBe('customized by hand\n');
  });
});
