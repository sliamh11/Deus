/**
 * Project Registry for External Environment Mode
 *
 * Manages registration of external projects that Deus can work on.
 * Projects are mounted into containers as the primary workspace,
 * replacing the default group folder as cwd.
 *
 * Security: project paths are validated against the mount allowlist
 * (same system used for additionalMounts). The allowlist lives at
 * ~/.config/deus/mount-allowlist.json — outside the project root
 * and never mounted into containers, so agents cannot tamper with it.
 */
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

import {
  createProject,
  deleteProject as dbDeleteProject,
  getAllProjects,
  getProjectById,
  getProjectByPath,
  setGroupProject,
} from './db.js';
import { HOME_DIR } from './config.js';
import { logger } from './logger.js';
import { validateMount } from './mount-security.js';
import { ProjectConfig, ProjectType } from './types.js';

/**
 * Expand ~ to home directory
 */
function expandPath(p: string): string {
  if (p.startsWith('~/')) return path.join(HOME_DIR, p.slice(2));
  if (p === '~') return HOME_DIR;
  return path.resolve(p);
}

/**
 * Detect the project type from marker files in the directory.
 * Returns null for unrecognized project types (still mountable, just no auto-hints).
 */
export function detectProjectType(projectPath: string): ProjectType | null {
  const exists = (f: string) => fs.existsSync(path.join(projectPath, f));
  const readJson = (f: string) => {
    try {
      return JSON.parse(fs.readFileSync(path.join(projectPath, f), 'utf-8'));
    } catch {
      return null;
    }
  };

  // Rust
  if (exists('Cargo.toml')) {
    return {
      language: 'rust',
      packageManager: 'cargo',
      testRunner: 'cargo test',
    };
  }

  // Go
  if (exists('go.mod')) {
    return { language: 'go', testRunner: 'go test' };
  }

  // Python
  if (
    exists('pyproject.toml') ||
    exists('setup.py') ||
    exists('requirements.txt')
  ) {
    const pt: ProjectType = { language: 'python' };
    if (exists('pyproject.toml')) pt.packageManager = 'pip';
    if (exists('Pipfile')) pt.packageManager = 'pipenv';
    if (exists('poetry.lock')) pt.packageManager = 'poetry';
    // Framework detection
    const req = exists('requirements.txt')
      ? fs.readFileSync(path.join(projectPath, 'requirements.txt'), 'utf-8')
      : '';
    if (req.includes('django') || exists('manage.py')) pt.framework = 'django';
    else if (req.includes('flask')) pt.framework = 'flask';
    else if (req.includes('fastapi')) pt.framework = 'fastapi';
    if (exists('pytest.ini') || exists('conftest.py')) pt.testRunner = 'pytest';
    return pt;
  }

  // Ruby
  if (exists('Gemfile')) {
    const pt: ProjectType = { language: 'ruby', packageManager: 'bundler' };
    if (exists('Rakefile') || exists('config/application.rb'))
      pt.framework = 'rails';
    return pt;
  }

  // Java / Kotlin
  if (exists('pom.xml')) {
    return {
      language: 'java',
      packageManager: 'maven',
      testRunner: 'maven test',
    };
  }
  if (exists('build.gradle') || exists('build.gradle.kts')) {
    return {
      language: 'java',
      packageManager: 'gradle',
      testRunner: 'gradle test',
    };
  }

  // Node.js / TypeScript / JavaScript (checked last — many projects have package.json)
  if (exists('package.json')) {
    const pkg = readJson('package.json');
    const pt: ProjectType = { language: 'javascript' };

    if (exists('tsconfig.json')) pt.language = 'typescript';

    // Package manager
    if (exists('pnpm-lock.yaml')) pt.packageManager = 'pnpm';
    else if (exists('yarn.lock')) pt.packageManager = 'yarn';
    else if (exists('bun.lockb')) pt.packageManager = 'bun';
    else pt.packageManager = 'npm';

    // Framework detection from dependencies
    const deps = { ...pkg?.dependencies, ...pkg?.devDependencies };
    if (deps?.next) pt.framework = 'next.js';
    else if (deps?.nuxt) pt.framework = 'nuxt';
    else if (deps?.['@angular/core']) pt.framework = 'angular';
    else if (deps?.svelte || deps?.['@sveltejs/kit']) pt.framework = 'svelte';
    else if (deps?.vue) pt.framework = 'vue';
    else if (deps?.react && !deps?.next) pt.framework = 'react';
    else if (deps?.express) pt.framework = 'express';
    else if (deps?.fastify) pt.framework = 'fastify';

    // Test runner
    if (deps?.vitest) pt.testRunner = 'vitest';
    else if (deps?.jest) pt.testRunner = 'jest';
    else if (deps?.mocha) pt.testRunner = 'mocha';

    return pt;
  }

  return null;
}

