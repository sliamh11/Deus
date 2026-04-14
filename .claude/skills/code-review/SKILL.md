---
name: code-review
description: Multi-agent code review with learning feedback loop — reviews PRs using parallel specialized agents (style, logic, security) with confidence scoring and false-positive reduction
version: 1.0.0
triggers:
  - code.?review
  - review.?pr
  - review.?code
  - pr.?review
  - deus.?review
---

# Multi-Agent Code Review

Run a multi-agent code review on the current branch's PR. Three specialized agents (style, logic, security) review in parallel, findings are confidence-scored, and dismissed findings feed back into the evolution system as negative examples for future reviews.

## Prerequisites

- **Git** — must be in a git repository
- **GitHub CLI** — `gh` authenticated
- Current branch must have an open PR

## Instructions

When the user asks for a code review or triggers this skill:

### Step 1: Validate environment

```bash
git rev-parse --is-inside-work-tree  # Must be in a repo
gh auth status                        # Must be authenticated
```

Get the current branch and find the open PR:

```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --state open --json number,title,baseRefName
```

If no PR exists, ask the user if they want to create one first. Do NOT proceed without a PR.

### Step 2: Check eligibility

Skip review if:
- PR is a draft (`gh pr view <number> --json isDraft`)
- PR has zero code changes (only docs/config)
- This exact commit SHA was already reviewed (check `resources/review-history.log` if it exists)

### Step 3: Gather context

Collect all inputs the review agents will need:

```bash
# Get the unified diff
gh pr diff <number>

# Get file list and stats
gh pr view <number> --json files,additions,deletions

# Get PR description for intent context
gh pr view <number> --json body
```

Also load:
1. **CLAUDE.md files** from the repo root AND from each modified directory (use Glob to find them)
2. **Review criteria** from `resources/review-criteria.md` (repo-specific rules)
3. **Dismissed findings** from `resources/dismissed-log.md` (negative examples — "do NOT flag X when Y")

### Step 4: Parallel review — launch 3 agents

Launch three review agents in parallel using the Task tool. Each agent receives:
- The PR diff
- Relevant CLAUDE.md content
- Review criteria for their domain
- Negative examples from dismissed-log.md for their category
- Instructions to output findings in a structured format

**IMPORTANT:** Include the phrase "in parallel" in your orchestration to ensure Teams tools are available.

#### Agent prompts

**Style Agent (Haiku):**
```
You are a code style reviewer. Analyze this PR diff for:
- Naming inconsistencies (variables, functions, files)
- Dead code or unused imports
- Inconsistency with the codebase conventions described in CLAUDE.md
- Code duplication within the diff
- Missing or misleading comments

DO NOT flag:
- Anything a linter would catch (formatting, semicolons, trailing spaces)
- Style preferences not documented in CLAUDE.md
{dismissed_style_examples}

For each finding, output exactly:
FILE: <path>
LINE: <number>
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
TITLE: <short title>
DETAIL: <1-2 sentences explaining the issue>
SUGGESTION: <concrete fix>
---
```

**Logic Agent (Sonnet):**
```
You are a code logic reviewer. Analyze this PR diff AND the full file context for:
- Off-by-one errors
- Null/undefined handling gaps
- Race conditions or async issues
- Missing error propagation
- Incorrect boolean logic
- Edge cases not handled
- Type mismatches or unsafe casts

Read the FULL file for each changed file to understand context beyond the diff.

DO NOT flag:
- Hypothetical issues that require specific runtime conditions unlikely in this codebase
- Missing validation for internal-only code paths
{dismissed_logic_examples}

For each finding, output exactly:
FILE: <path>
LINE: <number>
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
TITLE: <short title>
DETAIL: <1-2 sentences explaining the issue and a concrete failure scenario>
SUGGESTION: <concrete fix>
---
```

**Security Agent (Sonnet):**
```
You are a security reviewer. Analyze this PR diff for OWASP Top 10 vulnerabilities:
- SQL injection / NoSQL injection
- XSS (cross-site scripting)
- Command injection
- Path traversal
- Hardcoded secrets, API keys, tokens
- Insecure deserialization
- Missing authentication/authorization checks
- Sensitive data exposure
- SSRF (server-side request forgery)

Focus on actual exploitable patterns, not theoretical risks.

DO NOT flag:
- Internal code paths with no user input
- Environment variables used correctly
- Test files or fixtures
{dismissed_security_examples}

For each finding, output exactly:
FILE: <path>
LINE: <number>
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
TITLE: <short title>
DETAIL: <1-2 sentences explaining the vulnerability and attack vector>
SUGGESTION: <concrete fix>
---
```

