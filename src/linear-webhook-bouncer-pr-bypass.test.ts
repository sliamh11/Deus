/**
 * LIA-119: Bouncer-gate open-PR bypass tests.
 *
 * Drives _handleIssueUpdateForTest (the real handleIssueUpdate entry) with
 * module-level mocks so that:
 *   - getIssuePr  returns a real PR record from the DB layer stub
 *   - queryPrState returns a controlled live-GH state
 *   - executeAgentRun (the LLM path) is spied — never called on OPEN/MERGED
 *
 * Intentional CLOSED-PR fall-through: a CLOSED (abandoned) PR must NOT be
 * caught by this bypass. Work was abandoned; a fresh bouncer eval should
 * decide whether to re-dispatch. The no-assertion on CLOSED below documents
 * this: the bouncer LLM IS called (executeAgentRun spy is invoked).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { _initTestDatabase, insertWebhookEvent } from './db.js';
import type { EntityWebhookPayloadWithIssueData } from '@linear/sdk/webhooks';
import type { LinearContext } from './linear-dispatcher.js';
import type { GateSpec } from './linear-gate-specs.js';

// ── Module-level mocks ────────────────────────────────────────────────────────
// Must be declared before any imports that consume these modules.

vi.mock('./db.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./db.js')>();
  return {
    ...actual,
    // getIssuePr is overridden per-test via mockReturnValue
    getIssuePr: vi.fn(),
  };
});

vi.mock('./linear-auto-merge.js', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('./linear-auto-merge.js')>();
  return {
    ...actual,
    queryPrState: vi.fn(),
    triggerAutoMerge: vi.fn().mockResolvedValue(undefined),
  };
});

vi.mock('./linear-dispatcher.js', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('./linear-dispatcher.js')>();
  return {
    ...actual,
    executeAgentRun: vi.fn().mockResolvedValue({ text: '', error: '' }),
  };
});

// ── Import units under test AFTER vi.mock declarations ────────────────────────

const { getIssuePr } = await import('./db.js');
const { queryPrState } = await import('./linear-auto-merge.js');
const { executeAgentRun } = await import('./linear-dispatcher.js');
const { _handleIssueUpdateForTest } = await import('./linear-webhook.js');

// ── Test helpers ──────────────────────────────────────────────────────────────

const TO_STATE_ID = 'rfa-state-id';
const FROM_STATE_ID = 'todo-state-id';
const ISSUE_ID = 'issue-lia-119';
const PR_URL = 'https://github.com/owner/repo/pull/613';

function makeBouncerGateSpec(): GateSpec {
  return {
    name: 'bouncer-gate',
    gateTo: 'Ready for Agent',
    allowedFrom: [], // no restriction
    mode: 'advise',
    fallback: 'REVISE',
    cooldownMinutes: 0, // disable cooldown so it doesn't interfere
    content: 'gate content',
  };
}

function makeCtx(): LinearContext {
  const stateById = new Map([
    [
      TO_STATE_ID,
      { id: TO_STATE_ID, name: 'Ready for Agent', type: 'unstarted' },
    ],
    [FROM_STATE_ID, { id: FROM_STATE_ID, name: 'In Review', type: 'started' }],
  ]);
  const stateByName = new Map([
    [
      'Ready for Agent',
      { id: TO_STATE_ID, name: 'Ready for Agent', type: 'unstarted' },
    ],
    ['In Review', { id: FROM_STATE_ID, name: 'In Review', type: 'started' }],
  ]);
  return {
    stateById,
    stateByName,
    botUserId: 'bot-user-id',
    viewerId: 'human-user-id',
    inFlightGate: new Set<string>(),
    inFlightDispatch: new Set<string>(),
    gateLabels: {
      evaluating: null,
      revise: null,
      error: '',
      bouncedUnscoped: null,
      bouncedStale: null,
      bouncedNoContext: null,
      wardenSkip: null,
      effort: {},
      complexity: {},
    },
    teamId: 'team-1',
    repoSlug: 'owner/repo',
    vaultPath: null,
    client: {
      updateIssue: vi.fn().mockResolvedValue(undefined),
      createComment: vi.fn().mockResolvedValue({ id: 'comment-1' }),
    } as unknown as LinearContext['client'],
    bus: { emit: vi.fn(), on: vi.fn() } as unknown as LinearContext['bus'],
    deps: {} as LinearContext['deps'],
    dispatchGroup: {} as LinearContext['dispatchGroup'],
  } as unknown as LinearContext;
}

function makePayload(
  issueId: string = ISSUE_ID,
): EntityWebhookPayloadWithIssueData {
  return {
    action: 'update',
    actor: { id: 'human-user-id' }, // not the bot
    webhookTimestamp: Date.now(),
    updatedFrom: { stateId: FROM_STATE_ID },
    data: {
      id: issueId,
      identifier: 'LIA-119',
      title: 'Test issue with open PR',
      description: 'Some description',
      stateId: TO_STATE_ID,
      labels: [],
    },
  } as unknown as EntityWebhookPayloadWithIssueData;
}

function makeGateSpecs(): Map<string, GateSpec> {
  return new Map([['Ready for Agent', makeBouncerGateSpec()]]);
}

// ── Tests ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  _initTestDatabase();
  vi.clearAllMocks();
});

describe('bouncer-gate open-PR bypass (LIA-119)', () => {
  it('calls queryPrState with the PR URL from getIssuePr, skips LLM on OPEN', async () => {
    vi.mocked(getIssuePr).mockReturnValue({
      pr_url: PR_URL,
      branch: 'feat/lia-119',
      auto_merge_state: 'none',
    });
    vi.mocked(queryPrState).mockResolvedValue({ state: 'OPEN' });

    await _handleIssueUpdateForTest(makePayload(), makeCtx(), makeGateSpecs());

    // (a) queryPrState was called with the URL from getIssuePr
    expect(queryPrState).toHaveBeenCalledWith(PR_URL);
    // (b) the LLM path was never invoked
    expect(executeAgentRun).not.toHaveBeenCalled();
  });

  it('skips LLM eval on MERGED PR', async () => {
    vi.mocked(getIssuePr).mockReturnValue({
      pr_url: PR_URL,
      branch: 'feat/lia-119',
      auto_merge_state: 'merged',
    });
    vi.mocked(queryPrState).mockResolvedValue({ state: 'MERGED' });

    await _handleIssueUpdateForTest(makePayload(), makeCtx(), makeGateSpecs());

    expect(queryPrState).toHaveBeenCalledWith(PR_URL);
    expect(executeAgentRun).not.toHaveBeenCalled();
  });

  it('falls through to normal eval when no PR record exists (regression: fresh issue)', async () => {
    // No PR in DB — getIssuePr returns undefined
    vi.mocked(getIssuePr).mockReturnValue(undefined);
    // executeAgentRun would be called by the full gate path; mock it to avoid
    // actually running an LLM or erroring on missing infra
    vi.mocked(executeAgentRun).mockResolvedValue({
      text: '## Verdict: SHIP\n',
      error: '',
    });

    await _handleIssueUpdateForTest(makePayload(), makeCtx(), makeGateSpecs());

    // queryPrState must NOT be called when there is no PR record
    expect(queryPrState).not.toHaveBeenCalled();
    // The LLM path WAS reached (normal bouncer eval runs on fresh issues)
    expect(executeAgentRun).toHaveBeenCalled();
  });

  // Intentional CLOSED-PR fall-through: a closed (abandoned) PR means the
  // previous work was superseded. The bouncer should re-evaluate whether the
  // issue is ready for a fresh agent run — so the LLM path must still fire.
  it('falls through to normal eval when PR state is CLOSED (abandoned work)', async () => {
    vi.mocked(getIssuePr).mockReturnValue({
      pr_url: PR_URL,
      branch: 'feat/lia-119',
      auto_merge_state: 'failed',
    });
    vi.mocked(queryPrState).mockResolvedValue({ state: 'CLOSED' });
    vi.mocked(executeAgentRun).mockResolvedValue({
      text: '## Verdict: SHIP\n',
      error: '',
    });

    await _handleIssueUpdateForTest(makePayload(), makeCtx(), makeGateSpecs());

    expect(queryPrState).toHaveBeenCalledWith(PR_URL);
    // CLOSED → bypass did NOT fire → LLM path ran
    expect(executeAgentRun).toHaveBeenCalled();
  });
});
