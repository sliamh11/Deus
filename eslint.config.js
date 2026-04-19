import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import prettier from "eslint-config-prettier";

export default tseslint.config(
  {
    // Note: `container/` and `packages/` source is no longer globally ignored —
    // the no-process-exit rule below needs to fire on them. Only build output
    // and irrelevant trees stay globally ignored.
    ignores: [
      "dist/",
      "node_modules/",
      "eval/",
      "evolution/",
      "container/**/dist/",
      "container/**/node_modules/",
      "packages/**/dist/",
      "packages/**/node_modules/",
    ],
  },
  {
    files: ["src/**/*.ts"],
    extends: [eslint.configs.recommended, ...tseslint.configs.recommended],
    rules: {
      // Downgraded to warn — existing codebase has many instances.
      // New code should avoid these; they'll be tightened to errors over time.
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "no-empty": "warn",
      "@typescript-eslint/no-unused-expressions": "warn",
      "preserve-caught-error": "warn",
    },
  },
  // ── Cross-platform enforcement (ADR: platform-abstraction-layer) ──────
  // All OS-sensitive calls must go through src/platform.ts.
  // See docs/decisions/platform-abstraction-layer.md
  {
    files: ["src/**/*.ts"],
    ignores: ["src/platform.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "MemberExpression[object.name='process'][property.name='platform']",
          message:
            "Use IS_WINDOWS/IS_LINUX/IS_MACOS from src/platform.ts instead of process.platform (ADR: platform-abstraction-layer)",
        },
        {
          selector:
            "CallExpression[callee.object.name='os'][callee.property.name='platform']",
          message:
            "Use IS_WINDOWS/IS_LINUX/IS_MACOS from src/platform.ts instead of os.platform() (ADR: platform-abstraction-layer)",
        },
        {
          selector:
            "MemberExpression[object.object.name='process'][object.property.name='env'][property.value='HOME']",
          message:
            "Use homeDir from src/platform.ts instead of process.env.HOME (undefined on Windows) (ADR: platform-abstraction-layer)",
        },
      ],
    },
  },
  // ── No process.exit in library code (PR #7/10 — error-discipline) ────
  // Long-lived MCP servers (packages/mcp-*) and the container agent runner
  // must use throw + bootstrap harness for shutdown so structured exit
  // logging fires. Direct process.exit bypasses the harness and loses
  // attribution. setup/* + src/deus-listen.ts are short-lived CLIs and not
  // covered by this rule.
  // See docs/decisions/error-discipline.md "PR #7: when process.exit is OK"
  // for the legitimate exceptions.
  {
    files: ["packages/*/src/**/*.ts", "container/*/src/**/*.ts"],
    ignores: ["**/*.test.ts"],
    languageOptions: { parser: tseslint.parser },
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "CallExpression[callee.object.name='process'][callee.property.name='exit']",
          message:
            "process.exit is banned in packages/* and container/*/src/. Throw an error and let the bootstrap harness exit cleanly. Disable per-line (with rationale) only when there is no upstream catcher — e.g., inside the bootstrap harness itself, or to signal unrecoverable failure to a host orchestrator from an MCP server. See docs/decisions/error-discipline.md.",
        },
      ],
    },
  },
  prettier,
);
