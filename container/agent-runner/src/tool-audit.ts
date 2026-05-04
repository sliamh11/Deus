import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

const AUDITED_TOOLS = new Set([
  'mcp__deus__send_message',
  'mcp__deus__schedule_task',
  'mcp__deus__update_task',
  'mcp__deus__delete_task',
]);

const AUDITED_PREFIXES = ['mcp__google_calendar__', 'mcp__gmail__'];

const LOG_PATH = '/workspace/group/logs/tool-audit.jsonl';

export function isAuditedTool(name: string): boolean {
  return (
    AUDITED_TOOLS.has(name) || AUDITED_PREFIXES.some((p) => name.startsWith(p))
  );
}

export function writeAuditEntry(
  tool: string,
  toolUseId: string,
  args: unknown,
): void {
  if (process.env.DEUS_TOOL_AUDIT_LOG === '0') return;
  try {
    const entry = {
      ts: new Date().toISOString(),
      tool,
      tool_use_id: toolUseId,
      group: process.env.DEUS_GROUP_FOLDER ?? 'unknown',
      args_preview: JSON.stringify(args ?? '').slice(0, 500),
    };
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    fs.appendFileSync(LOG_PATH, JSON.stringify(entry) + '\n');
  } catch {
    // Audit logging must never crash the tool call
  }
}

export function generateToolUseId(): string {
  return `openai-${Date.now()}-${crypto.randomInt(1e6)}`;
}
