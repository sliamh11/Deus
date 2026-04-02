import type { NewMessage, RegisteredGroup } from './types.js';
import { logger } from './logger.js';

// ── /settings command ─────────────────────────────────────────────────────────

/**
 * Extract a /settings command from a message content, stripping the trigger prefix.
 * Returns the full command string (e.g. '/settings' or '/settings timeout=600')
 * or null if not a settings command.
 */
export function extractSettingsCommand(
  content: string,
  triggerPattern: RegExp,
): string | null {
  let text = content.trim();
  text = text.replace(triggerPattern, '').trim();
  if (text === '/settings' || text.startsWith('/settings ')) return text;
  return null;
}

export interface SettingsCommandResult {
  response: string;
  updatedGroup?: RegisteredGroup; // Present when a setting was changed
}

const SETTINGS_HELP =
  'Available settings:\n' +
  '  session_idle_hours=N  — reset session after N idle hours (0 = never)\n' +
  '  timeout=N             — container timeout in seconds (min 30)\n' +
  '  requires_trigger=true/false  — whether @Name prefix is required';

/**
 * Parse and apply a /settings command.
 * Pure function — returns the response text and an optionally updated group.
 * Caller is responsible for persisting updatedGroup to the DB and state.
 */
export function handleSettingsCommand(
  command: string,
  group: RegisteredGroup,
  globalIdleHours: number,
): SettingsCommandResult {
  const args = command.slice('/settings'.length).trim();

  if (!args) {
    const idleHours = group.containerConfig?.sessionIdleResetHours;
    const timeoutMs = group.containerConfig?.timeout;
    const lines = [
      `Settings — ${group.name}`,
      `  session_idle_hours: ${
        idleHours !== undefined
          ? idleHours === 0
            ? '0 (never reset)'
            : String(idleHours)
          : `${globalIdleHours} (global default)`
      }`,
      `  timeout: ${timeoutMs !== undefined ? `${Math.round(timeoutMs / 1000)}s` : '300s (default)'}`,
      `  requires_trigger: ${group.requiresTrigger !== false}`,
      '',
      SETTINGS_HELP,
    ];
    return { response: lines.join('\n') };
  }

  const eqIdx = args.indexOf('=');
  if (eqIdx === -1) {
    return {
      response: `Invalid syntax. Use: /settings key=value\n\n${SETTINGS_HELP}`,
    };
  }

  const key = args.slice(0, eqIdx).trim().toLowerCase();
  const value = args.slice(eqIdx + 1).trim();

  if (!value) {
    return { response: `Missing value for ${key}.\n\n${SETTINGS_HELP}` };
  }

  const updatedGroup: RegisteredGroup = {
    ...group,
    containerConfig: { ...group.containerConfig },
  };

  switch (key) {
    case 'session_idle_hours': {
      const n = parseInt(value, 10);
      if (isNaN(n) || n < 0) {
        return {
          response:
            'session_idle_hours must be a non-negative integer (0 = never reset).',
        };
      }
      updatedGroup.containerConfig = {
        ...updatedGroup.containerConfig,
        sessionIdleResetHours: n,
      };
      return {
        response: `session_idle_hours set to ${n === 0 ? '0 (session never resets)' : `${n}h`}`,
        updatedGroup,
      };
    }
    case 'timeout': {
      const n = parseInt(value, 10);
      if (isNaN(n) || n < 30) {
        return { response: 'timeout must be at least 30 seconds.' };
      }
      updatedGroup.containerConfig = {
        ...updatedGroup.containerConfig,
        timeout: n * 1000,
      };
      return { response: `timeout set to ${n}s`, updatedGroup };
    }
    case 'requires_trigger': {
      if (value !== 'true' && value !== 'false') {
        return { response: 'requires_trigger must be true or false.' };
      }
      updatedGroup.requiresTrigger = value === 'true';
      return { response: `requires_trigger set to ${value}`, updatedGroup };
    }
    default:
      return { response: `Unknown setting: ${key}\n\n${SETTINGS_HELP}` };
  }
}

