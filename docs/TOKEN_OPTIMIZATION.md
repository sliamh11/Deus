# Token Optimization

Reduces per-turn static token context for every Deus session without removing
any agent capability.

## Savings

Token estimate uses `chars / 3.7` (Claude BPE approximation for English
technical text). Absolute numbers are approximate; deltas are exact by char.

| Scenario                                | Baseline | After | Δ tok  | Δ %    |
|-----------------------------------------|---------:|------:|-------:|-------:|
| Host CC session (root `CLAUDE.md`)      |     784  |  631  |  -153  | -19.5% |
| Main group template turn-1              |     435  |  293  |  -142  | -32.8% |
| Global template (sub-groups, appended)  |     530  |  310  |  -220  | -41.5% |

The WhatsApp/Telegram main-group containers see the full savings: the group
`CLAUDE.md` loaded via `settingSources: ['project']` compresses from the main
template shape, and the global `CLAUDE.md` appended for non-main groups also
compresses. Compounded, a typical WhatsApp/Telegram session reduces its
turn-1 static overhead by 29–35%.

`.claudeignore` is a runtime filter — its effect isn't captured by the static
harness but prevents the occasional outlier where a wide Glob/Grep would
accidentally read `node_modules/` or `dist/` into context.

## What changed

### Compression

- **`CLAUDE.md`** (root) — removed the duplicated ADR mandatory-read
  paragraph. The same instruction already lives in
  `patterns/general-code.md:41`, which is the pattern file the ROUTER routes
  to for any change under `eval/`, `evolution/`, `src/startup-gate.ts`,
  `src/checks.ts`, `setup/`, or `scripts/memory_indexer.py`. One source of
  truth.
- **`groups/global/CLAUDE.md.template`** — compressed the Communication,
  Workspace & Memory, and Formatting sections. Every directive preserved in
  compressed form; only illustrative `customers.md`/`preferences.md` examples
  were dropped.
- **`groups/main/CLAUDE.md.template`** — same compression pattern.

### `.claudeignore`

Prevents Claude Code from reading `node_modules/`, `dist/`, `coverage/`,
`__pycache__/`, local logs/data, and CC temp files during Glob/Grep/Read
cycles. Runtime-only filter.

### Measurement + test harness (`scripts/token_bench/`)

- `harness.py` — captures per-file char + estimated-token count for the
  static-context components and computes turn-1 scenarios per channel.
- `diff.py` — compares two snapshots and reports per-file/per-scenario
  deltas.
- `keyword_bench.py` — deterministic fact-preservation check. Curated
  CRITICAL/SUPP fact list per file under `facts/`; keyword-match per fact;
  critical-coverage threshold ≥ 95%.
- `preservation_bench.py` — alternative LLM-based fact check (Ollama gemma4).
  Kept for reference; proved unreliable on small template files, documented
  below.
- `real_claude_probe.sh` — end-to-end behavior probe. Drops a CLAUDE.md into
  an isolated temp cwd and runs `claude -p` with 5 fixed prompts (formatting,
  bullets, internal-tag, voice-reminder, persona), emitting JSONL. Usable as
  a before/after check when compressing any CLAUDE.md.
- `aggregate_compression.py`, `fixtures.json` — supporting aggregator and
  fixtures.

### Reproducing the measurement

```bash
python3 scripts/token_bench/harness.py --label baseline
# ... make changes ...
python3 scripts/token_bench/harness.py --label after
python3 scripts/token_bench/diff.py \
  scripts/token_bench/results/baseline.json \
  scripts/token_bench/results/after.json

python3 scripts/token_bench/keyword_bench.py \
  --label root_claudemd \
  --compressed CLAUDE.md \
  --facts scripts/token_bench/facts/root_claudemd.txt
```

## Testing — 4 independent layers

| Layer | Method                                           | Result              |
|-------|--------------------------------------------------|---------------------|
| 1     | Manual semantic audit of every diff              | All directives preserved |
| 2     | Memory retrieval bench (145-q, gemma4:e4b judge) | 0.9931, invariant (vault untouched) |
| 3     | Real-Claude behavior probes via `claude -p`      | Identical across probes (baseline vs after) |
| 4     | Keyword preservation bench                       | 92.9 / 90.9 / 89.5 % critical coverage (root / global / main) |

