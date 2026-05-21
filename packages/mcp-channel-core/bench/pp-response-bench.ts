/**
 * Benchmark: mcpResponse compact/select payload reduction on synthetic fixtures.
 *
 * Run manually with:
 *   npx tsx packages/mcp-channel-core/bench/pp-response-bench.ts
 *
 * Lives outside src/ so:
 *   - tsc (build) does NOT compile it into dist/
 *   - vitest does NOT discover it as a test
 *
 * Fixtures are synthetic (representative field counts), not real user data.
 * Output is a markdown table of bytes per variant — captured into the
 * "Empirical findings" section of docs/decisions/printing-press-adoption.md.
 */

import { mcpResponse } from '../src/response.js';

// ── Fixtures (synthetic; representative shapes) ──────────────────────────────

/** Synthetic Google Calendar event with the 11 fields the gcal MCP returns. */
const gcalEvent = {
  id: 'evt_a8f3c2b1e9d4',
  summary: 'Team sync — weekly engineering review',
  start_time: '2026-05-17T14:00:00Z',
  end_time: '2026-05-17T15:00:00Z',
  location: 'Office HQ, Conference Room 3, 4th floor',
  description:
    'Weekly engineering review. Agenda: roadmap progress, blockers, on-call rotation, infra updates. Bring laptops and any prep notes.',
  html_link:
    'https://www.google.com/calendar/event?eid=fakebase64encodedid_x9z',
  organizer: 'alice@example.com',
  status: 'confirmed',
  updated_at: '2026-05-15T09:42:18.000Z',
  synced_at: '2026-05-16T12:00:00.000Z',
};

/** List of 10 such events (representative for list_events / search_events). */
const gcalEventList = Array.from({ length: 10 }, (_, i) => ({
  ...gcalEvent,
  id: `evt_${i.toString(16).padStart(12, '0')}`,
  summary: `${gcalEvent.summary} #${i + 1}`,
}));

/** Synthetic Gmail thread/email with 8 fields. */
const gmailMessage = {
  id: '198f3c2b1e9d4a8f',
  thread_id: '198f3c2b1e9d4a8f',
  from: 'sender@example.com',
  subject: 'Re: Q2 planning — proposed roadmap revisions',
  snippet:
    'Thanks for the detailed proposal. I have a few thoughts on the data-layer changes — see inline below. The biggest risk I see is...',
  date: '2026-05-16T08:23:11.000Z',
  labels: ['INBOX', 'IMPORTANT', 'CATEGORY_PERSONAL'],
  body: 'Hi team,\n\nThanks for the detailed proposal. I have a few thoughts on the data-layer changes — see inline below. The biggest risk I see is around the cache-key dimension: if we project on field subsets, the cache hit-rate calculation changes substantially. Suggest we benchmark before committing.\n\nBest,\nAlice'.repeat(
    8,
  ),
};

const gmailList = Array.from({ length: 10 }, (_, i) => ({
  ...gmailMessage,
  id: `msg_${i.toString(16).padStart(12, '0')}`,
}));

// ── Measurement ──────────────────────────────────────────────────────────────

interface Row {
  fixture: string;
  variant: string;
  bytes: number;
  ratio: number;
}

function bytesOf(result: { content: { text: string }[] }): number {
  return result.content[0].text.length;
}

function measure(name: string, fixture: unknown, select: string): Row[] {
  const raw = bytesOf(mcpResponse(fixture));
  const compact = bytesOf(mcpResponse(fixture, { compact: true }));
  const sel = bytesOf(mcpResponse(fixture, { select }));
  const both = bytesOf(mcpResponse(fixture, { compact: true, select }));
  return [
    { fixture: name, variant: 'raw', bytes: raw, ratio: 1.0 },
    {
      fixture: name,
      variant: 'compact only',
      bytes: compact,
      ratio: compact / raw,
    },
    { fixture: name, variant: 'select only', bytes: sel, ratio: sel / raw },
    {
      fixture: name,
      variant: 'compact + select',
      bytes: both,
      ratio: both / raw,
    },
  ];
}

const rows: Row[] = [
  ...measure('gcal single event', gcalEvent, 'id,start_time,summary,location'),
  ...measure('gcal list (10 events)', gcalEventList, 'id,start_time,summary'),
  ...measure(
    'gmail single message',
    gmailMessage,
    'id,from,subject,snippet,date',
  ),
  ...measure('gmail list (10 messages)', gmailList, 'id,from,subject,snippet'),
];

// ── Output ───────────────────────────────────────────────────────────────────

function pct(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

console.log('| Fixture | Variant | Bytes | vs raw |');
console.log('|---------|---------|-------|--------|');
for (const r of rows) {
  console.log(`| ${r.fixture} | ${r.variant} | ${r.bytes} | ${pct(r.ratio)} |`);
}

console.log('\nNotes:');
console.log(
  '- "select" projection cuts wide-record payloads dramatically when only a few fields matter to the caller.',
);
console.log(
  '- "compact" alone strips nulls and truncates long strings at 300 chars (default).',
);
console.log(
  '- The bytes column is the length of the JSON string emitted in the MCP text content — i.e., what crosses the wire to the agent.',
);
