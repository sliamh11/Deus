# Development Process Rules

> The development discipline for this repository. Enforced by the quality gates in
> `.claude/hooks/warden-gate.py` and reviewed by the agents in `.claude/agents/`.

## Review gates (enforced)

Three gates run automatically through Claude Code hooks. Each is satisfied by running the
matching agent, getting a SHIP verdict, then recording it.

| Gate | Fires before | Satisfy with |
|------|--------------|--------------|
| plan-review | any Edit / Write / MultiEdit | run the `plan-reviewer` agent → SHIP → `mark plan-reviewed SHIP "reason"` |
| code-review | `git commit` | run the `code-reviewer` agent on the diff → SHIP → `mark code-reviewed SHIP "reason"` |
| verification | `git commit` | run the `verification-gate` agent → SHIP → `mark verified SHIP "reason"` |

The mark command is:

```
python3 .claude/hooks/warden-gate.py mark <gate> SHIP "reason"
```

Markers are cleared automatically at the start of each session and whenever a new plan begins, so
every change is reviewed fresh. A REVISE or BLOCK verdict means fix the issues and re-run the agent
until it returns SHIP — never bypass the gate.

For a genuinely trivial change (a typo, a comment, a single-line rename), record:

```
python3 .claude/hooks/warden-gate.py mark plan-reviewed TRIVIAL "what and why"
```

Trivial bypass is not available after a REVISE or BLOCK verdict.

## Workflow

- **Plan before non-trivial work.** Draft a plan, run it through the `plan-reviewer`, then implement.
- **Branch before implementing.** Never commit directly to the default branch — use a feature branch.
- **One concern per branch.** Unrelated changes bundled together are harder to review, revert, and bisect.
- **Show the commit message and wait for approval before committing.**
- **Verify before claiming done.** Run the build/tests/lint and read the output — don't say "should work."

## Honesty

- State only verified facts. If unsure, say so.
- Before fixing anything, identify what changed to cause it. Diagnosis before treatment.
- Evaluate alternatives before committing to a solution.
- Don't add features, abstractions, or hardening for problems that don't exist yet.
