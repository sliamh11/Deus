/**
 * Deus Agent Runner
 * Runs inside a container, receives config via stdin, outputs result to stdout
 *
 * Input protocol:
 *   Stdin: Full ContainerInput JSON (read until EOF, like before)
 *   IPC:   Follow-up messages written as JSON files to /workspace/ipc/input/
 *          Files: {type:"message", text:"..."}.json — polled and consumed
 *          Sentinel: /workspace/ipc/input/_close — signals session end
 *
 * Stdout protocol:
 *   Each result is wrapped in OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs.
 *   Multiple results may be emitted (one per agent teams result).
 *   Final marker after loop ends signals completion.
 */

import fs from 'fs';
import path from 'path';
import {
  query,
  HookCallback,
  PreCompactHookInput,
  PostToolUseHookInput,
  SDKResultMessage,
} from '@anthropic-ai/claude-agent-sdk';
import { fileURLToPath } from 'url';

interface ContainerInput {
  prompt: string;
  sessionId?: string;
  groupFolder: string;
  chatJid: string;
  isMain: boolean;
  isScheduledTask?: boolean;
  assistantName?: string;
  imageAttachments?: Array<{ relativePath: string; mediaType: string }>;
}

interface ImageContentBlock {
  type: 'image';
  source: { type: 'base64'; media_type: string; data: string };
}
interface TextContentBlock {
  type: 'text';
  text: string;
}
type ContentBlock = ImageContentBlock | TextContentBlock;

interface ContainerOutput {
  status: 'success' | 'error';
  result: string | null;
  newSessionId?: string;
  error?: string;
}

interface SessionEntry {
  sessionId: string;
  fullPath: string;
  summary: string;
  firstPrompt: string;
}

interface SessionsIndex {
  entries: SessionEntry[];
}

interface SDKUserMessage {
  type: 'user';
  message: { role: 'user'; content: string | ContentBlock[] };
  parent_tool_use_id: null;
  session_id: string;
}

const IPC_INPUT_DIR = '/workspace/ipc/input';
const IPC_INPUT_CLOSE_SENTINEL = path.join(IPC_INPUT_DIR, '_close');
const IPC_POLL_MS = 500;

// Keywords that signal the prompt requires multi-agent orchestration.
// When none are present and no external project is mounted, swarm tools
// (TeamCreate, TeamDelete, SendMessage) are excluded from allowedTools,
// saving ~300 tokens per call on personal-assistant queries.
const SWARM_SIGNALS = [
  'parallel agent',
  'subagent',
  'agent team',
  'agent swarm',
  'orchestrate',
  'in parallel',
  'multiple agents',
  'spawn agent',
];

/**
 * Push-based async iterable for streaming user messages to the SDK.
 * Keeps the iterable alive until end() is called, preventing isSingleUserTurn.
 */
class MessageStream {
  private queue: SDKUserMessage[] = [];
  private waiting: (() => void) | null = null;
  private done = false;

  push(text: string): void {
    this.queue.push({
      type: 'user',
      message: { role: 'user', content: text },
      parent_tool_use_id: null,
      session_id: '',
    });
    this.waiting?.();
  }

  pushMultimodal(content: ContentBlock[]): void {
    this.queue.push({
      type: 'user',
      message: { role: 'user', content },
      parent_tool_use_id: null,
      session_id: '',
    });
    this.waiting?.();
  }

