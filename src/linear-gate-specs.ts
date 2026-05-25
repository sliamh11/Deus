import fs from 'fs';
import path from 'path';
import { extractFrontmatter } from './linear-dispatcher.js';
import { logger } from './logger.js';

export interface GateSpec {
  name: string;
  gateTo: string;
  allowedFrom: string[];
  mode: 'advise' | 'strict';
  fallback: 'REVISE';
  cooldownMinutes: number;
  content: string;
  model?: string;
  effort?: 'low' | 'medium' | 'high';
  fetchComments?: boolean;
  revertTo?: string;
  maxAttempts?: number;
}

export function loadGateSpecs(wardensDir: string): Map<string, GateSpec> {
  const specs = new Map<string, GateSpec>();
  if (!fs.existsSync(wardensDir)) return specs;

  const files = fs.readdirSync(wardensDir).filter((f) => f.endsWith('.md'));
  for (const file of files) {
    const raw = fs.readFileSync(path.join(wardensDir, file), 'utf-8');
    const { data, body } = extractFrontmatter(raw);

    const gateTo = typeof data.gate_to === 'string' ? data.gate_to : undefined;
    if (!gateTo) continue;

    const name =
      typeof data.name === 'string' ? data.name : file.replace('.md', '');

    const allowedFrom = Array.isArray(data.allowed_from)
      ? (data.allowed_from as unknown[]).filter(
          (v): v is string => typeof v === 'string',
        )
      : [];

    const mode = data.mode === 'strict' ? 'strict' : 'advise';

    if (data.fallback === 'SHIP') {
      logger.warn(
        { file, gate: gateTo },
        'gate spec declares fallback: SHIP (deprecated, overriding to REVISE)',
      );
    }
    const fallback = 'REVISE' as const;

    const rawCooldown =
      typeof data.cooldown_minutes === 'number' ? data.cooldown_minutes : 60;

    specs.set(gateTo, {
      name,
      gateTo,
      allowedFrom,
      mode,
      fallback,
      cooldownMinutes: rawCooldown,
      content: body.trim(),
      model: typeof data.model === 'string' ? data.model : undefined,
      effort: ['low', 'medium', 'high'].includes(data.effort as string)
        ? (data.effort as GateSpec['effort'])
        : undefined,
      fetchComments: data.fetch_comments === true,
      revertTo: typeof data.revert_to === 'string' ? data.revert_to : undefined,
      maxAttempts:
        typeof data.max_attempts === 'number' ? data.max_attempts : undefined,
    });
  }

  logger.debug(
    { gates: [...specs.keys()] },
    'linear-gate-specs: loaded gate specs',
  );
  return specs;
}
