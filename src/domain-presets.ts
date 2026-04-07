/**
 * Lightweight domain detection for the evolution loop.
 *
 * Tags interactions with domain labels (e.g. "engineering", "marketing") so the
 * evolution loop can filter, extract principles, and optimize per domain.
 *
 * Detection is keyword-based (zero API cost, sub-millisecond).
 * No content is injected into the agent prompt — domains are metadata only.
 *
 * V2 adds:
 * - LLM fallback via Gemini when keyword detection returns no results
 * - DEUS_CUSTOM_DOMAINS env var for user-defined domains beyond the 5 built-ins
 */

import { execFile } from 'child_process';
import path from 'path';

import { IS_WINDOWS } from './platform.js';

/** Minimum keyword hits to tag a domain. */
const MIN_KEYWORD_HITS = 2;

/** LLM fallback hard timeout in milliseconds. Container spawn must not be blocked. */
const FALLBACK_TIMEOUT_MS = 3000;

interface DomainDef {
  name: string;
  keywords: string[];
}

/**
 * Domain definitions with associated keywords.
 * Used purely for tagging interactions — no prompt injection.
 */
const DOMAINS: DomainDef[] = [
  {
    name: 'engineering',
    keywords: [
      'code',
      'bug',
      'debug',
      'refactor',
      'api',
      'database',
      'deploy',
      'ci',
      'cd',
      'test',
      'architecture',
      'performance',
      'latency',
      'backend',
      'frontend',
      'infrastructure',
      'docker',
      'container',
      'microservice',
      'endpoint',
    ],
  },
  {
    name: 'marketing',
    keywords: [
      'marketing',
      'seo',
      'brand',
      'campaign',
      'funnel',
      'conversion',
      'audience',
      'cta',
      'social media',
      'content strategy',
      'advertising',
      'growth',
      'retention',
      'engagement',
      'analytics',
      'a/b test',
    ],
  },
  {
    name: 'strategy',
    keywords: [
      'strategy',
      'decision',
      'trade-off',
      'prioritize',
      'roadmap',
      'okr',
      'goal',
      'plan',
      'risk',
      'stakeholder',
      'initiative',
      'milestone',
      'competitive',
      'market',
      'business model',
      'pivot',
      'scaling',
      'resource allocation',
    ],
  },
  {
    name: 'study',
    keywords: [
      'study',
      'exam',
      'homework',
      'assignment',
      'course',
      'lecture',
      'university',
      'college',
      'quiz',
      'problem set',
      'textbook',
      'chapter',
      'theorem',
      'proof',
      'equation',
      'formula',
      'physics',
      'math',
      'calculus',
      'linear algebra',
    ],
  },
  {
    name: 'writing',
    keywords: [
      'draft',
      'essay',
      'article',
      'blog',
      'copy',
      'edit',
      'proofread',
      'tone',
      'narrative',
      'outline',
      'paragraph',
      'headline',
      'storytelling',
      'documentation',
      'email',
      'proposal',
    ],
  },
];

/**
 * Parse DEUS_CUSTOM_DOMAINS env var into an array of trimmed lowercase names.
 * Returns [] when the env var is unset or empty.
 *
 * Custom domains participate in LLM classification and all downstream evolution
 * features (principles extraction, DSPy optimization).
 *
 * Example: DEUS_CUSTOM_DOMAINS=legal,finance,health
 */
export function parseCustomDomains(): string[] {
  const raw = process.env.DEUS_CUSTOM_DOMAINS ?? '';
  if (!raw.trim()) return [];
  return raw
    .split(',')
    .map((d) => d.trim().toLowerCase())
    .filter(Boolean);
}

/**
 * Return all known domain names: built-in domains plus any custom ones.
 */
export function getAllDomainNames(): string[] {
  return [...DOMAINS.map((d) => d.name), ...parseCustomDomains()];
}

/**
 * Detect which domains match the given prompt.
 *
 * Returns domain names for evolution loop tagging.
 * A domain activates when MIN_KEYWORD_HITS distinct keywords are found.
 */
export function detectDomains(prompt: string): string[] {
  const lowerPrompt = prompt.toLowerCase();
  const matched: string[] = [];

  for (const domain of DOMAINS) {
    const hits = domain.keywords.filter((kw) => lowerPrompt.includes(kw));
    if (hits.length >= MIN_KEYWORD_HITS) {
      matched.push(domain.name);
    }
  }

  return matched;
}

