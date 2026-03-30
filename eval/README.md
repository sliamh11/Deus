# Eval Layer

Tests containerized Deus agents against curated datasets using [DeepEval](https://github.com/confident-ai/deepeval). Each test case spawns a real Docker container, sends a prompt, reads the response via IPC files, and scores it with LLM-based metrics.

## Prerequisites

- Python 3.11+
- Docker (or Podman) running
- Built container image: `./container/build.sh`
- `CLAUDE_CODE_OAUTH_TOKEN` set (can be `"placeholder"` if the credential proxy on localhost:3001 is running)
- Alternatively, `ANTHROPIC_API_KEY` for API-key auth

## Setup

```bash
cd eval
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running Tests

```bash
# Full suite
CLAUDE_CODE_OAUTH_TOKEN=placeholder .venv/bin/pytest -v

# Single suite
.venv/bin/pytest test_core_qa.py -v

# Single test case
.venv/bin/pytest test_core_qa.py -k "cqa_001" -v

# Skip warmup noise
.venv/bin/pytest -v --no-header -q
```

## Judge Configuration

The judge model scores agent responses. Configured in `judge_model.py`.

| Priority | Judge | Requires |
|----------|-------|----------|
| 1 | GeminiJudge | `GEMINI_API_KEY` in `~/.config/deus/.env` |
| 2 | ClaudeProxyJudge (fallback) | Credential proxy on localhost:3001. Currently blocked by Anthropic OAuth auth issue. |

Override the judge model name with `DEEPEVAL_JUDGE_MODEL` (default: `claude-sonnet-4-5`).

## Concurrency

Containers are I/O-bound (waiting on Anthropic API), so the pre-warm phase runs multiple containers in parallel.

- **Default:** `max(1, min(cpu_count, 8) // 2)` workers
- **Override:** `DEUS_EVAL_CONCURRENT=N`
- **Selective warmup:** only datasets matching collected test files are warmed. Running `pytest test_core_qa.py` warms only `core_qa.jsonl`, not all three suites.

## Test Suites

| File | Dataset | Metrics |
|------|---------|---------|
| `test_core_qa.py` | `datasets/core_qa.jsonl` | AnswerRelevancy, Correctness (GEval), InstructionFollowing (GEval), Latency |
| `test_tool_use.py` | `datasets/tool_use.jsonl` | ToolSelection, ToolEvidence, PlanQuality (custom metrics in `metrics/`) |
| `test_safety.py` | `datasets/safety.jsonl` | Toxicity, Bias, RefusalQuality, AdversarialRobustness |

Thresholds for all metrics are in `thresholds.json`.

## Adding Tests

### Dataset format

Each dataset is a JSONL file in `datasets/`. One JSON object per line:

```json
{
  "id": "cqa_001",
  "suite": "core_qa",
  "input": "What are three key differences between TCP and UDP?",
  "expected_output": "TCP is connection-oriented, reliable...",
  "context": "Networking protocols",
  "metadata": {
    "category": "factual",
    "difficulty": "medium",
    "tags": ["networking"]
  }
}
```

Required fields: `id`, `suite`, `input`, `expected_output`. Optional: `context`, `retrieval_context`, `metadata`.

### Custom metrics

Add new metrics in `metrics/`. Subclass `deepeval.metrics.BaseMetric` and follow the pattern in `metrics/efficiency_metric.py` or `metrics/tool_use_metric.py`.

### New test file

Convention: `test_{name}.py` loads `datasets/{name}.jsonl`. The warmup fixture auto-detects this mapping.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_CODE_OAUTH_TOKEN` | (none) | OAuth token for agent auth |
| `ANTHROPIC_API_KEY` | (none) | Alternative API-key auth |
| `DEUS_EVAL_IMAGE` | `deus-agent:latest` | Docker image to run |
| `DEUS_EVAL_TIMEOUT` | `300` | Container timeout in seconds |
| `DEUS_EVAL_CONCURRENT` | auto | Parallel warmup workers |
| `CREDENTIAL_PROXY_PORT` | `3001` | Credential proxy port |
| `GEMINI_API_KEY` | (none) | For GeminiJudge |
| `DEEPEVAL_JUDGE_MODEL` | `claude-sonnet-4-5` | Judge model name override |

## Architecture Notes

- **IPC files, not stdout:** Results are read from `/workspace/ipc/output/*.json` on a shared Docker volume. Docker buffers container stdout until exit, making it unreliable for real-time result detection. See `docs/decisions/eval-ipc-file-output.md`.
- **In-memory cache only:** Agent responses are cached per-session in Python memory. No disk cache -- it silently masks regressions across builds. See `docs/decisions/eval-no-disk-cache.md`.
- **Selective warmup:** Only datasets matching collected test files are pre-warmed, saving ~3x time when running a single suite. See `docs/decisions/eval-selective-warmup.md`.

## Known Limitations

- **Rate limits:** API rate limits saturate at roughly 30 containers per session. The warmup phase backs off on 429s but total runtime increases significantly.
- **Full suite cost:** ~40 container starts across all datasets. Needs an extended period without competing API usage.
- **Container timeout:** Default 300s. Calibration data in `thresholds.json` shows 30-115s observed latency depending on rate-limit state. Set `DEUS_EVAL_TIMEOUT` to at least 120 for reliable runs.
- **ClaudeProxyJudge:** Currently non-functional due to Anthropic OAuth auth rejection. Use GeminiJudge.
