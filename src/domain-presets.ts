/**
 * Lightweight domain detection for the evolution loop.
 *
 * Tags interactions with domain labels (e.g. "engineering", "marketing") so the
 * evolution loop can filter, extract principles, and optimize per domain.
 *
 * Detection is keyword-based (zero API cost, sub-millisecond).
 * No content is injected into the agent prompt — domains are metadata only.
 */

/** Minimum keyword hits to tag a domain. */
const MIN_KEYWORD_HITS = 2;

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
