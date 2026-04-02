/**
 * Volume mount assembly for Deus agent containers.
 *
 * Builds the list of bind mounts for a container run. This is the
 * security-critical layer: every host path that enters the container is
 * decided here, including credential shadowing and TOCTOU defenses.
 *
 * Separation rationale: mounting logic is complex, security-sensitive, and
 * independently testable. Keeping it in a dedicated module makes security
 * audits and contributor changes easier to reason about.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import { DATA_DIR, GROUPS_DIR } from './config.js';
import { resolveGroupFolderPath, resolveGroupIpcPath } from './group-folder.js';
import { logger } from './logger.js';
import { getProjectById } from './db.js';
import { detectAuthMode } from './credential-proxy.js';
import {
  SENSITIVE_FILE_PATTERNS,
  SENSITIVE_DIR_PATTERNS,
} from './project-registry.js';
import { validateAdditionalMounts } from './mount-security.js';
import { RegisteredGroup } from './types.js';

export interface VolumeMount {
  hostPath: string;
  containerPath: string;
  readonly: boolean;
}

export function buildVolumeMounts(
  group: RegisteredGroup,
  isControlGroup: boolean,
): VolumeMount[] {
  const mounts: VolumeMount[] = [];
  const projectRoot = process.cwd();
  const groupDir = resolveGroupFolderPath(group.folder);

  if (isControlGroup) {
    // Main gets the project root read-only. Writable paths the agent needs
    // (group folder, IPC, .claude/) are mounted separately below.
    // Read-only prevents the agent from modifying host application code
    // (src/, dist/, package.json, etc.) which would bypass the sandbox
    // entirely on next restart.
    mounts.push({
      hostPath: projectRoot,
      containerPath: '/workspace/project',
      readonly: true,
    });

    // Shadow .env so the agent cannot read secrets from the mounted project root.
    // Credentials are injected by the credential proxy, never exposed to containers.
    // os.devNull is '/dev/null' on Unix and '\\.\nul' on Windows.
    const envFile = path.join(projectRoot, '.env');
    if (fs.existsSync(envFile)) {
      mounts.push({
        hostPath: os.devNull,
        containerPath: '/workspace/project/.env',
        readonly: true,
      });
    }

    // Main also gets its group folder as the working directory
    mounts.push({
      hostPath: groupDir,
      containerPath: '/workspace/group',
      readonly: false,
    });
  } else {
    // Other groups only get their own folder
    mounts.push({
      hostPath: groupDir,
      containerPath: '/workspace/group',
      readonly: false,
    });

    // Global memory directory (read-only for non-main)
    // Only directory mounts are supported, not file mounts
    const globalDir = path.join(GROUPS_DIR, 'global');
    if (fs.existsSync(globalDir)) {
      mounts.push({
        hostPath: globalDir,
        containerPath: '/workspace/global',
        readonly: true,
      });
    }
  }

  // External project mount: when a group has an associated project,
  // mount it at /workspace/project so the agent works on the external codebase.
  // Security: project path was validated against mount-allowlist at registration time.
  // We re-validate the real path hasn't changed (symlink TOCTOU defense) and
  // shadow sensitive files to prevent credential exfiltration.
  if (group.projectId) {
    const project = getProjectById(group.projectId);
    if (project && fs.existsSync(project.path)) {
      // TOCTOU defense: re-resolve symlinks at mount time.
      // The path was validated at registration, but a symlink target
      // could have been swapped between registration and now.
      let realProjectPath: string;
      try {
        realProjectPath = fs.realpathSync(project.path);
      } catch {
        logger.warn(
          { projectId: group.projectId, path: project.path },
          'Project path no longer resolvable, skipping mount',
        );
        realProjectPath = ''; // Will skip the mount below
      }

      if (realProjectPath && realProjectPath === project.path) {
        // Determine effective readonly: project config + non-main override
        const effectiveReadonly = project.readonly || !isControlGroup;

        mounts.push({
          hostPath: realProjectPath,
          containerPath: '/workspace/project',
          readonly: effectiveReadonly,
        });

        // Shadow sensitive files to prevent credential leakage.
        // The container will see /dev/null instead of the real file.
        for (const pattern of SENSITIVE_FILE_PATTERNS) {
          const filePath = path.join(realProjectPath, pattern);
          if (fs.existsSync(filePath)) {
            mounts.push({
              hostPath: '/dev/null',
              containerPath: `/workspace/project/${pattern}`,
              readonly: true,
            });
          }
        }

        // Shadow sensitive directories
        for (const dirPattern of SENSITIVE_DIR_PATTERNS) {
          const dirPath = path.join(realProjectPath, dirPattern);
          if (fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory()) {
            // Can't shadow a directory with /dev/null — use an empty tmpdir instead.
            // Create a project-specific empty dir that persists across runs.
            const shadowDir = path.join(
              DATA_DIR,
              'project-shadows',
              group.projectId!,
              dirPattern,
            );
            fs.mkdirSync(shadowDir, { recursive: true, mode: 0o700 });
            mounts.push({
              hostPath: shadowDir,
              containerPath: `/workspace/project/${dirPattern}`,
              readonly: true,
            });
          }
        }

        // Security note: symlinks WITHIN the mounted project can escape the
        // project boundary (e.g., project/data -> /etc/passwd). Docker/Apple
        // Container bind mounts follow symlinks by default. This is mitigated
        // by container isolation (the container process can only access
        // what's mounted) and the mount-allowlist blocking sensitive roots
        // (.ssh, .gnupg, .aws, etc). For maximum safety, users should:
        // 1. Only register trusted project directories
        // 2. Use readonly mode for untrusted projects
        // 3. Review projects for suspicious symlinks before registering

        logger.info(
          {
            group: group.name,
            projectId: group.projectId,
            projectName: project.name,
            readonly: effectiveReadonly,
          },
          'Project mounted as primary workspace',
        );
      } else if (realProjectPath) {
        // Symlink target changed since registration — block the mount.
        // This prevents an attack where someone registers ~/projects/legit,
        // then replaces it with a symlink to /etc or ~/.ssh before the next run.
        logger.error(
          {
            projectId: group.projectId,
            registeredPath: project.path,
            currentRealPath: realProjectPath,
          },
          'Project real path changed since registration — mount BLOCKED (possible symlink swap attack)',
        );
      }
    } else if (project) {
      logger.warn(
        { projectId: group.projectId, path: project.path },
        'Associated project path does not exist, skipping mount',
      );
    }
  }

  // Per-group Claude sessions directory (isolated from other groups)
  // Each group gets their own .claude/ to prevent cross-group session access
  const groupSessionsDir = path.join(
    DATA_DIR,
    'sessions',
    group.folder,
    '.claude',
  );
  fs.mkdirSync(groupSessionsDir, { recursive: true });
  const settingsFile = path.join(groupSessionsDir, 'settings.json');
  if (!fs.existsSync(settingsFile)) {
    fs.writeFileSync(
      settingsFile,
      JSON.stringify(
        {
          env: {
            // Enable agent swarms (subagent orchestration)
            // https://code.claude.com/docs/en/agent-teams#orchestrate-teams-of-claude-code-sessions
            CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: '1',
            // Load CLAUDE.md from additional mounted directories
            // https://code.claude.com/docs/en/memory#load-memory-from-additional-directories
            CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD: '1',
            // Enable Claude's memory feature (persists user preferences between sessions)
            // https://code.claude.com/docs/en/memory#manage-auto-memory
            CLAUDE_CODE_DISABLE_AUTO_MEMORY: '0',
          },
        },
        null,
        2,
      ) + '\n',
    );
  }

  // OAuth session auth: write placeholder credentials so the SDK uses
  // session-based auth (Bearer token). The credential proxy swaps the
  // placeholder with the real token. Written into the session .claude dir
  // (which is already mounted at /home/node/.claude) to avoid Docker
  // mount conflicts with overlapping bind mounts.
  if (detectAuthMode() === 'oauth') {
    const credsFile = path.join(groupSessionsDir, '.credentials.json');
    fs.writeFileSync(
      credsFile,
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'placeholder',
          expiresAt: 4102444800000, // 2100-01-01
          scopes: [
            'user:file_upload',
            'user:inference',
            'user:mcp_servers',
            'user:profile',
            'user:sessions:claude_code',
          ],
        },
      }),
    );
  }

  // Sync skills from container/skills/ into each group's .claude/skills/
  const skillsSrc = path.join(process.cwd(), 'container', 'skills');
  const skillsDst = path.join(groupSessionsDir, 'skills');
  if (fs.existsSync(skillsSrc)) {
    for (const skillDir of fs.readdirSync(skillsSrc)) {
      const srcDir = path.join(skillsSrc, skillDir);
      if (!fs.statSync(srcDir).isDirectory()) continue;
      const dstDir = path.join(skillsDst, skillDir);
      fs.cpSync(srcDir, dstDir, { recursive: true });
    }
  }
  mounts.push({
    hostPath: groupSessionsDir,
    containerPath: '/home/node/.claude',
    readonly: false,
  });

  // Per-group IPC namespace: each group gets its own IPC directory
  // This prevents cross-group privilege escalation via IPC
  const groupIpcDir = resolveGroupIpcPath(group.folder);
  fs.mkdirSync(path.join(groupIpcDir, 'messages'), { recursive: true });
  fs.mkdirSync(path.join(groupIpcDir, 'tasks'), { recursive: true });
  fs.mkdirSync(path.join(groupIpcDir, 'input'), { recursive: true });
  mounts.push({
    hostPath: groupIpcDir,
    containerPath: '/workspace/ipc',
    readonly: false,
  });

  // Copy agent-runner source into a per-group writable location so agents
  // can customize it (add tools, change behavior) without affecting other
  // groups. Recompiled on container startup via entrypoint.sh.
  const agentRunnerSrc = path.join(
    projectRoot,
    'container',
    'agent-runner',
    'src',
  );
  const groupAgentRunnerDir = path.join(
    DATA_DIR,
    'sessions',
    group.folder,
    'agent-runner-src',
  );
  if (fs.existsSync(agentRunnerSrc)) {
    fs.cpSync(agentRunnerSrc, groupAgentRunnerDir, { recursive: true });
  }
  mounts.push({
    hostPath: groupAgentRunnerDir,
    containerPath: '/app/src',
    readonly: true,
  });

  // Additional mounts validated against external allowlist (tamper-proof from containers)
  if (group.containerConfig?.additionalMounts) {
    const validatedMounts = validateAdditionalMounts(
      group.containerConfig.additionalMounts,
      group.name,
      isControlGroup,
    );
    mounts.push(...validatedMounts);
  }

  return mounts;
}
