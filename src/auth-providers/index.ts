/**
 * Auth provider barrel export.
 *
 * Does NOT auto-register providers at import time — that would break test
 * mocks because readEnvFile would be called before vi.mock() takes effect.
 * Instead, call ensureDefaultProviders() to lazily register built-in providers.
 */

export {
  AuthProvider,
  AuthProviderRegistry,
  NoProviderAvailableError,
} from './types.js';
export {
  AnthropicAuthProvider,
  _resetCredentialsCacheForTest,
} from './anthropic.js';

import { AuthProviderRegistry } from './types.js';
import { AnthropicAuthProvider } from './anthropic.js';

/**
 * Ensure the default Anthropic provider is registered.
 * Safe to call multiple times — skips if already registered.
 */
export function ensureDefaultProviders(): void {
  const registry = AuthProviderRegistry.default();
  if (registry.listProviders().includes('anthropic')) return;
  registry.register(new AnthropicAuthProvider());
}
