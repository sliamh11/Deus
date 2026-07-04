/**
 * llama.cpp container-side driver.
 *
 * Connects the Deus container agent to a local `llama-server` via the
 * OpenAI-compatible `/v1/chat/completions` endpoint. Distinct from
 * `runOpenAIConversation` (which targets the OpenAI Responses API).
 *
 * Reused plumbing (transport-agnostic):
 *   - `createOpenAIMcpToolBridge`, `getOpenAIToolDefinitions`,
 *     `executeBrokerTool` from `tool-broker.ts`
 *   - `loadRegisteredContextFiles` for system instructions
 *   - `fetchMemoryContext` for per-turn memory injection
 *   - `DoomLoopDetector` and `normalizeArgs` for repeated-call detection
 *
 * Session continuity: messages array kept in-memory across turns within a
 * single container lifecycle. Cross-restart resume is a follow-up.
 *
 * `/compact`: simple history truncation (system + last N turns). A future
 * summary-based compaction is tracked as a follow-up.
 */

import { randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import { loadRegisteredContextFiles } from './context-registry.js';
import { fetchMemoryContext } from './memory-retrieval-hook.js';
import { DoomLoopDetector, normalizeArgs } from './doom-loop-detector.js';
import {
  maskStaleToolResults,
  maskingEnabled,
  maskKeepTurns,
  maskMinBytes,
} from './observation-masking.js';
import { dispatchPreToolUseGate } from './pre-tool-use-hook.js';
import {
  type AgentRuntimeId,
  createOpenAIMcpToolBridge,
  executeBrokerTool,
  getOpenAIToolDefinitions,
} from './tool-broker.js';

export interface RuntimeSession {
  backend: AgentRuntimeId;
  session_id: string;
  resume_cursor?: string;
  metadata_json?: string;
}

export interface ContainerInput {
  prompt: string;
  backend?: AgentRuntimeId;
  sessionId?: string;
  sessionRef?: RuntimeSession;
  groupFolder: string;
  chatJid: string;
  isMain?: boolean;
  isControlGroup?: boolean;
  isScheduledTask?: boolean;
  assistantName?: string;
  imageAttachments?: Array<{ relativePath: string; mediaType: string }>;
  projectHint?: string;
  effort?: 'low' | 'medium' | 'high' | 'max';
}

interface ContextStats {
  tokens: number;
  limit: number;
  pct: number;
  warn?: boolean;
  autoCompact?: boolean;
}

export interface ContainerOutput {
  status: 'success' | 'error';
  result: string | null;
  newSessionRef?: RuntimeSession;
  newSessionId?: string;
  error?: string;
  contextStats?: ContextStats;
}

interface LlamaCppContext {
  containerInput: ContainerInput;
  log: (message: string) => void;
  writeOutput: (output: ContainerOutput) => void;
  drainIpcInput: () => string[];
  waitForIpcMessage: () => Promise<string | null>;
  shouldClose: () => boolean;
}

// OpenAI chat-completions message shapes. Defined here (not imported)
// because llama.cpp's response format diverges from the Responses API.
type ChatRole = 'system' | 'user' | 'assistant' | 'tool';

interface ChatToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

interface ChatMessage {
  role: ChatRole;
  content?: string | Array<Record<string, unknown>> | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ChatToolCall[];
}

interface ChatCompletionResponse {
  id: string;
  choices: Array<{
    message: ChatMessage;
    finish_reason?: string;
  }>;
}

const COMPACT_KEEP_TURNS = 8;
const LLAMA_CPP_CONTEXT_WINDOW = parseInt(
  process.env.LLAMA_CPP_CONTEXT_WINDOW || '32768',
  10,
);
const AUTO_COMPACT_PCT = parseInt(
  process.env.DEUS_CONTEXT_AUTO_COMPACT_PCT || '75',
  10,
);

function estimateTokens(messages: ChatMessage[]): number {
  let chars = 0;
  for (const m of messages) {
    chars += typeof m.content === 'string' ? m.content.length : 0;
  }
  return Math.round(chars / 4);
}

function isControlGroup(containerInput: ContainerInput): boolean {
  return containerInput.isControlGroup ?? containerInput.isMain ?? false;
}

function getRuntimeContext(containerInput: ContainerInput): {
  cwd: string;
  hasProject: boolean;
  systemInstructions: string;
} {
  const projectDir = '/workspace/project';
  let cwd = '/workspace/group';
  let hasProject = false;
  try {
    const stat = fs.statSync(projectDir);
    if (stat.isDirectory()) {
      const realProjectDir = fs.realpathSync(projectDir);
      if (
        realProjectDir.startsWith('/workspace/') &&
        fs.readdirSync(projectDir).some((f) => !f.startsWith('.'))
      ) {
        cwd = projectDir;
        hasProject = true;
      }
    }
  } catch {
    // No project mount — use group workspace.
  }

  const instructions = [
    'You are running inside the Deus backend-neutral llama.cpp adapter.',
    'Preserve the same Deus user experience as the Claude and OpenAI backends: same tone, memory, privacy boundaries, chat commands, and long-term personal context.',
    'Use the provided Deus tools for shell, filesystem, web, browser, and IPC actions.',
    `Primary working directory: ${cwd}`,
    ...loadRegisteredContextFiles({
      isControlGroup: isControlGroup(containerInput),
      hasProject: cwd === projectDir,
    }),
    containerInput.projectHint || '',
  ]
    .filter(Boolean)
    .join('\n\n');

  return { cwd, hasProject, systemInstructions: instructions };
}

function getOptionalMcpServerConfigs(containerInput: ContainerInput): Array<{
  serverName: string;
  command: string;
  args?: string[];
  env?: Record<string, string>;
  required?: boolean;
}> {
  const configs: Array<{
    serverName: string;
    command: string;
    args?: string[];
    env?: Record<string, string>;
    required?: boolean;
  }> = [];
  const __dirname = path.dirname(fileURLToPath(import.meta.url));

  configs.push({
    serverName: 'deus',
    command: 'node',
    args: [path.join(__dirname, 'ipc-mcp-stdio.js')],
    env: {
      DEUS_CHAT_JID: containerInput.chatJid,
      DEUS_GROUP_FOLDER: containerInput.groupFolder,
      DEUS_IS_MAIN: isControlGroup(containerInput) ? '1' : '0',
    },
    required: true,
  });

  const projectDir = '/workspace/project';
  const gcalDistPath = path.join(projectDir, 'packages/mcp-gcal/dist/index.js');
  const gcalCredsPath = path.join(
    projectDir,
    'integrations/gcal/credentials.json',
  );
  const gcalTokensPath = path.join(projectDir, 'integrations/gcal/tokens.json');
  if (
    fs.existsSync(gcalDistPath) &&
    fs.existsSync(gcalCredsPath) &&
    fs.existsSync(gcalTokensPath)
  ) {
    configs.push({
      serverName: 'gcal',
      command: 'node',
      args: [gcalDistPath],
      env: {
        DEUS_PROJECT_ROOT: projectDir,
        LOG_LEVEL: process.env.LOG_LEVEL || 'info',
      },
      required: false,
    });
  }

  return configs;
}

async function createChatCompletion(body: {
  model: string;
  messages: ChatMessage[];
  tools?: Array<Record<string, unknown>>;
  tool_choice?: 'auto' | 'none' | Record<string, unknown>;
}): Promise<ChatCompletionResponse> {
  const baseUrl = process.env.LLAMA_CPP_BASE_URL || 'http://127.0.0.1:8080/v1';
  const normalizedBase = baseUrl.endsWith('/v1')
    ? baseUrl
    : `${baseUrl.replace(/\/$/, '')}/v1`;
  const res = await fetch(`${normalizedBase}/chat/completions`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${process.env.LLAMA_CPP_API_KEY || 'placeholder'}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `llama.cpp chat completion failed (${res.status}): ${text.slice(0, 300)}`,
    );
  }
  return (await res.json()) as ChatCompletionResponse;
}

