/**
 * AuthProvider interface and registry for credential proxy backends.
 *
 * Mirrors the provider/registry pattern from evolution/judge/provider.py.
 * Each API provider (Anthropic, OpenAI, Gemini) implements AuthProvider and
 * registers with the singleton AuthProviderRegistry.
 */

/**
 * A backend that can inject credentials into outbound API requests.
 *
 * Subclass for each provider (Anthropic, OpenAI, Gemini, etc.).
 */
export interface AuthProvider {
  /** Unique provider name: 'anthropic', 'openai', 'gemini' */
  readonly name: string;

  /** Lower = preferred for auto-detection */
  readonly priority: number;

  /** Can this provider serve requests? (has credentials configured) */
  isAvailable(): boolean;

  /** The upstream base URL for this provider's API */
  getUpstreamUrl(): string;

  /**
   * Inject auth credentials into outbound request headers.
   * Mutates the headers object in place.
   */
  injectAuth(headers: Record<string, string | string[] | undefined>): void;

  /** Env var keys this provider reads (for documentation/startup checks) */
  readonly envKeys: string[];
}

export class NoProviderAvailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NoProviderAvailableError';
  }
}

/**
 * Central registry of auth providers.
 *
 * Usage:
 *   const registry = AuthProviderRegistry.default();
 *   const provider = registry.resolve();            // auto-detect best
 *   const provider = registry.resolve('anthropic'); // explicit choice
 *   provider.injectAuth(headers);
 */
export class AuthProviderRegistry {
  private static instance: AuthProviderRegistry | null = null;
  private providers = new Map<string, AuthProvider>();

  /** Return the singleton registry, creating it on first call. */
  static default(): AuthProviderRegistry {
    if (!AuthProviderRegistry.instance) {
      AuthProviderRegistry.instance = new AuthProviderRegistry();
    }
    return AuthProviderRegistry.instance;
  }

  /** Reset singleton — for testing only. */
  static reset(): void {
    AuthProviderRegistry.instance = null;
  }

  /** Register a provider. Last-write-wins for same name. */
  register(provider: AuthProvider): void {
    this.providers.set(provider.name, provider);
  }

  /** Remove a provider by name. */
  unregister(name: string): void {
    this.providers.delete(name);
  }

  /** Get a provider by exact name. Throws if not found. */
  get(name: string): AuthProvider {
    const provider = this.providers.get(name);
    if (!provider) {
      throw new NoProviderAvailableError(
        `Provider '${name}' not registered. Available: ${this.listProviders().join(', ')}`,
      );
    }
    return provider;
  }

  /** Return registered provider names sorted by priority. */
  listProviders(): string[] {
    return [...this.providers.values()]
      .sort((a, b) => a.priority - b.priority)
      .map((p) => p.name);
  }

  /** Return only available provider names sorted by priority. */
  listAvailable(): string[] {
    return [...this.providers.values()]
      .filter((p) => p.isAvailable())
      .sort((a, b) => a.priority - b.priority)
      .map((p) => p.name);
  }

  /**
   * Resolve the best available provider.
   *
   * Resolution order:
   * 1. DEUS_AUTH_PROVIDER env var (if set)
   * 2. Explicit preference argument
   * 3. Auto-detect: lowest priority number among available providers
   *
   * Throws NoProviderAvailableError if nothing works.
   */
  resolve(preference?: string): AuthProvider {
    const envPref = (process.env.DEUS_AUTH_PROVIDER || '').toLowerCase();
    const effective = envPref || (preference ? preference.toLowerCase() : '');

    // Explicit preference
    if (effective) {
      const provider = this.providers.get(effective);
      if (!provider) {
        throw new NoProviderAvailableError(
          `Provider '${effective}' not registered. Available: ${this.listProviders().join(', ')}`,
        );
      }
      if (!provider.isAvailable()) {
        throw new NoProviderAvailableError(
          `Provider '${effective}' is registered but not available.`,
        );
      }
      return provider;
    }

    // Auto-detect by priority
    const candidates = [...this.providers.values()].sort(
      (a, b) => a.priority - b.priority,
    );
    for (const provider of candidates) {
      if (provider.isAvailable()) {
        return provider;
      }
    }

    throw new NoProviderAvailableError(
      `No auth provider available. Registered: ${this.listProviders().join(', ')}`,
    );
  }
}
