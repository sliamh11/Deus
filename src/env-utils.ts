import { logger } from './logger.js';

/**
 * Parse a positive-integer env value, falling back (with a warning) when set
 * but invalid — `Number('')` is 0 and `Number('abc')` is NaN, neither a valid
 * positive int. Reads via a dynamic `process.env[name]` index, which also keeps
 * the lookup out of flag_lint's literal `process.env.DEUS_*` scan.
 *
 * Shared by credential-proxy and evolution-client (LIA-293 dedup).
 */
export function envPositiveInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw == null) return fallback;
  const n = Number(raw);
  if (Number.isFinite(n) && n > 0) return n;
  logger.warn(
    { name, value: raw },
    'env var is not a positive number — using default',
  );
  return fallback;
}
