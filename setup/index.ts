/**
 * Setup CLI entry point.
 * Usage: npx tsx setup/index.ts --step <name> [args...]
 */
import fs from 'fs';
import path from 'path';

import { logger } from '../src/logger.js';
import { emitStatus } from './status.js';

const STEPS: Record<
  string,
  () => Promise<{ run: (args: string[]) => Promise<void> }>
> = {
  environment: () => import('./environment.js'),
  container: () => import('./container.js'),
  memory: () => import('./memory.js'),
  groups: () => import('./groups.js'),
  register: () => import('./register.js'),
  mounts: () => import('./mounts.js'),
  service: () => import('./service.js'),
  cli: () => import('./cli.js'),
  verify: () => import('./verify.js'),
  'smoke-test': () => import('./smoke-test.js'),
};

async function main(): Promise<void> {
  // Scaffold .env from .env.example if it doesn't exist yet.
  const projectRoot = process.cwd();
  const envPath = path.join(projectRoot, '.env');
  const envExamplePath = path.join(projectRoot, '.env.example');
  if (!fs.existsSync(envPath) && fs.existsSync(envExamplePath)) {
    fs.copyFileSync(envExamplePath, envPath);
    logger.info(
      'Created .env from .env.example — fill in your API credentials.',
    );
  }

  const args = process.argv.slice(2);
  const stepIdx = args.indexOf('--step');

  if (stepIdx === -1 || !args[stepIdx + 1]) {
    console.error(
      `Usage: npx tsx setup/index.ts --step <${Object.keys(STEPS).join('|')}> [args...]`,
    );
    process.exit(1);
  }

  const stepName = args[stepIdx + 1];
  const stepArgs = args.filter(
    (a, i) => i !== stepIdx && i !== stepIdx + 1 && a !== '--',
  );

  const loader = STEPS[stepName];
  if (!loader) {
    console.error(`Unknown step: ${stepName}`);
    console.error(`Available steps: ${Object.keys(STEPS).join(', ')}`);
    process.exit(1);
  }

  try {
    const mod = await loader();
    await mod.run(stepArgs);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    logger.error({ err, step: stepName }, 'Setup step failed');
    emitStatus(stepName.toUpperCase(), {
      STATUS: 'failed',
      ERROR: message,
    });
    process.exit(1);
  }
}

main();