function compactMessages(messages: ChatMessage[]): ChatMessage[] {
  // Keep the system prompt and the most recent N turns. A turn is loosely
  // a user→assistant exchange (counted as one user message); tool messages
  // attach to the assistant turn that triggered them.
  if (messages.length <= 1) return messages;
  const system = messages[0]?.role === 'system' ? messages[0] : undefined;
  const rest = system ? messages.slice(1) : messages;
  const userIndices: number[] = [];
  for (let i = 0; i < rest.length; i += 1) {
    if (rest[i].role === 'user') userIndices.push(i);
  }
  if (userIndices.length <= COMPACT_KEEP_TURNS) return messages;
  const startUserIdx = userIndices[userIndices.length - COMPACT_KEEP_TURNS];
  const truncated = rest.slice(startUserIdx);
  return system ? [system, ...truncated] : truncated;
}

// Exported for unit tests (PreToolUse gate seam). Internal to the conversation
// driver otherwise.
export async function runSingleTurn(
  prompt: string,
  containerInput: ContainerInput,
  messages: ChatMessage[],
  log: (message: string) => void,
  doomDetector: DoomLoopDetector,
): Promise<{ result: string | null }> {
  const { cwd, hasProject } = getRuntimeContext(containerInput);
  const mcpBridge = await createOpenAIMcpToolBridge(
    getOptionalMcpServerConfigs(containerInput).filter(
      (config) => config.serverName !== 'gcal' || hasProject,
    ),
    log,
  );

  // Append the new user prompt to the running messages array.
  messages.push({ role: 'user', content: prompt });

  try {
    while (true) {
      // Per-surface model resolution (Phase 3): LLAMA_CPP_AGENT_MODEL > LLAMA_CPP_MODEL > ''
      // Empty string is correct for llama-server router mode (auto-pick loaded model).
      // The 'gpt-3.5-turbo' default was removed — it would 404 in router mode.
      const response = await createChatCompletion({
        model:
          process.env.LLAMA_CPP_AGENT_MODEL ||
          process.env.LLAMA_CPP_MODEL ||
          '',
        messages,
        tools: getOpenAIToolDefinitions(mcpBridge.definitions).map((def) => ({
          type: 'function',
          function: {
            name: def.name,
            description: def.description,
            parameters: def.parameters,
          },
        })),
        tool_choice: 'auto',
      });

      const choice = response.choices?.[0];
      if (!choice?.message) {
        throw new Error('llama.cpp response missing assistant message');
      }
      const assistantMessage = choice.message;
      messages.push(assistantMessage);

      const calls = assistantMessage.tool_calls ?? [];
      if (calls.length === 0) {
        const text =
          typeof assistantMessage.content === 'string'
            ? assistantMessage.content
            : Array.isArray(assistantMessage.content)
              ? assistantMessage.content
                  .map((b) =>
                    b && typeof b === 'object' && typeof b.text === 'string'
                      ? b.text
                      : '',
                  )
                  .join('\n')
              : '';
        return { result: text || null };
      }

      log(`llama.cpp requested ${calls.length} tool call(s)`);
      for (const call of calls) {
        let parsedArgs: Record<string, unknown> = {};
        try {
          parsedArgs = JSON.parse(call.function.arguments || '{}') as Record<
            string,
            unknown
          >;
        } catch {
          parsedArgs = {};
        }

        // PreToolUse gate (default-off via HOOK_DISPATCH_ENABLED, fail-open) —
        // mirrors the Claude SDK's PreToolUse hook. On a block the tool is NOT
        // executed; the refusal is fed back as the tool result. No doomDetector
        // record on a block — the detector tracks repeated *execution*, and a
        // blocked call never ran.
        const gate = await dispatchPreToolUseGate({
          toolName: call.function.name,
          toolInput: parsedArgs,
          toolUseId: call.id,
          // sessionId is not available in this scope (the llama-cpp runSingleTurn
          // tracks state via the messages array, not a response id). The OpenAI
          // backend passes its responseId; observers must tolerate an absent
          // session_id on this path.
        });
        if (gate.block) {
          messages.push({
            role: 'tool',
            tool_call_id: call.id,
            content: JSON.stringify({
              ok: false,
              error: gate.reason ?? 'Blocked by PreToolUse gate',
            }),
          });
          continue;
        }

        let toolResult =
          (await mcpBridge.execute(call.function.name, parsedArgs)) ??
          undefined;
        if (!toolResult) {
          try {
            toolResult = await executeBrokerTool(
              call.function.name,
              parsedArgs,
              {
                cwd,
                containerInput,
              },
            );
          } catch (err) {
            toolResult = {
              ok: false,
              error: err instanceof Error ? err.message : String(err),
            };
          }
        }

        const exitCode: number =
          typeof toolResult?.exitCode === 'number'
            ? toolResult.exitCode
            : toolResult?.ok === false
              ? 1
              : 0;
        const succeeded = toolResult?.ok !== false && exitCode === 0;
        doomDetector.record({
          toolName: call.function.name,
          normalizedArgs: normalizeArgs(call.function.name, parsedArgs),
          exitCode,
          succeeded,
        });

        messages.push({
          role: 'tool',
          tool_call_id: call.id,
          content: JSON.stringify(toolResult ?? {}),
        });
      }
    }
  } finally {
    await mcpBridge.close();
  }
}