/**
 * Call the evolution layer's Gemini generative provider to classify a prompt.
 *
 * Uses a tight prompt that returns a JSON array of domain names.
 * Executed via a Python subprocess so we reuse the existing provider stack
 * (API key loading, model fallback, etc.) without duplicating Gemini auth in TS.
 *
 * Returns [] on any failure — caller must never rely on this for correctness.
 */
async function classifyWithLLM(
  prompt: string,
  allDomains: string[],
): Promise<string[]> {
  // Resolve project root relative to this file's compiled location (dist/...)
  // path.resolve works both from src/ (tsx dev) and dist/ (compiled).
  const projectRoot = path.resolve(
    path.dirname(new URL(import.meta.url).pathname),
    '..',
  );

  const domainList = allDomains.join(', ');
  // Build the classification prompt; encode as JSON string to survive shell escaping.
  const classifyPrompt =
    `Classify this prompt into zero or more domains: ${domainList}. ` +
    `Return only matching domain names as a JSON array. If none fit, return []. ` +
    `Prompt: ${prompt}`;

  // Embed valid domains as a JSON literal inside the Python script so we can
  // validate the response without shell-level quoting issues.
  const validDomainsJson = JSON.stringify(allDomains);
  const classifyPromptJson = JSON.stringify(classifyPrompt);

  const pyScript = `
import sys, json, os, re
_root = ${JSON.stringify(projectRoot)}
if _root not in sys.path:
    sys.path.insert(0, _root)
try:
    from evolution.generative.providers.gemini import GeminiGenerativeProvider
    from evolution.config import GEN_MODELS
    provider = GeminiGenerativeProvider()
    if not provider.is_available():
        print('[]')
        sys.exit(0)
    user_prompt = ${classifyPromptJson}
    result = provider.generate(user_prompt, model=GEN_MODELS[-1])
    m = re.search(r'\\[.*?\\]', result, re.DOTALL)
    if not m:
        print('[]')
        sys.exit(0)
    candidates = json.loads(m.group())
    valid = set(${validDomainsJson})
    domains = [d.lower().strip() for d in candidates if isinstance(d, str) and d.lower().strip() in valid]
    print(json.dumps(domains))
except Exception:
    print('[]')
`;

  return new Promise((resolve) => {
    const py = IS_WINDOWS ? 'python' : 'python3';
    let settled = false;

    const settle = (result: string[]) => {
      if (!settled) {
        settled = true;
        resolve(result);
      }
    };

    // Belt-and-suspenders timeout independent of execFile's own timeout
    const guard = setTimeout(() => {
      proc.kill();
      settle([]);
    }, FALLBACK_TIMEOUT_MS + 200);

    const proc = execFile(
      py,
      ['-c', pyScript],
      { timeout: FALLBACK_TIMEOUT_MS, cwd: projectRoot },
      (err, stdout) => {
        clearTimeout(guard);
        if (err) {
          settle([]);
          return;
        }
        try {
          const lines = stdout.trim().split('\n');
          const lastLine = lines[lines.length - 1].trim();
          const parsed: unknown = JSON.parse(lastLine);
          if (
            Array.isArray(parsed) &&
            parsed.every((x) => typeof x === 'string')
          ) {
            settle(parsed as string[]);
          } else {
            settle([]);
          }
        } catch {
          settle([]);
        }
      },
    );
  });
}

/**
 * Detect domains with an LLM fallback for unrecognised prompts.
 *
 * Algorithm:
 *   1. Run keyword detection (synchronous, zero cost). Return immediately if
 *      any domains match — the LLM is never called on the hot path.
 *   2. If no keywords match, call Gemini with a tight classification prompt.
 *      The call is bounded to FALLBACK_TIMEOUT_MS (3 s) and always resolves.
 *   3. On any error (timeout, parse failure, API down), return [] gracefully —
 *      domain detection must never block or crash container spawn.
 *
 * Custom domains from DEUS_CUSTOM_DOMAINS are included in the LLM prompt and
 * participate in all downstream evolution features.
 */
export async function detectDomainsWithFallback(
  prompt: string,
): Promise<string[]> {
  // Fast path — pure keyword detection, synchronous and zero-cost
  const keywordResult = detectDomains(prompt);
  if (keywordResult.length > 0) {
    return keywordResult;
  }

  // Slow path — LLM fallback; wrapped to guarantee no throws reach the caller
  try {
    const allDomains = getAllDomainNames();
    const llmResult = await classifyWithLLM(prompt, allDomains);
    // Validate at the TypeScript level as a defence-in-depth measure — the Python
    // script also filters, but this ensures correctness even in tests or when the
    // subprocess is bypassed.
    const validSet = new Set(allDomains);
    return llmResult.filter((d) => validSet.has(d));
  } catch {
    return [];
  }
}
