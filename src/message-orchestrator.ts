/**
 * Message orchestration — the core processing loop for Deus.
 *
 * Owns: polling for new messages, trigger detection, cursor management,
 * session command interception, agent invocation, and startup recovery.
 *
 * Depends on RouterState for mutable state and GroupQueue for container
 * lifecycle. All other dependencies are imported directly from stable modules.
 */

import { autoCompressSession } from './auto-compress.js';
import {
  ASSISTANT_NAME,
  CONTEXT_NOTIFY,
  IDLE_TIMEOUT,
  INGRESS_WEBHOOK_RUN_COST,
  INJECTION_SCANNER_CONFIG,
  POLL_INTERVAL,
  SESSION_IDLE_RESET_HOURS,
  TIMEZONE,
  TRIGGER_PATTERN,
} from './config.js';
import {
  scanForInjection,
  type ScanResult,
} from './guardrails/injection-scanner.js';
import {
  defaultSession,
  type RunContext,
  type RuntimeEventSink,
} from './agent-runtimes/types.js';
import type { RuntimeRegistry } from './agent-runtimes/registry.js';
import {
  type ContainerOutput,
  writeGroupsSnapshot,
  writeTasksSnapshot,
} from './container-runner.js';
import {
  clearSession,
  getAllTasks,
  getLastCompactedAt,
  getMessagesSince,
  getNewMessages,
  getSessionLastUsedAt,
  setLastCompactedAt,
  setRegisteredGroup,
  setSession,
} from './db.js';
import { GroupQueue } from './group-queue.js';
import { parseImageReferences } from './image.js';
import { logger } from './logger.js';
import {
  MultiAgentOrchestrator,
  parseTaskBlock,
  formatMultiAgentResult,
  MALFORMED_TASK_BLOCK,
  type OrchestratorResult,
} from './multi-agent/index.js';
import { findChannel, formatMessages } from './router.js';
import { RouterState, getAvailableGroups } from './router-state.js';
import { isTriggerAllowed, loadSenderAllowlist } from './sender-allowlist.js';
import {
  dispatchHostCommand,
  extractSessionCommand,
  handleSessionCommand,
  isSessionCommandAllowed,
} from './session-commands.js';
import { Channel, NewMessage, RegisteredGroup } from './types.js';
import type { IngressCaps } from './ingress/caps.js';

export interface OrchestratorDeps {
  state: RouterState;
  queue: GroupQueue;
  registry: RuntimeRegistry;
  /** Mutable array — channels are pushed into it during startup before the
   *  orchestrator starts processing, so this reference stays valid. */
  channels: Channel[];
  /** LIA-315 Phase 4: R5 DoS/spend caps + R6 audit for webhook-originated
   *  (publicIngress) runs. Undefined when INGRESS_WEBHOOK_ENABLED is off; a
   *  publicIngress group then fails CLOSED (refuses to run). */
  ingressCaps?: IngressCaps;
}

