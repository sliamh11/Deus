#!/usr/bin/env python3
"""
Token-efficiency analyzer for Deus.

Covers two execution paths:

  A) Container (channel traffic — WhatsApp/Telegram/etc.)
     - groups/<group_folder>/logs/usage.jsonl — per-turn SDK-reported billing
       tokens (input, output, cache_read, cache_create, cost, duration).
       Written by the usage-logging hook in container/agent-runner/.
     - groups/<group_folder>/logs/tool-sizes.jsonl — per-tool-call response
       sizes. Written by the tool-size logging hook.

  B) CLI (the `claude` binary launched by deus-cmd.sh — your interactive
        sessions in this terminal)
     - ~/.claude/projects/<encoded-cwd>/*.jsonl — Claude Code already records
       per-turn usage on every assistant message in its transcripts. No
       separate logger needed; the analyzer harvests those files directly.

Quality signal (applies to both paths):
  - ~/.deus/evolution.db `interactions` table — OllamaJudge scores + latency.

Usage:
    # Full report across everything:
    python3 scripts/analyze_token_efficiency.py

    # Scope to a date range (inclusive):
    python3 scripts/analyze_token_efficiency.py --since 2026-04-18 --until 2026-04-25

    # Scope to one channel group (CLI is always included):
    python3 scripts/analyze_token_efficiency.py --group whatsapp_main

    # Before/after comparison (splits the window at the cutoff):
    python3 scripts/analyze_token_efficiency.py \\
        --baseline-until 2026-04-22 --compare-from 2026-04-23

    # Override CLI transcript dir (useful when running from a worktree):
    python3 scripts/analyze_token_efficiency.py \\
        --cli-project-dir ~/.claude/projects/-Users-liam10play-deus

    # JSON output for scripting:
    python3 scripts/analyze_token_efficiency.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
GROUPS_DIR = PROJECT_ROOT / 'groups'
EVOLUTION_DB = Path.home() / '.deus' / 'evolution.db'

# Claude Code stores per-project transcripts at
# ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl — encoded-cwd replaces
# every '/' in the absolute project path with '-' and prepends a leading '-'.
# This is our source of truth for CLI-session token usage (no extra hook
# needed — Claude Code already records usage on every assistant message).
_cwd_encoded = '-' + str(PROJECT_ROOT).replace('/', '-')
CLI_TRANSCRIPTS_DIR = Path.home() / '.claude' / 'projects' / _cwd_encoded


@dataclass
class UsageEntry:
    ts: datetime
    session_id: str
    group: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_create: int
    num_turns: int
    duration_ms: float
    total_cost_usd: float


@dataclass
class ToolSizeEntry:
    ts: datetime
    group: str
    tool: str
    bytes_: int
    approx_tokens: int


@dataclass
class InteractionRow:
    ts: datetime
    group: str
    session_id: str
    judge_score: float | None
    latency_ms: float | None


def parse_iso(s: str) -> datetime:
    # JSONL log uses ISO-8601 with Z; SQLite stores as ISO string too.
    # Normalize to offset-naive UTC so we can compare against --since/--until
    # which argparse gives us as offset-naive.
    dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    if since and ts < since:
        return False
    if until and ts > until:
        return False
    return True


def load_usage(groups: list[str], since, until) -> list[UsageEntry]:
    out: list[UsageEntry] = []
    for g in groups:
        p = GROUPS_DIR / g / 'logs' / 'usage.jsonl'
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                ts = parse_iso(d['ts'])
                if not in_window(ts, since, until):
                    continue
                out.append(
                    UsageEntry(
                        ts=ts,
                        session_id=d.get('session_id', ''),
                        group=g,
                        input_tokens=int(d.get('input_tokens', 0) or 0),
                        output_tokens=int(d.get('output_tokens', 0) or 0),
                        cache_read=int(d.get('cache_read_input_tokens', 0) or 0),
                        cache_create=int(
                            d.get('cache_creation_input_tokens', 0) or 0
                        ),
                        num_turns=int(d.get('num_turns', 0) or 0),
                        duration_ms=float(d.get('duration_ms', 0) or 0),
                        total_cost_usd=float(d.get('total_cost_usd', 0) or 0),
                    )
                )
            except (ValueError, KeyError) as e:
                print(f'skipping malformed usage line in {p}: {e}', file=sys.stderr)
    return out


def load_cli_usage(since, until) -> list[UsageEntry]:
    """Harvest per-turn usage from Claude Code transcripts (this project only).
    Each assistant message in the transcript carries full usage metadata
    (input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens) — we don't need a separate logger.
    """
    if not CLI_TRANSCRIPTS_DIR.exists():
        return []
    out: list[UsageEntry] = []
    for transcript in CLI_TRANSCRIPTS_DIR.glob('*.jsonl'):
        try:
            lines = transcript.read_text(
                encoding='utf-8', errors='replace'
            ).splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('type') != 'assistant':
                continue
            msg = entry.get('message')
            if not isinstance(msg, dict):
                continue
            usage = msg.get('usage')
            if not isinstance(usage, dict):
                continue
            ts_str = entry.get('timestamp')
            if not ts_str:
                continue
            try:
                ts = parse_iso(ts_str)
            except ValueError:
                continue
            if not in_window(ts, since, until):
                continue
            out.append(
                UsageEntry(
                    ts=ts,
                    session_id=entry.get('sessionId', transcript.stem),
                    group='cli:deus',
                    input_tokens=int(usage.get('input_tokens', 0) or 0),
                    output_tokens=int(usage.get('output_tokens', 0) or 0),
                    cache_read=int(usage.get('cache_read_input_tokens', 0) or 0),
                    cache_create=int(
                        usage.get('cache_creation_input_tokens', 0) or 0
                    ),
                    # Claude Code transcripts don't store num_turns /
                    # duration_ms / cost per-message; leave as zeros.
                    num_turns=0,
                    duration_ms=0.0,
                    total_cost_usd=0.0,
                )
            )
    return out


def load_tool_sizes(groups: list[str], since, until) -> list[ToolSizeEntry]:
    out: list[ToolSizeEntry] = []
    for g in groups:
        p = GROUPS_DIR / g / 'logs' / 'tool-sizes.jsonl'
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                ts = parse_iso(d['ts'])
                if not in_window(ts, since, until):
                    continue
                out.append(
                    ToolSizeEntry(
                        ts=ts,
                        group=g,
                        tool=d.get('tool', ''),
                        bytes_=int(d.get('bytes', 0) or 0),
                        approx_tokens=int(d.get('approx_tokens', 0) or 0),
                    )
                )
            except (ValueError, KeyError) as e:
                print(
                    f'skipping malformed tool-size line in {p}: {e}',
                    file=sys.stderr,
                )
    return out


def load_interactions(
    groups: list[str] | None, since, until
) -> list[InteractionRow]:
    if not EVOLUTION_DB.exists():
        return []
    conn = sqlite3.connect(str(EVOLUTION_DB))
    conn.row_factory = sqlite3.Row
    q = 'SELECT timestamp, group_folder, session_id, judge_score, latency_ms FROM interactions WHERE 1=1'
    params: list = []
    if groups:
        placeholders = ','.join('?' for _ in groups)
        q += f' AND group_folder IN ({placeholders})'
        params.extend(groups)
    rows: list[InteractionRow] = []
    for r in conn.execute(q, params):
        try:
            ts = parse_iso(r['timestamp'])
        except ValueError:
            continue
        if not in_window(ts, since, until):
            continue
        rows.append(
            InteractionRow(
                ts=ts,
                group=r['group_folder'] or '',
                session_id=r['session_id'] or '',
                judge_score=r['judge_score'],
                latency_ms=r['latency_ms'],
            )
        )
    conn.close()
    return rows


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summarize_usage(entries: list[UsageEntry]) -> dict:
    if not entries:
        return {'n_turns': 0, 'n_sessions': 0}
    input_tokens = [e.input_tokens for e in entries]
    output_tokens = [e.output_tokens for e in entries]
    cache_reads = [e.cache_read for e in entries]
    cache_creates = [e.cache_create for e in entries]
    durations = [e.duration_ms for e in entries]
    costs = [e.total_cost_usd for e in entries]

    # Per-session totals
    by_session: dict[str, list[UsageEntry]] = defaultdict(list)
    for e in entries:
        by_session[e.session_id].append(e)
    session_input_totals = [sum(x.input_tokens for x in v) for v in by_session.values()]
    session_output_totals = [
        sum(x.output_tokens for x in v) for v in by_session.values()
    ]
    session_turn_counts = [len(v) for v in by_session.values()]

    # Anthropic cache semantics: for each request, total prompt size is
    # input_tokens (uncached) + cache_read_input_tokens (served from cache)
    # + cache_creation_input_tokens (newly cached this turn).
    # Hit ratio = cache_read / total_prompt_size.
    total_prompt = sum(input_tokens) + sum(cache_reads) + sum(cache_creates)
    cache_hit_ratio = sum(cache_reads) / total_prompt if total_prompt else 0.0

    return {
        'n_turns': len(entries),
        'n_sessions': len(by_session),
        'per_turn': {
            'input_tokens': {
                'mean': statistics.mean(input_tokens),
                'median': statistics.median(input_tokens),
                'p90': percentile([float(x) for x in input_tokens], 0.9),
            },
            'output_tokens': {
                'mean': statistics.mean(output_tokens),
                'median': statistics.median(output_tokens),
                'p90': percentile([float(x) for x in output_tokens], 0.9),
            },
            'cache_read_tokens': {
                'mean': statistics.mean(cache_reads),
                'median': statistics.median(cache_reads),
            },
            'cache_creation_tokens': {
                'mean': statistics.mean(cache_creates),
                'median': statistics.median(cache_creates),
            },
            'duration_ms': {
                'mean': statistics.mean(durations),
                'median': statistics.median(durations),
                'p95': percentile(durations, 0.95),
            },
            'cost_usd': {
                'mean': statistics.mean(costs),
                'total': sum(costs),
            },
        },
        'per_session': {
            'input_tokens_total': {
                'mean': statistics.mean(session_input_totals),
                'median': statistics.median(session_input_totals),
            },
            'output_tokens_total': {
                'mean': statistics.mean(session_output_totals),
                'median': statistics.median(session_output_totals),
            },
            'turns_per_session': {
                'mean': statistics.mean(session_turn_counts),
                'median': statistics.median(session_turn_counts),
            },
        },
        'cache_hit_ratio': cache_hit_ratio,
    }


def summarize_tool_sizes(entries: list[ToolSizeEntry]) -> dict:
    if not entries:
        return {'n_calls': 0}
    by_tool: dict[str, list[ToolSizeEntry]] = defaultdict(list)
    for e in entries:
        by_tool[e.tool].append(e)
    tool_summary = {}
    for tool, es in by_tool.items():
        toks = [e.approx_tokens for e in es]
        tool_summary[tool] = {
            'n_calls': len(es),
            'approx_tokens_total': sum(toks),
            'approx_tokens_mean': statistics.mean(toks),
            'approx_tokens_p90': percentile([float(x) for x in toks], 0.9),
        }
    total_tool_tokens = sum(e.approx_tokens for e in entries)
    return {
        'n_calls': len(entries),
        'approx_tokens_total': total_tool_tokens,
        'per_tool': tool_summary,
    }


def summarize_quality(rows: list[InteractionRow]) -> dict:
    scored = [r.judge_score for r in rows if r.judge_score is not None]
    latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
    if not scored:
        return {'n_scored': 0, 'n_total': len(rows)}
    return {
        'n_total': len(rows),
        'n_scored': len(scored),
        'judge_score': {
            'mean': statistics.mean(scored),
            'median': statistics.median(scored),
            'p10': percentile(scored, 0.1),
            'stdev': statistics.pstdev(scored) if len(scored) > 1 else 0.0,
        },
        'latency_ms': {
            'mean': statistics.mean(latencies) if latencies else 0.0,
            'p95': percentile(latencies, 0.95) if latencies else 0.0,
        },
    }


def tool_share_of_input(usage: dict, tools: dict) -> float | None:
    total_input = usage.get('per_turn', {}).get('input_tokens', {}).get('mean', 0) * usage.get(
        'n_turns', 0
    )
    total_tool = tools.get('approx_tokens_total', 0)
    if not total_input:
        return None
    return total_tool / total_input


def format_number(x, digits=1) -> str:
    if isinstance(x, float):
        return f'{x:,.{digits}f}'
    return f'{x:,}'


def _format_usage_block(title: str, usage: dict) -> list[str]:
    lines: list[str] = [f'  -- {title} --']
    lines.append(f'    Turns logged:   {usage.get("n_turns", 0):,}')
    lines.append(f'    Sessions:       {usage.get("n_sessions", 0):,}')
    if usage.get('n_turns'):
        pt = usage['per_turn']
        lines.append(
            f'    input tokens:   mean {format_number(pt["input_tokens"]["mean"])}  '
            f'median {format_number(pt["input_tokens"]["median"])}  '
            f'p90 {format_number(pt["input_tokens"]["p90"])}'
        )
        lines.append(
            f'    output tokens:  mean {format_number(pt["output_tokens"]["mean"])}  '
            f'median {format_number(pt["output_tokens"]["median"])}  '
            f'p90 {format_number(pt["output_tokens"]["p90"])}'
        )
        lines.append(
            f'    cache read:     mean {format_number(pt["cache_read_tokens"]["mean"])}  '
            f'median {format_number(pt["cache_read_tokens"]["median"])}'
        )
        lines.append(
            f'    cache create:   mean {format_number(pt["cache_creation_tokens"]["mean"])}  '
            f'median {format_number(pt["cache_creation_tokens"]["median"])}'
        )
        lines.append(
            f'    cache hit %:    {usage["cache_hit_ratio"] * 100:.1f}%  '
            f'(cache_read / (input + cache_read + cache_create))'
        )
        ps = usage['per_session']
        lines.append(
            f'    per session:    input_total mean {format_number(ps["input_tokens_total"]["mean"])}  '
            f'output_total mean {format_number(ps["output_tokens_total"]["mean"])}  '
            f'turns mean {format_number(ps["turns_per_session"]["mean"])}'
        )
    return lines


def format_report(
    label: str,
    container_usage: dict,
    cli_usage: dict,
    tools: dict,
    quality: dict,
) -> str:
    lines: list[str] = []
    lines.append(f'=== {label} ===')
    lines.extend(_format_usage_block('Container (channel traffic)', container_usage))
    lines.append('')
    lines.extend(_format_usage_block('CLI (this session path)', cli_usage))

    # Container-only extras: duration / cost come from the SDK result message
    # which CLI transcripts don't store.
    if container_usage.get('n_turns'):
        pt = container_usage['per_turn']
        lines.append('')
        lines.append('  -- Container-only detail (from SDK result) --')
        lines.append(
            f'    duration ms:    mean {format_number(pt["duration_ms"]["mean"])}  '
            f'median {format_number(pt["duration_ms"]["median"])}  '
            f'p95 {format_number(pt["duration_ms"]["p95"])}'
        )
        lines.append(
            f'    cost USD:       mean {pt["cost_usd"]["mean"]:.5f}  '
            f'total {pt["cost_usd"]["total"]:.4f}'
        )

    if tools.get('n_calls'):
        lines.append('')
        lines.append('  -- Tool output (container only) --')
        lines.append(f'    calls:          {tools["n_calls"]:,}')
        lines.append(
            f'    tokens total:   {format_number(tools["approx_tokens_total"])}'
        )
        share = tool_share_of_input(container_usage, tools)
        if share is not None:
            lines.append(f'    share of input: {share * 100:.1f}%')
        lines.append('    top tools:')
        top = sorted(
            tools['per_tool'].items(),
            key=lambda kv: kv[1]['approx_tokens_total'],
            reverse=True,
        )[:5]
        for tool, stats in top:
            lines.append(
                f'      {tool:<12}  calls {stats["n_calls"]:>4}  '
                f'tokens_total {format_number(stats["approx_tokens_total"]):>10}  '
                f'mean {format_number(stats["approx_tokens_mean"]):>8}'
            )

    if quality.get('n_scored'):
        js = quality['judge_score']
        lines.append(
            f'  Quality (judge_score, n={quality["n_scored"]}):'
        )
        lines.append(
            f'    mean {js["mean"]:.3f}  median {js["median"]:.3f}  '
            f'p10 {js["p10"]:.3f}  stdev {js["stdev"]:.3f}'
        )
        lat = quality['latency_ms']
        lines.append(
            f'  Interaction latency: mean {format_number(lat["mean"])} ms  '
            f'p95 {format_number(lat["p95"])} ms'
        )
    else:
        lines.append(f'  Quality: n_total {quality.get("n_total", 0)}, none scored yet')

    return '\n'.join(lines)


def _compare_usage_block(label: str, b_usage: dict, c_usage: dict) -> list[str]:
    if not (b_usage.get('n_turns') and c_usage.get('n_turns')):
        return [
            f'  {label}: insufficient data '
            f'(baseline={b_usage.get("n_turns", 0)} turns, '
            f'compare={c_usage.get("n_turns", 0)} turns)'
        ]
    out: list[str] = [f'  -- {label} --']
    b_in = b_usage['per_turn']['input_tokens']['mean']
    c_in = c_usage['per_turn']['input_tokens']['mean']
    b_out = b_usage['per_turn']['output_tokens']['mean']
    c_out = c_usage['per_turn']['output_tokens']['mean']
    out.append(
        f'    input tokens / turn:   {format_number(b_in)} → {format_number(c_in)}   '
        f'Δ {c_in - b_in:+.1f} ({(c_in - b_in) / b_in * 100 if b_in else 0:+.2f}%)'
    )
    out.append(
        f'    output tokens / turn:  {format_number(b_out)} → {format_number(c_out)}   '
        f'Δ {c_out - b_out:+.1f} ({(c_out - b_out) / b_out * 100 if b_out else 0:+.2f}%)'
    )
    b_cache = b_usage['cache_hit_ratio']
    c_cache = c_usage['cache_hit_ratio']
    out.append(
        f'    cache hit ratio:       {b_cache * 100:.1f}% → {c_cache * 100:.1f}%   '
        f'Δ {(c_cache - b_cache) * 100:+.1f} pp'
    )
    b_ps = b_usage['per_session']['input_tokens_total']['mean']
    c_ps = c_usage['per_session']['input_tokens_total']['mean']
    out.append(
        f'    input tokens / sess:   {format_number(b_ps)} → {format_number(c_ps)}   '
        f'Δ {c_ps - b_ps:+.1f} ({(c_ps - b_ps) / b_ps * 100 if b_ps else 0:+.2f}%)'
    )
    return out


def compare_periods(baseline: dict, compare: dict) -> str:
    lines: list[str] = ['=== Comparison (compare − baseline) ===']
    lines.extend(
        _compare_usage_block(
            'Container', baseline['container_usage'], compare['container_usage']
        )
    )
    lines.append('')
    lines.extend(
        _compare_usage_block('CLI', baseline['cli_usage'], compare['cli_usage'])
    )
    # Container-only cost delta
    b_cu = baseline['container_usage']
    c_cu = compare['container_usage']
    if b_cu.get('n_turns') and c_cu.get('n_turns'):
        b_cost = b_cu['per_turn']['cost_usd']['mean']
        c_cost = c_cu['per_turn']['cost_usd']['mean']
        lines.append('')
        lines.append(
            f'  container cost / turn:  {b_cost:.5f} → {c_cost:.5f}   '
            f'Δ {c_cost - b_cost:+.5f} '
            f'({(c_cost - b_cost) / b_cost * 100 if b_cost else 0:+.2f}%)'
        )

    b_q = baseline['quality']
    c_q = compare['quality']
    if b_q.get('n_scored') and c_q.get('n_scored'):
        b_score = b_q['judge_score']['mean']
        c_score = c_q['judge_score']['mean']
        lines.append(
            f'  quality (mean score):   {b_score:.3f} → {c_score:.3f}   '
            f'Δ {c_score - b_score:+.3f} '
            f'({(c_score - b_score) / b_score * 100 if b_score else 0:+.2f}%)'
        )
    return '\n'.join(lines)


def discover_groups() -> list[str]:
    if not GROUPS_DIR.exists():
        return []
    return sorted(
        [
            p.name
            for p in GROUPS_DIR.iterdir()
            if p.is_dir() and (p / 'logs').exists()
        ]
    )


def analyze(
    groups: list[str], since, until, include_cli: bool = True
) -> dict:
    container_usage = load_usage(groups, since, until)
    cli_usage = load_cli_usage(since, until) if include_cli else []
    tools = load_tool_sizes(groups, since, until)
    quality_rows = load_interactions(groups, since, until)
    return {
        'container_usage': summarize_usage(container_usage),
        'cli_usage': summarize_usage(cli_usage),
        'tools': summarize_tool_sizes(tools),
        'quality': summarize_quality(quality_rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Analyze Deus token-usage and quality logs.'
    )
    ap.add_argument('--group', action='append', help='Restrict to this group folder (repeatable). Default: all.')
    ap.add_argument('--since', help='Start of window (YYYY-MM-DD, inclusive).')
    ap.add_argument('--until', help='End of window (YYYY-MM-DD, inclusive).')
    ap.add_argument('--baseline-until', help='For before/after: last day of baseline period.')
    ap.add_argument('--compare-from', help='For before/after: first day of comparison period.')
    ap.add_argument('--json', action='store_true', help='Emit JSON instead of text report.')
    ap.add_argument(
        '--cli-project-dir',
        help=(
            'Override the Claude Code transcript project dir. '
            'Default: ~/.claude/projects/<encoded-cwd-of-script-root>. '
            'Set this when running the script from a worktree.'
        ),
    )
    args = ap.parse_args()

    # Allow overriding where we look for Claude Code transcripts.
    if args.cli_project_dir:
        global CLI_TRANSCRIPTS_DIR
        CLI_TRANSCRIPTS_DIR = Path(args.cli_project_dir).expanduser()

    def parse_day(s: str | None, end_of_day: bool = False) -> datetime | None:
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            print(f'bad date: {s}', file=sys.stderr)
            sys.exit(2)
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d.replace(tzinfo=None) if d.tzinfo is None else d

    groups = args.group or discover_groups()
    # CLI data is still loaded even when no channel groups exist; the deus
    # repo installation always has a CLI transcript dir (or not — we handle
    # that gracefully). So we only warn about groups, not fail.
    if not groups and not CLI_TRANSCRIPTS_DIR.exists():
        print(
            'no groups with logs/ and no CLI transcripts found',
            file=sys.stderr,
        )
        return 1

    if args.baseline_until and args.compare_from:
        # Before/after mode
        baseline_until = parse_day(args.baseline_until, end_of_day=True)
        compare_from = parse_day(args.compare_from)
        since = parse_day(args.since)
        until = parse_day(args.until, end_of_day=True)
        baseline = analyze(groups, since, baseline_until)
        compare = analyze(groups, compare_from, until)
        if args.json:
            print(
                json.dumps(
                    {
                        'baseline': {'window': (args.since, args.baseline_until), **baseline},
                        'compare': {'window': (args.compare_from, args.until), **compare},
                    },
                    indent=2,
                )
            )
            return 0
        print(
            format_report(
                f'Baseline (…→{args.baseline_until})',
                baseline['container_usage'],
                baseline['cli_usage'],
                baseline['tools'],
                baseline['quality'],
            )
        )
        print()
        print(
            format_report(
                f'Compare ({args.compare_from}→…)',
                compare['container_usage'],
                compare['cli_usage'],
                compare['tools'],
                compare['quality'],
            )
        )
        print()
        print(compare_periods(baseline, compare))
        return 0

    # Single-window mode
    since = parse_day(args.since)
    until = parse_day(args.until, end_of_day=True)
    result = analyze(groups, since, until)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    window_label = ''
    if args.since or args.until:
        window_label = f' ({args.since or "…"} → {args.until or "…"})'
    print(
        format_report(
            f'Deus token-efficiency report{window_label} — groups: {", ".join(groups) or "none"}',
            result['container_usage'],
            result['cli_usage'],
            result['tools'],
            result['quality'],
        )
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