  end(): void {
    this.done = true;
    this.waiting?.();
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<SDKUserMessage> {
    while (true) {
      while (this.queue.length > 0) {
        yield this.queue.shift()!;
      }
      if (this.done) return;
      await new Promise<void>((r) => {
        this.waiting = r;
      });
      this.waiting = null;
    }
  }
}

async function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

// SYNC-REQUIRED: Duplicated in src/container-runner.ts (host side).
// Cannot be shared via import — this package runs inside an isolated container.
const OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---';
const OUTPUT_END_MARKER = '---DEUS_OUTPUT_END---';

// Counter for IPC output files (written in addition to stdout for eval harness).
let _outputSeq = 0;
const IPC_OUTPUT_DIR = '/workspace/ipc/output';

function writeOutput(output: ContainerOutput): void {
  // Write to stdout (buffered in pipe mode, but kept for compatibility).
  console.log(OUTPUT_START_MARKER);
  console.log(JSON.stringify(output));
  console.log(OUTPUT_END_MARKER);

  // Also write to the mounted IPC output directory. Docker pipes buffer stdout
  // until the container exits, so the eval harness cannot see OUTPUT_END in
  // real time. Writing to a shared mount is immediate — the harness polls this
  // directory to detect results without depending on Docker stdout flushing.
  try {
    fs.mkdirSync(IPC_OUTPUT_DIR, { recursive: true });
    fs.writeFileSync(
      path.join(IPC_OUTPUT_DIR, `${_outputSeq++}.json`),
      JSON.stringify(output),
    );
  } catch {
    /* ignore — stdout is still the authoritative channel in production */
  }
}

function log(message: string): void {
  console.error(`[agent-runner] ${message}`);
}

function getSessionSummary(
  sessionId: string,
  transcriptPath: string,
): string | null {
  const projectDir = path.dirname(transcriptPath);
  const indexPath = path.join(projectDir, 'sessions-index.json');

  if (!fs.existsSync(indexPath)) {
    log(`Sessions index not found at ${indexPath}`);
    return null;
  }

  try {
    const index: SessionsIndex = JSON.parse(
      fs.readFileSync(indexPath, 'utf-8'),
    );
    const entry = index.entries.find((e) => e.sessionId === sessionId);
    if (entry?.summary) {
      return entry.summary;
    }
  } catch (err) {
    log(
      `Failed to read sessions index: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  return null;
}

/**
 * Archive the full transcript to conversations/ before compaction.
 */
function createPreCompactHook(assistantName?: string): HookCallback {
  return async (input, _toolUseId, _context) => {
    const preCompact = input as PreCompactHookInput;
    const transcriptPath = preCompact.transcript_path;
    const sessionId = preCompact.session_id;

    if (!transcriptPath || !fs.existsSync(transcriptPath)) {
      log('No transcript found for archiving');
      return {};
    }

    try {
      const content = fs.readFileSync(transcriptPath, 'utf-8');
      const messages = parseTranscript(content);

      if (messages.length === 0) {
        log('No messages to archive');
        return {};
      }

      const summary = getSessionSummary(sessionId, transcriptPath);
      const name = summary ? sanitizeFilename(summary) : generateFallbackName();

      const conversationsDir = '/workspace/group/conversations';
      fs.mkdirSync(conversationsDir, { recursive: true });

      const date = new Date().toISOString().split('T')[0];
      const filename = `${date}-${name}.md`;
      const filePath = path.join(conversationsDir, filename);

      const markdown = formatTranscriptMarkdown(
        messages,
        summary,
        assistantName,
      );
      fs.writeFileSync(filePath, markdown);

      log(`Archived conversation to ${filePath}`);
    } catch (err) {
      log(
        `Failed to archive transcript: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    return {};
  };
}

/**
 * Append SDK-reported per-turn usage metadata to JSONL. This is the direct
 * billing signal (inputTokens, outputTokens, cache read/create, cost) and the
 * basis for before/after token-efficiency comparisons. Opt-out: DEUS_USAGE_LOG=0.
 */
function logUsage(msg: SDKResultMessage): void {
  if (process.env.DEUS_USAGE_LOG === '0') return;
  try {
    const logPath = '/workspace/group/logs/usage.jsonl';
    const entry = {
      ts: new Date().toISOString(),
      session_id: msg.session_id,
      subtype: msg.subtype,
      num_turns: msg.num_turns,
      duration_ms: msg.duration_ms,
      duration_api_ms: msg.duration_api_ms,
      total_cost_usd: msg.total_cost_usd,
      input_tokens: msg.usage.inputTokens,
      output_tokens: msg.usage.outputTokens,
      cache_read_input_tokens: msg.usage.cacheReadInputTokens,
      cache_creation_input_tokens: msg.usage.cacheCreationInputTokens,
      model_usage: msg.modelUsage,
    };
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    fs.appendFileSync(logPath, JSON.stringify(entry) + '\n');
  } catch (err) {
    log(
      `usage-log failed: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

/**
 * Log per-tool-call response size to JSONL. Measurement only; does not modify
 * the tool output the model sees. Feeds the Headroom POC Phase A decision
 * (docs/HEADROOM_POC.md). Opt-out via DEUS_TOOL_SIZE_LOG=0.
 */
function createToolSizeLogHook(): HookCallback {
  const logPath = '/workspace/group/logs/tool-sizes.jsonl';
  return async (input, _toolUseId, _context) => {
    try {
      const hookInput = input as PostToolUseHookInput;
      const serialized =
        typeof hookInput.tool_response === 'string'
          ? hookInput.tool_response
          : JSON.stringify(hookInput.tool_response ?? '');
      const bytes = Buffer.byteLength(serialized, 'utf8');
      // Rough heuristic: ~3.7 bytes per token for mixed English+code. Phase A
      // uses this for relative comparison, not absolute budgeting.
      const approxTokens = Math.round(bytes / 3.7);
      const entry = {
        ts: new Date().toISOString(),
        tool: hookInput.tool_name,
        tool_use_id: hookInput.tool_use_id,
        bytes,
        approx_tokens: approxTokens,
      };
      fs.mkdirSync(path.dirname(logPath), { recursive: true });
      fs.appendFileSync(logPath, JSON.stringify(entry) + '\n');
    } catch (err) {
      log(
        `tool-size-log failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    return {};
  };
}

function sanitizeFilename(summary: string): string {
  return summary
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 50);
}

function generateFallbackName(): string {
  const time = new Date();
  return `conversation-${time.getHours().toString().padStart(2, '0')}${time.getMinutes().toString().padStart(2, '0')}`;
}

interface ParsedMessage {
  role: 'user' | 'assistant';
  content: string;
}

function parseTranscript(content: string): ParsedMessage[] {
  const messages: ParsedMessage[] = [];

  for (const line of content.split('\n')) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line);
      if (entry.type === 'user' && entry.message?.content) {
        const text =
          typeof entry.message.content === 'string'
            ? entry.message.content
            : entry.message.content
                .map((c: { text?: string }) => c.text || '')
                .join('');
        if (text) messages.push({ role: 'user', content: text });
      } else if (entry.type === 'assistant' && entry.message?.content) {
        const textParts = entry.message.content
          .filter((c: { type: string }) => c.type === 'text')
          .map((c: { text: string }) => c.text);
        const text = textParts.join('');
        if (text) messages.push({ role: 'assistant', content: text });
      }
    } catch {}
  }

  return messages;
}