### Step 5: Confidence scoring

For each finding from Step 4, launch a Haiku agent to score confidence (0-100):

```
Given this code review finding and the actual code context, rate your confidence (0-100) that this is a genuine issue worth fixing, not a false positive.

Finding: {finding}
Code context: {surrounding code from the file}

Consider:
- Is this actually reachable in practice?
- Does the surrounding code already handle this?
- Is this a real bug or just a style preference?

Reply with ONLY a number 0-100.
```

**Drop findings scoring below 80.** This is the key false-positive filter.

### Step 6: Deduplicate and present

1. Deduplicate findings by file + line number (within 3 lines counts as same location)
2. If multiple agents flagged the same location, merge into one finding with the highest severity
3. Sort by severity (CRITICAL → HIGH → MEDIUM → LOW), then by file path

Present as a markdown table:

```
## Code Review: PR #<number> — <title>

| # | Severity | File | Line | Issue | Suggestion |
|---|----------|------|------|-------|------------|
| 1 | 🔴 CRITICAL | src/auth.ts | 42 | SQL injection in query builder | Use parameterized query |
| 2 | 🟠 HIGH | src/api.ts | 156 | Unhandled null from getUserById | Add null check before access |
```

### Step 7: User action

Ask the user how to proceed using AskUserQuestion:

**Options:**
- 🔍 **Review each** — walk through findings one by one
- ⚡ **Auto-fix all** — apply all fixes automatically
- 📝 **Post to PR** — post findings as a PR comment without fixing
- ❌ **Cancel**

#### If "Review each":
For each finding:
1. Show the finding with current code context
2. Show the proposed fix as a diff
3. Ask: ✅ Apply | ⏭️ Dismiss | 🔧 Modify

- **Apply**: Edit the file, commit: `git add <file> && git commit -m "fix: <title>"`
- **Dismiss**: Log to `resources/dismissed-log.md` with reason, AND call the evolution feedback loop (see Step 8)
- **Modify**: Let the user adjust, then apply

#### If "Auto-fix all":
Apply all fixes sequentially, commit each one.

#### If "Post to PR":
Format findings and post via `gh pr comment <number> --body '<formatted findings>'`

### Step 8: Feedback loop (on dismiss)

When a finding is dismissed, two things happen:

**A. Local persistent memory** — append to `resources/dismissed-log.md`:

```markdown
### [STYLE|LOGIC|SECURITY] <title>
- **Dismissed:** <date>
- **File:** <path>:<line>
- **Reason:** <user's reason>
- **Rule:** Do NOT flag <specific pattern> when <specific context>
```

**B. Evolution reflexion** — create a forced reflection by building the JSON safely with Python:

```bash
python3 -c "
import json, subprocess, sys
payload = json.dumps({
    'finding': sys.argv[1],
    'reason': sys.argv[2],
    'file': sys.argv[3],
    'line': int(sys.argv[4]) if sys.argv[4] != 'null' else None,
    'group_folder': sys.argv[5] if sys.argv[5] != 'null' else None
})
subprocess.run([sys.executable, 'evolution/cli.py', 'dismiss_review_finding', payload])
" "<title>" "<user reason>" "<path>" "<line or null>" "<group or null>"
```

This bypasses the judge and directly creates a negative reflection that will be retrieved in future reviews via `getReflections()`. Using `json.dumps` avoids shell injection from special characters in finding titles or reasons.

### Step 9: Log review commit SHA

After all findings are processed, append the reviewed commit SHA to `resources/review-history.log`:

```
<commit-sha> <date> <pr-number> <findings-count> <fixed> <dismissed>
```

### Step 10: Push

If any fixes were applied, ask the user if they want to push:
- Yes → `git push`
- No → inform they can push later

## Review Criteria Customization

Users can customize review rules by editing `resources/review-criteria.md`. The default is created on first run with sensible defaults. The file is loaded and injected into each agent's prompt.

## How the Feedback Loop Works

```
User dismisses finding
    ↓
resources/dismissed-log.md (persistent, per-repo)
    ↓
evolution/cli.py dismiss_review_finding
    ↓
save_reflection(category="code_review", content="Do NOT flag X when Y")
    ↓
Next review → getReflections(query, tools: "code-review")
    ↓
Injected as negative examples into agent prompts
    ↓
False positive rate decreases over time
```
