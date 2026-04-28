import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import { loadRegisteredContextFiles } from './context-registry.js';
import { fetchMemoryContext } from './memory-retrieval-hook.js';
import {
  createOpenAIMcpToolBridge,
  executeBrokerTool,
  getOpenAIToolDefinitions,
  resolveGroupAttachmentPath as resolveBrokerGroupAttachmentPath,
} from './tool-broker.js';

export {
  buildRipgrepSearchArgs,
  resolveGroupAttachmentPath,
  resolvePublicWebTarget,
  resolveWorkspacePath,
  splitCommandArgs,
  validateScheduleInput,
} from './tool-broker.js';

export interface BackendSessionRef {
  backend: 'claude' | 'openai';
  session_id: string;
  resume_cursor?: string;
  metadata_json?: string;
}

export interface ContainerInput {
  prompt: string;
  backend?: 'claude' | 'openai';
  sessionId?: string;
  sessionRef?: BackendSessionRef;
  groupFolder: string;
  chatJid: string;
  isMain?: boolean;
  isControlGroup?: boolean;
  isScheduledTask?: boolean;
  assistantName?: string;
  imageAttachments?: Array<{ relativePath: string; mediaType: string }>;
  projectHint?: string;
}

export interface ContainerOutput {
  status: 'success' | 'error';
  result: string | null;
  newSessionRef?: BackendSessionRef;
  newSessionId?: string;
  error?: string;
}

interface OpenAIResponse {
  id: string;
  output?: Array<Record<string, unknown>>;
  output_text?: string;
}

interface OpenAIContext {
  containerInput: ContainerInput;
  log: (message: string) => void;
  writeOutput: (output: ContainerOutput) => void;
  drainIpcInput: () => string[];
  waitForIpcMessage: () => Promise<string | null>;
  shouldClose: () => boolean;
}

interface OpenAISessionMetadata {
  compact_summary?: string;
  compacted_at?: string;
  base_response_id?: string;
}

export function parseOpenAISessionMetadata(
  metadataJson: string | undefined,
): OpenAISessionMetadata {
  if (!metadataJson) return {};
  try {
    const parsed = JSON.parse(metadataJson) as Record<string, unknown>;
    return {
      compact_summary:
        typeof parsed.compact_summary === 'string'
          ? parsed.compact_summary
          : undefined,
      compacted_at:
        typeof parsed.compacted_at === 'string'
          ? parsed.compacted_at
          : undefined,
      base_response_id:
        typeof parsed.base_response_id === 'string'
          ? parsed.base_response_id
          : undefined,
    };
  } catch {
    return {};
  }
}

export function shouldResumeOpenAIResponse(
  sessionId: string | undefined,
): boolean {
  return Boolean(sessionId && !sessionId.startsWith('openai-compact-'));
}

function isControlGroup(containerInput: ContainerInput): boolean {
  return containerInput.isControlGroup ?? containerInput.isMain ?? false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object');
}