function formatTranscriptMarkdown(
  messages: ParsedMessage[],
  title?: string | null,
  assistantName?: string,
): string {
  const now = new Date();
  const formatDateTime = (d: Date) =>
    d.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });

  const lines: string[] = [];
  lines.push(`# ${title || 'Conversation'}`);
  lines.push('');
  lines.push(`Archived: ${formatDateTime(now)}`);
  lines.push('');
  lines.push('---');
  lines.push('');

  for (const msg of messages) {
    const sender = msg.role === 'user' ? 'User' : assistantName || 'Assistant';
    const content =
      msg.content.length > 2000
        ? msg.content.slice(0, 2000) + '...'
        : msg.content;
    lines.push(`**${sender}**: ${content}`);
    lines.push('');
  }

  return lines.join('\n');
}

/**
 * Check for _close sentinel.
 */
function shouldClose(): boolean {
  if (fs.existsSync(IPC_INPUT_CLOSE_SENTINEL)) {
    try {
      fs.unlinkSync(IPC_INPUT_CLOSE_SENTINEL);
    } catch {
      /* ignore */
    }
    return true;
  }
  return false;
}

/**
 * Drain all pending IPC input messages.
 * Returns messages found, or empty array.
 */
function drainIpcInput(): string[] {
  try {
    fs.mkdirSync(IPC_INPUT_DIR, { recursive: true });
    const files = fs
      .readdirSync(IPC_INPUT_DIR)
      .filter((f) => f.endsWith('.json'))
      .sort();

    const messages: string[] = [];
    for (const file of files) {
      const filePath = path.join(IPC_INPUT_DIR, file);
      try {
        const data = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
        fs.unlinkSync(filePath);
        if (data.type === 'message' && data.text) {
          messages.push(data.text);
        }
      } catch (err) {
        log(
          `Failed to process input file ${file}: ${err instanceof Error ? err.message : String(err)}`,
        );
        try {
          fs.unlinkSync(filePath);
        } catch {
          /* ignore */
        }
      }
    }
    return messages;
  } catch (err) {
    log(`IPC drain error: ${err instanceof Error ? err.message : String(err)}`);
    return [];
  }
}

