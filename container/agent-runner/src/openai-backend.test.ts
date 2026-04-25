import fs from 'fs';
import path from 'path';

import { describe, expect, it } from 'vitest';

import {
  assertOpenAIResponse,
  parseOpenAISessionMetadata,
  shouldResumeOpenAIResponse,
} from './openai-backend.js';
import {
  buildRipgrepSearchArgs,
  buildOpenAIMcpToolName,
  createOpenAIMcpToolBridge,
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

  it('validates OpenAI response ids and output item shapes at the boundary', () => {
    expect(
      assertOpenAIResponse({
        id: 'resp_123',
        output: [{ type: 'message', content: [] }],
      }),
    ).toEqual({
      id: 'resp_123',
      output: [{ type: 'message', content: [] }],
      output_text: undefined,
    });
    expect(() => assertOpenAIResponse({ output: [] })).toThrow(/valid id/);
    expect(() =>
      assertOpenAIResponse({ id: 'resp_123', output: ['bad'] }),
    ).toThrow(/output item/);
  });

  it('bridges MCP tools into OpenAI tool definitions with Claude-style prefixes', async () => {
    const tempDir = fs.mkdtempSync(
      path.join(process.cwd(), '.tmp-mcp-bridge-'),
    );
    const serverPath = path.join(tempDir, 'echo-mcp.mjs');
    fs.writeFileSync(
      serverPath,
      `
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const server = new McpServer({ name: 'echo', version: '1.0.0' });
server.tool(
  'echo_tool',
  'Echoes text back to the caller.',
  { text: z.string().optional() },
  async (args) => ({
    content: [{ type: 'text', text: \`echo:\${args.text ?? ''}\` }],
  }),
);

await server.connect(new StdioServerTransport());
      `.trim(),
    );

    const bridge = await createOpenAIMcpToolBridge([
      {
        serverName: 'deus',
        command: process.execPath,
        args: [serverPath],
      },
    ]);

    expect(bridge.definitions).toEqual([
      expect.objectContaining({
        type: 'function',
        name: buildOpenAIMcpToolName('deus', 'echo_tool'),
        description: 'Echoes text back to the caller.',
      }),
    ]);

    const result = await bridge.execute(
      buildOpenAIMcpToolName('deus', 'echo_tool'),
      { text: 'hi' },
    );
    expect(result).toMatchObject({
      content: [{ type: 'text', text: 'echo:hi' }],
    });

    await bridge.close();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  it('returns a tool error payload when a bridged MCP tool is called after disconnect', async () => {
    const tempDir = fs.mkdtempSync(
      path.join(process.cwd(), '.tmp-mcp-bridge-error-'),
    );
    const serverPath = path.join(tempDir, 'echo-mcp.mjs');
    fs.writeFileSync(
      serverPath,
      `
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const server = new McpServer({ name: 'echo', version: '1.0.0' });
server.tool(
  'echo_tool',
  'Echoes text back to the caller.',
  { text: z.string().optional() },
  async (args) => ({
    content: [{ type: 'text', text: \`echo:\${args.text ?? ''}\` }],
  }),
);

await server.connect(new StdioServerTransport());
      `.trim(),
    );

    const bridge = await createOpenAIMcpToolBridge([
      {
        serverName: 'deus',
        command: process.execPath,
        args: [serverPath],
      },
    ]);

    await bridge.close();
    const result = await bridge.execute(
      buildOpenAIMcpToolName('deus', 'echo_tool'),
      { text: 'hi' },
    );
    expect(result).toMatchObject({
      ok: false,
      error: expect.any(String),
    });

    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  it('fails closed when the required deus MCP bridge cannot start', async () => {
    await expect(
      createOpenAIMcpToolBridge([
        {
          serverName: 'deus',
          command: process.execPath,
          args: [path.join(process.cwd(), 'does-not-exist.mjs')],
          required: true,
        },
      ]),
    ).rejects.toThrow(/required MCP tools from deus/);
  });
});
