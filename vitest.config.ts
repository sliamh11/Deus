import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: [
      'src/**/*.test.ts',
      'setup/**/*.test.ts',
      'scripts/spikes/**/*.test.ts',
      // deus-v2-cmd.mjs (LIA-434) lives at repo root, mirroring deus-cmd.sh /
      // scripts/migrate.mjs's build-free top-level placement — its test needs
      // an explicit entry since it's outside the globs above.
      'deus-v2-cmd.test.ts',
    ],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary', 'lcov'],
      include: ['src/**/*.ts'],
      exclude: ['src/**/*.test.ts', 'src/**/*.d.ts'],
    },
  },
});
