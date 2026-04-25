import { readEnvFile } from '../env.js';
import type { AuthProvider } from './types.js';

export class OpenAIAuthProvider implements AuthProvider {
  readonly name = 'openai';
  readonly priority = 20;
  readonly envKeys = ['OPENAI_API_KEY', 'OPENAI_BASE_URL'];

  private readonly secrets: Record<string, string>;

  constructor() {
    this.secrets = readEnvFile(this.envKeys);
  }

  isAvailable(): boolean {
    return typeof this.secrets.OPENAI_API_KEY === 'string';
  }

  getUpstreamUrl(): string {
    return this.secrets.OPENAI_BASE_URL || 'https://api.openai.com';
  }

  injectAuth(headers: Record<string, string | string[] | undefined>): void {
    delete headers['x-api-key'];
    delete headers.authorization;
    if (this.secrets.OPENAI_API_KEY) {
      headers.authorization = `Bearer ${this.secrets.OPENAI_API_KEY}`;
    }
  }
}
