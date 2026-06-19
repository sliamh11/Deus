// GitHub webhook handler for the centralized ingress gateway (the `/github` route).
//
// Replaces polling the GitHub API for CI/PR state with push: GitHub posts an event,
// we react. SECURITY (the endpoint is PUBLIC via ngrok — anyone can POST):
//   - verify() fails CLOSED on a bad/missing `X-Hub-Signature-256` HMAC (verifyHmacSha256)
//     and on a duplicate `X-GitHub-Delivery` (checkReplay) — the gateway 403s on {ok:false}.
//   - handle() NEVER trusts the payload as data: it extracts only the PR url, maps it to a
//     Linear issue, then RE-QUERIES authoritative state via the merge-only wrappers
//     (mergeIfGreen / markDoneIfMerged). Those cannot reach the agent-spawn ("Ready for Agent")
//     path, so a forged-but-signed event can neither merge a red PR nor spawn an agent.
//
// LIA-315 Phase 4 (GitHub source 0), scoped to merge-on-green + done-on-merge only.

import type { IncomingMessage, ServerResponse } from 'http';
import type { IngressHandler } from './gateway.js';
import { verifyHmacSha256, ReplayStore, checkReplay } from './hmac.js';
import type { ReplayConfig } from './hmac.js';
import { logger } from '../logger.js';
import type { LinearContext } from './../linear-dispatcher.js';

/** GitHub redeliveries arrive within minutes; a short TTL bounds the dedup store. */
const GITHUB_DELIVERY_TTL_MS = 10 * 60_000;

/** Only these event types do anything; everything else is a 204 no-op. */
const ALLOWED_EVENTS = new Set([
  'check_suite',
  'workflow_run',
  'pull_request',
  'ping',
]);

/** Minimal, optional-chained view of the GitHub payloads we read. */
interface GitHubPayload {
  action?: string;
  repository?: { full_name?: string };
  pull_request?: { number?: number; merged?: boolean };
  check_suite?: {
    conclusion?: string;
    pull_requests?: Array<{ number?: number }>;
  };
  workflow_run?: {
    conclusion?: string;
    pull_requests?: Array<{ number?: number }>;
  };
}

type MergeAction = (
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  identifier?: string,
) => Promise<void>;

export interface GitHubIngressDeps {
  ctx: LinearContext;
  secret: string;
  mergeIfGreen: MergeAction;
  markDoneIfMerged: MergeAction;
  getIssueByPrUrl: (
    prUrl: string,
  ) => { issue_id: string; identifier: string | null } | undefined;
}

function headerString(v: string | string[] | undefined): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

function writeJson(res: ServerResponse, status: number, body: unknown): void {
  if (!res.writable) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

/**
 * Reconstruct the canonical PR html url (`https://github.com/<owner>/<repo>/pull/<n>`) from
 * the event payload, to match the format stored in `linear_issue_prs` and parsed by the
 * re-query fns. Returns undefined when the event carries no PR (e.g. a check_suite with an
 * empty `pull_requests` array, common for fork PRs) — the handler then no-ops.
 */
function extractPrUrl(
  event: string,
  payload: GitHubPayload,
): string | undefined {
  const repo = payload.repository?.full_name;
  if (!repo) return undefined;
  let prNumber: number | undefined;
  if (event === 'pull_request') prNumber = payload.pull_request?.number;
  else if (event === 'check_suite')
    prNumber = payload.check_suite?.pull_requests?.[0]?.number;
  else if (event === 'workflow_run')
    prNumber = payload.workflow_run?.pull_requests?.[0]?.number;
  if (typeof prNumber !== 'number') return undefined;
  return `https://github.com/${repo}/pull/${prNumber}`;
}

/** True when a check_suite / workflow_run event reports a successful conclusion. */
function isCiSuccess(event: string, payload: GitHubPayload): boolean {
  if (event === 'check_suite')
    return payload.check_suite?.conclusion === 'success';
  if (event === 'workflow_run')
    return payload.workflow_run?.conclusion === 'success';
  return false;
}

export function createGitHubIngressHandler(
  deps: GitHubIngressDeps,
): IngressHandler {
  const { ctx, secret, mergeIfGreen, markDoneIfMerged, getIssueByPrUrl } = deps;
  // ReplayStore is process-lifetime only: a post-restart redelivery passes dedup. That is
  // acceptable because the merge-only actions are idempotent — re-querying authoritative
  // state and no-op'ing on an already-merged / not-green PR is the second layer.
  const replayStore = new ReplayStore(GITHUB_DELIVERY_TTL_MS);
  const replayCfg: ReplayConfig = {
    strategy: 'delivery-id',
    idHeader: 'X-GitHub-Delivery',
  };

  return {
    pathPrefix: '/github',

    verify(req: IncomingMessage, bodyRaw: Buffer) {
      const sig = verifyHmacSha256(
        secret,
        bodyRaw,
        headerString(req.headers['x-hub-signature-256']),
      );
      if (!sig.ok) return sig;
      return checkReplay(replayCfg, req.headers, replayStore, Date.now());
    },

    async handle(req: IncomingMessage, res: ServerResponse, bodyRaw: Buffer) {
      const event = headerString(req.headers['x-github-event']);
      if (!event || !ALLOWED_EVENTS.has(event)) {
        writeJson(res, 204, { ok: true, ignored: 'event' });
        return;
      }
      if (event === 'ping') {
        writeJson(res, 200, { ok: true });
        return;
      }

      let payload: GitHubPayload;
      try {
        payload = JSON.parse(bodyRaw.toString('utf-8')) as GitHubPayload;
      } catch {
        writeJson(res, 400, { ok: false, message: 'invalid json' });
        return;
      }

      // Decide the action from the payload BEFORE any DB I/O, so a non-actionable event
      // (e.g. a non-success check_suite — the common case) is a pure no-op with zero I/O on
      // this public hot path.
      const wantsMerge =
        (event === 'check_suite' || event === 'workflow_run') &&
        isCiSuccess(event, payload);
      const wantsDone =
        event === 'pull_request' &&
        payload.action === 'closed' &&
        payload.pull_request?.merged === true;
      if (!wantsMerge && !wantsDone) {
        writeJson(res, 204, { ok: true, ignored: 'no-action' });
        return;
      }

      const prUrl = extractPrUrl(event, payload);
      const issue = prUrl ? getIssueByPrUrl(prUrl) : undefined;
      if (!prUrl || !issue) {
        // Unknown / untracked PR — safe no-op.
        writeJson(res, 204, { ok: true, ignored: 'pr' });
        return;
      }
      const ident = issue.identifier ?? undefined;

      // Re-query authoritative state inside the merge-only wrappers; never trust the payload.
      if (wantsMerge) {
        mergeIfGreen(ctx, issue.issue_id, prUrl, ident).catch((err) =>
          logger.error({ err, prUrl }, 'github-webhook: mergeIfGreen failed'),
        );
      } else {
        markDoneIfMerged(ctx, issue.issue_id, prUrl, ident).catch((err) =>
          logger.error(
            { err, prUrl },
            'github-webhook: markDoneIfMerged failed',
          ),
        );
      }

      // Fast 2xx — any host action above is fire-and-forget.
      writeJson(res, 200, { ok: true });
    },
  };
}
