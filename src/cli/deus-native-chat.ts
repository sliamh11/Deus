/**
 * `deus chat` — deus-native CLI chat controller (LIA-428 / G1).
 *
 * This module owns the terminal-chat seam over the registered `deus-native`
 * `AgentRuntime`: the fixed synthetic CLI identity, the injectable
 * session-store interface, the turn/session lifecycle, and the
 * anti-corruption boundary that converts the internal `RuntimeEvent` union
 * into a small, UI-safe display-event union. Nothing here talks HTTP or
 * readline — the daemon-side server (`deus-native-chat-server.ts`) and the
 * terminal client (`deus-native-chat-client.ts`) compose around this
 * controller.
 *
 * Non-goals of this module, intentionally deferred to later roadmap items
 * (do not add these here):
 * - G2 model-selection UX: no `--model` flag, no model picker, no model
 *   persistence, no provider switching. G2 extends `DeusNativeChatOptions`
 *   with a typed field and translates it into the runtime-supported model
 *   configuration; LIA-428 ships no placeholder `model` field.
 * - G3 plan-mode toggle: no `--plan`, `/plan`, mode prompt, or
 *   permission-profile change. Same options-object seam as G2.
 * - No client-controlled `backendConfig` passthrough: exposing arbitrary
 *   runtime configuration to the terminal client would widen a
 *   security-sensitive surface for no current caller.
 * - No multi-session picker, named conversations, `/new`, transcript
 *   export, history UI, or concurrent clients sharing one CLI thread —
 *   `deus chat` resumes the ONE recorded deus-native CLI thread.
 * - No invented tool-result event and no fake token streaming: the
 *   `RuntimeEvent` contract has no tool-result variant and `deus-native`
 *   emits after buffered `agent.invoke()`; this module renders only the
 *   normalized events the contract supplies today.
 * - No change to `DeusNativeRuntime` session-minting/checkpointer
 *   semantics and no second persistence mechanism: the controller caches
 *   the authoritative SQLite row (via the injected store), nothing more.
 */

import path from 'path';

import type {
  AgentRuntime,
  AgentRuntimeId,
  RunContext,
  RuntimeEvent,
  RuntimeSession,
} from '../agent-runtimes/types.js';
import { CONFIG_DIR } from '../config.js';

// ---------------------------------------------------------------------------
// Fixed synthetic CLI identity.
//
// The group folder is deliberately unregistered: DeusNativeRuntime.runTurn
// only consults resolveGroup for the publicIngress refusal
// (deus-native-backend.ts), so `undefined` follows the safe
// non-public-ingress path — and a synthetic folder guarantees the CLI
// session can never overwrite a real channel group's backend-scoped session
// row. isControlGroup stays false: this ticket grants the terminal client
// no control-group privileges.
// ---------------------------------------------------------------------------
export const CLI_CHAT_GROUP_FOLDER = 'deus-native-cli';
export const CLI_CHAT_JID = 'cli:deus-native';
export const CLI_CHAT_BACKEND = 'deus-native' satisfies AgentRuntimeId;

/** Protocol version shared by the discovery record and the turn request body. */
export const NATIVE_CHAT_PROTOCOL_VERSION = 1;

/** Options seam for G2 (model selection) / G3 (plan mode) to extend later. */
export interface DeusNativeChatOptions {
  cwd: string;
  resume: true;
}

/**
 * Adapter interface over the daemon's `db.getSession`/`db.setSession`
 * persistence (backend-scoped, one active row per (group, backend) pair).
 * Injectable so controller tests run against an in-memory fake.
 */
export interface NativeChatSessionStore {
  get(groupFolder: string, backend: AgentRuntimeId): RuntimeSession | undefined;
  set(groupFolder: string, session: RuntimeSession): void;
}

// ---------------------------------------------------------------------------
// Normalized display events — the ONLY shape that crosses toward a terminal.
// The internal `RuntimeEvent` union never leaves this module.
// ---------------------------------------------------------------------------
export type ChatDisplayEvent =
  | { kind: 'assistant_text'; text: string }
  | { kind: 'tool_use'; label: string }
  | { kind: 'progress'; text: string }
  | { kind: 'assistant_done' }
  | { kind: 'chat_error'; message: string };

export type ChatDisplayEventSink = (
  event: ChatDisplayEvent,
) => void | Promise<void>;

export interface NativeChatUsageSnapshot {
  provider: string;
  model: string;
  inputTokens: number | undefined;
  outputTokens: number | undefined;
  totalTokens: number | undefined;
}

export interface NativeChatStatus {
  backend: 'deus-native';
  sessionId: string | undefined;
  /** 'resumed' = a backend-matching row existed at controller start. */
  state: 'new' | 'resumed';
  /** Honest event mode: derived from the runtime's capabilities today. */
  output: 'buffered' | 'streaming';
  /** Latest per-model-call usage, for diagnostics only (never chat output). */
  usage?: NativeChatUsageSnapshot;
}

