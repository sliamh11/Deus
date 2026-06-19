import {
  Channel,
  OnInboundMessage,
  OnInboundReaction,
  OnChatMetadata,
  RegisteredGroup,
} from '../types.js';
import type { IngressHandler } from '../ingress/gateway.js';

export interface ChannelOpts {
  onMessage: OnInboundMessage;
  onReaction: OnInboundReaction;
  onChatMetadata: OnChatMetadata;
  registeredGroups: () => Record<string, RegisteredGroup>;
  /** LIA-315 Phase 4: provision a sandbox group (webhook channel registers its
   *  per-source publicIngress groups). Optional — only the webhook channel uses it. */
  registerGroup?: (jid: string, group: RegisteredGroup) => void;
  /** LIA-315 Phase 4: push an ingress route onto the shared gateway Strategy
   *  registry. Optional + present only when the ingress gateway is enabled. */
  registerIngressHandler?: (handler: IngressHandler) => void;
}

export type ChannelFactory = (opts: ChannelOpts) => Channel | null;

const registry = new Map<string, ChannelFactory>();

export function registerChannel(name: string, factory: ChannelFactory): void {
  registry.set(name, factory);
}

export function getChannelFactory(name: string): ChannelFactory | undefined {
  return registry.get(name);
}

export function getRegisteredChannelNames(): string[] {
  return [...registry.keys()];
}
