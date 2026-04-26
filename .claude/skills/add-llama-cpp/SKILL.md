---
name: add-llama-cpp
description: Install and verify a local llama.cpp server for optional Deus local-generation experiments. Keeps Ollama as the required default for embeddings and judge work.
---

# Add llama.cpp

This skill installs `llama.cpp`, runs `llama-server` as a local host service, and wires the local endpoint into Deus only when the current checkout already supports the optional `llama_cpp` provider.

Use this when the user wants a faster or cheaper local text-generation path for experiments, benchmarks, or future backend work.

Slash command: `/add-llama-cpp`.

**Important boundaries:**

- This does **not** replace Ollama for memory embeddings or the default judge. Ollama remains required unless the repo deliberately changes those surfaces.
- This skill is **macOS-first** for installation and service management. On Linux or Windows, continue only if `llama-server` is already installed or the user explicitly wants a manual install path.
- If the current checkout does not yet contain the optional Deus-side `llama_cpp` integration, complete the host install anyway and tell the user the runtime wiring is a separate source task.

## Phase 1: Pre-flight

### Check current state

```bash
command -v llama-server >/dev/null 2>&1 && llama-server --version || echo "llama.cpp not installed"
curl -fsS http://127.0.0.1:8080/health 2>/dev/null || echo "llama-server not responding on 127.0.0.1:8080"
test -f evolution/generative/providers/llama_cpp.py && echo "DEUS_LLAMA_CPP_PROVIDER=true" || echo "DEUS_LLAMA_CPP_PROVIDER=false"
test -f setup/llama-cpp.ts && echo "DEUS_LLAMA_CPP_SETUP=true" || echo "DEUS_LLAMA_CPP_SETUP=false"
```

### Ask scope

AskUserQuestion: Do you want host install only, or host install plus optional Deus wiring if this checkout supports it?
1. **Host install only** - install `llama.cpp`, run `llama-server`, and verify the local endpoint
2. **Host install + Deus wiring** - also configure repo env vars and run Deus-side verification when the checkout supports it

If the user chooses option 2 but either provider/setup file is missing, say clearly that the host install can proceed now and the checkout wiring remains a separate code task.

## Phase 2: Install llama.cpp

### macOS

If `llama-server` is not already installed:

```bash
brew install llama.cpp
```

Verify:

```bash
llama-server --version
```

### Linux or Windows

If `llama-server` is already available in `PATH`, continue.

If it is missing, stop and tell the user this skill currently automates installation on macOS only. Offer to continue once `llama-server` is installed manually, or handle platform-specific installation as a separate task.

## Phase 3: Configure the Local Service

Create a local env file outside git:

```bash
mkdir -p "$HOME/.config/deus" "$HOME/.config/deus/scripts"
cat > "$HOME/.config/deus/llama-cpp.env" <<'EOF'
LLAMA_CPP_BIND_HOST=127.0.0.1
LLAMA_CPP_PORT=8080
LLAMA_CPP_MODEL=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M
LLAMA_CPP_ALIAS=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M
LLAMA_CPP_CTX_SIZE=8192
EOF
```

Create the launcher script:

```bash
cat > "$HOME/.config/deus/scripts/start-llama-cpp.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="$HOME/.config/deus/llama-cpp.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

: "${LLAMA_CPP_BIND_HOST:=127.0.0.1}"
: "${LLAMA_CPP_PORT:=8080}"
: "${LLAMA_CPP_MODEL:=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M}"
: "${LLAMA_CPP_ALIAS:=$LLAMA_CPP_MODEL}"
: "${LLAMA_CPP_CTX_SIZE:=8192}"

exec llama-server \
  --host "$LLAMA_CPP_BIND_HOST" \
  --port "$LLAMA_CPP_PORT" \
  -hf "$LLAMA_CPP_MODEL" \
  --alias "$LLAMA_CPP_ALIAS" \
  -c "$LLAMA_CPP_CTX_SIZE" \
  --jinja
EOF
chmod +x "$HOME/.config/deus/scripts/start-llama-cpp.sh"
```

## Phase 4: Run as a Host Service

### macOS LaunchAgent

Write the LaunchAgent:

```bash
PLIST="$HOME/Library/LaunchAgents/com.deus.llama-cpp.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.deus.llama-cpp</string>
    <key>ProgramArguments</key>
    <array>
      <string>$HOME/.config/deus/scripts/start-llama-cpp.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/deus-llama-cpp.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/deus-llama-cpp.error.log</string>
  </dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.deus.llama-cpp"
```

### Linux or Windows

Do not invent a service wrapper if the platform path is unclear. Prefer a foreground verification run:

```bash
"$HOME/.config/deus/scripts/start-llama-cpp.sh"
```

If the user wants persistent background service management on Linux or Windows, treat that as a follow-up task after the endpoint is verified.

## Phase 5: Verify the Local Endpoint

Check health:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/v1/models
```

Run a chat-completions smoke test:

```bash
curl -fsS http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ggml-org/gemma-3-1b-it-GGUF:Q4_K_M",
    "messages": [{"role": "user", "content": "Reply with exactly: OK"}]
  }'
```

If the first model download takes time, inspect the log instead of assuming failure:

```bash
tail -50 "$HOME/Library/Logs/deus-llama-cpp.log" 2>/dev/null || true
tail -50 "$HOME/Library/Logs/deus-llama-cpp.error.log" 2>/dev/null || true
```

## Phase 6: Optional Deus Wiring

Run this phase only if:

- the user chose host install plus Deus wiring, and
- `evolution/generative/providers/llama_cpp.py` exists in the current checkout, and
- `setup/llama-cpp.ts` exists in the current checkout.

### Configure repo env vars

Write or update the repo env:

```bash
if grep -q '^LLAMA_CPP_BASE_URL=' .env 2>/dev/null; then
  tmpf=$(mktemp) && sed 's#^LLAMA_CPP_BASE_URL=.*#LLAMA_CPP_BASE_URL=http://127.0.0.1:8080#' .env > "$tmpf" && mv "$tmpf" .env
else
  echo 'LLAMA_CPP_BASE_URL=http://127.0.0.1:8080' >> .env
fi

if grep -q '^LLAMA_CPP_MODEL=' .env 2>/dev/null; then
  tmpf=$(mktemp) && sed 's#^LLAMA_CPP_MODEL=.*#LLAMA_CPP_MODEL=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M#' .env > "$tmpf" && mv "$tmpf" .env
else
  echo 'LLAMA_CPP_MODEL=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M' >> .env
fi

mkdir -p data/env && cp .env data/env/env
```

### Verify setup surface

```bash
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 npx tsx setup/index.ts --step llama-cpp
```

### Verify provider path

If the benchmark harness exists:

```bash
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 python3 -m evolution.benchmark_generative \
  --providers llama_cpp \
  --model llama_cpp=ggml-org/gemma-3-1b-it-GGUF:Q4_K_M \
  --json
```

If the benchmark file is missing, use a minimal availability check instead:

```bash
python3 - <<'PY'
from evolution.generative.providers.llama_cpp import LlamaCppGenerativeProvider
provider = LlamaCppGenerativeProvider()
print({"available": provider.is_available(), "model": provider.get_default_model()})
PY
```

## Troubleshooting

### `llama-server` exits immediately

Check whether the model slug is valid and whether the first download returned 404 or auth errors:

```bash
tail -100 "$HOME/Library/Logs/deus-llama-cpp.error.log" 2>/dev/null || true
```

If the chosen Hugging Face preset is invalid, switch `LLAMA_CPP_MODEL` in `~/.config/deus/llama-cpp.env` to a known-good preset and restart the service.

### Health works but `/v1/chat/completions` fails

The endpoint is up, but the model did not finish loading or prompt templating is wrong. Check logs first. For instruction-tuned GGUFs like Gemma, keep `--jinja` enabled in the launcher script.

### Deus wiring files are missing

Host installation is still complete. Tell the user clearly:

> `llama.cpp` is running on the host, but this checkout does not yet include the optional Deus-side `llama_cpp` provider wiring. That remains a separate source change.

### Revert

Stop the service and remove the local files:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.deus.llama-cpp.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.deus.llama-cpp.plist"
rm -f "$HOME/.config/deus/scripts/start-llama-cpp.sh"
rm -f "$HOME/.config/deus/llama-cpp.env"
```
