# Spike: Claude-path observation masking via transcript rewrite (LIA-378)

**Date:** 2026-07-05 · **Verdict: VIABLE** — the Claude CLI reconstructs resumed
context from the on-disk transcript JSONL with no caching or checksum; an edited
`tool_result` is exactly what the resumed model sees.

## Question

The Claude backend has no Deus-owned message array (the SDK subprocess manages
history), and PostToolUse hooks cannot rewrite built-in tool results
(`updatedMCPToolOutput` is `mcp__*`-only — verified in LIA-379). The only
plausible seam for masking stale tool results is rewriting the transcript JSONL
**between turns** (no subprocess running; `resume`/`resumeSessionAt` rebuild
history from disk). Does the CLI actually re-read mutated content, or does it
cache/validate?

## Method (synthetic scratch session — no personal data)

1. Scratch dir with `probe.txt` containing a sentinel string
   (`SENTINEL-CONTENT-ALPHA-7391`); `claude -p --model haiku "Read probe.txt,
   reply READ DONE"` → transcript with one Read `tool_result` carrying the
   sentinel (1 occurrence in the JSONL).
2. **Probe A** — `claude -p --resume <sid> "quote the sentinel from the earlier
   tool result, do not re-read"` against the ORIGINAL transcript →
   `SENTINEL-CONTENT-ALPHA-7391` (baseline: resumed context carries the result).
3. Edit the session JSONL in place: replace the sentinel with
   `MASKED-PLACEHOLDER-BETA-0042` (content-only edit, structure untouched).
4. **Probe B** — same resume prompt against the EDITED transcript →
   `MASKED-PLACEHOLDER-BETA-0042`.

The resumed model reproduced the edited placeholder, not the original content:
history is re-read from disk on every resume, byte-for-byte, no integrity check
blocking content-only edits.

## Implications for slice 2 (Claude-path masking)

- Mechanism: a between-turn pass (the container agent-runner's outer loop, after
  a `query()` returns and before the next `resume`) rewrites stale `tool_result`
  contents in the session transcript with a placeholder + pointer.
- Losslessness: pair each rewrite with LIA-374-style content-addressed retention
  of the pre-edit transcript (or of each evicted block) so the placeholder can
  point at retained bytes — unlike llama-cpp's in-memory array, the Claude path
  CAN be made lossless.
- Cache economics caveat: editing early-transcript content invalidates the
  prompt-cache prefix from the edit point on the next call — the rewrite should
  batch (mask many results at once, e.g. at compaction-pressure thresholds)
  rather than fire per turn, or the cache-write cost eats the savings.
- Risks to design around: SDK version drift in JSONL schema (pin a structural
  round-trip test), concurrent writes (only rewrite while no subprocess runs),
  and `resultWasTruncated`-style metadata consistency.

## Scope of this spike

Read-only finding; nothing wired into production. Slice-2 scoping lives in
LIA-378. Scratch session dirs deleted after the run.
