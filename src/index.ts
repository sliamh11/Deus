import { pathToFileURL } from 'url';

import {
  ASSISTANT_NAME,
  CREDENTIAL_PROXY_PORT,
  MAX_MESSAGE_LENGTH,
} from './config.js';
import { startCredentialProxy } from './credential-proxy.js';
import './channels/index.js';
import {
  getChannelFactory,
  getRegisteredChannelNames,
} from './channels/registry.js';
import {
  cleanupOrphans,
  ensureContainerRuntimeRunning,
  PROXY_BIND_HOST,
} from './container-runtime.js';
import {
  initDatabase,
  setSession as persistSession,
  storeChatMetadata,
  storeMessage,
} from './db.js';
import { GroupQueue } from './group-queue.js';
import { startIpcWatcher } from './ipc.js';
import { loadSkillIpcHandlers } from './skills/index.js';
import { createMessageOrchestrator } from './message-orchestrator.js';
import { findChannel, formatOutbound } from './router.js';
import {
  restoreRemoteControl,
  startRemoteControl,
  stopRemoteControl,
} from './remote-control.js';
import { RouterState, getAvailableGroups } from './router-state.js';
import {
  isSenderAllowed,
  loadSenderAllowlist,
  shouldDropMessage,
} from './sender-allowlist.js';
import { runStartupChecks, printStartupReport } from './startup-gate.js';
import { startSchedulerLoop } from './task-scheduler.js';
import { getAllTasks } from './db.js';
import { writeGroupsSnapshot, writeTasksSnapshot } from './container-runner.js';
import { Channel, NewMessage, NewReaction } from './types.js';
import { logReactionSignal } from './evolution-client.js';
import { resolveGroupFolderPath } from './group-folder.js';
import { processImage } from './image.js';
import { logger } from './logger.js';
import { initRuntimeRegistry } from './agent-runtimes/registry.js';
import { createClaudeRuntime } from './agent-runtimes/claude-backend.js';
import { createOpenAIRuntime } from './agent-runtimes/openai-backend.js';

export { getAvailableGroups } from './router-state.js';

