/**
 * Session-scoped dedup of bridge memory injections (LIA-355).
 *
 * The host bridge serves multiple groups and has no session identity, so dedup
 * happens client-side: filter the interior of the ALREADY-WRAPPED payload the
 * bridge returns. The container runs one session per process lifecycle
 * (index.ts module-state comment), so a module-level seen-set IS session scope.
 *
 * Trust-boundary rules (LIA-335): the parser anchors on the LITERAL random
 * sentinel token captured from the payload's FIRST line and requires an EXACT
 * match of that full sentinel on the closing line — never a generic pattern
 * match — so attacker-authored pseudo-sentinels inside stored memory bodies
 * cannot confuse the envelope. A forged block delimiter inside a body at worst
 * splits it into two blocks that both re-inject (fail-open, no loss).
 *
 * Fail-open everywhere: payload not matching the expected structure is
 * returned unfiltered. Dedup only ever REMOVES exact already-shown blocks.
 */

import { createHash } from 'crypto';

const SENTINEL_LINE_RE = /^<<<UNTRUSTED-MEMORY-[0-9a-f]{32}>>>$/;
const BLOCK_DELIM_RE = /^--- (.+) \(score: [0-9.]+\) ---$/;

/**
 * Key = path + body hash, mirroring the host's injection_dedup.block_key.
 * The score in the delimiter is per-query and MUST NOT enter the key — the
 * same file resurfacing on a later turn carries a different score, and a
 * score-bearing key would never match (dedup would silently never fire).
 */
function blockKey(delimiterLine: string, body: string): string {
  const match = delimiterLine.match(BLOCK_DELIM_RE);
  const path = match ? match[1] : delimiterLine;
  const digest = createHash('sha256').update(body, 'utf8').digest('hex');
  return `${path}:${digest.slice(0, 16)}`;
}

/**
 * Filter already-seen blocks out of a wrapped memory payload.
 *
 * Returns the filtered payload (sentinel envelope preserved verbatim), or ''
 * when every block was already seen. Returns the input UNCHANGED when the
 * payload doesn't match the expected sentinel/block structure, or when the
 * parsed blocks don't exactly correspond to the bridge's authoritative
 * `paths` list — a delimiter-shaped line INSIDE a stored body splits the
 * parse and makes the counts/paths mismatch, so forged delimiters degrade to
 * pass-through instead of ever suppressing fresh content.
 */
export function filterSeenBlocks(
  payload: string,
  seen: Set<string>,
  paths?: string[],
): string {
  const lines = payload.split('\n');
  // Expected wrapper shape (memory_query._wrap_untrusted):
  //   [0] framing header, [1] opening sentinel, ..., [n-2] closing sentinel,
  //   [n-1] end-of-memory footer.
  if (lines.length < 5) return payload;
  const openIdx = 1;
  const closeIdx = lines.length - 2;
  const sentinel = lines[openIdx];
  if (!SENTINEL_LINE_RE.test(sentinel)) return payload;
  // EXACT literal match of the captured token on the closing line — a forged
  // pseudo-sentinel with different hex cannot terminate the envelope early.
  if (lines[closeIdx] !== sentinel) return payload;

  const interior = lines.slice(openIdx + 1, closeIdx);
  // Split interior into blocks on delimiter lines.
  const blocks: { delim: string; body: string[] }[] = [];
  for (const line of interior) {
    if (BLOCK_DELIM_RE.test(line)) {
      blocks.push({ delim: line, body: [] });
    } else if (blocks.length > 0) {
      blocks[blocks.length - 1].body.push(line);
    } else {
      // Content before any delimiter (unexpected shape) — fail open.
      return payload;
    }
  }
  if (blocks.length === 0) return payload;

  // Integrity cross-check against the bridge's authoritative paths list: the
  // parsed block paths must match it exactly (count + order). A forged
  // delimiter inside a body breaks the correspondence → fail open.
  if (paths !== undefined) {
    if (blocks.length !== paths.length) return payload;
    for (let i = 0; i < blocks.length; i++) {
      const m = blocks[i].delim.match(BLOCK_DELIM_RE);
      if (!m || m[1] !== paths[i]) return payload;
    }
  }

  // A truncation marker in the last block means it may be partial — never mark
  // or filter a partial block (mark-only-what-survives, mirrored client-side).
  const fresh: { delim: string; body: string[]; partial: boolean }[] = [];
  let dropped = 0;
  blocks.forEach((b, i) => {
    const bodyText = b.body.join('\n');
    const partial =
      i === blocks.length - 1 && bodyText.includes('=== [truncated] ===');
    const key = blockKey(b.delim, bodyText);
    if (!partial && seen.has(key)) {
      dropped += 1;
      return;
    }
    if (!partial) seen.add(key);
    fresh.push({ ...b, partial });
  });

  if (fresh.length === 0) return '';
  if (dropped === 0) return payload;

  const rebuiltInterior = fresh.flatMap((b) => [b.delim, ...b.body]);
  return [
    ...lines.slice(0, openIdx + 1),
    ...rebuiltInterior,
    ...lines.slice(closeIdx),
  ].join('\n');
}

/** Module-level session seen-set (one session per container process). */
const sessionSeen = new Set<string>();

export function dedupEnabled(): boolean {
  return process.env.DEUS_MEMORY_DEDUP !== '0'; // LIA-355
}

export function dedupMemoryPayload(payload: string, paths?: string[]): string {
  if (!dedupEnabled()) return payload;
  return filterSeenBlocks(payload, sessionSeen, paths);
}

/** Test-only: reset the module-level session state. */
export function _resetSessionSeen(): void {
  sessionSeen.clear();
}
