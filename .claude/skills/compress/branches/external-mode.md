# External Project Mode — Memory Level Gate, Scope, and Redaction

This file is read by `/compress` when the current working directory is **not** `~/deus`
(External Project Mode). It covers three responsibilities called out in skill.md:

1. Memory level gate (whether to proceed at all)
2. Step 0 — Preserve permanent memories: scope rules
3. Session log redaction rules (post-save)

---

## Memory level gate

Compute the MD5 hash of the current working directory and read
`~/.config/deus/projects/<hash>.json`.

```bash
# macOS
dir_hash=$(echo -n "$(pwd)" | md5 -q)
# Linux
dir_hash=$(echo -n "$(pwd)" | md5sum | cut -d' ' -f1)
config_file="$HOME/.config/deus/projects/${dir_hash}.json"
```

Read `memory_level` and `save_summaries` from that file.

- If `memory_level` is **restricted**: tell the user:
  > "Session saving is disabled for restricted projects. Your work is preserved in git
  > commits and Claude Code's native session transcript. Use /project-settings to change this."
  >
  > Then **stop**.
- If `save_summaries` is **false**: tell the user the same message and **stop**.
- If `memory_level` is **standard** or **full** with summaries enabled: **proceed**.

---

## Step 0 scope — what to preserve in `$VAULT/CLAUDE.md`

`$VAULT` is resolved per the vault-path resolution logic in skill.md (not re-derived here).

**standard memory level:** Only preserve USER preferences and behavioral corrections — things
about the user, not the project. Skip project-specific architecture decisions or code patterns.

**full memory level:** Preserve both user preferences AND project-relevant decisions. No
restriction on scope.

---

## Session log redaction (standard memory level only)

After saving the session log, run the redaction script to strip any code snippets or file
contents that leaked through:

```bash
python3 ~/deus/scripts/redact_session.py "<full path to saved log>"
```

Only run this step when `memory_level` is `standard` (not `full` or `restricted`).
If the script exits non-zero, skip silently — the log is still saved; instruct the user
to review it manually. On success the script writes a `.pre-redact.md` backup of the
original alongside the log; that file appearing is expected, not an error.

**Session log field guidance (standard memory level):**

- Do NOT include specific file paths, function names, or code snippets in the session log
- Focus on decisions, architecture, and what was tried/learned
- `## Files Modified` should use descriptions ("updated the auth middleware") not paths
- Goal: the log captures WHAT was decided and WHY, without leaking code details

**Full memory level:** No redaction — include full details as in home mode.