export function assertOpenAIResponse(value: unknown): OpenAIResponse {
  if (!isRecord(value)) {
    throw new Error('OpenAI response payload was not an object');
  }

  if (typeof value.id !== 'string' || value.id.length === 0) {
    throw new Error('OpenAI response payload did not include a valid id');
  }
  if (value.output !== undefined && !Array.isArray(value.output)) {
    throw new Error('OpenAI response payload included invalid output');
  }
  if (Array.isArray(value.output) && !value.output.every(isRecord)) {
    throw new Error('OpenAI response payload included invalid output item');
  }
  if (
    value.output_text !== undefined &&
    typeof value.output_text !== 'string'
  ) {
    throw new Error('OpenAI response payload included invalid output_text');
  }

  return {
    id: value.id,
    output: value.output,
    output_text: value.output_text,
  };
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

  const sessionMetadata = parseOpenAISessionMetadata(
    containerInput.sessionRef?.metadata_json,
  );
  const instructions = [
    'You are running inside the Deus backend-neutral OpenAI adapter.',
    'Preserve the same Deus user experience as the Claude backend: same tone, memory, privacy boundaries, chat commands, and long-term personal context.',
    'Use the provided Deus tools for shell, filesystem, web, browser, and IPC actions.',
    `Primary working directory: ${cwd}`,
    ...loadRegisteredContextFiles({
      isControlGroup: isControlGroup(containerInput),
      hasProject: cwd === projectDir,
    }),
    sessionMetadata.compact_summary
      ? `Compacted conversation memory:\n${sessionMetadata.compact_summary}`
      : '',
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

function getAssistantText(response: OpenAIResponse): string | null {
  if (
    typeof response.output_text === 'string' &&
    response.output_text.length > 0
  ) {
    return response.output_text;
  }

  const texts: string[] = [];
  for (const item of response.output || []) {
    if (item.type !== 'message') continue;
    const content = Array.isArray(item.content) ? item.content : [];
    for (const block of content) {
      if (
        block &&
        typeof block === 'object' &&
        block.type === 'output_text' &&
        typeof block.text === 'string'
      ) {
        texts.push(block.text);
      }
    }
  }
  return texts.length > 0 ? texts.join('\n') : null;
}

function getFunctionCalls(response: OpenAIResponse): Array<{
  name: string;
  call_id: string;
  arguments: string;
}> {
  const calls: Array<{ name: string; call_id: string; arguments: string }> = [];
  for (const item of response.output || []) {
    if (item.type !== 'function_call') continue;
    const name = typeof item.name === 'string' ? item.name : '';
    const callId = typeof item.call_id === 'string' ? item.call_id : '';
    const args = typeof item.arguments === 'string' ? item.arguments : '{}';
    if (name && callId) {
      calls.push({ name, call_id: callId, arguments: args });
    }
  }
  return calls;
}

function buildInitialInput(
  prompt: string,
  containerInput: ContainerInput,
): Array<Record<string, unknown>> {
  const content: Array<Record<string, unknown>> = [
    {
      type: 'input_text',
      text: prompt,
    },
  ];

  for (const img of containerInput.imageAttachments || []) {
    const imgPath = resolveBrokerGroupAttachmentPath(img.relativePath);
    try {
      const data = fs.readFileSync(imgPath).toString('base64');
      content.push({
        type: 'input_image',
        image_url: `data:${img.mediaType};base64,${data}`,
      });
    } catch {
      // Ignore broken image reads — text prompt still proceeds.
    }
  }

  return [
    {
      role: 'user',
      content,
    },
  ];
}

async function createResponse(
  body: Record<string, unknown>,
): Promise<OpenAIResponse> {
  const baseUrl = process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1';
  const normalizedBase = baseUrl.endsWith('/v1')
    ? baseUrl
    : `${baseUrl.replace(/\/$/, '')}/v1`;
  const res = await fetch(`${normalizedBase}/responses`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${process.env.OPENAI_API_KEY || 'placeholder'}`,
      ...(process.env.DEUS_PROXY_TOKEN
        ? { 'x-deus-proxy-token': process.env.DEUS_PROXY_TOKEN }
        : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `OpenAI response failed (${res.status}): ${text.slice(0, 300)}`,
    );
  }
  return assertOpenAIResponse(await res.json());
}

async function runSingleTurn(
  prompt: string,
  containerInput: ContainerInput,
  previousResponseId: string | undefined,
  log: (message: string) => void,
): Promise<{ responseId: string; result: string | null }> {
  const { cwd, hasProject, systemInstructions } =
    getRuntimeContext(containerInput);
  const mcpBridge = await createOpenAIMcpToolBridge(
    getOptionalMcpServerConfigs(containerInput).filter(
      (config) => config.serverName !== 'gcal' || hasProject,
    ),
    log,
  );
  let input: Array<Record<string, unknown>> = buildInitialInput(
    prompt,
    containerInput,
  );
  let responseId = shouldResumeOpenAIResponse(previousResponseId)
    ? previousResponseId
    : undefined;

  try {
    while (true) {
      const response = await createResponse({
        model: process.env.DEUS_OPENAI_MODEL || 'gpt-4o',
        input,
        instructions: systemInstructions,
        previous_response_id: responseId,
        tools: getOpenAIToolDefinitions(mcpBridge.definitions),
        parallel_tool_calls: true,
      });

      responseId = response.id;
      const calls = getFunctionCalls(response);
      if (calls.length === 0) {
        return { responseId, result: getAssistantText(response) };
      }

      log(`OpenAI requested ${calls.length} tool call(s)`);
      input = [];
      for (const call of calls) {
        let parsedArgs: Record<string, unknown> = {};
        try {
          parsedArgs = JSON.parse(call.arguments || '{}') as Record<
            string,
            unknown
          >;
        } catch {
          parsedArgs = {};
        }
        let toolResult =
          (await mcpBridge.execute(call.name, parsedArgs)) ?? undefined;
        if (!toolResult) {
          try {
            toolResult = await executeBrokerTool(call.name, parsedArgs, {
              cwd,
              containerInput,
            });
          } catch (err) {
            toolResult = {
              ok: false,
              error: err instanceof Error ? err.message : String(err),
            };
          }
        }
        input.push({
          type: 'function_call_output',
          call_id: call.call_id,
          output: JSON.stringify(toolResult),
        });
      }
    }
  } finally {
    await mcpBridge.close();
  }
}

async function compactOpenAISession(
  containerInput: ContainerInput,
  sessionId: string,
): Promise<{
  syntheticSessionId: string;
  metadataJson: string;
  summary: string;
}> {
  const { systemInstructions } = getRuntimeContext(containerInput);
  const response = await createResponse({
    model: process.env.DEUS_OPENAI_MODEL || 'gpt-4o',
    instructions: `${systemInstructions}\n\nYou are compacting the active Deus conversation. Preserve durable user preferences, unresolved tasks, important decisions, project state, names, tone preferences, and any facts needed for continuity. Do not invent missing facts.`,
    previous_response_id: sessionId,
    input: [
      {
        role: 'user',
        content: [
          {
            type: 'input_text',
            text: 'Create a compact continuity summary for future turns. Keep it concise but complete enough that Deus can continue with the same memory and behavior.',
          },
        ],
      },
    ],
  });
  const summary = getAssistantText(response) || 'No compact summary returned.';
  const metadata: OpenAISessionMetadata = {
    compact_summary: summary,
    compacted_at: new Date().toISOString(),
    base_response_id: sessionId,
  };
  return {
    syntheticSessionId: `openai-compact-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    metadataJson: JSON.stringify(metadata),
    summary,
  };
}

export async function runOpenAIConversation(ctx: OpenAIContext): Promise<void> {
  const {
    containerInput,
    writeOutput,
    log,
    drainIpcInput,
    waitForIpcMessage,
    shouldClose,
  } = ctx;
  let sessionId =
    containerInput.sessionRef?.session_id || containerInput.sessionId;
  let metadataJson = containerInput.sessionRef?.metadata_json;

  let prompt = containerInput.prompt;
  if (prompt.trim() === '/compact') {
    if (sessionId && shouldResumeOpenAIResponse(sessionId)) {
      try {
        const compacted = await compactOpenAISession(containerInput, sessionId);
        sessionId = compacted.syntheticSessionId;
        metadataJson = compacted.metadataJson;
      } catch (err) {
        writeOutput({
          status: 'error',
          result: null,
          error: err instanceof Error ? err.message : String(err),
          newSessionId: sessionId,
          newSessionRef: sessionId
            ? {
                backend: 'openai',
                session_id: sessionId,
                metadata_json: metadataJson,
              }
            : undefined,
        });
        return;
      }
    }

    writeOutput({
      status: 'success',
      result: 'Conversation compacted.',
      newSessionId: sessionId,
      newSessionRef: sessionId
        ? {
            backend: 'openai',
            session_id: sessionId,
            metadata_json: metadataJson,
          }
        : undefined,
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

  while (true) {
    if (shouldClose()) break;

    const memoryContext = await fetchMemoryContext(prompt, 'container-openai');
    const enrichedPrompt = memoryContext
      ? `${memoryContext}\n\n${prompt}`
      : prompt;
    const turn = await runSingleTurn(
      enrichedPrompt,
      containerInput,
      sessionId,
      log,
    );
    sessionId = turn.responseId;
    writeOutput({
      status: 'success',
      result: turn.result,
      newSessionId: sessionId,
      newSessionRef: {
        backend: 'openai',
        session_id: sessionId,
        metadata_json: metadataJson,
      },
    });
    writeOutput({
      status: 'success',
      result: null,
      newSessionId: sessionId,
      newSessionRef: {
        backend: 'openai',
        session_id: sessionId,
        metadata_json: metadataJson,
      },
    });

    if (shouldClose()) break;

    const nextMessage = await waitForIpcMessage();
    if (nextMessage === null) break;
    prompt = nextMessage;
  }
}
