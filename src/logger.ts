import pino from 'pino';
import { isInteractiveTerminal } from './platform.js';

export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  // Pretty, colorized output only for an interactive terminal. Under launchd
  // (or any piped/redirected stdout) emit plain NDJSON so ANSI escape codes
  // don't corrupt persisted log files (LIA-272).
  transport: isInteractiveTerminal()
    ? { target: 'pino-pretty', options: { colorize: true } }
    : undefined,
});

// Route uncaught errors through pino so they get timestamps in stderr
process.on('uncaughtException', (err) => {
  logger.fatal({ err }, 'Uncaught exception');
  process.exit(1);
});

process.on('unhandledRejection', (reason) => {
  logger.error({ err: reason }, 'Unhandled rejection');
});
