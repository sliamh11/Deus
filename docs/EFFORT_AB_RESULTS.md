# `--effort` A/B probe results (2026-04-17)

## Setup

`scripts/token_bench/effort_probe.sh` runs 5 fixed prompts against the
WhatsApp main-group `CLAUDE.md` via `claude -p` in a temp cwd. Two variants:

- **default** — no `--effort` flag (Claude Code's own default)
- **low** — `--effort low`

Both run on Opus 4.7. Thinking is not explicitly enabled (SDK default: off on
Opus 4.7). Elapsed time is wall-clock including CLI startup; output captured
as JSON lines.

## Results

| Probe             | Default (ms) | Low (ms) | Δ ms  | Δ %   | Quality |
|-------------------|-------------:|---------:|------:|------:|---------|
| fmt_bold          |       7,347  |   5,219  | -2,128| -29%  | Low: cleaner, more concise |
| fmt_no_heading    |       8,755  |   8,227  |   -528|  -6%  | Tie — both pass, both well-structured |
| internal_tag      |       5,862  |   5,097  |   -765| -13%  | Tie — both answered "4", neither used tags (weak probe) |
| voice_reminder    |      13,907  |  14,531  |  +624 |  +4%  | Low: cleaner (no extra writability-warning noise) |
| persona_recall    |       5,853  |   5,104  |   -749| -13%  | Tie — both recalled correctly |
| **avg**           |     **8,345**|**7,636** |**-709**| **-8.5%** | |

**Zero regressions across all 5 probes.** On 4/5, `--effort low` is faster;
on `voice_reminder` it was 4% slower (within single-sample noise).

Output quality under `low` was **equal or better** — more concise, less
padding. Matches the migration-guide observation that Opus 4.7 calibrates
verbosity to task complexity, more aggressively at lower effort levels.

## Caveats

- n=5 probes — directional signal only, not statistical significance.
- `claude -p` CLI surface may differ slightly from container agent-runner's
  Agent SDK invocation. Test is an approximation of real container behavior.
- Opus 4.7 adaptive thinking was **not** opted into in either variant (we
  don't set `thinking: {type: 'adaptive'}` anywhere). The latency win is
  therefore attributable to effort-level output calibration, not thinking
  savings.
- Probes are single-turn. Multi-turn/tool-heavy sessions could show
  different patterns.

## Interpretation

`--effort low` on Opus 4.7 is a **small net win** for the WhatsApp/Telegram
personal-assistant use case. Short prompts, short tool chains, latency is
visible to users — exactly what `low` is designed for (per migration guide:
"Reserve for short, scoped tasks and latency-sensitive workloads that are
not intelligence-sensitive").

## Decision: ship now

Wired `effort: 'low'` into `container/agent-runner/src/index.ts`, gated by
`DEUS_AGENT_EFFORT` env var:

| env value                 | effort passed to SDK |
|---------------------------|----------------------|
| unset (default)           | `'low'`              |
| `low` / `medium` / `high` / `max` | matching value |
| `default`                 | `undefined` (SDK default) |
| anything else             | `'low'` (fallback)   |

Rationale for shipping now vs gating on more data:

- **Rollback is one env change + restart** — no code deploy, no risk of
  leaving bad code in `main`.
- **Zero regressions in the A/B** (n=5 but 5-for-5, not 3-of-5).
- **Migration guide explicitly endorses `low`** for short, scoped,
  latency-sensitive workloads. WhatsApp/Telegram personal-assistant
  traffic fits the profile exactly.
- **More synthetic benchmarking won't move the needle.** The real answer
  comes from production usage, which needs the flag shipped to observe.

## Monitoring

After deployment, watch for a week:

- Reflexion scores in `evolution/ilog/interaction_log.py` — any downward
  shift in judge quality scores.
- User-reported quality issues.
- p95 latency in container logs.

If anything regresses, revert with `DEUS_AGENT_EFFORT=default` and restart.

## Bigger samples (future work)

To tighten the quantitative picture:

- Expand `effort_probe.sh` to 20–30 more varied prompts (tool-using
  queries, gcal, voice reminders with complex detail, research queries).
- Add `gemma4:e4b` judge-scoring on the response pairs for a numeric
  quality score, not just eyeball inspection.
- Measure input/output tokens via the Messages API usage metadata (the
  `claude -p` CLI path in this probe doesn't expose it directly).
