import { defineConfig } from 'vitest/config';

// Package-local config so `vitest run` discovers tests under this package's
// src/ (the repo-root vitest.config.ts scopes include to the root's src/).
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
  },
});