/**
 * Wait for a new IPC message or _close sentinel.
 * Returns the messages as a single string, or null if _close.
 */
function waitForIpcMessage(): Promise<string | null> {
  return new Promise((resolve) => {
    const poll = () => {
      if (shouldClose()) {
        resolve(null);
        return;
      }
      const messages = drainIpcInput();
      if (messages.length > 0) {
        resolve(messages.join('\n'));
        return;
      }
      setTimeout(poll, IPC_POLL_MS);
    };
    poll();
  });
}

/**
 * Run a single query and stream results via writeOutput.
 * Uses MessageStream (AsyncIterable) to keep isSingleUserTurn=false,
 * allowing agent teams subagents to run to completion.
 * Also pipes IPC messages into the stream during the query.
 */
async function runQuery(
  prompt: string,
  sessionId: string | undefined,
  mcpServerPath: string,
  containerInput: ContainerInput,
  sdkEnv: Record<string, string | undefined>,
  resumeAt?: string,
): Promise<{
  newSessionId?: string;
  lastAssistantUuid?: string;
  closedDuringQuery: boolean;
}> {
  const stream = new MessageStream();
  stream.push(prompt);

  // Load image attachments and send as multimodal content blocks
  if (containerInput.imageAttachments?.length) {
    const blocks: ContentBlock[] = [];
    for (const img of containerInput.imageAttachments) {
      const imgPath = path.join('/workspace/group', img.relativePath);
      try {
        const data = fs.readFileSync(imgPath).toString('base64');
        blocks.push({
          type: 'image',
          source: { type: 'base64', media_type: img.mediaType, data },
        });
      } catch (err) {
        log(`Failed to load image: ${imgPath}`);
      }
    }
    if (blocks.length > 0) {
      stream.pushMultimodal(blocks);
    }
  }

  // Poll IPC for follow-up messages and _close sentinel during the query
  let ipcPolling = true;
  let closedDuringQuery = false;
  const pollIpcDuringQuery = () => {
    if (!ipcPolling) return;
    if (shouldClose()) {
      log('Close sentinel detected during query, ending stream');
      closedDuringQuery = true;
      stream.end();
      ipcPolling = false;
      return;
    }
    const messages = drainIpcInput();
    for (const text of messages) {
      log(`Piping IPC message into active query (${text.length} chars)`);
      stream.push(text);
    }
    setTimeout(pollIpcDuringQuery, IPC_POLL_MS);
  };
  setTimeout(pollIpcDuringQuery, IPC_POLL_MS);

  let newSessionId: string | undefined;
  let lastAssistantUuid: string | undefined;
  let messageCount = 0;
  let resultCount = 0;

  // Effort level (Opus 4.7): default 'low' for latency-sensitive
  // personal-assistant traffic. Override via DEUS_AGENT_EFFORT env var.
  // Valid values: low | medium | high | max | default (unset).
  // Migration guide: 'low' is recommended for "short, scoped tasks and
  // latency-sensitive workloads that are not intelligence-sensitive".
  const effortEnv = (process.env.DEUS_AGENT_EFFORT || 'low').toLowerCase();
  const effort =
    effortEnv === 'default'
      ? undefined
      : (['low', 'medium', 'high', 'max'].includes(effortEnv)
          ? (effortEnv as 'low' | 'medium' | 'high' | 'max')
          : 'low');
  log(`Effort level: ${effort ?? 'SDK default'}`);

  // Load global CLAUDE.md as additional system context (shared across all groups)
  const globalClaudeMdPath = '/workspace/global/CLAUDE.md';
  let globalClaudeMd: string | undefined;
  if (!containerInput.isMain && fs.existsSync(globalClaudeMdPath)) {
    globalClaudeMd = fs.readFileSync(globalClaudeMdPath, 'utf-8');
  }

  // Detect external project mount: if /workspace/project exists and has content,
  // use it as the primary cwd. The agent works in the user's project directory
  // while /workspace/group stays available as an additional directory for
  // Deus-specific memory, conversation archives, and CLAUDE.md.
  const projectDir = '/workspace/project';
  let hasProject = false;
  try {
    // Validate the project dir is a real directory (not a symlink to elsewhere).
    // The host already validates this, but defense-in-depth inside the container.
    const stat = fs.statSync(projectDir);
    if (stat.isDirectory()) {
      const realProjectDir = fs.realpathSync(projectDir);
      hasProject =
        realProjectDir.startsWith('/workspace/') &&
        fs.readdirSync(projectDir).some((f) => !f.startsWith('.'));
    }
  } catch {
    // projectDir doesn't exist — not an error, just no project mounted
  }
  const cwd = hasProject ? projectDir : '/workspace/group';

  if (hasProject) {
    log(`External project detected at ${projectDir}, using as cwd`);
  }

  // Discover additional directories mounted at /workspace/extra/*
  // These are passed to the SDK so their CLAUDE.md files are loaded automatically
  const extraDirs: string[] = [];
  const extraBase = '/workspace/extra';
  if (fs.existsSync(extraBase)) {
    for (const entry of fs.readdirSync(extraBase)) {
      const fullPath = path.join(extraBase, entry);
      if (fs.statSync(fullPath).isDirectory()) {
        extraDirs.push(fullPath);
      }
    }
  }

  // When working on an external project, add /workspace/group as an additional
  // directory so the SDK loads its CLAUDE.md (Deus-specific memory for this group)
  if (hasProject) {
    extraDirs.push('/workspace/group');
  }

  if (extraDirs.length > 0) {
    log(`Additional directories: ${extraDirs.join(', ')}`);
  }

  // Swarm tools are only needed for multi-agent orchestration.
  // Exclude them for plain personal-assistant queries to save ~300 tokens.
  // Always include when an external project is mounted (engineering context)
  // or the prompt explicitly signals multi-agent intent.
  const teamsNeeded =
    hasProject || SWARM_SIGNALS.some((kw) => prompt.toLowerCase().includes(kw));
  log(
    `Swarm tools: ${teamsNeeded ? 'included' : 'excluded (~300 tokens saved)'}`,
  );

  // Google Calendar MCP: available when the host project is mounted and gcal
  // credentials + built package exist. The MCP server runs from the host's
  // built dist via the read-only /workspace/project mount.
  const gcalDistPath = '/workspace/project/packages/mcp-gcal/dist/index.js';
  const gcalCredsPath = '/workspace/project/integrations/gcal/credentials.json';
  const gcalTokensPath = '/workspace/project/integrations/gcal/tokens.json';
  const hasGcalMcp =
    hasProject &&
    fs.existsSync(gcalDistPath) &&
    fs.existsSync(gcalCredsPath) &&
    fs.existsSync(gcalTokensPath);
  if (hasGcalMcp) {
    log('Google Calendar MCP: enabled (credentials + package found)');
  }

  // CLAUDE.md probe: log fingerprint before every query() call.
  // Compare across turns in the same session — if len= appears N times for
  // N turns, the SDK re-reads the file on every resumed call (lazy loading is worth it).
  // If it only appears once, the SDK already caches it and no change is needed.
  const claudeMdProbePath = '/workspace/group/CLAUDE.md';
  if (fs.existsSync(claudeMdProbePath)) {
    const _probeStat = fs.statSync(claudeMdProbePath);
    log(
      `[claude-md-probe] turn=${sessionId ? 'resume' : 'new'} len=${_probeStat.size}B mtime=${_probeStat.mtimeMs}`,
    );
  }

  for await (const message of query({
    prompt: stream,
    options: {
      cwd,
      additionalDirectories: extraDirs.length > 0 ? extraDirs : undefined,
      resume: sessionId,
      resumeSessionAt: resumeAt,
      effort,
      systemPrompt: globalClaudeMd
        ? {
            type: 'preset' as const,
            preset: 'claude_code' as const,
            append: globalClaudeMd,
          }
        : undefined,
      allowedTools: [
        'Bash',
        'Read',
        'Write',
        'Edit',
        'Glob',
        'Grep',
        'WebSearch',
        'WebFetch',
        'Task',
        'TaskOutput',
        'TaskStop',
        ...(teamsNeeded ? ['TeamCreate', 'TeamDelete', 'SendMessage'] : []),
        'TodoWrite',
        'ToolSearch',
        'Skill',
        'NotebookEdit',
        'mcp__deus__*',
        ...(hasGcalMcp ? ['mcp__gcal__*'] : []),
      ],
      env: sdkEnv,
      permissionMode: 'bypassPermissions',
      allowDangerouslySkipPermissions: true,
      settingSources: ['project', 'user'],
      mcpServers: {
        deus: {
          command: 'node',
          args: [mcpServerPath],
          env: {
            DEUS_CHAT_JID: containerInput.chatJid,
            DEUS_GROUP_FOLDER: containerInput.groupFolder,
            DEUS_IS_MAIN: containerInput.isMain ? '1' : '0',
          },
        },
        // Google Calendar MCP — only available when credentials exist on the host
        // and the project is mounted (main channel only). Runs from the host's
        // built package via the read-only /workspace/project mount.
        ...(hasGcalMcp
          ? {
              gcal: {
                command: 'node',
                args: ['/workspace/project/packages/mcp-gcal/dist/index.js'],
                env: {
                  DEUS_PROJECT_ROOT: '/workspace/project',
                  LOG_LEVEL: process.env.LOG_LEVEL || 'info',
                },
              },
            }
          : {}),
      },
      hooks: {
        PreCompact: [
          { hooks: [createPreCompactHook(containerInput.assistantName)] },
        ],
        ...(process.env.DEUS_TOOL_SIZE_LOG !== '0'
          ? { PostToolUse: [{ hooks: [createToolSizeLogHook()] }] }
          : {}),
      },
    },
  })) {
    messageCount++;
    const msgType =
      message.type === 'system'
        ? `system/${(message as { subtype?: string }).subtype}`
        : message.type;
    log(`[msg #${messageCount}] type=${msgType}`);

    if (message.type === 'assistant' && 'uuid' in message) {
      lastAssistantUuid = (message as { uuid: string }).uuid;
    }

    if (message.type === 'system' && message.subtype === 'init') {
      newSessionId = message.session_id;
      log(`Session initialized: ${newSessionId}`);
    }

    if (
      message.type === 'system' &&
      (message as { subtype?: string }).subtype === 'task_notification'
    ) {
      const tn = message as {
        task_id: string;
        status: string;
        summary: string;
      };
      log(
        `Task notification: task=${tn.task_id} status=${tn.status} summary=${tn.summary}`,
      );
    }

    if (message.type === 'result') {
      resultCount++;
      const textResult =
        'result' in message ? (message as { result?: string }).result : null;
      log(
        `Result #${resultCount}: subtype=${message.subtype}${textResult ? ` text=${textResult.slice(0, 200)}` : ''}`,
      );
      logUsage(message as SDKResultMessage);
      writeOutput({
        status: 'success',
        result: textResult || null,
        newSessionId,
      });
    }
  }

  ipcPolling = false;
  log(
    `Query done. Messages: ${messageCount}, results: ${resultCount}, lastAssistantUuid: ${lastAssistantUuid || 'none'}, closedDuringQuery: ${closedDuringQuery}`,
  );
  return { newSessionId, lastAssistantUuid, closedDuringQuery };
}

