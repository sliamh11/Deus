import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import prettier from "eslint-config-prettier";

export default tseslint.config(
  {
    ignores: ["dist/", "node_modules/", "eval/", "evolution/", "container/"],
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
  prettier,
);
