/**
 * Message orchestration — the core processing loop for Deus.
 *
 * Owns: polling for new messages, trigger detection, cursor management,
 * session command interception, agent invocation, and startup recovery.
 *
 * Depends on RouterState for mutable state and GroupQueue for container
 * lifecycle. All other dependencies are imported directly from stable modules.
 */

import {
  ASSISTANT_NAME,
  IDLE_TIMEOUT,
  POLL_INTERVAL,
  SESSION_IDLE_RESET_HOURS,
  TIMEZONE,
  TRIGGER_PATTERN,
} from './config.js';
import {
  ContainerOutput,
  runContainerAgent,
  writeGroupsSnapshot,
  writeTasksSnapshot,
} from './container-runner.js';
import {
  clearSession,
  getAllTasks,
  getMessagesSince,
  getNewMessages,
  getSessionLastUsedAt,
  setRegisteredGroup,
  setSession,
} from './db.js';
import { GroupQueue } from './group-queue.js';
import { parseImageReferences } from './image.js';
import { logger } from './logger.js';
import { findChannel, formatMessages } from './router.js';
import { RouterState, getAvailableGroups } from './router-state.js';
import { isTriggerAllowed, loadSenderAllowlist } from './sender-allowlist.js';
import {
  extractSessionCommand,
  extractSettingsCommand,
  handleSessionCommand,
  handleSettingsCommand,
  isSessionCommandAllowed,
} from './session-commands.js';
import { Channel, NewMessage, RegisteredGroup } from './types.js';

export interface OrchestratorDeps {
  state: RouterState;
  queue: GroupQueue;
  /** Mutable array — channels are pushed into it during startup before the
   *  orchestrator starts processing, so this reference stays valid. */
  channels: Channel[];
}

