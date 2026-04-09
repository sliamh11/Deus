/**
 * Token estimation for the evolution loop.
 *
 * Uses a simple heuristic (Math.floor(text.length / 4)) — symmetric with
 * evolution/token_counter.py. Not tiktoken-accurate; for trend tracking only.
 */

export function estimateTokens(text: string): number {
  return Math.floor(text.length / 4);
}

export function sumTokens(...parts: string[]): number {
  return parts.reduce((acc, p) => acc + estimateTokens(p), 0);
}
