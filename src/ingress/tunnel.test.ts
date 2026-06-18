import { describe, it, expect } from 'vitest';
import { buildNgrokArgs, extractPublicUrl } from './tunnel.js';

describe('buildNgrokArgs', () => {
  it('builds the base http args', () => {
    expect(buildNgrokArgs(3007)).toEqual([
      'http',
      '3007',
      '--log',
      'stdout',
      '--log-format',
      'json',
    ]);
  });

  it('adds --url for a static domain (hostname form)', () => {
    const args = buildNgrokArgs(3007, 'foo.ngrok-free.dev');
    expect(args).toContain('--url');
    expect(args).toContain('https://foo.ngrok-free.dev');
  });

  it('strips an existing scheme from the static domain', () => {
    const args = buildNgrokArgs(3007, 'https://foo.ngrok-free.dev');
    expect(args).toContain('https://foo.ngrok-free.dev');
    expect(args).not.toContain('https://https://foo.ngrok-free.dev');
  });

  it('adds --authtoken when provided', () => {
    expect(buildNgrokArgs(3007, undefined, 'tok')).toEqual(
      expect.arrayContaining(['--authtoken', 'tok']),
    );
  });
});

describe('extractPublicUrl', () => {
  it('prefers the https tunnel', () => {
    const api = {
      tunnels: [
        { proto: 'http', public_url: 'http://x.ngrok' },
        { proto: 'https', public_url: 'https://x.ngrok' },
      ],
    };
    expect(extractPublicUrl(api)).toBe('https://x.ngrok');
  });

  it('falls back to the first tunnel if no https', () => {
    const api = { tunnels: [{ proto: 'tcp', public_url: 'tcp://x:1' }] };
    expect(extractPublicUrl(api)).toBe('tcp://x:1');
  });

  it('returns null for empty/invalid payloads', () => {
    expect(extractPublicUrl(null)).toBeNull();
    expect(extractPublicUrl({})).toBeNull();
    expect(extractPublicUrl({ tunnels: [] })).toBeNull();
    expect(extractPublicUrl({ tunnels: 'nope' })).toBeNull();
  });
});
