import {
  AnthropicAuthProvider,
  AuthProviderRegistry,
  ensureDefaultProviders,
} from '../../src/auth-providers/index.js';
import { startCredentialProxy } from '../../src/credential-proxy.js';
import { checkCredentialFreshness } from './lia397_credential_proxy_billing_spike.js';

async function main(): Promise<void> {
  ensureDefaultProviders();
  const port = Number(process.env.SPIKE_PROXY_PORT);
  const provider = AuthProviderRegistry.default().get('anthropic');
  if (!(provider instanceof AnthropicAuthProvider)) {
    throw new Error('expected AnthropicAuthProvider');
  }

  const authMode = provider.getAuthMode();
  const usesRefreshableOAuth = provider.usesRefreshableOAuth();

  if (usesRefreshableOAuth) {
    // Refuse before proxy startup so no refresh-capable process can race the live host over the shared credential file.
    const freshness = checkCredentialFreshness();
    if (!freshness.safe) {
      console.log(`UNSAFE:${freshness.reason}`);
      // Natural event-loop drainage guarantees the piped safety verdict reaches the parent before exit.
      process.exitCode = 1;
      return;
    }
  }

  await startCredentialProxy(port);
  console.log(`LISTENING:${port}`);
  console.log(`AUTH_MODE:${authMode}`);
  console.log(`REFRESHABLE:${usesRefreshableOAuth}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
