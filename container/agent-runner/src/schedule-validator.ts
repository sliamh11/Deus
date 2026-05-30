import { z } from 'zod';
import { CronExpressionParser } from 'cron-parser';

export type Result<V, E> = { ok: true; value: V } | { ok: false; error: E };

export const ScheduleInput = z.object({
  schedule_type: z.enum(['cron', 'interval', 'once']),
  schedule_value: z.string(),
  target_group_jid: z.string().optional(),
});

export type ScheduleInput = z.infer<typeof ScheduleInput>;

export interface ValidSchedule {
  schedule_type: 'cron' | 'interval' | 'once';
  schedule_value: string;
  target_group_jid?: string;
}

export function validateSchedule(
  input: ScheduleInput,
): Result<ValidSchedule, string> {
  const { schedule_type, schedule_value, target_group_jid } = input;

  if (schedule_type === 'cron') {
    try {
      CronExpressionParser.parse(schedule_value);
    } catch {
      return {
        ok: false,
        error: `Invalid cron: "${schedule_value}". Use format like "0 9 * * *" or "*/5 * * * *".`,
      };
    }
  } else if (schedule_type === 'interval') {
    const ms = parseInt(schedule_value, 10);
    if (Number.isNaN(ms) || ms <= 0) {
      return {
        ok: false,
        error: `Invalid interval: "${schedule_value}". Must be positive milliseconds.`,
      };
    }
  } else {
    // once
    if (
      /[Zz]$/.test(schedule_value) ||
      /[+-]\d{2}:\d{2}$/.test(schedule_value)
    ) {
      return {
        ok: false,
        error: `Timestamp must be local time without timezone suffix. Got "${schedule_value}".`,
      };
    }
    const date = new Date(schedule_value);
    if (Number.isNaN(date.getTime())) {
      return {
        ok: false,
        error: `Invalid timestamp: "${schedule_value}". Use local time format like "2026-02-01T15:30:00".`,
      };
    }
  }

  return {
    ok: true,
    value: { schedule_type, schedule_value, target_group_jid },
  };
}
