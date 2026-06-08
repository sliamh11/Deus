---
name: add-editor
description: Wire Deus's memory + evolution layers into an external code editor (Zed and other ACP/MCP clients) so the editor's own agent gains Deus's vault recall and self-improving reflexion loop. Configuration only — no new code. Triggers on "add editor", "use Deus in my editor", "zed integration", "editor integration", "wire Deus into Zed", or ACP/MCP editor setup requests.
---

# Add Editor Integration (ACP / MCP)

Use Deus's **memory** and **evolution** layers from inside an external editor (or any
MCP-capable client) by composing them onto the editor's *own* coding agent — you do not
run Deus's container in the editor. The editor's agent keeps handling editor mechanics
(diffs, permissions, buffers); Deus's two MCP servers supply the "brain":

- **`deus-memory`** → `memory_recall` (semantic recall over your vault).
- **`deus-evolution`** → `get_reflections` / `log_interaction` / `get_active_prompt` /
  `record_feedback` (the reflexion loop).

This skill *performs* the wiring interactively. For the **why** (compose-not-port rationale,
caveats, why reflexion learns from mistakes not stated style), read
[`docs/EDITOR_INTEGRATION.md`](../../../docs/EDITOR_INTEGRATION.md) — do not duplicate that
reasoning here; just point the user at it if they ask.

**Principle:** when a precondition is missing, fix it (create the venv, pull the model). Only
pause for genuine user decisions (which editor, employer-machine containment, target project).
Use `AskUserQuestion` for every user-facing choice.

---

## 0. Scope — two independent parts

Tell the user this skill has two parts they can run independently:

1. **Machine-level editor config (steps 1-5)** — wires the two MCP servers into the editor.
   Run **once per machine**.
2. **Per-project instruction file (step 6)** — the close-the-loop `AGENTS.md`. Run **once per
   repo** you want the loop active in.

If they only want one part, skip to it. Do not assume the directory you're invoked from is the
project they want the loop in — step 6 asks explicitly.

## 1. Resolve the Deus repo root (absolute path)

The config below needs an **absolute** path to this Deus clone — some MCP clients do not expand
`~`, and on another machine (e.g. a work PC) the clone lives at a different path. Resolve it in
order and record it as `ROOT`:

```bash
git rev-parse --show-toplevel 2>/dev/null
```

- If that prints a path **and** it contains `scripts/deus-memory-mcp` → that's `ROOT`.
- Else if `$HOME/deus/scripts/deus-memory-mcp` exists → `ROOT="$HOME/deus"`.
- Else → `AskUserQuestion`: "Where is your Deus repo cloned?" and use the answer.

Verify `ROOT` is absolute (starts with `/`). Use it verbatim everywhere below.

## 2. Verify preconditions (deterministic — fix what's missing)

Run these against `ROOT`:

```bash
# (a) memory server present + executable
[ -x "$ROOT/scripts/deus-memory-mcp" ] && echo "memory OK" || echo "memory MISSING"

# (b) evolution server importable in the canonical venv
PYTHONPATH="$ROOT" "$ROOT/eval/.venv/bin/python3" -c "import mcp, evolution.mcp_server" \
  && echo "evolution OK" || echo "evolution venv MISSING/incomplete"
```

- **(a) missing** → the repo is incomplete; stop and tell the user to re-clone / run `/setup`.
- **(b) missing** → create the venv, then re-check:
  ```bash
  python3 -m venv "$ROOT/eval/.venv" && "$ROOT/eval/.venv/bin/pip" install -r "$ROOT/eval/requirements.txt"
  ```
- **(c) Judge — the silent-failure point. Make this prominent.** Reflection generation needs a
  model to score each turn. With **no** judge reachable, interactions are *logged but never
  produce reflections* — the loop looks wired but learns nothing. Check:
  ```bash
  ollama list 2>/dev/null | grep -i gemma4 || echo "no local judge"
  ```
  - Local model present → good (keyless, no egress).
  - No local model **and** no `GEMINI_API_KEY` / configured key → warn loudly: "Evolution will
    log but never reflect until a judge exists." Offer `ollama pull gemma4:e4b` (≈9.6 GB).
  - On a managed/MDM machine, confirm Ollama is actually *running* (`ollama list` responds),
    not just installed — that is the make-or-break for this feature there.

## 3. Detect the editor

`AskUserQuestion`: "Which editor / client are you wiring Deus into?"
- **Zed** (worked example below) — uses `~/.config/zed/settings.json`.
- **Other ACP editor** — same two servers, the editor's own MCP/context-server config.
- **Generic MCP client (e.g. Codex CLI)** — register the two server commands directly.

## 4. Merge the editor config — MERGE, NEVER OVERWRITE

> **Guard (read this before touching the file):** the editor config is **JSONC** — it has
> comments and likely existing settings. **Read it first with the Read tool, then make
> targeted Edits in place.** Do **not** round-trip it through `jq` / `JSON.parse` / a full
> rewrite — those silently strip the user's comments and unrelated settings. If an
> `agent_servers` or `context_servers` key already exists, **merge into it** (add/​update only
> the `claude-acp` / `deus-memory` / `deus-evolution` entries); do not replace the file or the
> block. If `deus-*` entries already exist, update their paths in place.

### Zed (`~/.config/zed/settings.json`)

Ensure these keys exist (substituting the real `ROOT`). `claude-acp` is Zed's built-in ACP
registry entry; the adapter auto-installs on first use.

