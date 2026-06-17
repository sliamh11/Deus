import { CronExpressionParser, type CronExpression } from 'cron-parser';

/**
 * Parse a cron expression, rejecting blank input first. cron-parser >= 5.5.0
 * silently parses the zero-length string `""` as `* * * * *` (every minute)
 * instead of throwing, which would create a runaway job (GH #788). Single
 * source of truth for host-side cron parsing so this guard can't be skipped.
 *
 * @throws {Error} when `value` is blank or not a valid cron expression.
 */
export function parseCronExpression(value: string, tz: string): CronExpression {
  if (!value || !value.trim()) {
    throw new Error(
      `Invalid cron: "${value}". Use format like "0 9 * * *" or "*/5 * * * *".`,
    );
  }
  return CronExpressionParser.parse(value, { tz });
}