export async function runLlamaCppConversation(
  ctx: LlamaCppContext,
): Promise<void> {
  const {
    containerInput,
    writeOutput,
    log,
    drainIpcInput,
    waitForIpcMessage,
    shouldClose,
  } = ctx;

  if (containerInput.effort && containerInput.effort !== 'low') {
    log(
      `Effort level '${containerInput.effort}' requested but not yet supported for llama-cpp backend — using model default`,
    );
  }

  const { systemInstructions } = getRuntimeContext(containerInput);
  // Session continuity within a single container lifecycle: keep messages
  // in-memory. Cross-restart resume is a follow-up (would persist via
  // sessionRef.metadata_json).
  const messages: ChatMessage[] = [
    { role: 'system', content: systemInstructions },
  ];
  // Synthetic correlation id (not a security token). Use crypto.randomBytes
  // — Math.random() triggered a CodeQL insecure-randomness warning here and
  // is inexpensive to resolve.
  const sessionId = `llama-cpp-${Date.now()}-${randomBytes(4).toString('hex')}`;

  let prompt = containerInput.prompt;

  if (prompt.trim() === '/compact') {
    const before = messages.length;
    const truncated = compactMessages(messages);
    messages.length = 0;
    messages.push(...truncated);
    const noopCompact = before <= 1 || before === messages.length;
    const result = noopCompact
      ? 'Nothing to compact — conversation is already at the start.'
      : `Conversation compacted (history truncated ${before} → ${messages.length} messages).`;
    log(`llama.cpp /compact: ${result}`);
    writeOutput({
      status: 'success',
      result,
      newSessionId: sessionId,
      newSessionRef: {
        backend: 'llama-cpp',
        session_id: sessionId,
      },
    });
    return;
  }

  if (containerInput.isScheduledTask) {
    prompt = `[SCHEDULED TASK - The following message was sent automatically and is not coming directly from the user or group.]\n\n${prompt}`;
  }
  const pending = drainIpcInput();
  if (pending.length > 0) {
    prompt += '\n' + pending.join('\n');
  }

  const doomDetector = new DoomLoopDetector();

  while (true) {
    if (shouldClose()) break;

    const memoryContext = await fetchMemoryContext(
      prompt,
      'container-llama-cpp',
    );
    const enrichedPrompt = memoryContext
      ? `${memoryContext}\n\n${prompt}`
      : prompt;

    try {
      // Observation masking (LIA-378): the gentler lever runs BEFORE the
      // token estimate, so proactive compaction (destructive) evaluates
      // post-mask sizes and triggers less often. Dark by default.
      if (maskingEnabled()) {
        const { masked } = maskStaleToolResults(messages, {
          keepRecentTurns: maskKeepTurns(),
          minBytes: maskMinBytes(),
        });
        if (masked > 0)
          log(`Observation masking: ${masked} stale tool result(s) masked`);
      }
      const estTokens = estimateTokens(messages);
      const estPct = Math.round((estTokens / LLAMA_CPP_CONTEXT_WINDOW) * 100);
      if (estPct >= AUTO_COMPACT_PCT && messages.length > 2) {
        const before = messages.length;
        const truncated = compactMessages(messages);
        messages.length = 0;
        messages.push(...truncated);
        log(
          `Proactive compact: ${before} → ${messages.length} msgs (${estPct}% of ${LLAMA_CPP_CONTEXT_WINDOW})`,
        );
      }

      const turn = await runSingleTurn(
        enrichedPrompt,
        containerInput,
        messages,
        log,
        doomDetector,
      );

      const postTokens = estimateTokens(messages);
      const postPct = Math.round((postTokens / LLAMA_CPP_CONTEXT_WINDOW) * 100);
      const contextStats: ContextStats = {
        tokens: postTokens,
        limit: LLAMA_CPP_CONTEXT_WINDOW,
        pct: postPct,
      };

      writeOutput({
        status: 'success',
        result: turn.result,
        newSessionId: sessionId,
        newSessionRef: {
          backend: 'llama-cpp',
          session_id: sessionId,
        },
        contextStats,
      });
      writeOutput({
        status: 'success',
        result: null,
        newSessionId: sessionId,
        newSessionRef: {
          backend: 'llama-cpp',
          session_id: sessionId,
        },
      });
    } catch (err) {
      writeOutput({
        status: 'error',
        result: null,
        error: err instanceof Error ? err.message : String(err),
        newSessionId: sessionId,
        newSessionRef: {
          backend: 'llama-cpp',
          session_id: sessionId,
        },
      });
    }

    if (shouldClose()) break;

    if (containerInput.isScheduledTask) {
      log('Scheduled task: exiting after first result');
      break;
    }

    const nextMessage = await waitForIpcMessage();
    if (nextMessage === null) break;
    prompt = nextMessage;
  }
}