// ── Host slash command registry ───────────────────────────────────────────────

/**
 * A host-side slash command handler.
 *
 * `extract` — returns the normalised command string if the message matches,
 *             null otherwise. Strips the trigger prefix before checking.
 * `handle`  — pure: returns response text and an optional updated group.
 *             Caller persists updatedGroup to DB + state.
 */
export interface HostCommandHandler {
  extract(content: string, triggerPattern: RegExp): string | null;
  handle(
    cmd: string,
    group: RegisteredGroup,
    globalIdleHours: number,
  ): { response: string; updatedGroup?: RegisteredGroup };
}

/** All host-side slash commands. Add new entries here to register a command. */
export const HOST_COMMAND_HANDLERS: HostCommandHandler[] = [
  {
    extract: extractSettingsCommand,
    handle: handleSettingsCommand,
  },
];

export interface HostDispatchResult {
  /** true if any handler matched (regardless of auth outcome) */
  matched: boolean;
  response?: string;
  updatedGroup?: RegisteredGroup;
  /** Timestamp of the matching message — advance the agent cursor to this */
  timestamp?: string;
}

/**
 * Scan `messages` for any registered host slash command and dispatch it.
 * Returns `matched: false` when no message matches any handler.
 * Auth check (admin-only) is applied uniformly before calling `handle`.
 */
export function dispatchHostCommand(
  messages: NewMessage[],
  triggerPattern: RegExp,
  group: RegisteredGroup,
  globalIdleHours: number,
  isMainGroup: boolean,
): HostDispatchResult {
  for (const handler of HOST_COMMAND_HANDLERS) {
    const msg = messages.find(
      (m) => handler.extract(m.content, triggerPattern) !== null,
    );
    if (!msg) continue;

    if (!isSessionCommandAllowed(isMainGroup, msg.is_from_me === true)) {
      return {
        matched: true,
        response: 'This command requires admin access.',
        timestamp: msg.timestamp,
      };
    }

    const cmd = handler.extract(msg.content, triggerPattern)!;
    const result = handler.handle(cmd, group, globalIdleHours);
    return { matched: true, ...result, timestamp: msg.timestamp };
  }
  return { matched: false };
}

// ── /compact command ───────────────────────────────────────────────────────────

/**
 * Extract a session slash command from a message, stripping the trigger prefix if present.
 * Returns the slash command (e.g., '/compact') or null if not a session command.
 */
export function extractSessionCommand(
  content: string,
  triggerPattern: RegExp,
): string | null {
  let text = content.trim();
  text = text.replace(triggerPattern, '').trim();
  if (text === '/compact') return '/compact';
  return null;
}

/**
 * Check if a session command sender is authorized.
 * Allowed: main group (any sender), or trusted/admin sender (is_from_me) in any group.
 */
export function isSessionCommandAllowed(
  isMainGroup: boolean,
  isFromMe: boolean,
): boolean {
  return isMainGroup || isFromMe;
}

/** Minimal agent result interface — matches the subset of ContainerOutput used here. */
export interface AgentResult {
  status: 'success' | 'error';
  result?: string | object | null;
}

/** Dependencies injected by the orchestrator. */
export interface SessionCommandDeps {
  sendMessage: (text: string) => Promise<void>;
  setTyping: (typing: boolean) => Promise<void>;
  runAgent: (
    prompt: string,
    onOutput: (result: AgentResult) => Promise<void>,
  ) => Promise<'success' | 'error'>;
  closeStdin: () => void;
  advanceCursor: (timestamp: string) => void;
  formatMessages: (msgs: NewMessage[], timezone: string) => string;
  /** Whether the denied sender would normally be allowed to interact (for denial messages). */
  canSenderInteract: (msg: NewMessage) => boolean;
}

