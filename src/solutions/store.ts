/**
 * Solution atom store — structured lesson capture.
 *
 * Stores operational learnings (bug fixes, dead ends, patterns) as markdown
 * files with YAML frontmatter in the vault's solutions/ directory. These are
 * append-only: once written, never deleted or overwritten. File-based storage
 * ensures compatibility with the memory tree and indexer.
 *
 * Solutions are discoverable via:
 *   1. Text/tag grep search (this module)
 *   2. Memory indexer --add (indexes the markdown file for semantic search)
 *   3. Context injection into container agents (context-registry.ts)
 */

import { randomUUID } from 'crypto';
import fs from 'fs';
import path from 'path';

import { CONFIG_DIR, HOME_DIR } from '../config.js';

// ── Types ──────────────────────────────────────────────────────────────────

export type ProblemType = 'bug' | 'knowledge' | 'pattern';
export type Severity = 'low' | 'medium' | 'high';

export interface Solution {
  id: string;
  title: string;
  tags: string[];
  problemType: ProblemType;
  module?: string;
  severity: Severity;
  symptoms: string;
  deadEnds: string;
  solution: string;
  prevention: string;
}

// ── Vault path resolution ──────────────────────────────────────────────────

/**
 * Resolve the vault path from DEUS_VAULT_PATH env or ~/.config/deus/config.json.
 * Mirrors the logic in container-mounter.ts resolveVaultPath().
 */