export function createMessageOrchestrator(deps: OrchestratorDeps) {
  const { state, queue, registry, channels, ingressCaps } = deps;

  queue.setOnTerminalFailure((groupJid) => {
    const channel = findChannel(channels, groupJid);
    if (!channel) {
      logger.warn(
        { groupJid },
        'No channel owns JID, cannot send terminal-failure notice',
      );
      return;
    }
    channel
      .sendMessage(
        groupJid,
        'I hit an error processing that — please try again.',
      )
      .catch((err) =>
        logger.warn(
          { groupJid, err },
          'Failed to send terminal-failure notice',
        ),
      );
  });

  let messageLoopRunning = false;

  async function runAgent(
    group: RegisteredGroup,
    prompt: string,
    chatJid: string,
    imageAttachments: Array<{ relativePath: string; mediaType: string }>,
    onOutput?: (output: ContainerOutput) => Promise<void>,
  ): Promise<'success' | 'error'> {
    const isControlGroup = group.isControlGroup === true;
    const resolvedBackend = registry.resolve(group);
    const backend = resolvedBackend.name();
    let sessionRef = state.getSession(group.folder, backend);

    // Idle session reset: per-group setting takes precedence over global default.
    const effectiveIdleHours =
      group.containerConfig?.sessionIdleResetHours !== undefined
        ? group.containerConfig.sessionIdleResetHours
        : SESSION_IDLE_RESET_HOURS;

    if (sessionRef && effectiveIdleHours > 0) {
      const lastUsed = getSessionLastUsedAt(group.folder, backend);
      const idleMs = lastUsed
        ? Date.now() - new Date(lastUsed).getTime()
        : Infinity;
      if (idleMs > effectiveIdleHours * 3_600_000) {
        logger.info(
          { group: group.name, idleHours: (idleMs / 3_600_000).toFixed(1) },
          'Session idle too long — starting fresh',
        );
        try {
          await autoCompressSession(
            group,
            chatJid,
            effectiveIdleHours,
            lastUsed,
          );
        } catch (err) {
          logger.warn(
            { group: group.name, err },
            'Auto-compress failed (non-fatal)',
          );
        }
        clearSession(group.folder, backend);
        state.clearSession(group.folder, backend);
        sessionRef = undefined;
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

    const runContext: RunContext = {
      prompt,
      groupFolder: group.folder,
      chatJid,
      isControlGroup,
      ...(imageAttachments.length > 0 && { imageInputs: imageAttachments }),
    };

    const currentSessionRef = sessionRef ?? defaultSession('', backend);

    const eventSink: RuntimeEventSink = async (event) => {
      if (event.type === 'session') {
        state.setSession(group.folder, event.sessionRef);
        setSession(group.folder, event.sessionRef);
      }
      if (onOutput) {
        if (event.type === 'output_text') {
          await onOutput({ status: 'success', result: event.text });
        }
        if (event.type === 'turn_complete') {
          await onOutput({ status: 'success', result: null });
        }
        if (event.type === 'error') {
          await onOutput({
            status: 'error',
            result: null,
            error: event.error,
          });
        }
      }
    };

    // ── Injection scanner guardrail ──────────────────────────────────────
    // Scan the prompt BEFORE it reaches the container agent. If blocked,
    // return 'success' (not 'error') so the cursor stays advanced and the
    // message is not retried — returning 'error' would cause an infinite
    // retry loop on the same blocked message.
    // Wrapped in try/catch: scanner errors must not crash the pipeline (fail-open).
    let scanResult: ScanResult | undefined;
    try {
      scanResult = scanForInjection(prompt, INJECTION_SCANNER_CONFIG);
    } catch (err) {
      logger.error({ err }, 'Injection scanner error — failing open');
    }
    if (scanResult?.triggered) {
      if (scanResult.blocked) {
        logger.warn(
          {
            group: group.name,
            score: scanResult.score,
            matches: scanResult.matches,
          },
          'Injection attempt blocked — message will not reach the agent',
        );
        return 'success';
      }
      // logOnly mode: warn but let the message through
      logger.warn(
        {
          group: group.name,
          score: scanResult.score,
          matches: scanResult.matches,
        },
        'Injection attempt detected (logOnly mode, message passing through)',
      );
    }

    try {
      const runResult = await resolvedBackend.runTurn(
        runContext,
        currentSessionRef,
        eventSink,
      );

      if (runResult.status === 'error') {
        // Claude SDK error for dead sessions. Mirrored in container/agent-runner/src/index.ts.
        if (runResult.error?.includes('No conversation found')) {
          clearSession(group.folder, backend);
          state.clearSession(group.folder, backend);
        }
        logger.error(
          { group: group.name, error: runResult.error },
          'Container agent error',
        );
        return 'error';
      }

      if (runResult.sessionRef) {
        state.setSession(group.folder, runResult.sessionRef);
        setSession(group.folder, runResult.sessionRef);
      }

      return 'success';
    } catch (err) {
      logger.error({ group: group.name, err }, 'Agent error');
      return 'error';
    }
  }

  /**
   * Authorized-sender subset eligible to carry a multi-agent task block. Shared by
   * the cold-path dispatch and the warm-path interception so both gate on the same
   * rule — an unauthorized sender's message must never become an executable block.
   */
  function eligibleSenderMessages(
    messages: NewMessage[],
    group: RegisteredGroup,
    chatJid: string,
    allowlist: ReturnType<typeof loadSenderAllowlist>,
  ): NewMessage[] {
    if (group.isControlGroup === true || group.requiresTrigger === false) {
      return messages;
    }
    return messages.filter(
      (m) => m.is_from_me || isTriggerAllowed(chatJid, m.sender, allowlist),
    );
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

    // --- LIA-315 Phase 4: webhook (publicIngress) dispatch under R5/R6 caps ---
    // A webhook-originated run is gated by the ingress caps. This branch is
    // deliberately BEFORE the host/session/trigger logic below: those are chat
    // semantics that don't apply to an anonymous webhook event, and routing a
    // webhook through them would create post-admit abort points that leak the
    // inflight slot. For EACH event, admit → run → release/recordSpend is ONE
    // try/finally scope, so the cap can never leak (caps.ts:170-176).
    if (group.containerConfig?.publicIngress === true) {
      // The human-readable source NAME (e.g. "github") from the webhook:<name>
      // jid — the rate-limiter key + R6 audit identity, NOT the folder (caps.ts:58-61).
      const source = chatJid.startsWith('webhook:')
        ? chatJid.slice('webhook:'.length)
        : group.folder;
      const lastTs = missedMessages[missedMessages.length - 1]!.timestamp;

      // Fail-closed: a publicIngress group must NEVER run uncapped. Drop the whole
      // pending batch (advance the cursor past it) rather than dispatch uncapped.
      if (!ingressCaps) {
        logger.error(
          { chatJid, group: group.name },
          'publicIngress group has no ingress caps wired — refusing run (fail-closed)',
        );
        state.setLastAgentTimestamp(chatJid, lastTs);
        state.save();
        return true;
      }

      // Process EACH webhook event INDIVIDUALLY — never batch. Per event: its own
      // tryAdmit (rate token + inflight slot + R6 audit row keyed on THIS event's
      // requestId) and its own already-framed, per-event-capped prompt
      // (buildWebhookPrompt bounded each content ≤ MAX_PROMPT_BODY). Batching N
      // events into one run would charge them as one, leave N-1 unaudited, and let
      // the aggregate prompt exceed the per-event cap.
      for (const msg of missedMessages) {
        // SAME `now` for tryAdmit and recordSpend so the spend ledger day-key
        // cannot diverge across the UTC boundary (caps.ts:251-253).
        const now = Date.now();
        const admit = await ingressCaps.tryAdmit(
          { source, requestId: msg.id },
          now,
        );
        if (!admit.ok) {
          // Load-shed THIS event: the facade already emitted its R6 `rejected`
          // row. Advance past it (drop) so a capped event does not hot-loop.
          logger.warn(
            { chatJid, source, reason: admit.reason, requestId: msg.id },
            'webhook event rejected by ingress caps — dropped',
          );
          state.setLastAgentTimestamp(chatJid, msg.timestamp);
          state.save();
          continue;
        }

        // runAgent never throws (it catches internally and returns 'error'); the
        // try/finally guarantees release+recordSpend. The already-injection-framed
        // `msg.content` (its own random sentinel) is passed straight through — NOT
        // via formatMessages, which would XML-escape + nest it and dilute the
        // sentinel from being the outermost instruction boundary.
        try {
          await runAgent(group, msg.content, chatJid, []);
        } finally {
          ingressCaps.release();
          // v1 charges a FIXED per-run budget unit (real per-run token usage is
          // not available at this layer — see INGRESS_WEBHOOK_RUN_COST in config.ts;
          // a follow-up will thread real usage through RunResult). The daily
          // ceiling thus bounds runs/day.
          ingressCaps.recordSpend(INGRESS_WEBHOOK_RUN_COST, now);
        }

        // At-most-once: advance past this event regardless of run outcome (a
        // webhook is a fire-once external event; sendMessage is a no-op so there
        // is no user to re-serve, and a rollback-retry would re-spawn + double-charge).
        state.setLastAgentTimestamp(chatJid, msg.timestamp);
        state.save();
      }
      return true;
    }
    // --- End webhook dispatch ---

    // --- Host slash commands (host-side, no container spawn) ---
    const hostResult = dispatchHostCommand(
      missedMessages,
      TRIGGER_PATTERN,
      group,
      SESSION_IDLE_RESET_HOURS,
      isMainGroup,
    );
    if (hostResult.matched) {
      if (hostResult.updatedGroup) {
        setRegisteredGroup(chatJid, hostResult.updatedGroup);
        state.registeredGroups[chatJid] = hostResult.updatedGroup;
        logger.info({ group: group.name }, 'Group setting updated');
      }
      if (hostResult.response) {
        await channel.sendMessage(chatJid, hostResult.response);
      }
      state.setLastAgentTimestamp(chatJid, hostResult.timestamp!);
      state.save();
      return true;
    }
    // --- End host slash commands ---

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
        getContextInfo: () => {
          const resolvedBackend = registry.resolve(group);
          const backend = resolvedBackend.name();
          const stats = state.getContextStats(group.folder);
          const lastCompactedAt = getLastCompactedAt(group.folder, backend);
          return {
            backend,
            // null (SDK omitted usage, LIA-194) → undefined for the ContextInfo
            // contract; session-commands already treats absent as "unknown".
            tokens: stats?.tokens ?? undefined,
            limit: stats?.limit,
            pct: stats?.pct ?? undefined,
            lastCompactedAt,
          };
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
    let pendingAutoCompact = false;

    // Swallow channel send failures so they never propagate as an onOutput
    // rejection (LIA-286); logs with orchestrator-layer context. The boolean
    // return gates cursor/rollback state on whether delivery actually happened.
    const trySend = async (text: string, label: string): Promise<boolean> => {
      try {
        await channel.sendMessage(chatJid, text);
        return true;
      } catch (err) {
        logger.warn(
          { group: group.name, chatJid, label, err },
          'channel.sendMessage failed',
        );
        return false;
      }
    };

    // LIA-127: multi-agent dispatch (behind DEUS_MULTI_AGENT). When the prompt
    // carries a parseable ```deus-tasks block, fan out to MultiAgentOrchestrator
    // instead of the single-agent path. Flag-off, no block, or an unparseable
    // block (other than the explicit malformed-notice case) all leave the
    // single-agent path below byte-identical.
    if (process.env.DEUS_MULTI_AGENT === '1') {
      const finishEarly = async (rollback: boolean, ret: boolean) => {
        await channel.setTyping?.(chatJid, false);
        if (idleTimer) clearTimeout(idleTimer);
        if (rollback) {
          state.setLastAgentTimestamp(chatJid, previousCursor);
          state.save();
        }
        return ret;
      };

      // Parse from RAW message content, NOT `prompt`: formatMessages XML-escapes
      // content (" → &quot;), which would break JSON.parse on the fenced block.
      //
      // Authorization: only AUTHORIZED senders' messages are eligible to carry a
      // task block. A non-main group proceeds once ANY message has an authorized
      // trigger, but an UNauthorized sender's accumulated message must not become an
      // executable block — unlike the single-agent path (which treats other senders'
      // messages as mere context), this path EXECUTES the block, incl. `write` tasks.
      const eligible = eligibleSenderMessages(
        missedMessages,
        group,
        chatJid,
        loadSenderAllowlist(),
      );
      const rawContent = eligible.map((m) => m.content).join('\n');
      const parsed = parseTaskBlock(rawContent);
      if (parsed === MALFORMED_TASK_BLOCK) {
        await trySend(
          "I found a `deus-tasks` block but couldn't parse it — check the JSON and that each task has id/role/goal/prompt/mode.",
          'multi-agent-parse-error',
        );
        // Message consumed; retrying won't fix malformed input. No rollback.
        return finishEarly(false, true);
      }
      if (parsed) {
        // Injection guard on the DECODED task fields — the text that actually
        // reaches the container after JSON.parse + buildPrompt. Scanning the
        // pre-decode chat prompt would MISS JSON-escape-obfuscated injections
        // (e.g. "ignore previous instructions" scans clean but decodes to
        // "ignore previous instructions" at the agent). Fail-open on scanner error.
        const decoded = parsed
          .map((t) => [t.role, t.goal, t.backstory, t.prompt].join('\n'))
          .join('\n');
        let maScan: ScanResult | undefined;
        try {
          maScan = scanForInjection(decoded, INJECTION_SCANNER_CONFIG);
        } catch (err) {
          logger.error(
            { err },
            'Injection scanner error (multi-agent) — failing open',
          );
        }
        if (maScan?.blocked) {
          logger.warn(
            { group: group.name, score: maScan.score, matches: maScan.matches },
            'Injection attempt blocked — multi-agent tasks NOT dispatched',
          );
          // Consumed, no retry (mirror runAgent's blocked→success semantics).
          return finishEarly(false, true);
        }
        if (maScan?.triggered) {
          logger.warn(
            { group: group.name, score: maScan.score, matches: maScan.matches },
            'Injection detected in multi-agent tasks (logOnly, passing through)',
          );
        }

        logger.info(
          { group: group.name, taskCount: parsed.length },
          'Multi-agent dispatch',
        );
        let maResult: OrchestratorResult | undefined;
        try {
          const orchestrator = new MultiAgentOrchestrator(registry);
          maResult = await orchestrator.dispatch(parsed, group);
        } catch (err) {
          // A throw means dispatch failed BEFORE running any task: topologicalSort
          // (cyclic/dangling deps — both already caught at parse time) and
          // registry.resolve run before execution; Promise.allSettled never rejects.
          // So no work happened → safe to roll back and retry (transient/infra).
          logger.warn({ group: group.name, err }, 'Multi-agent dispatch threw');
          await trySend(
            'Multi-agent dispatch failed — please try again.',
            'multi-agent-error',
          );
          return finishEarly(true, false);
        }
        // Dispatch RETURNED → tasks ran and their side effects (incl. `write`)
        // already happened. CONSUME regardless of status or delivery success:
        // re-dispatching would re-run completed tasks (duplicate writes), and a
        // valid all-blocked result (status 'error') must be REPORTED to the user
        // with its blocked reasons, not silently looped. This mirrors the
        // single-agent invariant "work done → consume; only retry if work didn't
        // happen". (status 'error' conflates a deterministic all-blocked run with a
        // transient infra failure that surfaced as BLOCKED; auto-retrying only the
        // transient case would need the orchestrator to separate them.)
        const reply = formatMultiAgentResult(maResult, parsed);
        await trySend(reply, 'multi-agent-output');
        return finishEarly(false, true);
      }
      // No block present → fall through to the single-agent path unchanged.
    }

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
            if (await trySend(text, 'agent-output')) {
              outputSentToUser = true;
            } else if (
              // Primary delivery failed — terse user-facing notice so the loss
              // isn't silent. A delivered fallback sets the flag too, so the
              // rollback path below won't re-send it next poll (LIA-286).
              await trySend(
                'I generated a reply but could not deliver it — please ask again.',
                'agent-output-fallback',
              )
            ) {
              outputSentToUser = true;
            }
          }
          // Only reset idle timer on actual results, not session-update markers
          resetIdleTimer();
        }

        if (CONTEXT_NOTIFY && result.status === 'success' && result.result) {
          // tokens/pct can be null when the SDK omits usage (LIA-194). The
          // container won't set `warn` in that case, but guard defensively so a
          // null never reaches `.toLocaleString()`. Bind `s` first so TS narrows
          // s.tokens/s.pct from the null checks.
          const s = result.contextStats;
          if (s?.warn && s.tokens != null && s.pct != null) {
            // Best-effort advisory notice; swallow delivery failures.
            await trySend(
              `Context at ${s.pct}% (${s.tokens.toLocaleString()} / ${s.limit.toLocaleString()} tokens). Use /compact to free space.`,
              'context-notify',
            );
          }

          if (result.compactionEvent?.trigger === 'auto') {
            const e = result.compactionEvent;
            const preInfo = e.preTokens
              ? ` (was ${e.preTokens.toLocaleString()} tokens)`
              : '';
            // Best-effort advisory notice; swallow delivery failures.
            await trySend(
              `Context auto-compacted${preInfo}. Use /compact manually to control timing.`,
              'auto-compact-notice',
            );
          }
        }

        if (result.contextStats) {
          state.setContextStats(group.folder, result.contextStats);
        }

        if (result.compactionEvent) {
          const resolvedBackend = registry.resolve(group);
          setLastCompactedAt(group.folder, resolvedBackend.name());
          pendingAutoCompact = false;
        }

        if (
          result.contextStats?.autoCompact &&
          !result.compactionEvent &&
          !pendingAutoCompact
        ) {
          pendingAutoCompact = true;
          logger.info(
            { group: group.name, pct: result.contextStats.pct },
            'Auto-compact threshold reached, will dispatch /compact after this turn',
          );
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

    // Deferred until the primary turn has fully returned (LIA-367): dispatching
    // here — instead of un-awaited inside the onOutput callback above — means
    // GroupQueue.enqueueTask always queues (state.active is still true) rather
    // than racing a second container against this one on the same IPC dir.
    if (pendingAutoCompact) {
      queue.enqueueTask(chatJid, 'auto-compact', async () => {
        await runAgent(group, '/compact', chatJid, [], async () => {});
      });
    }

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

            // LIA-315 Phase 4: webhook (publicIngress) batches MUST route through
            // processGroupMessages so the R5/R6 ingress caps gate runs. Never take
            // the pipe-into-active-container path below, and never interpret a
            // webhook payload as a host/session command — those paths bypass
            // tryAdmit/audit/recordSpend (an attacker could otherwise burst events
            // during an active run to slip uncapped messages into the container).
            if (group.containerConfig?.publicIngress === true) {
              queue.enqueueMessageCheck(chatJid);
              continue;
            }

            const isMainGroup = group.isControlGroup === true;

            // --- Host slash commands (message loop — host-side, no container spawn) ---
            const loopHostResult = dispatchHostCommand(
              groupMessages,
              TRIGGER_PATTERN,
              group,
              SESSION_IDLE_RESET_HOURS,
              isMainGroup,
            );
            if (loopHostResult.matched) {
              if (loopHostResult.updatedGroup) {
                setRegisteredGroup(chatJid, loopHostResult.updatedGroup);
                state.registeredGroups[chatJid] = loopHostResult.updatedGroup;
                logger.info({ group: group.name }, 'Group setting updated');
              }
              if (loopHostResult.response) {
                await channel.sendMessage(chatJid, loopHostResult.response);
              }
              state.setLastAgentTimestamp(chatJid, loopHostResult.timestamp!);
              state.save();
              continue;
            }
            // --- End host slash commands ---

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

            // LIA-127: multi-agent dispatch (behind DEUS_MULTI_AGENT). A
            // ```deus-tasks block cannot be piped into a warm single-agent session
            // — it must reach MultiAgentOrchestrator, which lives only in
            // processGroupMessages. Mirror the authorized session-command
            // interception above: finalize the warm session (closeStdin) and route
            // to the cold path via enqueueMessageCheck, so BOTH cold and warm turns
            // hit the one dispatch site. Placed AFTER the trigger gate (a
            // non-triggered block in a trigger-gated group just accumulates as
            // context, matching the cold path) and detected on the SAME pending set
            // that would otherwise be piped, filtered to authorized senders. A
            // non-null parse result (valid SubagentTask[] OR malformed — both
            // truthy) means an authorized sender sent a block; the cold path then
            // dispatches it or emits the single canonical parse-error notice. Do NOT
            // advance the cursor — processGroupMessages pulls-since and consumes.
            // closeStdin is a no-op when no container is active, so this branch also
            // covers cold-start uniformly.
            if (process.env.DEUS_MULTI_AGENT === '1') {
              const maEligible = eligibleSenderMessages(
                messagesToSend,
                group,
                chatJid,
                loadSenderAllowlist(),
              );
              if (parseTaskBlock(maEligible.map((m) => m.content).join('\n'))) {
                queue.closeStdin(chatJid);
                queue.enqueueMessageCheck(chatJid);
                continue;
              }
            }

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
