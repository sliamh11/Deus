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
- Source edits require /plan mode + plan-reviewer SHIP for non-trivial changes. Trivial-bypass: touch marker with stated judgment.
- After drafting a plan in /plan mode, immediately invoke plan-reviewer (`Agent(subagent_type="plan-reviewer")`) with the plan summary as the prompt — do not ask the user, do not call ExitPlanMode first. Trigger: when you would otherwise present the plan or ask to proceed. On non-Claude-Code backends, the PreToolUse gate is the backstop.
- Never proceed while a review agent is running. Wait for its verdict.
- REVISE from any warden means re-run after fixes until SHIP. Never touch markers, commit, or proceed on REVISE — no exceptions, no "close enough," no time-pressure rationalization.
- Quality over speed by default. Never shortcut a warden loop, skip a review round, or rationalize lower standards because of time pressure, autonomy grants, or late-session fatigue. Only skip when the user explicitly says to prioritize speed.
- Never merge failing CI. Never use --admin/direct push except emergencies.

## Verification & Honesty
- Never speculate. Only state verified facts. If unsure, say so.
- Delegated review findings must include grep evidence, not just conclusions.
- Check production logs before optimizing synthetic benchmarks.
- Predict outcome before running expensive operations. Skip if predictable.
- Before fixing anything, identify what changed to cause it. Diagnosis before treatment — prescriptive error messages are not a substitute for root-cause investigation.
- Before committing to any solution, evaluate alternatives. Rushing into the first fix risks introducing bugs, missing better approaches, and degrading performance. Decide, then act — never the reverse.
- Don't solve problems that don't exist yet. Speculative hardening, premature abstractions, and features without a real caller are waste. Real usage reveals real gaps.

## Workflow
- Feature branch before implementing. Use git worktree, never checkout in main.
- One concern per branch. Unrelated changes bundled together are harder to review, revert, and bisect.
- All independent work runs parallel + background. Don't ask, just do it.
- Default subagent model is Sonnet. Escalate to Opus only with stated reason.
- Default to cross-platform. Flag OS-specific code loudly in PRs.
- Chat responses always in English. Hebrew only inside artifacts.

## Code Exploration
- Three-stage protocol: (1) `search_code` or `codegraph_context` (the composite primary) for semantic candidates, (2) `codegraph_callers`/`codegraph_callees`/`codegraph_impact` for structural context (what connects to the candidates), (3) grep/read for exact confirmation. Skip stages when the answer is already known.
- Before modifying any function, query `codegraph_callers` to know the blast radius. A change with 2 callers is not the same risk as a change with 50.
- Never open-code `grep -r` or `find -name` as the first move -- semantic search identifies the landscape, structural queries map the connections, exact search confirms specifics.
- When authoring a `Workflow` (or dispatching investigation subagents) over a codegraph-indexed repo, bake the codegraph-first mandate INTO the `agent()` prompts -- `workflow-subagent` agents inherit no exploration hook and codegraph is already directly callable from them, so their prompt is the only lever.

## Memory & Context
- Before implementing a feature, search memory (`memory_tree.py query "<topic>"`) for prior decisions and research. Cite the retrieved path.
- Never duplicate content across files. When a rule or config applies in multiple places, write it once and reference it. Duplication is drift waiting to happen.