export function resolveVaultPath(): string | null {
  const envPath = process.env.DEUS_VAULT_PATH;
  if (envPath) {
    const resolved = envPath.startsWith('~')
      ? path.join(HOME_DIR, envPath.slice(1))
      : envPath;
    return path.resolve(resolved);
  }
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
 * Return the solutions directory path inside the vault.
 * Creates it if it does not exist and the vault is configured.
 */
export function solutionsDir(): string | null {
  const vault = resolveVaultPath();
  if (!vault) return null;
  const dir = path.join(vault, 'solutions');
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  return dir;
}

// ── Serialization helpers ──────────────────────────────────────────────────

function toFrontmatter(sol: Solution): string {
  const tagList = sol.tags.map((t) => `"${t}"`).join(', ');
  const lines = [
    '---',
    `id: ${sol.id}`,
    'type: solution',
    `title: "${sol.title.replace(/"/g, '\\"')}"`,
    `tags: [${tagList}]`,
    `problem_type: ${sol.problemType}`,
  ];
  if (sol.module) {
    lines.push(`module: ${sol.module}`);
  }
  lines.push(`severity: ${sol.severity}`);
  lines.push(`updated: ${new Date().toISOString().slice(0, 10)}`);
  lines.push('---');
  return lines.join('\n');
}

function toBugBody(sol: Solution): string {
  return [
    '## Symptoms',
    sol.symptoms,
    '',
    "## What Didn't Work",
    sol.deadEnds,
    '',
    '## Solution',
    sol.solution,
    '',
    '## Prevention',
    sol.prevention,
  ].join('\n');
}

function toKnowledgeBody(sol: Solution): string {
  return [
    '## Context',
    sol.symptoms, // reuse symptoms field as context
    '',
    '## Guidance',
    sol.solution, // reuse solution field as guidance
    '',
    '## When to Apply',
    sol.prevention, // reuse prevention field as triggers
  ].join('\n');
}

function toMarkdown(sol: Solution): string {
  const fm = toFrontmatter(sol);
  const body =
    sol.problemType === 'knowledge' ? toKnowledgeBody(sol) : toBugBody(sol);
  return `${fm}\n\n${body}\n`;
}

/** Generate a filename-safe slug from the title. */
function slugify(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
    .slice(0, 60);
}

// ── Parsing ────────────────────────────────────────────────────────────────

function parseFrontmatter(content: string): Record<string, string> | null {
  const match = content.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return null;
  const kv: Record<string, string> = {};
  for (const line of match[1].split('\n')) {
    const idx = line.indexOf(':');
    if (idx > 0) {
      kv[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    }
  }
  return kv;
}

function parseSection(content: string, heading: string): string {
  const regex = new RegExp(`## ${heading}\\n([\\s\\S]*?)(?=\\n## |$)`);
  const match = content.match(regex);
  return match ? match[1].trim() : '';
}

function parseTags(raw: string): string[] {
  // Parse [tag1, tag2, ...] or ["tag1", "tag2", ...]
  const inner = raw.replace(/^\[|\]$/g, '');
  return inner
    .split(',')
    .map((t) => t.trim().replace(/^["']|["']$/g, ''))
    .filter(Boolean);
}

function parseSolution(content: string, filePath: string): Solution | null {
  const fm = parseFrontmatter(content);
  if (!fm || fm.type !== 'solution') return null;

  const problemType = (fm.problem_type || 'bug') as ProblemType;
  return {
    id: fm.id || path.basename(filePath, '.md'),
    title: (fm.title || '').replace(/^["']|["']$/g, '').replace(/\\"/g, '"'),
    tags: parseTags(fm.tags || '[]'),
    problemType,
    module: fm.module || undefined,
    severity: (fm.severity || 'medium') as Severity,
    symptoms:
      problemType === 'knowledge'
        ? parseSection(content, 'Context')
        : parseSection(content, 'Symptoms'),
    deadEnds: parseSection(content, "What Didn't Work"),
    solution:
      problemType === 'knowledge'
        ? parseSection(content, 'Guidance')
        : parseSection(content, 'Solution'),
    prevention:
      problemType === 'knowledge'
        ? parseSection(content, 'When to Apply')
        : parseSection(content, 'Prevention'),
  };
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Write a new solution atom to the vault. Returns the generated ID.
 * Append-only: never overwrites an existing file.
 */
export function writeSolution(
  data: Omit<Solution, 'id'>,
  dir?: string,
): string {
  const targetDir = dir ?? solutionsDir();
  if (!targetDir) {
    throw new Error(
      'No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json',
    );
  }

  const id = randomUUID();
  const sol: Solution = { id, ...data };
  const filename = `${slugify(sol.title)}-${id.slice(0, 8)}.md`;
  const filePath = path.join(targetDir, filename);

  // Append-only guard: never overwrite
  if (fs.existsSync(filePath)) {
    throw new Error(`Solution file already exists: ${filePath}`);
  }

  fs.writeFileSync(filePath, toMarkdown(sol), 'utf-8');
  return id;
}

/**
 * Search solutions by text query and optional tag filter.
 * Uses simple substring matching (grep-style) for v1.
 */
export function searchSolutions(
  query: string,
  tags?: string[],
  dir?: string,
): Solution[] {
  const targetDir = dir ?? solutionsDir();
  if (!targetDir || !fs.existsSync(targetDir)) return [];

  const files = fs
    .readdirSync(targetDir)
    .filter((f) => f.endsWith('.md'))
    .sort()
    .reverse(); // newest first (filenames are slug-uuid, but sort is stable)

  const lowerQuery = query.toLowerCase();
  const lowerTags = tags?.map((t) => t.toLowerCase());

  const results: Solution[] = [];
  for (const file of files) {
    const content = fs.readFileSync(path.join(targetDir, file), 'utf-8');
    const sol = parseSolution(content, file);
    if (!sol) continue;

    // Tag filter: all specified tags must be present
    if (lowerTags && lowerTags.length > 0) {
      const solTags = sol.tags.map((t) => t.toLowerCase());
      if (!lowerTags.every((t) => solTags.includes(t))) continue;
    }

    // Text match: query appears anywhere in the content
    if (lowerQuery && !content.toLowerCase().includes(lowerQuery)) continue;

    results.push(sol);
  }

  return results;
}

/**
 * Retrieve a single solution by its ID.
 */
export function getSolution(id: string, dir?: string): Solution | null {
  const targetDir = dir ?? solutionsDir();
  if (!targetDir || !fs.existsSync(targetDir)) return null;

  const files = fs.readdirSync(targetDir).filter((f) => f.endsWith('.md'));
  for (const file of files) {
    const content = fs.readFileSync(path.join(targetDir, file), 'utf-8');
    const sol = parseSolution(content, file);
    if (sol && sol.id === id) return sol;
  }
  return null;
}

/**
 * List all solutions sorted by file modification time (newest first).
 */
export function listSolutions(limit = 20, dir?: string): Solution[] {
  const targetDir = dir ?? solutionsDir();
  if (!targetDir || !fs.existsSync(targetDir)) return [];

  const files = fs
    .readdirSync(targetDir)
    .filter((f) => f.endsWith('.md'))
    .map((f) => ({
      name: f,
      mtime: fs.statSync(path.join(targetDir, f)).mtimeMs,
    }))
    .sort((a, b) => b.mtime - a.mtime)
    .slice(0, limit);

  const results: Solution[] = [];
  for (const { name } of files) {
    const content = fs.readFileSync(path.join(targetDir, name), 'utf-8');
    const sol = parseSolution(content, name);
    if (sol) results.push(sol);
  }
  return results;
}

/**
 * Load the N most recently modified solutions as formatted context strings.
 * Used by the context registry to inject solutions into agent runs.
 */
export function loadSolutionContext(limit = 3, dir?: string): string[] {
  const solutions = listSolutions(limit, dir);
  return solutions.map((sol) => {
    const typeLabel =
      sol.problemType === 'knowledge' ? 'Knowledge' : 'Solution';
    const tagStr = sol.tags.length > 0 ? ` [${sol.tags.join(', ')}]` : '';
    const header = `${typeLabel}: ${sol.title}${tagStr}`;
    const body =
      sol.problemType === 'knowledge'
        ? `Context: ${sol.symptoms}\nGuidance: ${sol.solution}`
        : `Symptoms: ${sol.symptoms}\nFix: ${sol.solution}\nPrevention: ${sol.prevention}`;
    return `${header}\n${body}`;
  });
}
