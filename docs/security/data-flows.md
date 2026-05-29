# Security Audit: External Data Flows

**Issue**: LIA-73
**Date**: 2026-05-28
**Status**: Current — update this file when external service integrations change.

This document lists every place where user conversation data, vault content,
or audio recordings leave the local machine. It is the single source of truth
for answering "what data does Deus share with third parties?"

---

## 1. LLM providers (intended, primary functionality)

### 1a. Anthropic API (`api.anthropic.com`)

- **What**: Full conversation history (user messages + agent responses) for
  every interactive turn and scheduled task.
- **Where**: `src/credential-proxy.ts` proxies all `/anthropic/*` requests;
  real credentials are injected by the host, never exposed to containers.
- **Controls**: None — this is the core product functionality. Disable Deus
  entirely to prevent.
- **Backend alternative**: Set `DEUS_AGENT_BACKEND=openai` to route to OpenAI
  instead.

### 1b. OpenAI API (`api.openai.com`) — optional

- **What**: Same as 1a when the OpenAI/Codex backend is active.
- **Where**: `src/credential-proxy.ts`, `src/auth-providers/openai.ts`.
- **Controls**: Only active when `DEUS_AGENT_BACKEND=openai` or the group
  overrides the backend. Default backend is Claude.

---

## 2. Google Gemini API — evolution loop (opt-out available)

The evolution loop scores every interaction to improve future responses.
When `EVOLUTION_ENABLED` is not `0` and the judge/generative providers resolve
to Gemini (the default when a `GEMINI_API_KEY` is present), the following
data flows to Google's Gemini API:

### 2a. Interaction scoring (judge)

- **What**: User prompt (up to `EVOLUTION_JUDGE_MAX_PROMPT_CHARS`, default
  2000 chars) + agent response (up to `EVOLUTION_JUDGE_MAX_RESPONSE_CHARS`,
  default 2000 chars) for every scored interaction.
- **Where**: `evolution/judge/gemini_judge.py` → `google.genai.Client`.
- **When**: Fire-and-forget after each interaction completes
  (`evolution/cli.py log_interaction` → async judge eval).
- **Controls**:
  - Set `EVOLUTION_ENABLED=0` to disable all evolution tracking (judge,
    reflexion, domain classification).
  - Set `EVOLUTION_SKIP_GROUPS=<folder>` to exclude specific groups.
  - Set `EVOLUTION_JUDGE_MAX_PROMPT_CHARS` / `EVOLUTION_JUDGE_MAX_RESPONSE_CHARS`
    to reduce payload size (default: 2000 chars each).

### 2b. Reflexion generation

- **What**: Low-scoring interaction user prompt (up to 1500 chars) + agent
  response (up to 1500 chars), sent to generate a corrective "lesson".
- **Where**: `evolution/reflexion/generator.py` → `evolution/generative`.
- **When**: Triggered when judge score falls below `EVOLUTION_REFLECTION_THRESHOLD`
  (default 0.6). Only interactions that score poorly generate a reflexion.
- **Controls**: Same as 2a — `EVOLUTION_ENABLED=0` disables.

### 2c. Domain classification (optional, keyword-fallback first)

- **What**: User prompt text, sent only when keyword-based domain detection
  finds no match.
- **Where**: `src/domain-presets.ts` `detectDomainsWithFallback` → Python
  subprocess → `evolution/generative`.
- **When**: Only when `EVOLUTION_ENABLED != 0` and keyword patterns do not
  match. Most prompts are classified locally without any API call.
- **Controls**: Same as 2a — `EVOLUTION_ENABLED=0` skips the LLM fallback.

### 2d. Memory/vault embeddings

- **What**: Vault file content (personal memory: CLAUDE.md, session logs,
  personal notes) chunked and embedded for semantic search.
- **Where**: `scripts/memory_indexer.py` → `evolution/providers/embeddings.py`.
- **When**: On startup and after vault edits (PostToolUse hook).
- **Controls**:
  - **Default provider is Ollama (local)**. Gemini embeddings are only used
    when `EMBEDDING_PROVIDER=gemini` or when Ollama is unreachable and
    `EMBEDDING_PROVIDER=auto` (the default with Ollama installed).
  - Keep Ollama running (`ollama serve`) to stay fully local.
  - Set `EMBEDDING_PROVIDER=ollama` to force local and never fall back.

---

## 3. OpenAI Whisper API — voice transcription (optional skill)

- **What**: Raw audio files (voice notes from WhatsApp).
- **Where**: WhatsApp channel `transcribeAudioMessage`, using the
  `openai` npm package.
- **When**: Only when the `add-voice-transcription` skill is installed AND
  `use-local-whisper` has not been applied.
- **Controls**:
  - Run `/use-local-whisper` to switch to on-device whisper.cpp — no data
    leaves the machine.
  - Default install (`src/transcription.ts`) uses local whisper.cpp; the
    OpenAI API variant is only active after `/add-voice-transcription`.

---

## 4. Google Calendar API — calendar integration (optional skill)

- **What**: Calendar event metadata (title, description, start/end, attendees)
  synced bidirectionally.
- **Where**: `src/cache/gcal-sync.ts` → Google Calendar API v3.
- **When**: Only when the `add-gcal` skill is installed.
- **Controls**: Not applicable — syncing calendar data with Google is the
  explicit purpose of the integration. Remove the skill to stop.

---

## 5. Linear API — pipeline/issue management (optional skill)

- **What**: Issue titles, descriptions, agent work output posted as comments.
  No raw conversation content is intentionally forwarded, but agent summaries
  of implemented work are posted back as PR descriptions and pipeline logs.
- **Where**: `src/linear-dispatcher.ts`, `src/linear-webhook.ts`.
- **When**: Only when the `add-linear` skill is installed and issues reach the
  dispatch pipeline.
- **Controls**: Not applicable — Linear integration is opt-in.

---

## Opt-out summary

| Data type | Destination | How to opt out |
|---|---|---|
| Conversation turns | Anthropic / OpenAI | Not possible (core functionality) |
| Interaction scores (judge) | Gemini | `EVOLUTION_ENABLED=0` or `EVOLUTION_SKIP_GROUPS` |
| Reflexion lessons | Gemini | `EVOLUTION_ENABLED=0` |
| Domain classification | Gemini | `EVOLUTION_ENABLED=0` |
| Vault embeddings | Gemini (fallback only) | Keep Ollama running or `EMBEDDING_PROVIDER=ollama` |
| Voice audio | OpenAI Whisper | Run `/use-local-whisper` |
| Calendar events | Google Calendar | Remove `add-gcal` skill |
| Issue summaries | Linear | Remove `add-linear` skill |

---

## What never leaves the machine

- Raw conversation history (stored in `~/.deus/memory.db`, SQLite)
- Vault files at rest (stored in `~/.config/deus/vault/` or configured path)
- Container agent logs (`~/.deus/logs/`)
- Session checkpoints (vault session-logs directory)
- Credentials / API keys (never passed to containers; injected by host proxy)
