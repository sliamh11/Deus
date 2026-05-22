---
name: add-claude-context
description: Add semantic code search via claude-context MCP server. Fully local — Ollama embeddings + network-isolated Docker Milvus. No data leaves the machine.
---

# Add Claude Context (Semantic Code Search)

This skill adds semantic code search to Claude Code via the [claude-context](https://github.com/zilliztech/claude-context) MCP server by Zilliz. Once installed, Claude can index any codebase and search it by meaning — not just keyword matching.

Tools added:
- `index_codebase` — index a directory into the vector database
- `search_code` — semantic search across indexed code
- `get_indexing_status` — check indexing progress
- `clear_index` — remove an index

**Privacy:** All data stays local. Embeddings via Ollama, vectors in a network-isolated Docker Milvus container. No cloud APIs, no telemetry.

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

If `deus-milvus` is already running, skip to Phase 4.

If `deus-milvus` exists but is stopped:

```bash
docker start deus-milvus
```

### Create network-isolated Milvus

Create an internal Docker network that blocks all outbound internet:

```bash
docker network create --internal deus-milvus-net 2>/dev/null || true
```

Start Milvus standalone (single container — includes etcd + minio + milvus):

```bash
docker run -d --name deus-milvus \
  --network deus-milvus-net \
  --restart unless-stopped \
  -p 127.0.0.1:19530:19530 \
  -v deus-milvus-data:/var/lib/milvus \
  milvusdb/milvus:latest standalone
```

Note: `milvusdb/milvus:latest` is used because Milvus does not publish clean semver tags (only date-based build tags like `2.6-20260522-fcd078ee`). To pin a specific build, replace `latest` with a date tag from Docker Hub.

Security notes:
- `--internal` network blocks ALL outbound internet from the container
- Port `127.0.0.1:19530` is only accessible from localhost, not from the local network
- Data persists in the `deus-milvus-data` Docker volume across restarts
- `--restart unless-stopped` auto-starts the container with Docker. On Linux (non-Docker Desktop), this means the container starts on daemon boot — users who don't want this can use `--restart no` instead and start manually

### Wait for Milvus to be ready

```bash
echo "Waiting for Milvus..."
for i in $(seq 1 45); do
  curl -sf http://127.0.0.1:19530/healthz > /dev/null 2>&1 && echo "Milvus is ready (after ${i}s)" && break
  [ $((i % 10)) -eq 0 ] && echo "Still waiting... (${i}s)"
  sleep 2
done
```

If Milvus doesn't start within 90 seconds, check logs:

```bash
docker logs deus-milvus --tail 20
```

### Verify network isolation

```bash
docker exec deus-milvus ping -c1 -W2 8.8.8.8 2>&1 || echo "Network isolation confirmed — no outbound internet"
```

This command must fail. If it succeeds, the `--internal` network was not applied correctly — recreate the container.

## Phase 4: Configure MCP

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

### Safety check

Verify `MILVUS_TOKEN` is not set in the user's environment — if present without `MILVUS_ADDRESS`, the SDK calls Zilliz Cloud:

```bash
[ -n "$MILVUS_TOKEN" ] && echo "WARNING: MILVUS_TOKEN is set in your environment. This can cause data to be sent to Zilliz Cloud. Unset it: unset MILVUS_TOKEN" || echo "OK — no MILVUS_TOKEN"
```

If `MILVUS_TOKEN` is set, warn the user and recommend unsetting it before proceeding.

Configuration notes:
- `EMBEDDING_BATCH_SIZE=5` is a conservative default to avoid OOM with local models. Users with more RAM can increase this.
- `HYBRID_MODE=false` disables sparse vector (BM25) search which has reported instability with Milvus standalone. Dense search with the MCP's own reranking is sufficient. Set to `true` to experiment.
- `MILVUS_ADDRESS` must always be set explicitly — omitting it while a `MILVUS_TOKEN` is present would trigger calls to Zilliz Cloud.
- This is a user-scope config (`~/.claude/mcp.json`), making code search available across all projects.

## Phase 5: Verify

### Smoke test Milvus

```bash
curl -sf http://127.0.0.1:19530/healthz && echo "Milvus healthy"
```

### Confirm network isolation

```bash
docker exec deus-milvus ping -c1 -W2 8.8.8.8 2>&1 && echo "WARNING: outbound internet reachable" || echo "Isolation confirmed"
```

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

## Troubleshooting

### Milvus won't start

1. Check if port 19530 is already in use: `lsof -i :19530`
2. Check Docker is running: `docker info`
3. Check container logs: `docker logs deus-milvus --tail 50`
4. If the container exists but won't start, remove and recreate:
   ```bash
   docker rm -f deus-milvus
   docker network rm deus-milvus-net 2>/dev/null
   ```
   Then re-run Phase 3.

### "Failed to connect to Milvus"

1. Check Milvus is running: `docker ps --filter "name=deus-milvus"`
2. Check health: `curl -sf http://127.0.0.1:19530/healthz`
3. Milvus takes 15-30 seconds to initialize — wait and retry
4. Check `MILVUS_ADDRESS` in `~/.claude/mcp.json` is `127.0.0.1:19530`

### Embedding dimension mismatch

If you see errors about dimension mismatch, the `EMBEDDING_DIMENSION` in `~/.claude/mcp.json` doesn't match your model's output. Re-detect:

```bash
curl -s http://127.0.0.1:11434/api/embed -d '{"model":"<your-model>","input":"test"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['embeddings'][0]))"
```

Update `EMBEDDING_DIMENSION` in `~/.claude/mcp.json` to match. This is a known upstream issue (claude-context #235) — auto-detection fails in the background indexer.

### Node 24 incompatibility

claude-context requires Node 20 or 22. Node 24 is not supported. Check your version with `node --version` and switch if needed (e.g., via `nvm use 22`).

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

4. Remove the Docker network:
   ```bash
   docker network rm deus-milvus-net
   ```