/** Thrown when a second turn is requested while one is in flight. */
export class NativeChatBusyError extends Error {
  constructor() {
    super('a chat turn is already in progress');
    this.name = 'NativeChatBusyError';
  }
}

export interface DeusNativeChatController {
  /** Load the stored deus-native row (if any) and fix the new/resumed state. */
  start(): Promise<void>;
  /**
   * Run one prompt through the runtime, streaming normalized display events
   * to `onEvent`. Resolves `{ ok: false }` on a runtime-reported error
   * (already surfaced as a `chat_error` display event).
   */
  runTurn(
    prompt: string,
    options: DeusNativeChatOptions,
    onEvent: ChatDisplayEventSink,
  ): Promise<{ ok: boolean }>;
  status(): NativeChatStatus;
  /** Close the runtime session. Never clears the stored row (resume depends on it). */
  close(): Promise<void>;
}

const PROGRESS_TEXT_MAX = 200;
const TOOL_SUMMARY_MAX = 120;
const ERROR_TEXT_MAX = 500;
const REDACTED_ARG_KEY = /token|auth|secret|password|credential|key/i;
/**
 * Argument keys worth summarizing on a tool-feedback line. Filtered through
 * REDACTED_ARG_KEY at definition time: this is defense-in-depth against a
 * future edit accidentally adding a sensitive-looking key literal here (none
 * of today's four literals match, so the filter is a no-op today) — it does
 * NOT scan the tool call's actual argument keys, since only the whitelisted
 * keys below are ever read from `args` in the first place. Never dumps
 * arbitrary argument JSON.
 */
const TOOL_SUMMARY_KEYS = ['query', 'url', 'q', 'href'].filter(
  (key) => !REDACTED_ARG_KEY.test(key),
);

/** Strip control characters so runtime-supplied text cannot inject terminal escapes. */
function sanitizeInline(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, ' ');
}

function bound(text: string, max: number): string {
  const clean = sanitizeInline(text);
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
}

/**
 * Compact tool feedback: `Using <name>…`, optionally with ONE bounded,
 * escaped argument summary (query/URL). Never dumps arbitrary argument JSON:
 * only TOOL_SUMMARY_KEYS's whitelisted keys are ever inspected.
 */
export function formatToolUse(
  name: string,
  args: Record<string, unknown>,
): string {
  let summary: string | undefined;
  for (const key of TOOL_SUMMARY_KEYS) {
    const value = args[key];
    if (typeof value === 'string' && value.trim() !== '') {
      summary = bound(value, TOOL_SUMMARY_MAX);
      break;
    }
  }
  const safeName = bound(name, 60);
  return summary ? `Using ${safeName}: ${summary}…` : `Using ${safeName}…`;
}