```jsonc
{
  "agent_servers": {
    "claude-acp": { "type": "registry" }
  },
  "context_servers": {
    "deus-memory": {
      "command": "<ROOT>/scripts/deus-memory-mcp",
      "args": [],
      "env": {}
    },
    "deus-evolution": {
      "command": "<ROOT>/eval/.venv/bin/python3",
      "args": ["-m", "evolution.mcp_server"],
      "env": { "PYTHONPATH": "<ROOT>" }
    }
  }
}
```

> Validated on **Zed 1.4.x** (flat `command` / `args` / `env`, no `source` field). Zed's schema
> shifts across releases — if these keys are rejected, cross-check
> https://zed.dev/docs/ai/mcp for the current shape.

### Other ACP editor / generic MCP client

Same two server commands; only the registration syntax differs. For the Codex CLI:

```bash
codex mcp add deus-memory -- "$ROOT/scripts/deus-memory-mcp"
codex mcp add deus-evolution --env PYTHONPATH="$ROOT" -- "$ROOT/eval/.venv/bin/python3" -m evolution.mcp_server
```

For any other client, point it at the same two commands using its own MCP config format.

## 5. Shared / employer-owned machine (containment)

`AskUserQuestion`: "Is this a shared or employer-owned machine where what Deus learns must
**not** leave the box (no cloud egress, no sync)?"

If **yes**, apply all of these:

- **Force the local judge.** Set `EVOLUTION_JUDGE_PROVIDER=ollama` so scoring runs on the
  local, keyless model and interaction text is never sent to a cloud judge. Otherwise, if a
  Gemini key is present, auto-detect routes turns through the cloud API.
- **Use a separate, named store.** Set a distinct `DEUS_EVOLUTION_DB` (e.g.
  `$HOME/.deus/evolution-work.db`) so this instance's learnings never mix with another instance
  on the same machine.
- **Keep the store off any synced/backup path** (iCloud Drive, OneDrive, etc.). It lives in the
  home dir by default — confirm the user's sync settings still exclude it.
- **Never copy the store across the boundary.** Local learning stays local by construction.

**Where these env vars go (depends on the client from step 3):**
- **Zed** — add them to the `deus-evolution` server's `env` block:
  ```jsonc
  "deus-evolution": {
    "command": "<ROOT>/eval/.venv/bin/python3",
    "args": ["-m", "evolution.mcp_server"],
    "env": {
      "PYTHONPATH": "<ROOT>",
      "EVOLUTION_JUDGE_PROVIDER": "ollama",
      "DEUS_EVOLUTION_DB": "<HOME>/.deus/evolution-work.db"
    }
  }
  ```
- **Generic client** — set them in that client's per-server MCP env config (e.g. `codex mcp add
  --env ...`), or export them in the environment the client launches the server from.

> `<HOME>` above is the user's absolute home directory (the output of `echo $HOME`, e.g.
> `/Users/you`) — substitute it literally, just like `<ROOT>` from step 1. JSON values are not
> shell-expanded, so a bare `$HOME` inside the config would **not** work.

## 6. Per-project instruction file (run once per repo)

Over ACP/MCP **nothing auto-injects** — the editor's agent only *has* the tools; it must choose
to call them. A project-root instruction file makes the loop self-driving.

`AskUserQuestion` (or prompt): "Which project directory should I add the loop instructions to?"
Do **not** assume the current directory — record it as `TARGET`. If `TARGET/AGENTS.md` (or
`TARGET/CLAUDE.md`) already exists, **Read it first, then Edit in place** to merge this section
in — do not overwrite the file or clobber existing instructions (same discipline as step 4).

Write this into `TARGET/AGENTS.md`:

```markdown
# AGENTS.md

> Deus brain is wired into this editor via MCP (`deus-memory`, `deus-evolution`).
> Use it so Deus learns across my projects.

## Working with Deus's memory + evolution
- **Start of a coding task:** call `get_reflections` (omit `group_folder` → global lessons) to
  load prior learnings. If the task touches past decisions, conventions, or research, also call
  `memory_recall`.
- **End of a task, or after a clear success/failure:** call `log_interaction` with a short
  summary of what was asked and what you did. Omit `group_folder` so the lesson is global and
  carries to my other projects.
- **When I give feedback** ("that was wrong" / "good"): call `record_feedback` for that
  interaction.
- Treat reflections as soft guidance learned from past misses — weigh them, don't obey blindly.

## Project conventions  (stated prefs — read directly, not via the loop)
- Stack:
- Style:
- Tests:
- Avoid:
```

> **Filename note.** The Claude adapter reliably reads `CLAUDE.md` from the project root;
> `AGENTS.md` is the cross-tool convention (Codex, Gemini). Keep `AGENTS.md` canonical and add a
> one-line `CLAUDE.md` (`Read AGENTS.md for project instructions.`), or name the file
> `CLAUDE.md` if the user only uses the Claude adapter.

## 7. Verify

Tell the user: restart the editor, open the agent panel, pick **`claude-acp`** (or your chosen
client's Deus-wired agent), and ask it to call `memory_recall` or `get_reflections` on something
— a real response confirms both servers are live. The stated conventions in `AGENTS.md` take
effect immediately; the reflexion loop fills in as you work (and only when a judge is reachable,
per step 2c).

## Reference

Full rationale, the compose-not-port architecture diagram, and the complete caveat list live in
[`docs/EDITOR_INTEGRATION.md`](../../../docs/EDITOR_INTEGRATION.md).
