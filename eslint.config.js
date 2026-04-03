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
  prettier,
);
