---
name: add-understand-anything
description: Install the Understand-Anything plugin so the agent gains /understand, /understand-chat, /understand-dashboard and related codebase-analysis skills (interactive knowledge graphs, guided tours, deep-dive explanations). Claude Code backend only — adds the plugin from its GitHub marketplace end-to-end; non–Claude-Code backends use the upstream install.sh fallback.
---

# Add Understand-Anything

This skill installs [**Understand-Anything**](https://github.com/Lum1104/Understand-Anything)
— an LLM-powered codebase-analysis tool that produces interactive knowledge
graphs, guided tours, and deep-dive explanations of any project. Once installed,
the agent gains the `/understand*` family of skills.

It ships as a **Claude Code plugin** distributed through a GitHub plugin
marketplace. This skill runs the whole marketplace flow (add marketplace →
install plugin → verify) end-to-end so a single `/add-understand-anything`
completely integrates it — no manual `/plugin` steps.

> **Backend note:** This is a Claude Code installer. The `claude plugin …`
> commands below only exist when Claude Code is the active backend (Deus's
> default). On non–Claude-Code backends (Codex, opencode, gemini, …) use the
> upstream `install.sh` fallback in Phase 0 instead.

## Phase 0: Backend guard

Confirm the Claude Code CLI is available before doing anything else:

```bash
command -v claude >/dev/null 2>&1 && echo "claude CLI found" || echo "NO claude CLI"
```

If the `claude` CLI is **not** found, this backend can't use the plugin
marketplace. Stop here and tell the user to install via the upstream script
instead (it symlinks the skills into the right place for their runtime):

```bash
# Replace <platform> with one of: codex, opencode, gemini, pi, vscode, ...
curl -fsSL https://raw.githubusercontent.com/Lum1104/Understand-Anything/main/install.sh | bash -s <platform>
```

Then skip the rest of this skill.

## Phase 1: Pre-flight (idempotent)

Check current state with the read-only `list` commands and branch on their
output — do not assume how the CLI handles a duplicate add/install.

### Is the marketplace already added?

```bash
claude plugin marketplace list 2>/dev/null | grep -qF "Lum1104/Understand-Anything" \
  && echo "MARKETPLACE_PRESENT" || echo "MARKETPLACE_MISSING"
```

(Match the exact marketplace source `Lum1104/Understand-Anything` — `list`
prints it as `Source: GitHub (Lum1104/Understand-Anything)` — so an unrelated
plugin that merely contains the substring `understand-anything` can't trigger a
false `MARKETPLACE_PRESENT`.)

- `MARKETPLACE_PRESENT` → skip the marketplace-add step in Phase 2.
- `MARKETPLACE_MISSING` → run it.

### Is the plugin already installed?

```bash
claude plugin list 2>/dev/null | grep -q "understand-anything@understand-anything" \
  && echo "PLUGIN_INSTALLED" || echo "PLUGIN_NOT_INSTALLED"
```

- `PLUGIN_INSTALLED` → tell the user it's already installed and skip straight to **Phase 3: Verify**.
- `PLUGIN_NOT_INSTALLED` → continue to Phase 2.

## Phase 2: Install and configure

Run only the steps Phase 1 marked as needed. **Check each command's exit status
— a non-zero exit (network failure, marketplace fetch error, install error) is a
failure: stop, show the CLI's error output to the user, and do NOT continue to
Verify or claim success.**

### Add the marketplace (only if `MARKETPLACE_MISSING`)

```bash
claude plugin marketplace add Lum1104/Understand-Anything
```

### Install the plugin (only if `PLUGIN_NOT_INSTALLED`)

Installed at **user scope** (the default) so it's available in every Claude Code
session on this machine. The plugin reference is intentionally version-free so it
always tracks the marketplace's current release.

```bash
claude plugin install understand-anything@understand-anything
```

## Phase 3: Verify

Confirm the plugin is registered:

```bash
claude plugin list 2>&1 | grep "understand-anything@understand-anything" \
  && echo "OK: installed" || echo "FAILED: not found"
```

If this prints `FAILED: not found`, the install did **not** complete — surface
the error from Phase 2, do not tell the user it's installed, and stop.

If verification passes (`OK: installed`), tell the user:

> **Understand-Anything is installed.** Restart (or reload) your Claude Code
> session so the new skills load — plugins are read at session start.
>
> Once reloaded you'll have:
> - `/understand` — analyze a codebase into an interactive knowledge graph
> - `/understand-chat` — ask questions about the codebase via the graph
> - `/understand-dashboard` — launch the interactive web dashboard
> - `/understand-diff` — analyze a git diff / PR (changes, affected components, risk)
> - `/understand-domain` — extract business-domain knowledge and flow graph
> - `/understand-explain` — deep-dive a specific file, function, or module
> - `/understand-knowledge` — graph an LLM/wiki knowledge base
> - `/understand-onboard` — generate an onboarding guide for new team members
>
> **Dashboard prerequisite:** `/understand-dashboard` needs Node ≥ 22 and
> pnpm ≥ 10 — it builds its React dashboard lazily on first launch. The analysis
> skills (`/understand`, `/understand-explain`, etc.) work without it.

## Removal

To remove Understand-Anything:

1. Uninstall the plugin:
   ```bash
   claude plugin uninstall understand-anything@understand-anything
   ```

2. (Optional) Remove the marketplace too — only if no other plugin from it is in use:
   ```bash
   claude plugin marketplace remove understand-anything
   ```

3. Restart the Claude Code session so the `/understand*` skills unload.
