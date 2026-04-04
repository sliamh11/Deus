/**
 * Step: container — Build container image and verify with test run.
 * Cross-platform: uses build.sh on macOS/Linux, replicates staging logic on Windows.
 */
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { CONTAINER_IMAGE } from '../src/config.js';
import { logger } from '../src/logger.js';
import { commandExists } from './platform.js';
import { emitStatus } from './status.js';

function parseArgs(args: string[]): { runtime: string } {
  let runtime = '';
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--runtime' && args[i + 1]) {
      runtime = args[i + 1];
      i++;
    }
  }
  return { runtime };
}

/**
 * Read local-only skill names from .git/info/exclude.
 * These are skills that exist locally but aren't committed (e.g. x-integration).
 * They should NOT be staged into the container — their dependencies aren't available.
 */
function getLocalOnlySkills(projectRoot: string): Set<string> {
  const excludePath = path.join(projectRoot, '.git', 'info', 'exclude');
  const localOnly = new Set<string>();
  if (!fs.existsSync(excludePath)) return localOnly;

  const content = fs.readFileSync(excludePath, 'utf-8');
  const pattern = /\.claude\/skills\/([^/\s]+)/g;
  let match;
  while ((match = pattern.exec(content)) !== null) {
    localOnly.add(match[1]);
  }
  return localOnly;
}

/**
 * Stage skill agent files into container/skill-agents/ for the Docker build.
 * This replicates the staging logic from build.sh in a cross-platform way.
 * Skips local-only skills listed in .git/info/exclude.
 */
function stageSkillAgents(projectRoot: string): void {
  const stagingDir = path.join(projectRoot, 'container', 'skill-agents');

  // Clean previous staging
  if (fs.existsSync(stagingDir)) {
    fs.rmSync(stagingDir, { recursive: true });
  }
  fs.mkdirSync(stagingDir, { recursive: true });

  const skillsDir = path.join(projectRoot, '.claude', 'skills');
  if (!fs.existsSync(skillsDir)) return;

  const localOnly = getLocalOnlySkills(projectRoot);

  for (const skillName of fs.readdirSync(skillsDir)) {
    if (localOnly.has(skillName)) {
      logger.info({ skill: skillName }, 'Skipped local-only skill');
      continue;
    }

    const skillDir = path.join(skillsDir, skillName);
    if (!fs.statSync(skillDir).isDirectory()) continue;

    const agentFile = path.join(skillDir, 'agent.ts');
    if (fs.existsSync(agentFile)) {
      const destDir = path.join(stagingDir, skillName);
      fs.mkdirSync(destDir, { recursive: true });
      fs.copyFileSync(agentFile, path.join(destDir, 'agent.ts'));
      logger.info({ skill: skillName }, 'Staged skill agent');
    }
  }
}

/**
 * Clean up staging directory after build.
 */
function cleanupStaging(projectRoot: string): void {
  const stagingDir = path.join(projectRoot, 'container', 'skill-agents');
  if (fs.existsSync(stagingDir)) {
    fs.rmSync(stagingDir, { recursive: true });
  }
}

export async function run(args: string[]): Promise<void> {
  const projectRoot = process.cwd();
  const { runtime } = parseArgs(args);
  const image = CONTAINER_IMAGE;

  if (!runtime) {
    emitStatus('SETUP_CONTAINER', {
      RUNTIME: 'unknown',
      IMAGE: image,
      BUILD_OK: false,
      TEST_OK: false,
      STATUS: 'failed',
      ERROR: 'missing_runtime_flag',
      LOG: 'logs/setup.log',
    });
    process.exit(4);
  }

  if (runtime !== 'docker') {
    emitStatus('SETUP_CONTAINER', {
      RUNTIME: runtime,
      IMAGE: image,
      BUILD_OK: false,
      TEST_OK: false,
      STATUS: 'failed',
      ERROR: 'unknown_runtime',
      LOG: 'logs/setup.log',
    });
    process.exit(4);
  }

  if (!commandExists('docker')) {
    emitStatus('SETUP_CONTAINER', {
      RUNTIME: runtime,
      IMAGE: image,
      BUILD_OK: false,
      TEST_OK: false,
      STATUS: 'failed',
      ERROR: 'runtime_not_available',
      LOG: 'logs/setup.log',
    });
    process.exit(2);
  }

  try {
    execSync('docker info', { stdio: 'ignore' });
  } catch {
    emitStatus('SETUP_CONTAINER', {
      RUNTIME: runtime,
      IMAGE: image,
      BUILD_OK: false,
      TEST_OK: false,
      STATUS: 'failed',
      ERROR: 'runtime_not_available',
      LOG: 'logs/setup.log',
    });
    process.exit(2);
  }

  // Build — use build.sh on unix, manual staging on Windows
  let buildOk = false;
  logger.info({ runtime }, 'Building container');

  const isWindows = process.platform === 'win32';

  try {
    if (isWindows) {
      // Windows: stage skills and run docker build from project root
      stageSkillAgents(projectRoot);
      execSync(`docker build -t ${image} -f container/Dockerfile .`, {
        cwd: projectRoot,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      cleanupStaging(projectRoot);
    } else {
      // macOS/Linux: use build.sh which handles staging + build
      execSync(`bash container/build.sh`, {
        cwd: projectRoot,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    }
    buildOk = true;
    logger.info('Container build succeeded');
  } catch (err) {
    logger.error({ err }, 'Container build failed');
    // Clean up staging on failure too
    if (isWindows) cleanupStaging(projectRoot);
  }

  // Test
  let testOk = false;
  if (buildOk) {
    logger.info('Testing container');
    try {
      const output = execSync(
        `echo '{}' | docker run -i --rm --entrypoint /bin/echo ${image} "Container OK"`,
        { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
      );
      testOk = output.includes('Container OK');
      logger.info({ testOk }, 'Container test result');
    } catch {
      logger.error('Container test failed');
    }
  }

  const status = buildOk && testOk ? 'success' : 'failed';

  emitStatus('SETUP_CONTAINER', {
    RUNTIME: runtime,
    IMAGE: image,
    BUILD_OK: buildOk,
    TEST_OK: testOk,
    STATUS: status,
    LOG: 'logs/setup.log',
  });

  if (status === 'failed') process.exit(1);
}