async function main(): Promise<void> {
  ensureContainerRuntimeRunning();
  cleanupOrphans();

  // Validate prerequisites before heavy initialization.
  const startupReport = runStartupChecks();
  printStartupReport(startupReport);
  if (startupReport.fatals.length > 0) {
    process.exit(1);
  }

  initDatabase();
  logger.info('Database initialized');

  const state = new RouterState();
  state.load();
  restoreRemoteControl();

  // Start credential proxy (containers route API calls through this)
  const proxyServer = await startCredentialProxy(
    CREDENTIAL_PROXY_PORT,
    PROXY_BIND_HOST,
  );

  const channels: Channel[] = [];
  const queue = new GroupQueue();

  // Initialize backend registry — all container-based backends share the same deps
  const registry = initRuntimeRegistry();
  const backendDeps = {
    resolveGroup: (groupFolder: string) =>
      Object.values(state.registeredGroups).find(
        (g) => g.folder === groupFolder,
      ),
    assistantName: ASSISTANT_NAME,
    registerProcess: (
      chatJid: string,
      proc: import('child_process').ChildProcess,
      containerName: string,
      groupFolder: string,
    ) => queue.registerProcess(chatJid, proc, containerName, groupFolder),
  };
  registry.register(createClaudeRuntime(backendDeps));
  registry.register(createOpenAIRuntime(backendDeps));
  logger.info({ backends: registry.list() }, 'Backend registry initialized');

  // Graceful shutdown handlers
  const shutdown = async (signal: string) => {
    logger.info({ signal }, 'Shutdown signal received');
    proxyServer.close();
    await queue.shutdown(10000);
    for (const ch of channels) await ch.disconnect();
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));

  process.on('exit', (code) => {
    logger.info({ code }, 'Process exiting');
  });
  process.on('uncaughtException', (err) => {
    logger.error({ err }, 'Uncaught exception');
    process.exit(1);
  });
  process.on('unhandledRejection', (reason) => {
    logger.error({ reason }, 'Unhandled rejection');
  });

  // Handle /remote-control and /remote-control-end commands
  async function handleRemoteControl(
    command: string,
    chatJid: string,
    msg: NewMessage,
  ): Promise<void> {
    const group = state.registeredGroups[chatJid];
    if (!group?.isControlGroup) {
      logger.warn(
        { chatJid, sender: msg.sender },
        'Remote control rejected: not main group',
      );
      return;
    }

    const channel = findChannel(channels, chatJid);
    if (!channel) return;

    if (command === '/remote-control') {
      const result = await startRemoteControl(
        msg.sender,
        chatJid,
        process.cwd(),
      );
      if (result.ok) {
        // Send URL via DM to the sender — never expose it in a group chat
        const dmJid = msg.sender !== chatJid ? msg.sender : chatJid;
        await channel.sendMessage(dmJid, result.url);
        if (dmJid !== chatJid) {
          await channel.sendMessage(
            chatJid,
            'Remote Control URL sent to your DMs.',
          );
        }
      } else {
        await channel.sendMessage(
          chatJid,
          `Remote Control failed: ${result.error}`,
        );
      }
    } else {
      const result = stopRemoteControl();
      if (result.ok) {
        await channel.sendMessage(chatJid, 'Remote Control session ended.');
      } else {
        await channel.sendMessage(chatJid, result.error);
      }
    }
  }

  // Channel callbacks (shared by all channels)
  const channelOpts = {
    onMessage: async (chatJid: string, msg: NewMessage) => {
      // Remote control commands — intercept before storage
      const trimmed = msg.content.trim();
      if (trimmed === '/remote-control' || trimmed === '/remote-control-end') {
        handleRemoteControl(trimmed, chatJid, msg).catch((err) =>
          logger.error({ err, chatJid }, 'Remote control command error'),
        );
        return;
      }

      // Sender allowlist drop mode: discard messages from denied senders before storing
      if (
        !msg.is_from_me &&
        !msg.is_bot_message &&
        state.registeredGroups[chatJid]
      ) {
        const cfg = loadSenderAllowlist();
        if (
          shouldDropMessage(chatJid, cfg) &&
          !isSenderAllowed(chatJid, msg.sender, cfg)
        ) {
          if (cfg.logDenied) {
            logger.debug(
              { chatJid, sender: msg.sender },
              'sender-allowlist: dropping message (drop mode)',
            );
          }
          return;
        }
      }

      // Truncate oversized messages to prevent abuse / memory exhaustion
      if (msg.content.length > MAX_MESSAGE_LENGTH) {
        logger.warn(
          {
            chatJid,
            originalLength: msg.content.length,
            maxLength: MAX_MESSAGE_LENGTH,
          },
          'Message truncated',
        );
        msg.content =
          msg.content.slice(0, MAX_MESSAGE_LENGTH) +
          '\n\n[Message truncated — exceeded maximum length]';
      }

      if (msg.imageData) {
        const group = state.registeredGroups[chatJid];
        if (group) {
          const groupDir = resolveGroupFolderPath(group.folder);
          try {
            const result = await processImage(
              Buffer.from(msg.imageData, 'base64'),
              groupDir,
              msg.content,
            );
            if (result) {
              msg.content = result.content;
              logger.info(
                { chatJid, path: result.relativePath },
                'Processed image attachment',
              );
            }
          } catch (err) {
            logger.warn({ err, chatJid }, 'Image processing failed');
          }
        }
        delete msg.imageData;
        if (!msg.content) return;
      }

      storeMessage(msg);
    },
    onReaction: (chatJid: string, reaction: NewReaction) => {
      const group = state.registeredGroups[chatJid];
      if (!group) return; // Reaction in an unregistered chat — ignore.
      logReactionSignal({
        emoji: reaction.emoji,
        groupFolder: group.folder,
        sessionId: state.getSession(group.folder)?.session_id,
        reactedToMessageId: reaction.reacted_to_message_id,
      });
    },
    onChatMetadata: (
      chatJid: string,
      timestamp: string,
      name?: string,
      channel?: string,
      isGroup?: boolean,
    ) => storeChatMetadata(chatJid, timestamp, name, channel, isGroup),
    registeredGroups: () => state.registeredGroups,
  };

  // Create and connect all registered channels.
  // Each channel self-registers via the barrel import above.
  // Factories return null when credentials are missing, so unconfigured channels are skipped.
  for (const channelName of getRegisteredChannelNames()) {
    const factory = getChannelFactory(channelName)!;
    const channel = factory(channelOpts);
    if (!channel) {
      logger.warn(
        { channel: channelName },
        'Channel installed but credentials missing — skipping. Check .env or re-run the channel skill.',
      );
      continue;
    }
    channels.push(channel);
    try {
      await channel.connect();
    } catch (err: unknown) {
      logger.error(
        { err, channel: channelName },
        'channel connect failed during startup',
      );
      throw err;
    }
  }
  if (channels.length === 0) {
    logger.warn(
      'No messaging channels connected — running without channels. ' +
        'Run /add-whatsapp or /add-telegram in Claude Code to add one.',
    );
  }

  const orchestrator = createMessageOrchestrator({
    state,
    queue,
    registry,
    channels,
  });

  // Start subsystems (independently of connection handler)
  startSchedulerLoop({
    registeredGroups: () => state.registeredGroups,
    getSessions: () => state.sessions,
    getSession: (groupFolder, backend) =>
      state.getSession(groupFolder, backend),
    setSession: (groupFolder, sessionRef) => {
      state.setSession(groupFolder, sessionRef);
      persistSession(groupFolder, sessionRef);
    },
    registry,
    queue,
    sendMessage: async (jid, rawText) => {
      const channel = findChannel(channels, jid);
      if (!channel) {
        logger.warn({ jid }, 'No channel owns JID, cannot send message');
        return;
      }
      const text = formatOutbound(rawText);
      if (text) await channel.sendMessage(jid, text);
    },
  });

  // Load skill IPC handlers before starting the IPC watcher
  await loadSkillIpcHandlers();

  startIpcWatcher({
    sendMessage: (jid, text) => {
      const channel = findChannel(channels, jid);
      if (!channel) throw new Error(`No channel for JID: ${jid}`);
      return channel.sendMessage(jid, text);
    },
    registeredGroups: () => state.registeredGroups,
    registerGroup: (jid, group) => state.registerGroup(jid, group),
    syncGroups: async (force: boolean) => {
      await Promise.all(
        channels
          .filter((ch) => ch.syncGroups)
          .map((ch) => ch.syncGroups!(force)),
      );
    },
    getAvailableGroups: () => getAvailableGroups(state.registeredGroups),
    writeGroupsSnapshot: (gf, im, ag, rj) =>
      writeGroupsSnapshot(gf, im, ag, rj),
    onTasksChanged: () => {
      const tasks = getAllTasks();
      const taskRows = tasks.map((t) => ({
        id: t.id,
        groupFolder: t.group_folder,
        prompt: t.prompt,
        schedule_type: t.schedule_type,
        schedule_value: t.schedule_value,
        status: t.status,
        next_run: t.next_run,
      }));
      for (const group of Object.values(state.registeredGroups)) {
        writeTasksSnapshot(
          group.folder,
          group.isControlGroup === true,
          taskRows,
        );
      }
    },
  });

  queue.setProcessMessagesFn(orchestrator.processGroupMessages);
  orchestrator.recoverPendingMessages();
  orchestrator.startMessageLoop().catch((err) => {
    logger.fatal({ err }, 'Message loop crashed unexpectedly');
    process.exit(1);
  });
}

// Guard: only run when executed directly, not when imported by tests.
// pathToFileURL handles Windows drive letters correctly (file:///C:/...)
const isDirectRun =
  process.argv[1] &&
  new URL(import.meta.url).href === pathToFileURL(process.argv[1]).href;

if (isDirectRun) {
  main().catch((err) => {
    logger.error({ err }, 'Failed to start Deus');
    process.exit(1);
  });
}
