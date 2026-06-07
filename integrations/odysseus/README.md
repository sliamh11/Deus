# Odysseus ↔ Deus integration

Connects [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus) (a
self-hosted AI workspace) with Deus, in two independent directions.

## ① Odysseus → Deus curated memory (this directory)

A small read-only MCP **sidecar** that lets the Odysseus web agent recall a
**curated subset** of your Deus memory. The sidecar runs in its own container on
Odysseus's Docker network and mounts only a pre-built index — it has no vault
access and imports no Deus code.

### Security model — read this first

The Odysseus agent is prompt-injectable and (per Odysseus's own `THREAT_MODEL.md`)
runs with **no shell/filesystem sandbox**. Anything reachable through `recall`
can, in principle, be surfaced and exfiltrated by a successful injection (e.g. a
poisoned page during Deep Research). No transport or auth removes this risk.

Mitigations built into this design:
- **Curated, pre-baked index.** Only files you place in the vault `Shareable/`
  folder are indexed. The sidecar physically cannot read anything else.
- **Read-only, single tool.** `recall` only searches; it cannot write or delete.
- **Network-internal only.** No host port; reachable only as `deus-memory` on
  `odysseus_default`.
- **Audit log.** Every query is appended to a host-side log so you can review
  what was asked after the fact.

**Your job: keep `Shareable/` conservative.** Treat everything in it as visible
to Odysseus.

### Setup

```bash
# 0. One-time: curate. Create the folder and add only non-sensitive notes.
mkdir -p "$(python3 scripts/memory_tree.py vault 2>/dev/null)/Shareable"   # or wherever your vault is

# 1. Build the curated index (run in the Deus venv; needs Ollama running).
python integrations/odysseus/build_share_index.py

# 2. Configure paths.
cd integrations/odysseus
cp .env.example .env            # then edit: set DEUS_SHARE_DB_PATH + DEUS_RECALL_LOG
touch "$DEUS_RECALL_LOG"        # create the log file so Docker mounts a file, not a dir

# 3. Start the sidecar on Odysseus's network.
docker compose up -d --build

# 4. Register in Odysseus: Settings → MCP → Add server
#      transport: http   url: http://deus-memory:8200/mcp   name: deus-memory
```

Re-run step 1 whenever you change `Shareable/` — the index is a snapshot. The
index is stamped with the embedding model it was built with; the sidecar refuses
to serve recalls if the configured model later differs (rebuild to fix).

> **Operational note (by design, not pending work):** step 4 — registering the
> MCP server in Odysseus — is a **permanent one-time manual** admin action. It is
> not deferred wiring awaiting automation: Odysseus is a separately-managed
> third-party app, so Deus deliberately never writes to its config. There is
> intentionally no Deus runtime hook for it. Treat it as a setup step, like
> adding any MCP server in Odysseus's own UI.

### Teardown / rollback

```bash
docker compose down                    # stop the sidecar
# In Odysseus: Settings → MCP → remove "deus-memory" (or DELETE /api/servers/<id>)
```

## ② Deus terminal → Odysseus

Host-side Claude Code can drive Odysseus (todos, calendar, memory, documents,
read-only email) through Odysseus's scoped `/api/codex/*` API. That wiring lives
in host config (`~/.claude/skills/odysseus/` + `~/.deus/odysseus.env`), not in
this repo, because it contains a personal API token. See the project notes for
the Deus-authored skill; it uses only a thin curl wrapper, no upstream code.

## Files

| File | Role |
|------|------|
| `_embed.py` | Shared Ollama embedder (stdlib-only, no Deus imports). |
| `build_share_index.py` | Host ETL: `Shareable/*.md` → self-contained sqlite-vec DB. |
| `share_mcp_server.py` | The sidecar: one read-only `recall` MCP tool. |
| `Dockerfile` | `python:3.11-slim` + `mcp` + `sqlite-vec`, with full load-sequence verify. |
| `docker-compose.yml` | Runs the sidecar on `odysseus_default`, no host port. |
| `.env.example` | Template for the gitignored `.env` (absolute host paths). |
