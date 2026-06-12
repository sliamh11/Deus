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

import { DATA_DIR, GROUPS_DIR, HOME_DIR, CONFIG_DIR } from './config.js';
import {
  resolveGroupFolderPath,
  resolveGroupIpcPath,
  assertValidGroupFolder,
} from './group-folder.js';
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

/**
 * Resolve the vault path from env var or config file.
 * Returns null if no vault is configured.
 */
function resolveVaultPath(): string | null {
  // 1. Environment variable
  const envPath = process.env.DEUS_VAULT_PATH;
  if (envPath) {
    const resolved = envPath.startsWith('~')
      ? path.join(HOME_DIR, envPath.slice(1))
      : envPath;
    return path.resolve(resolved);
  }
  // 2. Config file
  const configPath = path.join(CONFIG_DIR, 'config.json');
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    if (cfg.vault_path) {
      const vp = cfg.vault_path as string;
      const resolved = vp.startsWith('~')
        ? path.join(HOME_DIR, vp.slice(1))
        : vp;
      return path.resolve(resolved);
    }
  } catch {
    // No config file or parse error
  }
  return null;
}

/**
 * Shadow sensitive files (devNull) and dirs (empty dir under `dirShadowBase`)
 * inside a `/workspace/project` mount. Single-sourced across every project-root
 * mount branch: divergence here is a credential-exposure bug — it was exactly
 * that (the control branch shadowed only `.env` while the others ran these
 * loops; LIA-210).
 */
function pushProjectShadows(
  mounts: VolumeMount[],
  hostDir: string,
  dirShadowBase: string,
): void {
  for (const pattern of SENSITIVE_FILE_PATTERNS) {
    if (fs.existsSync(path.join(hostDir, pattern))) {
      mounts.push({
        hostPath: os.devNull,
        containerPath: `/workspace/project/${pattern}`,
        readonly: true,
      });
    }
  }
  for (const dirPattern of SENSITIVE_DIR_PATTERNS) {
    const dirPath = path.join(hostDir, dirPattern);
    if (fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory()) {
      const shadowDir = path.join(dirShadowBase, dirPattern);
      fs.mkdirSync(shadowDir, { recursive: true, mode: 0o700 });
      mounts.push({
        hostPath: shadowDir,
        containerPath: `/workspace/project/${dirPattern}`,
        readonly: true,
      });
    }
  }
}