function resultToText(result: string | object | null | undefined): string {
  if (!result) return '';
  const raw = typeof result === 'string' ? result : JSON.stringify(result);
  return raw.replace(/<internal>[\s\S]*?<\/internal>/g, '').trim();
}

/**
 * Handle session command interception in processGroupMessages.
 * Scans messages for a session command, handles auth + execution.
 * Returns { handled: true, success } if a command was found; { handled: false } otherwise.
 * success=false means the caller should retry (cursor was not advanced).
 */
export async function handleSessionCommand(opts: {
  missedMessages: NewMessage[];
  isMainGroup: boolean;
  groupName: string;
  triggerPattern: RegExp;
  timezone: string;
  deps: SessionCommandDeps;
}): Promise<{ handled: false } | { handled: true; success: boolean }> {
  const {
    missedMessages,
    isMainGroup,
    groupName,
    triggerPattern,
    timezone,
    deps,
  } = opts;

  const cmdMsg = missedMessages.find(
    (m) => extractSessionCommand(m.content, triggerPattern) !== null,
  );
  const command = cmdMsg
    ? extractSessionCommand(cmdMsg.content, triggerPattern)
    : null;

  if (!command || !cmdMsg) return { handled: false };

  if (!isSessionCommandAllowed(isMainGroup, cmdMsg.is_from_me === true)) {
    // DENIED: send denial if the sender would normally be allowed to interact,
    // then silently consume the command by advancing the cursor past it.
    // Trade-off: other messages in the same batch are also consumed (cursor is
    // a high-water mark). Acceptable for this narrow edge case.
    if (deps.canSenderInteract(cmdMsg)) {
      await deps.sendMessage('Session commands require admin access.');
    }
    deps.advanceCursor(cmdMsg.timestamp);
    return { handled: true, success: true };
  }

  // AUTHORIZED: process pre-compact messages first, then run the command
  logger.info({ group: groupName, command }, 'Session command');

  const cmdIndex = missedMessages.indexOf(cmdMsg);
  const preCompactMsgs = missedMessages.slice(0, cmdIndex);

  // Send pre-compact messages to the agent so they're in the session context.
  if (preCompactMsgs.length > 0) {
    const prePrompt = deps.formatMessages(preCompactMsgs, timezone);
    let hadPreError = false;
    let preOutputSent = false;

    const preResult = await deps.runAgent(prePrompt, async (result) => {
      if (result.status === 'error') hadPreError = true;
      const text = resultToText(result.result);
      if (text) {
        await deps.sendMessage(text);
        preOutputSent = true;
      }
      // Close stdin on session-update marker — emitted after query completes,
      // so all results (including multi-result runs) are already written.
      if (result.status === 'success' && result.result === null) {
        deps.closeStdin();
      }
    });

    if (preResult === 'error' || hadPreError) {
      logger.warn(
        { group: groupName },
        'Pre-compact processing failed, aborting session command',
      );
      await deps.sendMessage(
        `Failed to process messages before ${command}. Try again.`,
      );
      if (preOutputSent) {
        // Output was already sent — don't retry or it will duplicate.
        // Advance cursor past pre-compact messages, leave command pending.
        deps.advanceCursor(preCompactMsgs[preCompactMsgs.length - 1].timestamp);
        return { handled: true, success: true };
      }
      return { handled: true, success: false };
    }
  }

  // Forward the literal slash command as the prompt (no XML formatting)
  await deps.setTyping(true);

  let hadCmdError = false;
  const cmdOutput = await deps.runAgent(command, async (result) => {
    if (result.status === 'error') hadCmdError = true;
    const text = resultToText(result.result);
    if (text) await deps.sendMessage(text);
  });

  // Advance cursor to the command — messages AFTER it remain pending for next poll.
  deps.advanceCursor(cmdMsg.timestamp);
  await deps.setTyping(false);

  if (cmdOutput === 'error' || hadCmdError) {
    await deps.sendMessage(`${command} failed. The session is unchanged.`);
  }

  return { handled: true, success: true };
}
