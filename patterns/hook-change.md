---
governs:
  - .claude/hooks/
  - scripts/codex_warden_hooks.py
  - .claude/settings.json
last_verified: "2026-08-20" # auto-bump @1787223223
test_tasks:
  - "Add a PreToolUse gate that blocks a write when the pending content matches a pattern"
  - "Change which tools an existing warden gate fires on"
  - "Make a hook emit an advisory message instead of blocking"
---
# Pattern: hook-change

Authoring hooks and warden gates. For co-gate *operation* — verdict buckets, marker routing,
backend marking — see `.claude/rules/orchestration-rules.md` instead; this file is about writing
the hook, not running the gate.

The hook input/output **types** are already documented at `docs/SDK_DEEP_DIVE.md:465,509`. Read
those first; this file records only what they do not cover.

## PreToolUse receives the pending content

`tool_input` carries the tool's actual arguments, so a gate can refuse a write based on **what is
about to be written**, not merely which file is touched:

| Tool | Keys available at PreToolUse |
|------|------------------------------|
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all` |
| `Write` | `file_path`, `content` |
| `Bash` | `command`, `description` |

This is what makes a content-matching gate possible at all — e.g. refusing a test file whose
`new_string` introduces a call that would corrupt a production database.

**Caveat, stated because it matters:** the official hook documentation does not enumerate
`tool_input` keys per tool. The table above is confirmed from real tool calls observed in a live
session, not from a published contract. Before shipping a gate that depends on a content key, log
`tool_input` from one real invocation and confirm the key is present.

`_event_paths` (`scripts/codex_warden_hooks.py:375`) reads only `file_path` and `command` today —
its silence about the content keys reflects what existing gates needed, not what is available.

## Blocking versus advising is one JSON key

Two helpers, same file, entirely different force:

- `_block_pre_tool` (`scripts/codex_warden_hooks.py:451`) emits `permissionDecision: deny`. The
  harness refuses the tool call; the action never happens.
- `_warn_post_tool` (`:463`) emits `systemMessage`. Text appears and nothing is stopped.

A rule is only strict if it reaches the first one. Text injected into context — by any mechanism,
however prominently worded — is advisory, and a hook that merely warns is a reminder no matter
what the message says.

Note the event constraint: only `PreToolUse` can deny. `PostToolUse` runs after the write, so a
check there can catch and report but never prevent. When prevention is not available, reading the
file from disk at `PostToolUse` still works — the file exists by then.

## Which tools a gate fires on is a regex in settings.json

Hooks are registered per event in `.claude/settings.json`, and each entry carries a `matcher` —
a regex over the **tool name**, not the file path:

| Matcher | Gates registered under it |
|---------|---------------------------|
| `Write\|Edit\|MultiEdit\|apply_patch\|ExitPlanMode` | plan-review-gate, tdd-test-lock |
| `ExitPlanMode\|Task\|Agent` | plan-mode-invalidator, codegraph-cite-check |
| `Write\|apply_patch` | placement-guard |
| `Bash` | code-review-gate, ai-eng-gate, verification-gate, admin-merge-gate, format-check |

To change what a gate fires on, edit its matcher — adding a tool there is what makes the hook
receive that tool's events at all. Note the consequence for the Bash group: those gates see
*every* Bash call and must decide relevance themselves from the command text, which is why the
commit gate string-matches `git commit` rather than being scoped by the harness.

Path-based selectivity is the hook's own job, not the matcher's — see `_managed_paths` and
`_event_paths` in `scripts/codex_warden_hooks.py`.

## The block reason is written for the model, not the user

`permissionDecisionReason` is fed back to the agent as feedback so it can adjust; it is not
surfaced to the user as an explanation. Write it as an instruction — *"use `monkeypatch.context()`
instead; `undo()` reverts the `test_db` redirect"* — rather than as prose describing the problem
to a human.

Same evidentiary caveat as the key table above: this came from a documentation search, not from a
line in this repo, and nothing under `docs/` states the audience split. Treat it as well-founded
but unconfirmed locally — if a gate message ever needs to reach the user, verify before relying on
this.
