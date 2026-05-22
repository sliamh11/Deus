import { describe, it, expect } from 'vitest';
import { buildPipelineCommentBody } from './linear-notifications.js';

describe('buildPipelineCommentBody', () => {
  it('renders empty state', () => {
    const body = buildPipelineCommentBody([]);
    expect(body).toContain('No events yet');
  });

  it('renders timeline from events', () => {
    const events = [
      {
        event_type: 'gate_ship',
        detail: 'agent-readiness-gate: SHIP',
        created_at: '2026-05-23T00:18:00.000Z',
      },
      {
        event_type: 'agent_dispatched',
        detail: null,
        created_at: '2026-05-23T00:19:00.000Z',
      },
      {
        event_type: 'pr_created',
        detail: '#488',
        created_at: '2026-05-23T00:24:00.000Z',
      },
    ];
    const body = buildPipelineCommentBody(events);
    expect(body).toContain('**Pipeline Log**');
    expect(body).toContain('00:18');
    expect(body).toContain('Gate → SHIP');
    expect(body).toContain('Agent dispatched');
    expect(body).toContain('PR created');
    expect(body).toContain('#488');
  });

  it('uses event_type as label for unknown types', () => {
    const events = [
      {
        event_type: 'custom_event',
        detail: null,
        created_at: '2026-05-23T01:00:00.000Z',
      },
    ];
    const body = buildPipelineCommentBody(events);
    expect(body).toContain('custom_event');
  });
});
