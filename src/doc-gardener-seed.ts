import { CronExpressionParser } from 'cron-parser';

import { TIMEZONE } from './config.js';
import { createTask, getTaskById } from './db.js';

const GARDENER_ID = 'doc-gardener-weekly';
const CRON_EXPR = '0 3 * * 1'; // Monday 03:00 local — low-traffic window

export function seedDocGardener(jid: string, folder: string): void {
  if (!jid || !folder) return;
  if (getTaskById(GARDENER_ID)) return;
  const interval = CronExpressionParser.parse(CRON_EXPR, { tz: TIMEZONE });
  createTask({
    id: GARDENER_ID,
    chat_jid: jid,
    group_folder: folder,
    prompt: 'Run the doc-gardener agent per .claude/agents/doc-gardener.md',
    schedule_type: 'cron',
    schedule_value: CRON_EXPR,
    context_mode: 'isolated',
    next_run: interval.next().toISOString(),
    status: 'active',
    created_at: new Date().toISOString(),
    agent_backend: null,
  });
}
