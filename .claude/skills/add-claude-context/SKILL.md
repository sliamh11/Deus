---
name: add-claude-context
description: "[DEPRECATED] Replaced by scripts/code_search.py (native sqlite-vec + Ollama). Do not use."
disable-model-invocation: true
---

# Add Claude Context (Semantic Code Search) -- DEPRECATED

> **This skill is deprecated.** claude-context (Milvus-based) was evaluated and found to have never worked due to 5 cascading bugs (wrong config path, corrupt npx cache, wrong protocol, broken network isolation, short timeout). Milvus is also massive overkill for code search at any realistic codebase scale.
>
> **Replacement:** `scripts/code_search.py` -- native semantic code search using sqlite-vec + Ollama embeddings + RRF fusion, following the `memory_tree.py` pattern. No Docker, no Milvus, no external dependencies beyond Ollama.

The content below is preserved for historical reference only.

---

This skill adds semantic code search to Claude Code via the [claude-context](https://github.com/zilliztech/claude-context) MCP server by Zilliz. Once installed, Claude can index any codebase and search it by meaning — not just keyword matching.

Tools added:
- `index_codebase` — index a directory into the vector database
- `search_code` — semantic search across indexed code
- `get_indexing_status` — check indexing progress
- `clear_index` — remove an index

**Privacy:** All data stays local. Embeddings via Ollama, vectors in a network-isolated Docker Milvus container. No cloud APIs, no telemetry. See [Security](#security) for the full defense-in-depth breakdown.

## Phase 1: Pre-flight

### Check if already configured

```bash
grep -q "claude-context" ~/.claude/mcp.json 2>/dev/null && echo "INSTALLED" || echo "NOT_INSTALLED"
```

If already installed, skip to Phase 5 (Verify).

### Check prerequisites

**Docker:**

```bash
docker info > /dev/null 2>&1 && echo "Docker OK" || echo "Docker not running"
```

If Docker is not running, tell the user to start Docker Desktop (macOS) or the Docker daemon (Linux).

**Ollama:**

```bash
ollama list > /dev/null 2>&1 && echo "Ollama OK" || echo "Ollama not running"
```

If Ollama is not installed, direct the user to https://ollama.com/download.

**Node.js version:**

```bash
node --version
```

Must be >= 20 and < 24. Node 24 is incompatible with claude-context. If the version is wrong, tell the user which versions are supported.

## Phase 2: Embedding Model

### Detect available embedding models

```bash
ollama list 2>/dev/null | grep -i embed
```

### Choose a model

If embedding models are found, ask the user which one to use via `AskUserQuestion`.

If no embedding models are found, suggest pulling one:

> No embedding models found in Ollama. You need one for code search. Options:
>
> - **nomic-embed-text** (768-dim, 274MB) — lightweight, recommended for most use cases
> - **mxbai-embed-large** (1024-dim, 669MB) — higher quality embeddings
> - **snowflake-arctic-embed2** (1024-dim, 1.2GB) — multilingual support

Pull the chosen model:

```bash
ollama pull <model-name>
```

### Detect embedding dimension

Get the dimension from a test embedding call — never hardcode this value:

```bash
curl -s http://127.0.0.1:11434/api/embed -d '{"model":"<model-name>","input":"test"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['embeddings'][0]))"
```

Store the model name and dimension for Phase 4.

## Phase 3: Start Milvus

### Check if Milvus is already running

```bash
docker ps --filter "name=deus-milvus" --format "{{.Status}}" 2>/dev/null
```

If `deus-milvus` is already running and healthy, skip to Phase 4:

```bash
docker exec deus-milvus curl -sf http://localhost:9091/healthz > /dev/null 2>&1 && echo "HEALTHY" || echo "NOT_HEALTHY"
```

### Step 3a: Create config files

Create the Milvus config directory:

```bash
mkdir -p ~/.config/deus/milvus
```

Create `~/.config/deus/milvus/embedEtcd.yaml`:

```bash
cat > ~/.config/deus/milvus/embedEtcd.yaml << 'EOF'
listen-client-urls: http://0.0.0.0:2379
advertise-client-urls: http://0.0.0.0:2379
quota-backend-bytes: 4294967296
auto-compaction-mode: revision
auto-compaction-retention: "1000"
EOF
```

Create `~/.config/deus/milvus/user.yaml` (required mount, no overrides needed):

```bash
touch ~/.config/deus/milvus/user.yaml
```

Create `~/.config/deus/milvus/start-milvus.sh`:

```bash
cat > ~/.config/deus/milvus/start-milvus.sh << 'SCRIPT'
#!/bin/bash
set -euo pipefail

CONTAINER=deus-milvus
INTERNAL_NET=deus-milvus-net
BRIDGE_NET=deus-milvus-bridge

# Create networks if missing
docker network create --internal "$INTERNAL_NET" 2>/dev/null || true
docker network create "$BRIDGE_NET" 2>/dev/null || true

# If container exists but stopped, remove it (clean restart with correct config)
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    echo "deus-milvus already running"
    exit 0
  fi
  docker rm "$CONTAINER"
fi

# Start on bridge network (port publishing requires a non-internal network at creation time)
docker run -d --name "$CONTAINER" \
  --network "$BRIDGE_NET" \
  --restart no \
  --security-opt seccomp:unconfined \
  -e ETCD_USE_EMBED=true \
  -e ETCD_DATA_DIR=/var/lib/milvus/etcd \
  -e ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml \
  -e COMMON_STORAGETYPE=local \
  -e DEPLOY_MODE=STANDALONE \
  --memory=3g --memory-swap=3g --cpus=2 \
  -p 127.0.0.1:19530:19530 \
  -v deus-milvus-data:/var/lib/milvus \
  -v "$HOME/.config/deus/milvus/embedEtcd.yaml:/milvus/configs/embedEtcd.yaml:ro" \
  -v "$HOME/.config/deus/milvus/user.yaml:/milvus/configs/user.yaml:ro" \
  milvusdb/milvus:latest \
  milvus run standalone

# Wait for Milvus to be healthy before switching networks
HEALTHY=false
for i in $(seq 1 60); do
  docker exec "$CONTAINER" curl -sf http://localhost:9091/healthz >/dev/null 2>&1 && HEALTHY=true && break
  sleep 2
done

if [ "$HEALTHY" != "true" ]; then
  echo "ERROR: Milvus did not become healthy after 120s"
  echo "Run: docker logs $CONTAINER --tail 50"
  exit 1
fi

# Switch to internal network (blocks all outbound internet)
docker network connect "$INTERNAL_NET" "$CONTAINER" || { echo "ERROR: failed to connect internal network"; exit 1; }
docker network disconnect "$BRIDGE_NET" "$CONTAINER" || { echo "ERROR: failed to disconnect bridge network"; exit 1; }

# Verify isolation — hard abort if outbound is reachable
if docker exec "$CONTAINER" curl -sf --connect-timeout 3 http://1.1.1.1 >/dev/null 2>&1; then
  echo "ABORT: outbound internet still reachable after network switch"
  exit 1
fi
echo "Network isolation confirmed"
SCRIPT
chmod +x ~/.config/deus/milvus/start-milvus.sh
```

**Why the network dance?** Docker Desktop for Mac's `--internal` network blocks port publishing — the Docker proxy can't route traffic to containers on internal-only networks. The startup script creates the container on a regular bridge (port publishing works), waits for health, then switches to the internal network (blocks outbound). This survives restarts because the LaunchAgent/systemd unit replays the full sequence.

**Why `seccomp:unconfined`?** Milvus v2.6 uses `clone3()` and `io_uring` syscalls that the Docker default seccomp profile blocks. Without this flag, Milvus crashes on startup.

**Why `--restart no`?** The service manager (LaunchAgent/systemd) owns the lifecycle, not Docker. This ensures the network dance runs on every start.

**Resource limits:** `--memory=3g --cpus=2` prevents Milvus from consuming all host RAM during indexing. Users with more RAM can increase these values in the script.

### Step 3b: Run the startup script

```bash
bash ~/.config/deus/milvus/start-milvus.sh
```

### Step 3c: Install service manager

**macOS — LaunchAgent:**

```bash
cat > ~/Library/LaunchAgents/com.deus.milvus.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.deus.milvus</string>
  <key>ProgramArguments</key>
  <array>
    <string>bash</string>
    <string>$HOME/.config/deus/milvus/start-milvus.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/deus-milvus.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/deus-milvus.error.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deus.milvus.plist
```

Note: The heredoc uses `<< EOF` (not `<< 'EOF'`) so `$HOME` expands to the absolute path. `launchd` does not expand `~` in plist values.

**Linux — systemd user unit:**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/deus-milvus.service << 'EOF'
[Unit]
Description=Deus Milvus (semantic code search)
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%h/.config/deus/milvus/start-milvus.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
systemctl --user enable --now deus-milvus.service
```

## Phase 4: Configure MCP

### Safety gate

Before writing MCP config, verify these invariants. **Abort if any check fails** — do not proceed with a warning.

**Check 1 — No MILVUS_TOKEN:**

```bash
[ -n "$MILVUS_TOKEN" ] && echo "ABORT: MILVUS_TOKEN is set. This causes data to be sent to Zilliz Cloud. Run: unset MILVUS_TOKEN" && exit 1 || echo "OK"
```

If `MILVUS_TOKEN` is set, tell the user to unset it and stop. Do NOT write the MCP config.

**Check 2 — MILVUS_ADDRESS is localhost:**

The config being written must set `MILVUS_ADDRESS` to exactly `127.0.0.1:19530`. Never omit this field — without it, the SDK may auto-provision a Zilliz Cloud cluster if `MILVUS_TOKEN` is set in the future.

### Read and merge MCP config

Read `~/.claude/mcp.json` and merge the new `claude-context` entry. Never overwrite existing servers.

The final entry should be:

```json
{
  "claude-context": {
    "command": "npx",
    "args": ["-y", "@zilliz/claude-context-mcp@0.1.13"],
    "env": {
      "EMBEDDING_PROVIDER": "Ollama",
      "OLLAMA_HOST": "http://127.0.0.1:11434",
      "OLLAMA_MODEL": "<detected-model>",
      "EMBEDDING_MODEL": "<detected-model>",
      "EMBEDDING_DIMENSION": "<detected-dimension>",
      "EMBEDDING_BATCH_SIZE": "5",
      "MILVUS_ADDRESS": "127.0.0.1:19530",
      "HYBRID_MODE": "false"
    }
  }
}
```

Replace `<detected-model>` and `<detected-dimension>` with the values from Phase 2.

Configuration notes:
- `EMBEDDING_BATCH_SIZE=5` is a conservative default to avoid OOM with local models. Users with more RAM can increase this.
- `HYBRID_MODE=false` disables sparse vector (BM25) search which has reported instability with Milvus standalone. Dense search with the MCP's own reranking is sufficient. Set to `true` to experiment.
- `MILVUS_ADDRESS` must always be set explicitly — omitting it while a `MILVUS_TOKEN` is present would trigger calls to Zilliz Cloud.
- This is a user-scope config (`~/.claude/mcp.json`), making code search available across all projects.

## Phase 5: Verify

### Smoke test Milvus

Port 9091 is the Milvus management HTTP API (healthz). Port 19530 is the gRPC data port (used by the MCP server). Both must be working.

```bash
docker exec deus-milvus curl -sf http://localhost:9091/healthz && echo "Milvus healthy"
```

### Confirm port accessibility

```bash
python3 -c "import socket; s=socket.socket(); s.settimeout(3); r=s.connect_ex(('127.0.0.1',19530)); print('Port 19530: OK' if r==0 else 'Port 19530: FAIL'); s.close()"
```

### Confirm network isolation

```bash
docker exec deus-milvus curl -sf --connect-timeout 3 http://1.1.1.1 2>&1 && echo "WARNING: outbound internet reachable" || echo "Isolation confirmed — no outbound internet"
```

This command must fail. If it succeeds, the network switch in `start-milvus.sh` did not complete — re-run the script.

### Test the MCP server

Tell the user:

> Claude Context is now installed. To use it, **start a new Claude Code session** and try:
>
> 1. Index a codebase: the agent will use `index_codebase` with a directory path
> 2. Search: ask something like "find the function that handles message routing"
> 3. The agent uses `search_code` to find semantically relevant code
>
> **First-time indexing** may take a few minutes depending on codebase size.
>
> To verify no data leaves your machine during indexing, run in a separate terminal:
> ```bash
> lsof -i -P | grep node
> ```
> You should only see connections to `127.0.0.1:19530` (Milvus) and `127.0.0.1:11434` (Ollama).

## Security

**Defense in depth — what prevents data leakage:**

| Layer | Mechanism |
|-------|-----------|
| Embeddings | `EMBEDDING_PROVIDER=Ollama` — computed locally, never sent to cloud |
| Vector DB address | `MILVUS_ADDRESS=127.0.0.1:19530` — always set explicitly, prevents Zilliz Cloud auto-provisioning |
| Cloud token | No `MILVUS_TOKEN` — hard gate enforced by skill (abort, not warn) |
| Storage | `COMMON_STORAGETYPE=local` — vectors on local filesystem, no S3/MinIO |
| Port binding | `127.0.0.1` only — not accessible from LAN |
| Network isolation | `--internal` Docker network — blocks all outbound internet from container |
| Resource limits | `--memory=3g --cpus=2` — prevents resource exhaustion on shared machines |

**MCP trust boundary:** The `claude-context` MCP server runs on the host as the user — it has unrestricted filesystem read access. It is not sandboxed. Only index directories you trust; avoid indexing paths containing secrets (`.env`, `.aws/`, `.ssh/`, `~/.config/`). Embeddings of cleartext secrets would persist in the unencrypted Milvus volume.

**Supply chain:** The top-level npm package is pinned to `@0.1.13`, but transitive dependencies are resolved at `npx` time without a lockfile. The `--internal` Docker network mitigates container-side supply chain risk; the MCP server's npm tree is the remaining surface.

**Known limitation:** Between container start and the network switch (a few seconds during startup), the container is on the bridge network and theoretically has outbound access. The bridge-first approach is required because Docker Desktop for Mac cannot publish ports on `--internal` or `--network none` networks. In practice, Milvus doesn't make outbound calls during this window — it's initializing etcd and internal services. The startup script applies isolation as soon as healthz returns OK.

## Troubleshooting

### Milvus won't start

1. Check if port 19530 is already in use: `lsof -i :19530`
2. Check Docker is running: `docker info`
3. Check container logs: `docker logs deus-milvus --tail 50`
4. If the container exists but won't start, remove and recreate:
   ```bash
   docker rm -f deus-milvus
   bash ~/.config/deus/milvus/start-milvus.sh
   ```

### "embedded etcd can not be used under distributed mode"

`DEPLOY_MODE=STANDALONE` is missing. The startup script sets this automatically. If running Docker manually, add `-e DEPLOY_MODE=STANDALONE` to your `docker run` command.

### "failed to create etcd client: connection refused"

`ETCD_USE_EMBED=true` and the config file mounts are missing. The startup script handles this. If running manually, add:
- `-e ETCD_USE_EMBED=true`
- `-e ETCD_DATA_DIR=/var/lib/milvus/etcd`
- `-e ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml`
- `-v ~/.config/deus/milvus/embedEtcd.yaml:/milvus/configs/embedEtcd.yaml:ro`

### "Failed to connect to Milvus" / Port 19530 not accessible

1. Check Milvus is running: `docker ps --filter "name=deus-milvus"`
2. Check health: `docker exec deus-milvus curl -sf http://localhost:9091/healthz`
3. Check port: `python3 -c "import socket; s=socket.socket(); s.settimeout(3); r=s.connect_ex(('127.0.0.1',19530)); print('open' if r==0 else 'closed'); s.close()"`
4. If port is closed but container is running, the container may be on the internal network only (port publishing broken). Re-run: `bash ~/.config/deus/milvus/start-milvus.sh`
5. Check `MILVUS_ADDRESS` in `~/.claude/mcp.json` is `127.0.0.1:19530`

### Port not accessible after Docker restart

The LaunchAgent (macOS) or systemd unit (Linux) should handle restarts automatically. Check logs:
- macOS: `cat ~/Library/Logs/deus-milvus.error.log`
- Linux: `journalctl --user -u deus-milvus.service`

If the service manager isn't running, start manually: `bash ~/.config/deus/milvus/start-milvus.sh`

### Embedding dimension mismatch

If you see errors about dimension mismatch, the `EMBEDDING_DIMENSION` in `~/.claude/mcp.json` doesn't match your model's output. Re-detect:

```bash
curl -s http://127.0.0.1:11434/api/embed -d '{"model":"<your-model>","input":"test"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['embeddings'][0]))"
```

Update `EMBEDDING_DIMENSION` in `~/.claude/mcp.json` to match. This is a known upstream issue (claude-context #235) — auto-detection fails in the background indexer.

### Node 24 incompatibility

claude-context requires Node 20 or 22. Node 24 is not supported. Check your version with `node --version` and switch if needed (e.g., via `nvm use 22`).

### Upgrading from old installation

If you installed from an earlier version of this skill (before the Docker fix), your container may have `--restart unless-stopped` and be missing config mounts. Run:

```bash
docker stop deus-milvus && docker rm deus-milvus
bash ~/.config/deus/milvus/start-milvus.sh
```

The named volume `deus-milvus-data` is preserved — no indexed data is lost.

### Re-indexing after volume removal

If you removed the Milvus volume (`docker volume rm deus-milvus-data`), all indexed data is gone. Re-index by asking Claude to run `index_codebase` again on your project directories.

## Removal

To completely remove claude-context:

1. Remove from MCP config:
   ```bash
   # Edit ~/.claude/mcp.json and remove the "claude-context" key from mcpServers
   ```

2. Stop and remove Milvus:
   ```bash
   docker stop deus-milvus && docker rm deus-milvus
   ```

3. Remove the Docker volume (**warning: destroys all indexed codebase data** — re-indexing required after reinstall):
   ```bash
   docker volume rm deus-milvus-data
   ```

4. Remove Docker networks:
   ```bash
   docker network rm deus-milvus-net deus-milvus-bridge 2>/dev/null
   ```

5. Remove config files:
   ```bash
   rm -rf ~/.config/deus/milvus/
   ```

6. Remove service manager:

   **macOS:**
   ```bash
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.deus.milvus.plist
   rm ~/Library/LaunchAgents/com.deus.milvus.plist
   rm -f ~/Library/Logs/deus-milvus.log ~/Library/Logs/deus-milvus.error.log
   ```

   **Linux:**
   ```bash
   systemctl --user disable --now deus-milvus.service
   rm ~/.config/systemd/user/deus-milvus.service
   ```

## Telemetry

**Audited: 2026-05-23. Milvus 2.6 has no configurable telemetry opt-out - network isolation is sufficient.**

### Milvus 2.6 server (Go)

Searched the `milvus-io/milvus` repo for: `telemetry`, `analytics`, `phoneHome`, `usageReport`, `pingHome`. All GitHub code search queries returned **0 results**.

The repo contains `internal/rootcoord/telemetry/` but it is **internal cluster telemetry only** - client SDK metrics (request counts, error rates, SDK versions) stored in etcd for cluster-internal monitoring. No data is transmitted to external services:

- `manager.go`: stores client metrics in-memory + etcd; pull-based (not push); no external HTTP calls
- `command_store.go`: writes only to etcd (internal); no Zilliz/external endpoints
- `configs/milvus.yaml`: no `telemetry`, `analytics`, or phone-home keys present
- Dockerfile (`build/docker/milvus/ubuntu22.04/Dockerfile`): no telemetry ENV vars; entrypoint is `/tini --`
- docker-compose env vars (v2.6 standalone): only `MINIO_REGION`, `ETCD_ENDPOINTS`, `MINIO_ADDRESS`

### PyMilvus (Python client)

GitHub code search for `telemetry` in `milvus-io/pymilvus`: **0 results**. No external reporting found.

### milvus-sdk-node (Node.js client)

GitHub code search for `telemetry` in `milvus-io/milvus-sdk-node`: **0 results**. No external reporting found.

### Conclusion

No configurable telemetry opt-out keys exist because Milvus 2.6 does not phone home. The `--internal` Docker network isolation applied in Phase 3 is the complete mitigation. No `user.yaml` overrides are needed for telemetry suppression - `~/.config/deus/milvus/user.yaml` is mounted empty (as a required mount point) and should remain so.