async function main(): Promise<void> {
  let containerInput: ContainerInput;

  try {
    const stdinData = await readStdin();
    containerInput = JSON.parse(stdinData);
    try {
      fs.unlinkSync('/tmp/input.json');
    } catch {
      /* may not exist */
    }
    log(`Received input for group: ${containerInput.groupFolder}`);
  } catch (err) {
    writeOutput({
      status: 'error',
      result: null,
      error: `Failed to parse input: ${err instanceof Error ? err.message : String(err)}`,
    });
    process.exit(1);
  }

  // Credentials are injected by the host's credential proxy via ANTHROPIC_BASE_URL.
  // No real secrets exist in the container environment.
  const sdkEnv: Record<string, string | undefined> = { ...process.env };

  const __dirname = path.dirname(fileURLToPath(import.meta.url));
  const mcpServerPath = path.join(__dirname, 'ipc-mcp-stdio.js');

  let sessionId = containerInput.sessionId;
  fs.mkdirSync(IPC_INPUT_DIR, { recursive: true });

  // Clean up stale _close sentinel from previous container runs
  try {
    fs.unlinkSync(IPC_INPUT_CLOSE_SENTINEL);
  } catch {
    /* ignore */
  }

  // Build initial prompt (drain any pending IPC messages too)
  let prompt = containerInput.prompt;
  if (containerInput.isScheduledTask) {
    prompt = `[SCHEDULED TASK - The following message was sent automatically and is not coming directly from the user or group.]\n\n${prompt}`;
  }
  const pending = drainIpcInput();
  if (pending.length > 0) {
    log(`Draining ${pending.length} pending IPC messages into initial prompt`);
    prompt += '\n' + pending.join('\n');
  }

  // --- Slash command handling ---
  // Only known session slash commands are handled here. This prevents
  // accidental interception of user prompts that happen to start with '/'.
  const KNOWN_SESSION_COMMANDS = new Set(['/compact']);
  const trimmedPrompt = prompt.trim();
  const isSessionSlashCommand = KNOWN_SESSION_COMMANDS.has(trimmedPrompt);

  if (isSessionSlashCommand) {
    log(`Handling session command: ${trimmedPrompt}`);
    let slashSessionId: string | undefined;
    let compactBoundarySeen = false;
    let hadError = false;
    let resultEmitted = false;

    try {
      for await (const message of query({
        prompt: trimmedPrompt,
        options: {
          cwd: '/workspace/group',
          resume: sessionId,
          systemPrompt: undefined,
          allowedTools: [],
          env: sdkEnv,
          permissionMode: 'bypassPermissions' as const,
          allowDangerouslySkipPermissions: true,
          settingSources: ['project', 'user'] as const,
          hooks: {
            PreCompact: [
              { hooks: [createPreCompactHook(containerInput.assistantName)] },
            ],
          },
        },
      })) {
        const msgType =
          message.type === 'system'
            ? `system/${(message as { subtype?: string }).subtype}`
            : message.type;
        log(`[slash-cmd] type=${msgType}`);

        if (message.type === 'system' && message.subtype === 'init') {
          slashSessionId = message.session_id;
          log(`Session after slash command: ${slashSessionId}`);
        }

        // Observe compact_boundary to confirm compaction completed
        if (
          message.type === 'system' &&
          (message as { subtype?: string }).subtype === 'compact_boundary'
        ) {
          compactBoundarySeen = true;
          log('Compact boundary observed — compaction completed');
        }

        if (message.type === 'result') {
          const resultSubtype = (message as { subtype?: string }).subtype;
          const textResult =
            'result' in message
              ? (message as { result?: string }).result
              : null;

          if (resultSubtype?.startsWith('error')) {
            hadError = true;
            writeOutput({
              status: 'error',
              result: null,
              error: textResult || 'Session command failed.',
              newSessionId: slashSessionId,
            });
          } else {
            writeOutput({
              status: 'success',
              result: textResult || 'Conversation compacted.',
              newSessionId: slashSessionId,
            });
          }
          resultEmitted = true;
        }
      }
    } catch (err) {
      hadError = true;
      const errorMsg = err instanceof Error ? err.message : String(err);
      log(`Slash command error: ${errorMsg}`);
      writeOutput({ status: 'error', result: null, error: errorMsg });
    }

    log(
      `Slash command done. compactBoundarySeen=${compactBoundarySeen}, hadError=${hadError}`,
    );

    // Warn if compact_boundary was never observed — compaction may not have occurred
    if (!hadError && !compactBoundarySeen) {
      log(
        'WARNING: compact_boundary was not observed. Compaction may not have completed.',
      );
    }

    // Only emit final session marker if no result was emitted yet and no error occurred
    if (!resultEmitted && !hadError) {
      writeOutput({
        status: 'success',
        result: compactBoundarySeen
          ? 'Conversation compacted.'
          : 'Compaction requested but compact_boundary was not observed.',
        newSessionId: slashSessionId,
      });
    } else if (!hadError) {
      // Emit session-only marker so host updates session tracking
      writeOutput({
        status: 'success',
        result: null,
        newSessionId: slashSessionId,
      });
    }
    return;
  }
  // --- End slash command handling ---

  // Query loop: run query → wait for IPC message → run new query → repeat
  let resumeAt: string | undefined;
  try {
    while (true) {
      log(
        `Starting query (session: ${sessionId || 'new'}, resumeAt: ${resumeAt || 'latest'})...`,
      );

      const queryResult = await runQuery(
        prompt,
        sessionId,
        mcpServerPath,
        containerInput,
        sdkEnv,
        resumeAt,
      );
      if (queryResult.newSessionId) {
        sessionId = queryResult.newSessionId;
      }
      if (queryResult.lastAssistantUuid) {
        resumeAt = queryResult.lastAssistantUuid;
      }

      // If _close was consumed during the query, exit immediately.
      // Don't emit a session-update marker (it would reset the host's
      // idle timer and cause a 30-min delay before the next _close).
      if (queryResult.closedDuringQuery) {
        log('Close sentinel consumed during query, exiting');
        break;
      }

      // Emit session update so host can track it
      writeOutput({ status: 'success', result: null, newSessionId: sessionId });

      log('Query ended, waiting for next IPC message...');

      // Wait for the next message or _close sentinel
      const nextMessage = await waitForIpcMessage();
      if (nextMessage === null) {
        log('Close sentinel received, exiting');
        break;
      }

      log(`Got new message (${nextMessage.length} chars), starting new query`);
      prompt = nextMessage;
    }
  } catch (err) {
    const errorMessage = err instanceof Error ? err.message : String(err);
    log(`Agent error: ${errorMessage}`);
    writeOutput({
      status: 'error',
      result: null,
      newSessionId: sessionId,
      error: errorMessage,
    });
    process.exit(1);
  }
}

main();