export function createMessageOrchestrator(deps: OrchestratorDeps) {
  const { state, queue, channels } = deps;
  let messageLoopRunning = false;

  async function runAgent(
    group: RegisteredGroup,
    prompt: string,
    chatJid: string,
    imageAttachments: Array<{ relativePath: string; mediaType: string }>,
    onOutput?: (output: ContainerOutput) => Promise<void>,
  ): Promise<'success' | 'error'> {
    const isControlGroup = group.isControlGroup === true;
    let sessionId = state.getSession(group.folder);

    // Idle session reset: per-group setting takes precedence over global default.
    const effectiveIdleHours =
      group.containerConfig?.sessionIdleResetHours !== undefined
        ? group.containerConfig.sessionIdleResetHours
        : SESSION_IDLE_RESET_HOURS;

    if (sessionId && effectiveIdleHours > 0) {
      const lastUsed = getSessionLastUsedAt(group.folder);
      const idleMs = lastUsed
        ? Date.now() - new Date(lastUsed).getTime()
        : Infinity;
      if (idleMs > effectiveIdleHours * 3_600_000) {
        logger.info(
          { group: group.name, idleHours: (idleMs / 3_600_000).toFixed(1) },
          'Session idle too long — starting fresh',
        );
        clearSession(group.folder);
        state.clearSession(group.folder);
        sessionId = undefined;
      }
    }

    const tasks = getAllTasks();
    writeTasksSnapshot(
      group.folder,
      isControlGroup,
      tasks.map((t) => ({
        id: t.id,
        groupFolder: t.group_folder,
        prompt: t.prompt,
        schedule_type: t.schedule_type,
        schedule_value: t.schedule_value,
        status: t.status,
        next_run: t.next_run,
      })),
    );

    const availableGroups = getAvailableGroups(state.registeredGroups);
    writeGroupsSnapshot(
      group.folder,
      isControlGroup,
      availableGroups,
      new Set(Object.keys(state.registeredGroups)),
    );

    const wrappedOnOutput = onOutput
      ? async (output: ContainerOutput) => {
          if (output.newSessionId && output.status !== 'error') {
            state.setSession(group.folder, output.newSessionId);
            setSession(group.folder, output.newSessionId);
          }
          await onOutput(output);
        }
      : undefined;

    try {
      const output = await runContainerAgent(
        group,
        {
          prompt,
          sessionId,
          groupFolder: group.folder,
          chatJid,
          isControlGroup,
          assistantName: ASSISTANT_NAME,
          ...(imageAttachments.length > 0 && { imageAttachments }),
        },
        (proc, containerName) =>
          queue.registerProcess(chatJid, proc, containerName, group.folder),
        wrappedOnOutput,
      );

      if (output.newSessionId && output.status !== 'error') {
        state.setSession(group.folder, output.newSessionId);
        setSession(group.folder, output.newSessionId);
      }

      if (output.status === 'error') {
        logger.error(
          { group: group.name, error: output.error },
          'Container agent error',
        );
        return 'error';
      }

      return 'success';
    } catch (err) {
      logger.error({ group: group.name, err }, 'Agent error');
      return 'error';
    }
  }

  /**
   * Process all pending messages for a group.
   * Called by GroupQueue when it's this group's turn.
   */
  async function processGroupMessages(chatJid: string): Promise<boolean> {
    const group = state.registeredGroups[chatJid];
    if (!group) return true;

    const channel = findChannel(channels, chatJid);
    if (!channel) {
      logger.warn({ chatJid }, 'No channel owns JID, skipping messages');
      return true;
    }

    const isMainGroup = group.isControlGroup === true;
    const sinceTimestamp = state.getLastAgentTimestamp(chatJid);
    const missedMessages = getMessagesSince(
      chatJid,
      sinceTimestamp,
      ASSISTANT_NAME,
    );

    if (missedMessages.length === 0) return true;

    // --- /settings command (host-side, no container spawn) ---
    const settingsMsg = missedMessages.find(
      (m) => extractSettingsCommand(m.content, TRIGGER_PATTERN) !== null,
    );
    if (settingsMsg) {
      if (
        !isSessionCommandAllowed(isMainGroup, settingsMsg.is_from_me === true)
      ) {
        await channel.sendMessage(
          chatJid,
          'Settings commands require admin access.',
        );
      } else {
        const cmd = extractSettingsCommand(
          settingsMsg.content,
          TRIGGER_PATTERN,
        )!;
        const result = handleSettingsCommand(
          cmd,
          group,
          SESSION_IDLE_RESET_HOURS,
        );
        if (result.updatedGroup) {
          setRegisteredGroup(chatJid, result.updatedGroup);
          state.registeredGroups[chatJid] = result.updatedGroup;
          logger.info({ group: group.name, cmd }, 'Group setting updated');
        }
        await channel.sendMessage(chatJid, result.response);
      }
      state.setLastAgentTimestamp(chatJid, settingsMsg.timestamp);
      state.save();
      return true;
    }
    // --- End /settings ---

    // --- Session command interception (before trigger check) ---
    const cmdResult = await handleSessionCommand({
      missedMessages,
      isMainGroup,
      groupName: group.name,
      triggerPattern: TRIGGER_PATTERN,
      timezone: TIMEZONE,
      deps: {
        sendMessage: (text) => channel.sendMessage(chatJid, text),
        setTyping: (typing) =>
          channel.setTyping?.(chatJid, typing) ?? Promise.resolve(),
        runAgent: (prompt, onOutput) =>
          runAgent(group, prompt, chatJid, [], onOutput),
        closeStdin: () => queue.closeStdin(chatJid),
        advanceCursor: (ts) => {
          state.setLastAgentTimestamp(chatJid, ts);
          state.save();
        },
        formatMessages,
        canSenderInteract: (msg) => {
          const hasTrigger = TRIGGER_PATTERN.test(msg.content.trim());
          const reqTrigger = !isMainGroup && group.requiresTrigger !== false;
          return (
            isMainGroup ||
            !reqTrigger ||
            (hasTrigger &&
              (msg.is_from_me ||
                isTriggerAllowed(chatJid, msg.sender, loadSenderAllowlist())))
          );
        },
      },
    });
    if (cmdResult.handled) return cmdResult.success;
    // --- End session command interception ---

    // For non-main groups, check if trigger is required and present
    if (!isMainGroup && group.requiresTrigger !== false) {
      const allowlistCfg = loadSenderAllowlist();
      const hasTrigger = missedMessages.some(
        (m) =>
          TRIGGER_PATTERN.test(m.content.trim()) &&
          (m.is_from_me || isTriggerAllowed(chatJid, m.sender, allowlistCfg)),
      );
      if (!hasTrigger) return true;
    }

    const prompt = formatMessages(missedMessages, TIMEZONE);
    const imageAttachments = parseImageReferences(missedMessages);

    // Advance cursor so the piping path in startMessageLoop won't re-fetch
    // these messages. Save the old cursor so we can roll back on error.
    const previousCursor = state.getLastAgentTimestamp(chatJid);
    state.setLastAgentTimestamp(
      chatJid,
      missedMessages[missedMessages.length - 1].timestamp,
    );
    state.save();

    logger.info(
      { group: group.name, messageCount: missedMessages.length },
      'Processing messages',
    );

    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    const resetIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        logger.debug(
          { group: group.name },
          'Idle timeout, closing container stdin',
        );
        queue.closeStdin(chatJid);
      }, IDLE_TIMEOUT);
    };

    await channel.setTyping?.(chatJid, true);
    let hadError = false;
    let outputSentToUser = false;

    const output = await runAgent(
      group,
      prompt,
      chatJid,
      imageAttachments,
      async (result) => {
        if (result.result) {
          const raw =
            typeof result.result === 'string'
              ? result.result
              : JSON.stringify(result.result);
          // Strip <internal>...</internal> blocks — agent uses these for internal reasoning
          const text = raw
            .replace(/<internal>[\s\S]*?<\/internal>/g, '')
            .trim();
          logger.info(
            { group: group.name },
            `Agent output: ${raw.length} chars`,
          );
          if (text) {
            await channel.sendMessage(chatJid, text);
            outputSentToUser = true;
          }
          // Only reset idle timer on actual results, not session-update markers
          resetIdleTimer();
        }

        if (result.status === 'success') {
          queue.notifyIdle(chatJid);
        }

        if (result.status === 'error') {
          hadError = true;
        }
      },
    );

    await channel.setTyping?.(chatJid, false);
    if (idleTimer) clearTimeout(idleTimer);

    if (output === 'error' || hadError) {
      // If we already sent output to the user, don't roll back the cursor —
      // the user got their response and re-processing would send duplicates.
      if (outputSentToUser) {
        logger.warn(
          { group: group.name },
          'Agent error after output was sent, skipping cursor rollback to prevent duplicates',
        );
        return true;
      }
      // Roll back cursor so retries can re-process these messages
      state.setLastAgentTimestamp(chatJid, previousCursor);
      state.save();
      logger.warn(
        { group: group.name },
        'Agent error, rolled back message cursor for retry',
      );
      return false;
    }

    return true;
  }

  /** Poll for new messages across all registered groups and route them. */
  async function startMessageLoop(): Promise<void> {
    if (messageLoopRunning) {
      logger.debug('Message loop already running, skipping duplicate start');
      return;
    }
    messageLoopRunning = true;

    logger.info(`Deus running (trigger: @${ASSISTANT_NAME})`);

    while (true) {
      try {
        const jids = Object.keys(state.registeredGroups);
        const { messages, newTimestamp } = getNewMessages(
          jids,
          state.lastTimestamp,
          ASSISTANT_NAME,
        );

        if (messages.length > 0) {
          logger.info({ count: messages.length }, 'New messages');

          // Advance the "seen" cursor for all messages immediately
          state.lastTimestamp = newTimestamp;
          state.save();

          // Deduplicate by group
          const messagesByGroup = new Map<string, NewMessage[]>();
          for (const msg of messages) {
            const existing = messagesByGroup.get(msg.chat_jid);
            if (existing) {
              existing.push(msg);
            } else {
              messagesByGroup.set(msg.chat_jid, [msg]);
            }
          }

          for (const [chatJid, groupMessages] of messagesByGroup) {
            const group = state.registeredGroups[chatJid];
            if (!group) continue;

            const channel = findChannel(channels, chatJid);
            if (!channel) {
              logger.warn(
                { chatJid },
                'No channel owns JID, skipping messages',
              );
              continue;
            }

            const isMainGroup = group.isControlGroup === true;

            // --- Session command interception (message loop) ---
            // Scan ALL messages in the batch for a session command.
            const loopCmdMsg = groupMessages.find(
              (m) => extractSessionCommand(m.content, TRIGGER_PATTERN) !== null,
            );

            if (loopCmdMsg) {
              // Only close active container if the sender is authorized — otherwise an
              // untrusted user could kill in-flight work by sending /compact (DoS).
              if (
                isSessionCommandAllowed(
                  isMainGroup,
                  loopCmdMsg.is_from_me === true,
                )
              ) {
                queue.closeStdin(chatJid);
              }
              // Enqueue so processGroupMessages handles auth + cursor advancement.
              // Don't pipe via IPC — slash commands need a fresh container with
              // string prompt (not MessageStream) for SDK recognition.
              queue.enqueueMessageCheck(chatJid);
              continue;
            }
            // --- End session command interception ---

            const needsTrigger =
              !isMainGroup && group.requiresTrigger !== false;

            // For non-main groups, only act on trigger messages.
            // Non-trigger messages accumulate in DB and get pulled as
            // context when a trigger eventually arrives.
            if (needsTrigger) {
              const allowlistCfg = loadSenderAllowlist();
              const hasTrigger = groupMessages.some(
                (m) =>
                  TRIGGER_PATTERN.test(m.content.trim()) &&
                  (m.is_from_me ||
                    isTriggerAllowed(chatJid, m.sender, allowlistCfg)),
              );
              if (!hasTrigger) continue;
            }

            // Pull all messages since lastAgentTimestamp so non-trigger
            // context that accumulated between triggers is included.
            const allPending = getMessagesSince(
              chatJid,
              state.getLastAgentTimestamp(chatJid),
              ASSISTANT_NAME,
            );
            const messagesToSend =
              allPending.length > 0 ? allPending : groupMessages;
            const formatted = formatMessages(messagesToSend, TIMEZONE);

            if (queue.sendMessage(chatJid, formatted)) {
              logger.debug(
                { chatJid, count: messagesToSend.length },
                'Piped messages to active container',
              );
              state.setLastAgentTimestamp(
                chatJid,
                messagesToSend[messagesToSend.length - 1].timestamp,
              );
              state.save();
              // Show typing indicator while the container processes the piped message
              channel
                .setTyping?.(chatJid, true)
                ?.catch((err) =>
                  logger.warn(
                    { chatJid, err },
                    'Failed to set typing indicator',
                  ),
                );
            } else {
              // No active container — enqueue for a new one
              queue.enqueueMessageCheck(chatJid);
            }
          }
        }
      } catch (err) {
        logger.error({ err }, 'Error in message loop');
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
    }
  }

  /**
   * Startup recovery: check for unprocessed messages in registered groups.
   * Handles crash between advancing lastTimestamp and processing messages.
   */
  function recoverPendingMessages(): void {
    for (const [chatJid, group] of Object.entries(state.registeredGroups)) {
      const sinceTimestamp = state.getLastAgentTimestamp(chatJid);
      const pending = getMessagesSince(chatJid, sinceTimestamp, ASSISTANT_NAME);
      if (pending.length > 0) {
        logger.info(
          { group: group.name, pendingCount: pending.length },
          'Recovery: found unprocessed messages',
        );
        queue.enqueueMessageCheck(chatJid);
      }
    }
  }

  return {
    processGroupMessages,
    startMessageLoop,
    recoverPendingMessages,
    runAgent,
  };
}