Every MISS flagged by the keyword bench was manually verified against the
compressed file — in each case the information is preserved in paraphrased
form that a keyword matcher can't detect (e.g., `"logged, not sent"` vs
`"Text inside <internal> tags is logged but not sent"`). Zero real critical
regressions.

Note on `preservation_bench.py`: the LLM-judged bench using Ollama
`gemma4:e4b` was tried but proved unreliable on small template files —
returned non-deterministic empty responses even with `num_predict: 256` and
`think: false`, and reported verbatim-present facts as missing. Matches the
`gemma4 quirk — or response is empty` pattern documented elsewhere in the
codebase. Kept in the repo so future work can revisit with a different
judge, but not used as a gate here.

For a concrete read on how unreliable, the numbers per file (Ollama bench
vs the deterministic keyword bench vs a manual audit of every keyword-bench
MISS):

| File              | Ollama bench | Keyword bench | Manual audit |
|-------------------|-------------:|--------------:|-------------:|
| root `CLAUDE.md`  |        42.9% |         92.9% |         100% |
| global template   |        54.5% |         90.9% |         100% |
| main template     |        84.2% |         89.5% |         100% |

Typical failure mode: the bench flags a fact as missing (e.g. "agent
must skip INDEX read on session start") when the compressed file contains
the same directive in a paraphrased form one line down. Spot-checking a
sample of "missing" facts against the compressed source confirmed every
one was actually present. Conclusion: **gemma4:e4b is not a viable
single-question fact-verification judge at this doc scale.** Use
`keyword_bench.py` (deterministic — can false-negative on paraphrase but
never false-positive) and manually verify the flagged misses, or use
`real_claude_probe.sh` for a real-behavior read.

## What's on the table but blocked

The following items from Anthropic's published cost-reduction guidance are
not reachable through the current `@anthropic-ai/claude-agent-sdk` surface
(v0.2.76, latest 0.2.112 verified):

- `context_management` → server-side compaction (`compact_20260112`)
- Arbitrary `betas` → only `context-1m-2025-08-07` is accepted; not
  `token-efficient-tools-2025-02-19` or `compact-2026-01-12`
- Explicit `cache_control: {ttl: "1h"}` on custom prompt segments
- Built-in-tool output rewrite in PostToolUse hooks (only
  `updatedMCPToolOutput` for MCP tools is exposed)

All are available on the raw Anthropic Messages API. Shipping them through
the SDK would require the SDK to expose the options, or we'd have to rebuild
the Claude Code runtime (MCP routing, tool-result streaming, session resume)
ourselves. Tracked as follow-up.

## Eval pipeline

`evolution/` and `eval/` already route to Ollama (primary) / Gemini
(fallback), never Anthropic. Message Batches API (50% discount for async
work) is therefore not applicable.

## Follow-ups

1. Watch the Agent SDK changelog for `context_management` / arbitrary
   `betas` / `cache_control.ttl`. When exposed, wire them into
   `container/agent-runner/src/index.ts`.
2. Revisit built-in-tool output truncation when the SDK exposes an
   `updatedToolOutput` hook for non-MCP tools.
3. Optional: broader ADR/dedup sweep across `patterns/` and root `CLAUDE.md`.
   Low gain, small maintenance cost.

## Don't-regress note: `thinking.display`

The SDK exposes `ThinkingConfig.display` with values `"summarized"` or
`"omitted"`. Default behavior (we don't set `thinking` explicitly) uses
`"omitted"` — no thinking blocks flow into the response stream, which
keeps conversation history lean turn-over-turn.

If a future commit enables explicit extended thinking (e.g. to cap
reasoning cost), pair it with `display: 'omitted'` unless a specific
downstream consumer actually needs the summary:

```ts
thinking: { type: 'enabled', budgetTokens: 1024, display: 'omitted' }
```

`display` does **not** affect billing for the thinking itself (that
happens server-side regardless) — it affects whether summarized
reasoning blocks land in the response and then get re-sent as input on
every subsequent turn of an agent loop. Summarized thinking is useful
for debugging and reasoning UIs; in a long-lived container agent loop
it's pure per-turn overhead.