export function buildVolumeMounts(
  group: RegisteredGroup,
  isControlGroup: boolean,
  worktreePath?: string,
): VolumeMount[] {
  const mounts: VolumeMount[] = [];
  const projectRoot = process.cwd();
  const groupDir = resolveGroupFolderPath(group.folder);

  if (isControlGroup) {
    // Only mount the Deus project root as a fallback when no worktree or
    // external project will provide /workspace/project (avoids duplicate mounts).
    const hasProjectOverride = !!(worktreePath || group.projectId);
    if (!hasProjectOverride) {
      mounts.push({
        hostPath: projectRoot,
        containerPath: '/workspace/project',
        readonly: true,
      });
      // Credential shadowing (LIA-210): the whole repo is mounted read-only for
      // the control/dev agent, but read-only is still `cat`-able — every path
      // holding real secrets or PII must be shadowed. Three layers:
      // (1) shared patterns (.env*, credentials/, secrets/):
      pushProjectShadows(
        mounts,
        projectRoot,
        path.join(DATA_DIR, 'control-shadows'),
      );
      // (2) Deus runtime-state dirs (live creds in data/env/env + store/auth,
      // message DBs, logs). Denylist, not allowlist: the agent needs broad code
      // access, so an allowlist would break unknown/future code dirs. DENYLIST:
      // any new runtime/cred dir under the project root MUST be added here.
      for (const name of ['data', 'store', 'groups', 'logs']) {
        if (!fs.existsSync(path.join(projectRoot, name))) continue;
        const shadowDir = path.join(DATA_DIR, 'control-shadows', name);
        fs.mkdirSync(shadowDir, { recursive: true, mode: 0o700 });
        mounts.push({
          hostPath: shadowDir,
          containerPath: `/workspace/project/${name}`,
          readonly: true,
        });
      }
      // (3) integrations/: shadow every child EXCEPT gcal, whose MCP reads its
      // OAuth creds in-container by design (agent-runner reads
      // /workspace/project/integrations/gcal/*). gcal stays visible through the
      // parent mount; enumerating keeps new integrations hidden by default.
      // Residual in-container gcal-token exposure: follow-up LIA-282.
      const integrationsDir = path.join(projectRoot, 'integrations');
      if (fs.existsSync(integrationsDir)) {
        for (const child of fs.readdirSync(integrationsDir)) {
          if (child === 'gcal') continue;
          const childPath = path.join(integrationsDir, child);
          const containerChild = `/workspace/project/integrations/${child}`;
          if (fs.statSync(childPath).isDirectory()) {
            const shadowDir = path.join(
              DATA_DIR,
              'control-shadows',
              'integrations',
              child,
            );
            fs.mkdirSync(shadowDir, { recursive: true, mode: 0o700 });
            mounts.push({
              hostPath: shadowDir,
              containerPath: containerChild,
              readonly: true,
            });
          } else {
            mounts.push({
              hostPath: os.devNull,
              containerPath: containerChild,
              readonly: true,
            });
          }
        }
      }
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

  // Per-task worktree override: mount the worktree instead of the project root.
  // Worktree is always writable — the agent must commit back to its branch.
  // Apply credential shadows defensively (same as regular project mounts).
  if (worktreePath && fs.existsSync(worktreePath)) {
    const realWorktree = fs.realpathSync(worktreePath);
    if (realWorktree !== path.resolve(worktreePath)) {
      logger.warn(
        { worktreePath, realWorktree },
        'Worktree path changed after creation, skipping mount',
      );
    } else {
      mounts.push({
        hostPath: realWorktree,
        containerPath: '/workspace/project',
        readonly: false,
      });
      pushProjectShadows(
        mounts,
        worktreePath,
        path.join(DATA_DIR, 'worktree-shadows', path.basename(worktreePath)),
      );
    }
  } else if (group.projectId) {
    // External project mount: when a group has an associated project,
    // mount it at /workspace/project so the agent works on the external codebase.
    // Security: project path was validated against mount-allowlist at registration time.
    // We re-validate the real path hasn't changed (symlink TOCTOU defense) and
    // shadow sensitive files to prevent credential exfiltration.
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

        // Shadow sensitive files/dirs (.env*, credentials/, secrets/) so the
        // container sees /dev/null / an empty dir instead of real content.
        pushProjectShadows(
          mounts,
          realProjectPath,
          path.join(DATA_DIR, 'project-shadows', group.projectId!),
        );

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

  // Vault mount: control group gets full rw at /workspace/vault; non-control
  // groups get a private rw subdir + shared ro root (skills use /workspace/vault/).
  const vaultPath = resolveVaultPath();
  if (vaultPath && fs.existsSync(vaultPath)) {
    if (isControlGroup) {
      mounts.push({
        hostPath: vaultPath,
        containerPath: '/workspace/vault',
        readonly: false,
      });
      logger.info(
        { group: group.name, vaultPath },
        'Vault mounted at /workspace/vault (control, rw)',
      );
    } else {
      assertValidGroupFolder(group.folder);
      const groupVaultDir = path.join(vaultPath, 'groups', group.folder);
      // Defence-in-depth: assertValidGroupFolder blocks traversal, verify after join
      const rel = path.relative(vaultPath, groupVaultDir);
      if (rel.startsWith('..') || path.isAbsolute(rel)) {
        throw new Error(`Vault group path escapes base: ${groupVaultDir}`);
      }
      fs.mkdirSync(groupVaultDir, { recursive: true });
      mounts.push({
        hostPath: groupVaultDir,
        containerPath: '/workspace/vault/group',
        readonly: false,
      });
      mounts.push({
        hostPath: vaultPath,
        containerPath: '/workspace/vault/shared',
        readonly: true,
      });
      logger.info(
        { group: group.name, vaultPath, groupVaultDir },
        'Vault mounted: group rw + shared ro',
      );
    }
  }

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

export function buildFanOutMounts(
  group: RegisteredGroup,
  taskId: string,
): VolumeMount[] {
  const groupDir = resolveGroupFolderPath(group.folder);
  const sandboxDir = path.join(groupDir, '.multi-agent', taskId);

  if (!fs.existsSync(sandboxDir)) {
    fs.mkdirSync(sandboxDir, { recursive: true });
  }

  return [
    {
      hostPath: groupDir,
      containerPath: '/workspace/group',
      readonly: true,
    },
    {
      hostPath: sandboxDir,
      containerPath: '/workspace/sandbox',
      readonly: false,
    },
  ];
}
