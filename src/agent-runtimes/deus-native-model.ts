/**
 * Proxy-routed `ChatAnthropic` client construction for the `deus-native`
 * runtime (LIA-408 / B8 extraction).
 *
 * Moved unchanged (auth/routing logic byte-for-byte identical) from
 * `deus-native-backend.ts`'s prior `buildProxyRoutedChatAnthropic(runContext)`
 * — the ONLY model-construction path in this package before B8, hardcoded to
 * `model: 'claude-opus-4-8'`. B8 adds a `modelId` parameter so the same
 * credential-proxy route can build an INDEPENDENTLY selected client per
 * nested-dispatch child (LIA-408 AC4), while the parent keeps calling this
 * with `PARENT_DEFAULT_MODEL` unchanged. `resolveModel` (the nested-dispatch
 * seam) calls this factory fresh on every dispatch — it never inherits or
 * caches the parent's client.
 */

import { ChatAnthropic } from '@langchain/anthropic';
import Anthropic from '@anthropic-ai/sdk';

import type { RunContext } from './types.js';
import { PROXY_BIND_HOST } from '../container-runtime.js';
import { CREDENTIAL_PROXY_PORT } from '../config.js';
import { detectAuthMode } from '../credential-proxy.js';
import { getOrCreateGroupToken } from '../group-tokens.js';

/**
 * The parent's unchanged default model tier (was the hardcoded literal
 * inside the pre-B8 `buildProxyRoutedChatAnthropic`). Every parent call
 * still passes exactly this id — B8 introduces no parent-side model
 * selection, only child-side (AC4 is scoped to subagents).
 */
export const PARENT_DEFAULT_MODEL = 'claude-opus-4-8';

/**
 * Builds a ChatAnthropic client routed through the live credential proxy at
 * PROXY_BIND_HOST:CREDENTIAL_PROXY_PORT — NOT a hardcoded 127.0.0.1 (bare-
 * metal Linux binds the proxy to the docker0 bridge, not loopback; see
 * docs/decisions/deus-v2-langchain-runtime.md). Branches on detectAuthMode()
 * to build either an API-key-mode or OAuth-mode client via a `createClient`
 * override — same escape-hatch shape as A4's spike
 * (buildProxyRoutedChatAnthropic in
 * scripts/spikes/lia397_credential_proxy_billing_spike.ts), but branching on
 * the REAL auth mode rather than hardcoding OAuth like that spike does. This
 * is architecturally distinct from A4's spike: that spike deliberately used
 * an isolated, throwaway proxy child; deus-native hits the real production
 * daemon at its real bind address with a real per-group token.
 *
 * `modelId` (B8): every caller — parent (`PARENT_DEFAULT_MODEL`) and every
 * nested-dispatch child (the dispatch's requested `model` id) — supplies its
 * own value through this same single construction path. There is still only
 * ONE provider (Anthropic) and ONE credential-proxy route; only the model id
 * varies per call.
 */
export function buildProxyRoutedChatAnthropic(
  runContext: RunContext,
  modelId: string,
): ChatAnthropic {
  const baseURL = `http://${PROXY_BIND_HOST}:${CREDENTIAL_PROXY_PORT}`;
  const proxyToken = getOrCreateGroupToken(runContext.groupFolder);
  const authMode = detectAuthMode();

  return new ChatAnthropic({
    // B1: fixed at the top model tier for the parent (ai-eng-warden review:
    // runContext.effort, ContainerRuntime's per-turn tier signal, is not yet
    // honored here). B8: nested-dispatch children now pass their own
    // requested id through the same parameter instead of inheriting this
    // constant — see nested-dispatch.ts's `resolveModel` seam.
    model: modelId,
    createClient: (options) =>
      authMode === 'oauth'
        ? new Anthropic({
            baseURL: options.baseURL ?? baseURL,
            authToken: 'placeholder',
            apiKey: null,
            // Plain `authToken` never populates the SDK's own OAuth credential
            // state, so it never auto-appends this beta header — add it
            // explicitly, or the upstream OAuth-authenticated request can be
            // rejected (matches A4's own header-injection reasoning).
            defaultHeaders: {
              'anthropic-beta': 'oauth-2025-04-20',
              'x-deus-proxy-token': proxyToken,
            },
          })
        : new Anthropic({
            baseURL: options.baseURL ?? baseURL,
            apiKey: 'placeholder',
            defaultHeaders: {
              'x-deus-proxy-token': proxyToken,
            },
          }),
  });
}