export function createDeusNativeChatController(deps: {
  runtime: AgentRuntime;
  sessions: NativeChatSessionStore;
}): DeusNativeChatController {
  const { runtime, sessions } = deps;
  if (runtime.name() !== CLI_CHAT_BACKEND) {
    // Fail closed: this controller is deus-native-only by ticket scope; a
    // mis-resolved runtime must never silently drive the CLI thread.
    throw new Error(
      `deus-native chat controller requires the 'deus-native' runtime, got '${runtime.name()}'`,
    );
  }

  let current: RuntimeSession | undefined;
  let startedState: 'new' | 'resumed' = 'new';
  let lastUsage: NativeChatUsageSnapshot | undefined;
  let inFlight = false;

  /**
   * Accept a runtime-reported session ref only when it is a plausible
   * deus-native continuity marker: correct backend and a non-empty id.
   * A foreign-backend or empty ref must never replace a valid stored row.
   */
  function adoptSession(ref: RuntimeSession): void {
    if (ref.backend !== CLI_CHAT_BACKEND) return;
    if (ref.session_id === '') return;
    current = ref;
    sessions.set(CLI_CHAT_GROUP_FOLDER, ref);
  }

  async function normalize(
    event: RuntimeEvent,
    onEvent: ChatDisplayEventSink,
  ): Promise<void> {
    switch (event.type) {
      case 'output_text':
        await onEvent({ kind: 'assistant_text', text: event.text });
        break;
      case 'activity':
        await onEvent({
          kind: 'progress',
          text: bound(event.text, PROGRESS_TEXT_MAX),
        });
        break;
      case 'tool_call':
        await onEvent({
          kind: 'tool_use',
          label: formatToolUse(event.name, event.arguments),
        });
        break;
      case 'usage':
        // Diagnostics only — provider/model accounting stays out of chat output.
        lastUsage = {
          provider: event.provider,
          model: event.model,
          inputTokens: event.inputTokens,
          outputTokens: event.outputTokens,
          totalTokens: event.totalTokens,
        };
        break;
      case 'session':
        // Canonical event path (message-orchestrator.ts precedent): persist
        // immediately so a mid-turn crash cannot lose the continuity marker.
        adoptSession(event.sessionRef);
        break;
      case 'turn_complete':
        await onEvent({ kind: 'assistant_done' });
        break;
      case 'error':
        await onEvent({
          kind: 'chat_error',
          message: bound(event.error, ERROR_TEXT_MAX),
        });
        break;
      default: {
        // Exhaustiveness guard: a new RuntimeEvent variant must be mapped
        // here deliberately, never leaked to a terminal by accident.
        const unhandled: never = event;
        void unhandled;
        break;
      }
    }
  }

  return {
    async start(): Promise<void> {
      const stored = sessions.get(CLI_CHAT_GROUP_FOLDER, CLI_CHAT_BACKEND);
      if (stored) {
        current = stored;
        startedState = 'resumed';
      } else {
        startedState = 'new';
      }
    },

    async runTurn(
      prompt: string,
      options: DeusNativeChatOptions,
      onEvent: ChatDisplayEventSink,
    ): Promise<{ ok: boolean }> {
      if (inFlight) throw new NativeChatBusyError();
      inFlight = true;
      try {
        const runContext: RunContext = {
          prompt,
          cwd: options.cwd,
          groupFolder: CLI_CHAT_GROUP_FOLDER,
          chatJid: CLI_CHAT_JID,
          isControlGroup: false,
          stream: true,
        };

        // Backend-scoped read is load-bearing: sessions are per-backend and
        // the CLI must never pick up a claude/openai row.
        let sessionRef =
          current ?? sessions.get(CLI_CHAT_GROUP_FOLDER, CLI_CHAT_BACKEND);
        if (!sessionRef) {
          // Today this returns defaultSession('', 'deus-native'); the empty
          // id tells the runtime to mint the real LangGraph thread_id — the
          // controller must not mint a competing one.
          sessionRef = await runtime.startOrResume(runContext);
        }

        const result = await runtime.runTurn(runContext, sessionRef, (event) =>
          normalize(event, onEvent),
        );

        if (result.status === 'error') {
          await onEvent({
            kind: 'chat_error',
            message: bound(
              result.error ?? 'the agent reported an error',
              ERROR_TEXT_MAX,
            ),
          });
          // Do NOT clobber a valid stored session on failure; if a session
          // event already persisted this turn, it remains authoritative.
          return { ok: false };
        }

        // Success path (message-orchestrator.ts precedent): the runtime
        // echoes its minted/resumed id in RunResult.sessionRef and does not
        // currently emit a 'session' event, so this persist is required.
        if (result.sessionRef) adoptSession(result.sessionRef);
        return { ok: true };
      } finally {
        inFlight = false;
      }
    },

    status(): NativeChatStatus {
      const streaming = runtime.capabilities().tool_streaming === true;
      return {
        backend: CLI_CHAT_BACKEND,
        sessionId:
          current && current.session_id !== '' ? current.session_id : undefined,
        state: startedState,
        output: streaming ? 'streaming' : 'buffered',
        ...(lastUsage !== undefined ? { usage: lastUsage } : {}),
      };
    },

    async close(): Promise<void> {
      // Reload if needed so close targets the persisted row; never clear the
      // store — db.clearSession soft-orphans rows and would break resume.
      const ref =
        current ?? sessions.get(CLI_CHAT_GROUP_FOLDER, CLI_CHAT_BACKEND);
      if (ref) await runtime.close(ref);
    },
  };
}

// ---------------------------------------------------------------------------
// Discovery record: written by the daemon-side server, read by the client.
// Shared here (not in the server module) so the short-lived client never
// imports daemon-only modules.
// ---------------------------------------------------------------------------
export interface NativeChatDiscoveryRecord {
  version: number;
  pid: number;
  host: string;
  port: number;
  token: string;
}

/** Default discovery-file location (operator-owned, never mounted into containers). */
export function nativeChatDiscoveryPath(): string {
  return path.join(CONFIG_DIR, 'native-chat.json');
}

/**
 * Parse + validate a discovery record. Returns undefined on ANY mismatch
 * (shape, protocol version) — the client fails closed with an actionable
 * message rather than guessing.
 */
export function parseDiscoveryRecord(
  raw: string,
): NativeChatDiscoveryRecord | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (typeof parsed !== 'object' || parsed === null) return undefined;
  const record = parsed as Record<string, unknown>;
  if (record.version !== NATIVE_CHAT_PROTOCOL_VERSION) return undefined;
  if (typeof record.pid !== 'number') return undefined;
  if (typeof record.host !== 'string' || record.host === '') return undefined;
  if (
    typeof record.port !== 'number' ||
    !Number.isInteger(record.port) ||
    record.port <= 0 ||
    record.port > 65535
  ) {
    return undefined;
  }
  if (typeof record.token !== 'string' || record.token.length < 32) {
    return undefined;
  }
  return {
    version: record.version,
    pid: record.pid,
    host: record.host,
    port: record.port,
    token: record.token,
  };
}
