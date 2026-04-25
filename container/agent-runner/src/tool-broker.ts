import { spawn } from 'child_process';
import dns from 'dns/promises';
import fs from 'fs';
import { request as httpRequest } from 'http';
import { request as httpsRequest } from 'https';
import net, { type LookupFunction } from 'net';
import path from 'path';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import {
  StdioClientTransport,
  type StdioServerParameters,
} from '@modelcontextprotocol/sdk/client/stdio.js';
import { CronExpressionParser } from 'cron-parser';

export type AgentBackendName = 'claude' | 'openai';

export interface ToolBrokerContainerInput {
  groupFolder: string;
  chatJid: string;
  isMain?: boolean;
  isControlGroup?: boolean;
}

export interface ToolBrokerContext {
  cwd: string;
  containerInput: ToolBrokerContainerInput;
}

export interface OpenAIFunctionToolDefinition {
  type: 'function';
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface OpenAIMcpServerConfig {
  serverName: string;
  command: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  required?: boolean;
}

export interface OpenAIMcpToolBridge {
  definitions: OpenAIFunctionToolDefinition[];
  execute(
    name: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown> | null>;
  close(): Promise<void>;
}

const IPC_DIR = '/workspace/ipc';
const MESSAGES_DIR = path.join(IPC_DIR, 'messages');
const TASKS_DIR = path.join(IPC_DIR, 'tasks');

function isControlGroup(containerInput: ToolBrokerContainerInput): boolean {
  return containerInput.isControlGroup ?? containerInput.isMain ?? false;
}

function writeIpcFile(dir: string, data: object): string {
  fs.mkdirSync(dir, { recursive: true });
  const filename = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`;
  const filepath = path.join(dir, filename);
  const tempPath = `${filepath}.tmp`;
  fs.writeFileSync(tempPath, JSON.stringify(data, null, 2));
  fs.renameSync(tempPath, filepath);
  return filename;
}

async function runCommand(
  command: string,
  cwd: string,
): Promise<{
  stdout: string;
  stderr: string;
  exitCode: number;
}> {
  return new Promise((resolve) => {
    const proc = spawn('/bin/bash', ['-lc', command], {
      cwd,
      env: process.env,
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    proc.on('close', (code) => {
      resolve({
        stdout: stdout.slice(0, 60_000),
        stderr: stderr.slice(0, 20_000),
        exitCode: code ?? 0,
      });
    });
  });
}

async function runProgram(
  command: string,
  args: string[],
  cwd: string,
): Promise<{ stdout: string; stderr: string; exitCode: number }> {
  return new Promise((resolve) => {
    const proc = spawn(command, args, {
      cwd,
      env: process.env,
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    proc.on('error', (err) => {
      resolve({
        stdout: '',
        stderr: String(err),
        exitCode: 127,
      });
    });
    proc.on('close', (code) => {
      resolve({
        stdout: stdout.slice(0, 60_000),
        stderr: stderr.slice(0, 20_000),
        exitCode: code ?? 0,
      });
    });
  });
}

export function splitCommandArgs(command: string): string[] {
  const args: string[] = [];
  let current = '';
  let quote: '"' | "'" | null = null;
  let escaped = false;

  for (const char of command.trim()) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === '\\' && quote !== "'") {
      escaped = true;
      continue;
    }
    if ((char === '"' || char === "'") && quote === null) {
      quote = char;
      continue;
    }
    if (char === quote) {
      quote = null;
      continue;
    }
    if (/\s/.test(char) && quote === null) {
      if (current) {
        args.push(current);
        current = '';
      }
      continue;
    }
    current += char;
  }

  if (escaped) current += '\\';
  if (quote) throw new Error('Unterminated quote in command arguments');
  if (current) args.push(current);
  return args;
}

export function resolveWorkspacePath(cwd: string, targetPath: string): string {
  const resolved = path.resolve(
    targetPath.startsWith('/workspace/') ? '/' : cwd,
    targetPath,
  );
  const allowedRoots = [
    '/workspace/group',
    '/workspace/project',
    '/workspace/extra',
    '/workspace/vault',
    '/workspace/global',
  ];
  const allowed = allowedRoots.some(
    (root) => resolved === root || resolved.startsWith(`${root}/`),
  );
  if (!allowed) {
    throw new Error(`Path escapes the mounted workspace: ${targetPath}`);
  }
  return resolved;
}

function isPrivateIp(address: string): boolean {
  const family = net.isIP(address);
  if (family === 4) {
    const [a, b] = address.split('.').map(Number);
    return (
      a === 10 ||
      a === 127 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254) ||
      a === 0
    );
  }

  if (family === 6) {
    const lower = address.toLowerCase();
    const mappedV4 = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1];
    if (mappedV4) return isPrivateIp(mappedV4);
    return (
      lower === '::1' ||
      lower.startsWith('fc') ||
      lower.startsWith('fd') ||
      lower.startsWith('fe80:')
    );
  }

  return false;
}

export async function resolvePublicWebTarget(rawUrl: string): Promise<{
  url: URL;
  address: string;
  family: 4 | 6;
}> {
  const url = new URL(rawUrl);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error('Only http and https URLs are supported');
  }

  const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
  if (
    hostname === 'localhost' ||
    hostname.endsWith('.localhost') ||
    hostname === 'host.docker.internal' ||
    hostname === 'host.containers.internal' ||
    hostname === 'hostgateway'
  ) {
    throw new Error('Refusing to fetch local/internal hostnames');
  }

  if (net.isIP(hostname)) {
    if (isPrivateIp(hostname)) {
      throw new Error('Refusing to fetch private network addresses');
    }
    return { url, address: hostname, family: net.isIP(hostname) as 4 | 6 };
  }

  const addresses = await dns.lookup(hostname, { all: true, verbatim: true });
  if (addresses.some((entry) => isPrivateIp(entry.address))) {
    throw new Error('Refusing to fetch hostnames that resolve internally');
  }
  const first = addresses[0];
  if (!first) throw new Error('Hostname did not resolve');
  return { url, address: first.address, family: first.family as 4 | 6 };
}

async function fetchPublicText(rawUrl: string): Promise<{
  status: number;
  url: string;
  redirect?: string;
  content: string;
}> {
  const target = await resolvePublicWebTarget(rawUrl);
  const request = target.url.protocol === 'https:' ? httpsRequest : httpRequest;

  return new Promise((resolve, reject) => {
    const lookup: LookupFunction = (_hostname, _options, callback) => {
      callback(null, target.address, target.family);
    };
    const req = request(
      {
        protocol: target.url.protocol,
        hostname: target.url.hostname,
        port: target.url.port || undefined,
        path: `${target.url.pathname}${target.url.search}`,
        method: 'GET',
        headers: {
          host: target.url.host,
          'user-agent': 'Mozilla/5.0 Deus OpenAI backend',
        },
        servername: target.url.hostname,
        lookup,
      },
      (res) => {
        const redirectHeader = res.headers.location;
        let content = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          if (content.length < 40_000) content += chunk;
        });
        res.on('end', () => {
          resolve({
            status: res.statusCode || 0,
            url: target.url.toString(),
            redirect: Array.isArray(redirectHeader)
              ? redirectHeader[0]
              : redirectHeader,
            content: content.slice(0, 40_000),
          });
        });
      },
    );
    req.setTimeout(10_000, () => req.destroy(new Error('Request timed out')));
    req.on('error', reject);
    req.end();
  });
}

export function resolveGroupAttachmentPath(relativePath: string): string {
  const resolved = path.resolve('/workspace/group', relativePath);
  if (
    resolved !== '/workspace/group' &&
    !resolved.startsWith('/workspace/group/')
  ) {
    throw new Error(
      `Image attachment escapes group workspace: ${relativePath}`,
    );
  }
  return resolved;
}

export function buildRipgrepSearchArgs(pattern: string): string[] {
  return ['-n', '--', pattern, '.'];
}

function schema(properties: Record<string, unknown>, required: string[]) {
  return {
    type: 'object',
    properties,
    required,
    additionalProperties: false,
  };
}

export function getOpenAIToolDefinitions(
  extraTools: OpenAIFunctionToolDefinition[] = [],
): OpenAIFunctionToolDefinition[] {
  return [
    {
      type: 'function',
      name: 'bash_exec',
      description:
        'Run a bash command inside the existing Deus container sandbox. Prefer this for build/test/git/project tooling.',
      parameters: schema(
        {
          command: { type: 'string' },
        },
        ['command'],
      ),
    },
    {
      type: 'function',
      name: 'read_file',
      description: 'Read a UTF-8 text file from the mounted workspace.',
      parameters: schema({ path: { type: 'string' } }, ['path']),
    },
    {
      type: 'function',
      name: 'write_file',
      description:
        'Write or overwrite a UTF-8 text file in the mounted workspace.',
      parameters: schema(
        {
          path: { type: 'string' },
          content: { type: 'string' },
        },
        ['path', 'content'],
      ),
    },
    {
      type: 'function',
      name: 'edit_file',
      description:
        'Replace an exact substring in a UTF-8 text file. Fails if the old text does not appear exactly once.',
      parameters: schema(
        {
          path: { type: 'string' },
          old_text: { type: 'string' },
          new_text: { type: 'string' },
        },
        ['path', 'old_text', 'new_text'],
      ),
    },
    {
      type: 'function',
      name: 'glob_files',
      description:
        'List files using ripgrep file discovery filtered by an optional regex.',
      parameters: schema(
        {
          pattern: { type: 'string' },
          base_path: { type: 'string' },
        },
        [],
      ),
    },
    {
      type: 'function',
      name: 'grep_files',
      description:
        'Search file contents with ripgrep and return matching lines.',
      parameters: schema(
        {
          pattern: { type: 'string' },
          base_path: { type: 'string' },
        },
        ['pattern'],
      ),
    },
    {
      type: 'function',
      name: 'web_fetch',
      description: 'Fetch a web page and return its text content.',
      parameters: schema({ url: { type: 'string' } }, ['url']),
    },
    {
      type: 'function',
      name: 'web_search',
      description:
        'Search the web using DuckDuckGo HTML results and return the top hits.',
      parameters: schema({ query: { type: 'string' } }, ['query']),
    },
    {
      type: 'function',
      name: 'agent_browser',
      description:
        'Run an agent-browser command. Use for interactive browsing or screenshots.',
      parameters: schema({ command: { type: 'string' } }, ['command']),
    },
    {
      type: 'function',
      name: 'send_message',
      description:
        "Send a message to the current Deus chat immediately while you're still running.",
      parameters: schema(
        {
          text: { type: 'string' },
          sender: { type: 'string' },
        },
        ['text'],
      ),
    },
    {
      type: 'function',
      name: 'schedule_task',
      description: 'Schedule a recurring or one-time Deus task.',
      parameters: schema(
        {
          prompt: { type: 'string' },
          schedule_type: { type: 'string', enum: ['cron', 'interval', 'once'] },
          schedule_value: { type: 'string' },
          context_mode: { type: 'string', enum: ['group', 'isolated'] },
          target_group_jid: { type: 'string' },
          agent_backend: { type: 'string', enum: ['claude', 'openai'] },
        },
        ['prompt', 'schedule_type', 'schedule_value'],
      ),
    },
    {
      type: 'function',
      name: 'list_tasks',
      description: 'List visible scheduled tasks from the Deus snapshot.',
      parameters: schema({}, []),
    },
    {
      type: 'function',
      name: 'pause_task',
      description: 'Pause a scheduled task.',
      parameters: schema({ task_id: { type: 'string' } }, ['task_id']),
    },
    {
      type: 'function',
      name: 'resume_task',
      description: 'Resume a paused scheduled task.',
      parameters: schema({ task_id: { type: 'string' } }, ['task_id']),
    },
    {
      type: 'function',
      name: 'cancel_task',
      description: 'Cancel and delete a scheduled task.',
      parameters: schema({ task_id: { type: 'string' } }, ['task_id']),
    },
    {
      type: 'function',
      name: 'update_task',
      description: 'Update an existing scheduled task.',
      parameters: schema(
        {
          task_id: { type: 'string' },
          prompt: { type: 'string' },
          schedule_type: { type: 'string', enum: ['cron', 'interval', 'once'] },
          schedule_value: { type: 'string' },
          agent_backend: { type: 'string', enum: ['claude', 'openai'] },
        },
        ['task_id'],
      ),
    },
    {
      type: 'function',
      name: 'register_group',
      description: 'Register a new chat/group. Main chat only.',
      parameters: schema(
        {
          jid: { type: 'string' },
          name: { type: 'string' },
          folder: { type: 'string' },
          trigger: { type: 'string' },
          agent_backend: { type: 'string', enum: ['claude', 'openai'] },
        },
        ['jid', 'name', 'folder', 'trigger'],
      ),
    },
    ...extraTools,
  ];
}

export function buildOpenAIMcpToolName(
  serverName: string,
  toolName: string,
): string {
  return `mcp__${serverName}__${toolName}`;
}

function normalizeMcpInputSchema(
  inputSchema: Record<string, unknown> | undefined,
): Record<string, unknown> {
  if (inputSchema && inputSchema.type === 'object') {
    return inputSchema;
  }
  return schema({}, []);
}

function buildMcpTransportConfig(
  config: OpenAIMcpServerConfig,
): StdioServerParameters {
  return {
    command: config.command,
    args: config.args,
    env: config.env,
    cwd: config.cwd,
    stderr: 'pipe',
  };
}

export async function createOpenAIMcpToolBridge(
  configs: OpenAIMcpServerConfig[],
  log?: (message: string) => void,
): Promise<OpenAIMcpToolBridge> {
  const definitions: OpenAIFunctionToolDefinition[] = [];
  const clients: Client[] = [];
  const toolBindings = new Map<
    string,
    { client: Client; serverName: string; toolName: string }
  >();

  for (const config of configs) {
    const client = new Client({
      name: 'deus-openai-mcp-bridge',
      version: '1.0.0',
    });
    const transport = new StdioClientTransport(buildMcpTransportConfig(config));
    const stderr = transport.stderr;
    if (stderr) {
      stderr.on('data', (chunk) => {
        const text = chunk.toString().trim();
        if (text) log?.(`[mcp:${config.serverName}] ${text}`);
      });
    }

    try {
      await client.connect(transport);
      clients.push(client);
      const listed = await client.listTools();
      for (const tool of listed.tools) {
        const openaiName = buildOpenAIMcpToolName(config.serverName, tool.name);
        definitions.push({
          type: 'function',
          name: openaiName,
          description:
            tool.description ||
            `${config.serverName} MCP tool ${tool.name.replace(/_/g, ' ')}`,
          parameters: normalizeMcpInputSchema(tool.inputSchema),
        });
        toolBindings.set(openaiName, {
          client,
          serverName: config.serverName,
          toolName: tool.name,
        });
      }
      log?.(
        `Loaded ${listed.tools.length} MCP tool(s) from ${config.serverName}`,
      );
    } catch (err) {
      await client.close().catch(() => {});
      const message = `Failed to load ${config.required ? 'required ' : ''}MCP tools from ${config.serverName}: ${err instanceof Error ? err.message : String(err)}`;
      log?.(message);
      if (config.required) {
        throw new Error(message);
      }
    }
  }

  return {
    definitions,
    async execute(name: string, args: Record<string, unknown>) {
      const binding = toolBindings.get(name);
      if (!binding) return null;
      try {
        const result = await binding.client.callTool({
          name: binding.toolName,
          arguments: args,
        });
        return result as Record<string, unknown>;
      } catch (err) {
        return {
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        };
      }
    },
    async close() {
      await Promise.all(
        clients.map((client) =>
          client.close().catch((err) => {
            log?.(
              `Failed to close MCP client: ${err instanceof Error ? err.message : String(err)}`,
            );
          }),
        ),
      );
    },
  };
}

function parseScheduleType(
  value: unknown,
): 'cron' | 'interval' | 'once' | null {
  return value === 'cron' || value === 'interval' || value === 'once'
    ? value
    : null;
}

export function normalizeTaskContextMode(value: unknown): 'group' | 'isolated' {
  return value === 'isolated' ? 'isolated' : 'group';
}

export function validateScheduleInput(
  scheduleTypeValue: unknown,
  scheduleValueValue: unknown,
): string | null {
  const scheduleType = parseScheduleType(scheduleTypeValue);
  const scheduleValue = String(scheduleValueValue || '');

  if (!scheduleType) {
    return `Invalid schedule type: "${String(scheduleTypeValue || '')}".`;
  }

  if (scheduleType === 'cron') {
    try {
      CronExpressionParser.parse(scheduleValue);
      return null;
    } catch {
      return `Invalid cron: "${scheduleValue}". Use format like "0 9 * * *" or "*/5 * * * *".`;
    }
  }

  if (scheduleType === 'interval') {
    const ms = parseInt(scheduleValue, 10);
    if (Number.isNaN(ms) || ms <= 0) {
      return `Invalid interval: "${scheduleValue}". Must be positive milliseconds.`;
    }
    return null;
  }

  if (/[Zz]$/.test(scheduleValue) || /[+-]\d{2}:\d{2}$/.test(scheduleValue)) {
    return `Timestamp must be local time without timezone suffix. Got "${scheduleValue}".`;
  }
  const date = new Date(scheduleValue);
  if (Number.isNaN(date.getTime())) {
    return `Invalid timestamp: "${scheduleValue}". Use local time format like "2026-02-01T15:30:00".`;
  }
  return null;
}

function readVisibleTasks(containerInput: ToolBrokerContainerInput): unknown[] {
  const tasksFile = path.join(IPC_DIR, 'current_tasks.json');
  if (!fs.existsSync(tasksFile)) return [];

  const allTasks = JSON.parse(fs.readFileSync(tasksFile, 'utf-8')) as unknown;
  if (!Array.isArray(allTasks)) return [];
  if (isControlGroup(containerInput)) return allTasks;
  return allTasks.filter((task) => {
    if (!task || typeof task !== 'object') return false;
    return (
      (task as { groupFolder?: unknown }).groupFolder ===
      containerInput.groupFolder
    );
  });
}

export async function executeBrokerTool(
  name: string,
  args: Record<string, unknown>,
  ctx: ToolBrokerContext,
): Promise<Record<string, unknown>> {
  const { cwd, containerInput } = ctx;

  switch (name) {
    case 'bash_exec': {
      const command = String(args.command || '');
      const result = await runCommand(command, cwd);
      return result;
    }
    case 'read_file': {
      const target = resolveWorkspacePath(cwd, String(args.path || ''));
      return { path: target, content: fs.readFileSync(target, 'utf-8') };
    }
    case 'write_file': {
      const target = resolveWorkspacePath(cwd, String(args.path || ''));
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, String(args.content || ''));
      return { ok: true, path: target };
    }
    case 'edit_file': {
      const target = resolveWorkspacePath(cwd, String(args.path || ''));
      const oldText = String(args.old_text || '');
      const newText = String(args.new_text || '');
      const content = fs.readFileSync(target, 'utf-8');
      const occurrences = content.split(oldText).length - 1;
      if (occurrences !== 1) {
        return {
          ok: false,
          error: `Expected exactly 1 match for old_text, found ${occurrences}`,
        };
      }
      fs.writeFileSync(target, content.replace(oldText, newText));
      return { ok: true, path: target };
    }
    case 'glob_files': {
      const base = args.base_path
        ? resolveWorkspacePath(cwd, String(args.base_path))
        : cwd;
      const pattern = String(args.pattern || '.');
      let regex: RegExp;
      try {
        regex = new RegExp(pattern);
      } catch (err) {
        return {
          stdout: '',
          stderr: `Invalid regex: ${String(err)}`,
          exitCode: 2,
        };
      }
      const result = await runProgram('rg', ['--files'], base);
      if (result.exitCode !== 0) return result;
      return {
        ...result,
        stdout: result.stdout
          .split('\n')
          .filter((line) => line && regex.test(line))
          .join('\n'),
      };
    }
    case 'grep_files': {
      const base = args.base_path
        ? resolveWorkspacePath(cwd, String(args.base_path))
        : cwd;
      const pattern = String(args.pattern || '');
      const result = await runProgram(
        'rg',
        buildRipgrepSearchArgs(pattern),
        base,
      );
      return result;
    }
    case 'web_fetch': {
      return fetchPublicText(String(args.url || ''));
    }
    case 'web_search': {
      const query = String(args.query || '');
      const url = `https://duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
      const res = await fetchPublicText(url);
      const matches = [
        ...res.content.matchAll(
          /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)<\/a>/g,
        ),
      ];
      return {
        query,
        results: matches.slice(0, 5).map((match) => ({
          url: match[1],
          title: match[2].replace(/<[^>]+>/g, ''),
        })),
      };
    }
    case 'agent_browser': {
      const command = String(args.command || '');
      const browserArgs = splitCommandArgs(command);
      if (browserArgs.length === 0) {
        return {
          stdout: '',
          stderr: 'agent_browser command is required',
          exitCode: 2,
        };
      }
      return runProgram('agent-browser', browserArgs, cwd);
    }
    case 'send_message': {
      writeIpcFile(MESSAGES_DIR, {
        type: 'message',
        chatJid: containerInput.chatJid,
        text: String(args.text || ''),
        sender: args.sender ? String(args.sender) : undefined,
        groupFolder: containerInput.groupFolder,
        timestamp: new Date().toISOString(),
      });
      return { ok: true };
    }
    case 'schedule_task': {
      const validationError = validateScheduleInput(
        args.schedule_type,
        args.schedule_value,
      );
      if (validationError) return { ok: false, error: validationError };

      const taskId = `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      writeIpcFile(TASKS_DIR, {
        type: 'schedule_task',
        taskId,
        prompt: String(args.prompt || ''),
        schedule_type: String(args.schedule_type || ''),
        schedule_value: String(args.schedule_value || ''),
        context_mode: normalizeTaskContextMode(args.context_mode),
        targetJid:
          isControlGroup(containerInput) && args.target_group_jid
            ? String(args.target_group_jid)
            : containerInput.chatJid,
        agent_backend:
          args.agent_backend === 'openai' || args.agent_backend === 'claude'
            ? args.agent_backend
            : undefined,
        createdBy: containerInput.groupFolder,
        timestamp: new Date().toISOString(),
      });
      return { ok: true, task_id: taskId };
    }
    case 'list_tasks': {
      return { tasks: readVisibleTasks(containerInput) };
    }
    case 'pause_task':
    case 'resume_task':
    case 'cancel_task': {
      writeIpcFile(TASKS_DIR, {
        type: name,
        taskId: String(args.task_id || ''),
        groupFolder: containerInput.groupFolder,
        isMain: isControlGroup(containerInput),
        timestamp: new Date().toISOString(),
      });
      return { ok: true };
    }
    case 'update_task': {
      writeIpcFile(TASKS_DIR, {
        type: 'update_task',
        taskId: String(args.task_id || ''),
        prompt: args.prompt,
        schedule_type: args.schedule_type,
        schedule_value: args.schedule_value,
        agent_backend:
          args.agent_backend === 'openai' || args.agent_backend === 'claude'
            ? args.agent_backend
            : undefined,
        groupFolder: containerInput.groupFolder,
        isMain: isControlGroup(containerInput),
        timestamp: new Date().toISOString(),
      });
      return { ok: true };
    }
    case 'register_group': {
      if (!isControlGroup(containerInput)) {
        return { ok: false, error: 'Only the main group can register groups.' };
      }
      writeIpcFile(TASKS_DIR, {
        type: 'register_group',
        jid: String(args.jid || ''),
        name: String(args.name || ''),
        folder: String(args.folder || ''),
        trigger: String(args.trigger || ''),
        containerConfig:
          args.agent_backend === 'openai' || args.agent_backend === 'claude'
            ? { agentBackend: args.agent_backend }
            : undefined,
        timestamp: new Date().toISOString(),
      });
      return { ok: true };
    }
    default:
      return { ok: false, error: `Unknown tool: ${name}` };
  }
}
