# Editor Integration (ACP / MCP)

Use Deus's **memory** and **evolution** layers from inside an external code
editor or any MCP-capable client — so the agent you already code with gains
Deus's vault recall and its self-improving reflexion loop, without replacing
the editor's native agent.

This works with editors that speak the
[Agent Client Protocol (ACP)](https://agentclientprotocol.com) — Zed is the
worked example below — and, more generally, with any client that can register
[MCP](https://modelcontextprotocol.io) servers.

## What you get

- **`memory_recall`** — semantic recall over your Deus vault (past decisions,
  research, conventions) from the `deus-memory` server.
- **The reflexion loop** — `get_reflections`, `log_interaction`,
  `get_active_prompt`, `record_feedback` from the `deus-evolution` server, so
  the agent can load prior lessons and log turns for the judge to learn from.

Both servers already ship in this repo (`scripts/deus-memory-mcp`,
`evolution/mcp_server.py`). Integration is configuration only — no new code.

## How it works (compose, don't port)

Deus's native runtime sandboxes agent turns in **containers** for untrusted
channel input (WhatsApp, Telegram, …). That isolation is the wrong fit for an
editor, where *you* are the trusted driver editing your own repo. So the
integration does not run Deus's container in the editor. Instead it composes:

```
  ┌──────────┐   ACP (JSON-RPC/stdio)   ┌────────────────────┐
  │  Editor  │ ───────────────────────► │  Claude ACP adapter │  ← editor mechanics:
  │ (client) │                          │  (diffs, perms,     │    diffs, permissions,
  └──────────┘                          │   buffers, tools)   │    unsaved buffers
        │                               └─────────┬───────────┘
        │ forwards MCP servers                    │ MCP tool calls
        ▼                                         ▼
  context_servers ──────────────►  deus-memory  +  deus-evolution   ← Deus's "brain"
                                   (your vault)     (reflexion loop)
```

The editor's own ACP agent handles the editor-native experience; Deus's MCP
servers supply memory and learning on top. The result is "your editor's agent,
but with Deus's brain."

## Zed setup

Add the Claude ACP agent and forward Deus's two servers via `context_servers`.
`claude-acp` is Zed's built-in ACP registry entry; the adapter auto-installs on
first use (one-time network fetch). Use absolute paths — some MCP clients do not
expand `~`.

```jsonc
// ~/.config/zed/settings.json
{
  "agent_servers": {
    "claude-acp": { "type": "registry" }
  },
  "context_servers": {
    "deus-memory": {
      "command": "/path/to/deus/scripts/deus-memory-mcp",
      "args": [],
      "env": {}
    },
    "deus-evolution": {
      "command": "/path/to/deus/eval/.venv/bin/python3",
      "args": ["-m", "evolution.mcp_server"],
      "env": { "PYTHONPATH": "/path/to/deus" }
    }
  }
}
```

Restart Zed, open the agent panel, pick **`claude-acp`**, and ask it to
`memory_recall` or `get_reflections` something to confirm the servers are live.

> **Version note.** This `context_servers` block was validated on **Zed 1.4.x**
> (flat `command` / `args` / `env`, no `source` field). Zed's settings schema
> has shifted across releases — cross-check
> [zed.dev/docs/ai/mcp](https://zed.dev/docs/ai/mcp) if you are on a different
> version.

## Other editors / generic MCP clients

Both servers are standard stdio MCP servers, so any MCP-capable client can
register them the same way. For example, the Codex CLI (see also
[Multi-backend](MULTI_BACKEND.md)):

```bash
codex mcp add deus-memory -- /path/to/deus/scripts/deus-memory-mcp
```

Point your client at the same two commands; only the registration syntax
differs between clients.

## Closing the loop: per-project `AGENTS.md`

Over ACP/MCP **nothing is injected automatically** — Deus's native runtime
prepends reflections and memory to the prompt, but an external agent only *has*
the tools; it must choose to call them. To make the loop self-driving, give the
agent an instruction file in the project root. Drop this in and fill the
conventions section:

```markdown
# AGENTS.md

> Deus brain is wired into this editor via MCP (`deus-memory`, `deus-evolution`).
> Use it so Deus learns across my projects.

## Working with Deus's memory + evolution
- **Start of a coding task:** call `get_reflections` (omit `group_folder` → global
  lessons) to load prior learnings. If the task touches past decisions, conventions,
  or research, also call `memory_recall`.
- **End of a task, or after a clear success/failure:** call `log_interaction` with a
  short summary of what was asked and what you did. Omit `group_folder` so the lesson
  is global and carries to my other projects.
- **When I give feedback** ("that was wrong" / "good"): call `record_feedback` for that
  interaction.
- Treat reflections as soft guidance learned from past misses — weigh them, don't obey blindly.

## Project conventions  (stated prefs — read directly, not via the loop)
- Stack:
- Style:
- Tests:
- Avoid:
```

**Filename:** the Claude adapter reliably reads `CLAUDE.md` from the project
root; `AGENTS.md` is the cross-tool convention (Codex, Gemini). Keep `AGENTS.md`
as canonical and add a one-line `CLAUDE.md` (`Read AGENTS.md for project
instructions.`), or just name the file `CLAUDE.md` if you only use the Claude
adapter.

## Preconditions

- **A judge for the evolution loop.** Reflection generation needs a model to
  score each turn. The judge is resolved in order: the `EVOLUTION_JUDGE_PROVIDER`
  env var if set, otherwise auto-detect — which prefers **Gemini** (when a key is
  configured) and falls back to local, keyless **Ollama (`gemma4:e4b`)**. **With no
  provider available, interactions are logged but never produce reflections** — the
  loop looks wired but learns nothing.
- **A Python env for the evolution server.** `evolution.mcp_server` needs a
  Python with the `mcp` package and the repo on `PYTHONPATH`. The repo's
  `eval/.venv` is canonical; create it if absent:
  ```bash
  python3 -m venv eval/.venv && eval/.venv/bin/pip install -r eval/requirements.txt
  ```
  The memory server (`scripts/deus-memory-mcp`) picks a suitable Python
  automatically, or honors `DEUS_MEMORY_MCP_PYTHON`.

## Caveats

- **Best-effort, not guaranteed.** The instruction file is a strong nudge, but
  the model still chooses whether to call the tools each turn. The only
  *automatic* injection path is Deus's native runtime, which is not in the
  editor flow.
- **Reflexion learns from mistakes, not stated style.** A lesson is generated
  only when the judge scores a turn below `EVOLUTION_REFLECTION_THRESHOLD`
  (default `0.6`). For fixed conventions, use the `AGENTS.md` conventions
  section — the agent reads those directly.
- **Logging is fire-and-forget.** `log_interaction`'s scoring + reflection step
  runs as a background task inside the MCP server process; if the client tears
  the server down between calls, a pending reflection can be dropped.
- **A fresh memory store starts empty** and fills as you work, if pointed at a
  new vault rather than an existing one.

## Shared or employer-owned machines

The evolution store lives at `~/.deus/evolution.db` (override with
`DEUS_EVOLUTION_DB`). When learnings must not leave a machine, or must not cross
a trust boundary:

- **Force the local judge.** Set `EVOLUTION_JUDGE_PROVIDER=ollama` so scoring runs
  on the local, keyless model and interaction text is never sent to a cloud judge.
  Otherwise, if a Gemini key is present, auto-detect routes turns through the cloud API.
- Keep `~/.deus/evolution.db` **off any synced or backup path** (iCloud Drive,
  OneDrive, etc.). It is in the home dir, not Desktop/Documents — confirm your
  sync settings still exclude it.
- **Use a separate store per instance.** Set a distinct `DEUS_EVOLUTION_DB` (and
  vault path) for each instance so learnings from one context never bleed into
  another on the same machine.
- Never copy the store across the boundary. Local learning stays local by
  construction.
