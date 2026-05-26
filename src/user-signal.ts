/**
 * Detects explicit user feedback signals in short messages.
 *
 * When a user sends "perfect" or "wrong" as a follow-up, this signal is
 * passed to the evolution loop to generate a reflection for the previous
 * interaction in the same session.
 *
 * Only triggers on short messages (< 200 chars) to avoid false positives
 * in longer prompts that happen to contain these words.
 */

const POSITIVE_KEYWORDS = [
  'perfect',
  'exactly',
  'great job',
  'love it',
  "that's right",
  'thats right',
  'well done',
  'nailed it',
  'spot on',
  'that works',
  'looks good',
  'lgtm',
  'thanks',
  'thank you',
];

const NEGATIVE_KEYWORDS = [
  'wrong',
  'try again',
  'not what i wanted',
  "that's incorrect",
  'thats incorrect',
  'not right',
  'completely wrong',
  'no thats wrong',
  "no that's wrong",
  'not what i asked',
  'redo',
  'start over',
  'redo this',
];

const MAX_SIGNAL_LENGTH = 200;

export function detectUserSignal(
  prompt: string,
): 'positive' | 'negative' | null {
  if (prompt.length > MAX_SIGNAL_LENGTH) return null;

  const lower = prompt.toLowerCase().trim();

  for (const kw of NEGATIVE_KEYWORDS) {
    if (lower.includes(kw)) return 'negative';
  }
  for (const kw of POSITIVE_KEYWORDS) {
    if (lower.includes(kw)) return 'positive';
  }

  return null;
}