/**
 * Sensitive file patterns to shadow with /dev/null in project mounts.
 * These are blocked from container access even when the project is read-write.
 *
 * Security rationale: a compromised or misbehaving agent could exfiltrate
 * credentials from the user's project. Shadowing these files means the
 * container sees /dev/null instead of the real content.
 */
export const SENSITIVE_FILE_PATTERNS = [
  '.env',
  '.env.local',
  '.env.development',
  '.env.production',
  '.env.staging',
  '.env.test',
];

/**
 * Sensitive directory patterns to check within the project.
 * Files matching these globs under the project root are shadowed.
 */
export const SENSITIVE_DIR_PATTERNS = ['credentials', 'secrets'];

/**
 * Register a new external project.
 *
 * Validates the path against the mount allowlist before storing.
 * This ensures only pre-approved directories can be mounted.
 */
export function registerProject(
  name: string,
  hostPath: string,
  options?: { readonly?: boolean },
): ProjectConfig {
  const resolvedPath = expandPath(hostPath);

  // Verify path exists and is a directory
  if (!fs.existsSync(resolvedPath)) {
    throw new Error(
      `Path does not exist: ${hostPath} (resolved: ${resolvedPath})`,
    );
  }
  if (!fs.statSync(resolvedPath).isDirectory()) {
    throw new Error(`Path is not a directory: ${resolvedPath}`);
  }

  // Security: resolve symlinks to prevent symlink-based allowlist bypass.
  // An attacker could create ~/projects/evil -> /etc and register it
  // if we only checked the symlink path against the allowlist.
  const realPath = fs.realpathSync(resolvedPath);

  // Validate against mount allowlist (reuse existing security infrastructure).
  // We check as isControlGroup=true because project registration is main-only,
  // but the effective readonly is determined per-group at mount time.
  const validation = validateMount(
    { hostPath: realPath, readonly: options?.readonly ?? false },
    true, // isControlGroup — registration requires main privileges
  );

  if (!validation.allowed) {
    throw new Error(
      `Project path not allowed by mount allowlist: ${validation.reason}. ` +
        `Add the parent directory to ~/.config/deus/mount-allowlist.json`,
    );
  }

  // Check for duplicate path
  const existing = getProjectByPath(realPath);
  if (existing) {
    throw new Error(
      `A project is already registered at this path: "${existing.name}" (${existing.id})`,
    );
  }

  const projectType = detectProjectType(realPath);
  const project: ProjectConfig = {
    id: `proj-${crypto.randomUUID().slice(0, 8)}`,
    name,
    path: realPath,
    type: projectType,
    readonly: options?.readonly ?? false,
    created_at: new Date().toISOString(),
  };

  createProject(project);
  logger.info(
    {
      projectId: project.id,
      name: project.name,
      path: project.path,
      type: project.type,
      readonly: project.readonly,
    },
    'Project registered',
  );

  return project;
}

/**
 * Associate a project with a group folder.
 * The group's container will mount the project as its primary workspace.
 */
export function associateProject(projectId: string, groupFolder: string): void {
  const project = getProjectById(projectId);
  if (!project) {
    throw new Error(`Project not found: ${projectId}`);
  }
  setGroupProject(groupFolder, projectId);
  logger.info({ projectId, groupFolder }, 'Project associated with group');
}

/**
 * Remove project association from a group.
 */
export function dissociateProject(groupFolder: string): void {
  setGroupProject(groupFolder, null);
  logger.info({ groupFolder }, 'Project dissociated from group');
}

/**
 * Delete a project registration.
 * Automatically dissociates all groups.
 */
export function removeProject(id: string): void {
  const project = getProjectById(id);
  if (!project) {
    throw new Error(`Project not found: ${id}`);
  }
  dbDeleteProject(id);
  logger.info({ projectId: id, name: project.name }, 'Project deleted');
}

// Re-export DB accessors for convenience
export { getProjectById, getProjectByPath, getAllProjects };
