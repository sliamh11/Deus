# Code Review Criteria

These rules are loaded into each review agent's prompt. Edit this file to customize what gets flagged.

## Style Rules

- Follow naming conventions from CLAUDE.md
- No unused imports or dead code
- No commented-out code blocks (delete or explain why)
- Functions should do one thing
- Prefer early returns over deep nesting

## Logic Rules

- All async calls must be awaited or explicitly fire-and-forget
- Null/undefined must be checked before property access on external data
- Error handling must propagate or explicitly swallow with a comment
- Loop bounds must be verified (off-by-one)
- Type assertions must be justified

## Security Rules

- No hardcoded secrets, API keys, or tokens
- User input must be sanitized before: SQL queries, shell commands, file paths, HTML output
- Authentication checks must precede authorization checks
- Sensitive data must not appear in logs
- File paths from user input must be validated against traversal

## What NOT to Flag

- Linter-catchable issues (formatting, semicolons, trailing whitespace)
- Style preferences not documented in project conventions
- Missing tests (unless the change is security-critical)
- TODOs (unless they mask a bug)
- Import ordering
