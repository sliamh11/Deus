# Core Behavioral Rules
# Always loaded. 800-token budget. Domain-specific rules are hook-retrieved.
# Each rule: what + why. Adding a rule requires justification in the PR.

## Data & Security
- Never lose, overwrite, or downgrade user data. Merge, don't replace.
- Audit security before every commit. Treat the repo as public.
- Public repo changes must be user-agnostic. Personal fixtures/IDs stay in local paths.
- Private code goes to src/private/ (gitignored). Never PR private features.
- Never bundle personal ~/.claude/ tooling into public-repo PRs.

## Execution Gates
- Never execute without explicit user approval. Wait to be told.
- Show commit message and wait for approval before committing.
- Source edits require plan review approval before proceeding.
- Never proceed while a review agent is running. Wait for its verdict.
- Never merge failing CI. Never use --admin/direct push except emergencies.

## Verification & Honesty
- Never speculate. Only state verified facts. If unsure, say so.
- Delegated review findings must include grep evidence, not just conclusions.
- Check production logs before optimizing synthetic benchmarks.
- Predict outcome before running expensive operations. Skip if predictable.

## Workflow
- Feature branch before implementing. Use git worktree, never checkout in main.
- All independent work runs parallel + background. Don't ask, just do it.
- Default subagent model is Sonnet. Escalate to Opus only with stated reason.
- Default to cross-platform. Flag OS-specific code loudly in PRs.
- Chat responses always in English. Hebrew only inside artifacts.
