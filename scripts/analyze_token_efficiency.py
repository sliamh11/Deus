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

  B) CLI (the `claude` binary launched by deus-cmd.sh — interactive sessions)
     - ~/.claude/projects/<encoded-cwd>/*.jsonl — Claude Code records per-turn
       usage (incl. the model name) on every assistant message. By default we
       aggregate across ALL projects (matching ccusage), deduped by session id.
       --project filters by dir substring; --cli-project-dir scopes to one dir.

Layered output (the `deus usage` view):
  - Efficiency (pricing-independent): per-model cacheRead:output, amortization
    (cacheRead:cacheCreation), output-share, input/turn. Always computed.
  - Cost (configurable): notional $ via a built-in per-model rate table, or
    --rates override; degrades to "—" for models without a rate. --pricing none
    shows tokens/efficiency only.

Quality signal (applies to both paths):
  - ~/.deus/evolution.db `interactions` table — OllamaJudge scores + latency.

Usage:
    # Full report across ALL projects:
    python3 scripts/analyze_token_efficiency.py

    # Scope to a date range (inclusive):
    python3 scripts/analyze_token_efficiency.py --since 2026-04-18 --until 2026-04-25

    # Filter CLI to projects whose dir name contains "deus"; one channel group:
    python3 scripts/analyze_token_efficiency.py --project deus --group whatsapp_main

    # Before/after comparison (splits the window at the cutoff):
    python3 scripts/analyze_token_efficiency.py \\
        --baseline-until 2026-04-22 --compare-from 2026-04-23

    # Scope to a single transcript dir (disables all-projects scan):
    python3 scripts/analyze_token_efficiency.py \\
        --cli-project-dir ~/.claude/projects/-Users-<username>-<project>

    # Efficiency-only (no $), or custom rates, or JSON:
    python3 scripts/analyze_token_efficiency.py --pricing none
    python3 scripts/analyze_token_efficiency.py --rates ~/my-rates.json
    python3 scripts/analyze_token_efficiency.py --json
"""

from __future__ import annotations

import argparse
import json
import os
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
#
# By default we aggregate across ALL projects (every dir under projects/), not
# just this repo — so `deus usage` matches ccusage's all-projects totals and
# captures work in other repos. `--cli-project-dir` overrides to a single dir;
# `--project <substr>` filters the all-projects glob by encoded dirname.
CLI_PROJECTS_ROOT = Path.home() / '.claude' / 'projects'
# Single-dir override target (set via --cli-project-dir). None = all-projects.
CLI_TRANSCRIPTS_DIR: Path | None = None


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
    # Model name (e.g. 'claude-opus-4-8'); empty when unknown. Drives the
    # per-model breakdown.
    model: str = ''


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
    """Load container (channel) per-turn usage from groups/<g>/logs/usage.jsonl.

    Token counts live INSIDE the ``model_usage`` map (keyed by model name) using
    camelCase keys — ``inputTokens``, ``outputTokens``, ``cacheReadInputTokens``,
    ``cacheCreationInputTokens``, ``costUSD`` — NOT at the top level. (The top
    level only carries ts/session_id/num_turns/duration_ms/total_cost_usd.) We
    emit one UsageEntry per model sub-entry so the per-model breakdown is exact;
    record-level duration/num_turns are attributed to the first model only so
    multi-model turns don't inflate duration stats.
    """
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
                model_usage = d.get('model_usage') or {}
                session_id = d.get('session_id', '')
                num_turns = int(d.get('num_turns', 0) or 0)
                duration_ms = float(d.get('duration_ms', 0) or 0)
                if not model_usage:
                    # No per-model block (older/edge records): keep the
                    # record-level cost/duration so totals don't regress.
                    out.append(
                        UsageEntry(
                            ts=ts, session_id=session_id, group=g,
                            input_tokens=0, output_tokens=0, cache_read=0,
                            cache_create=0, num_turns=num_turns,
                            duration_ms=duration_ms,
                            total_cost_usd=float(d.get('total_cost_usd', 0) or 0),
                        )
                    )
                    continue
                first = True
                for model, mu in model_usage.items():
                    if not isinstance(mu, dict):
                        continue
                    out.append(
                        UsageEntry(
                            ts=ts,
                            session_id=session_id,
                            group=g,
                            input_tokens=int(mu.get('inputTokens', 0) or 0),
                            output_tokens=int(mu.get('outputTokens', 0) or 0),
                            cache_read=int(mu.get('cacheReadInputTokens', 0) or 0),
                            cache_create=int(
                                mu.get('cacheCreationInputTokens', 0) or 0
                            ),
                            # Duration/turns are per-record, not per-model:
                            # attribute to the first model only.
                            num_turns=num_turns if first else 0,
                            duration_ms=duration_ms if first else 0.0,
                            total_cost_usd=float(mu.get('costUSD', 0) or 0),
                            model=str(model),
                        )
                    )
                    first = False
            except (ValueError, KeyError) as e:
                print(f'skipping malformed usage line in {p}: {e}', file=sys.stderr)
    return out


def _decode_project_label(encoded: str) -> str:
    """Best-effort, display-only decode of an encoded project dir name back to a
    readable label. The encoding ('/'→'-', leading '-') is lossy (path
    components containing '-' are indistinguishable from separators), so we use
    this only for display — never for filtering. Strips the home prefix
    (Users/<user> or home/<user>) and rejoins the rest with '/', e.g.
    '-Users-<username>-deus' → 'deus', '-Users-<u>-work-acme' → 'work/acme'.
    """
    parts = [p for p in encoded.lstrip('-').split('-') if p]
    if len(parts) >= 2 and parts[0] in ('Users', 'home'):
        parts = parts[2:]
    return '/'.join(parts) if parts else encoded


def _iter_project_dirs(project_filters: list[str] | None) -> list[Path]:
    """Top-level project transcript dirs to scan.

    - If a single-dir override (CLI_TRANSCRIPTS_DIR) is set, use only that.
    - Otherwise scandir ~/.claude/projects/*, skipping dirs that contain no
      transcript anywhere in their subtree (avoids ~1000 ephemeral run dirs).
      Transcripts can be NESTED several levels deep, so the emptiness check and
      the loader both recurse (rglob). When project_filters is given, keep dirs
      whose name contains any filter as a case-insensitive substring.
    """
    if CLI_TRANSCRIPTS_DIR is not None:
        return [CLI_TRANSCRIPTS_DIR] if CLI_TRANSCRIPTS_DIR.exists() else []
    if not CLI_PROJECTS_ROOT.exists():
        return []
    filters = [f.lower() for f in (project_filters or [])]
    dirs: list[Path] = []
    with os.scandir(CLI_PROJECTS_ROOT) as it:
        for de in it:
            if not de.is_dir():
                continue
            name = de.name
            if filters and not any(f in name.lower() for f in filters):
                continue
            p = Path(de.path)
            # Skip subtrees with no transcripts (cheap: stop at first match).
            if not next(p.rglob('*.jsonl'), None):
                continue
            dirs.append(p)
    return dirs


def load_cli_usage(
    since, until, project_filters: list[str] | None = None
) -> list[UsageEntry]:
    """Harvest per-turn usage from Claude Code transcripts across ALL projects
    (or a single dir / filtered subset). Each assistant message carries full
    usage metadata plus the model name.

    Dedup is by the ``message.id``+``requestId`` composite GLOBALLY (first
    occurrence wins). Claude Code logs a single API response as several chained
    transcript nodes (distinct uuids, identical message.id/requestId/usage), and
    resuming/forking copies prior messages into new files — so the same response
    recurs many times (~2.9x overcount if not deduped). One API response is
    billed once, so we count it once. Sidechain/subagent messages have their own
    ids and ARE counted — they're real usage. Falls back to uuid (always unique,
    so the entry is counted) when a message lacks id/requestId.

    NOTE: absolute token totals can differ from ccusage (it handles these
    DAG-duplicate subagent messages differently); the efficiency RATIOS are
    dedup-invariant because duplication copies the whole usage block.
    """
    # message.id is API-assigned and globally unique, so a single global set is
    # safe (no cross-project collisions suppress real counts) and matches
    # ccusage's global dedup.
    out: list[UsageEntry] = []
    seen_msg_ids: set[str] = set()
    for proj_dir in _iter_project_dirs(project_filters):
        label = _decode_project_label(proj_dir.name)
        for transcript in proj_dir.rglob('*.jsonl'):
            try:
                lines = transcript.read_text(
                    encoding='utf-8', errors='replace'
                ).splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines):
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
                mid = msg.get('id')
                # Prefer the API message id (+ requestId); fall back to the
                # entry uuid; finally a per-file:line synthetic key so an entry
                # with no id at all is still deduped if it recurs verbatim.
                dedup_key = (
                    f'{mid}:{entry.get("requestId")}' if mid
                    else entry.get('uuid') or f'{transcript}:{lineno}'
                )
                if dedup_key in seen_msg_ids:
                    continue
                seen_msg_ids.add(dedup_key)
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
                        group=f'cli:{label}',
                        input_tokens=int(usage.get('input_tokens', 0) or 0),
                        output_tokens=int(usage.get('output_tokens', 0) or 0),
                        cache_read=int(
                            usage.get('cache_read_input_tokens', 0) or 0
                        ),
                        cache_create=int(
                            usage.get('cache_creation_input_tokens', 0) or 0
                        ),
                        # Claude Code transcripts don't store num_turns /
                        # duration_ms / cost per-message; leave as zeros.
                        num_turns=0,
                        duration_ms=0.0,
                        total_cost_usd=0.0,
                        model=str(msg.get('model', '') or ''),
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


# --------------------------------------------------------------------------
# Cost layer (configurable, pricing-DEPENDENT) and efficiency layer
# (pricing-INDEPENDENT). The efficiency ratios never need rates; the $ column
# uses MODEL_RATES and degrades to "unavailable" for models it doesn't know
# (e.g. proxied gemini/gpt-5). Rates are public Anthropic list prices (USD per
# 1M tokens) — for a subscription user these are notional/leverage; for an
# API-direct user they ARE the bill. Concept mirrors the private TS RATES map
# (src/private/orchestrator/cost-tracker.ts) but is reimplemented here so this
# public feature carries no private dependency. Override via --rates <json>.
#
# Matched by substring on the model name so opus-4-6/4-7/4-8 share one rule.
# Each value: input, output, cache_write (5-min), cache_read — USD per 1M.
MODEL_RATES: dict[str, dict[str, float]] = {
    'opus':   {'input': 15.0, 'output': 75.0, 'cache_write': 18.75, 'cache_read': 1.50},
    'sonnet': {'input': 3.0,  'output': 15.0, 'cache_write': 3.75,  'cache_read': 0.30},
    'haiku':  {'input': 1.0,  'output': 5.0,  'cache_write': 1.25,  'cache_read': 0.10},
}


def rate_for(model: str, rates: dict[str, dict[str, float]]) -> dict | None:
    """Resolve a model name to a rate row by case-insensitive substring match
    (longest key first so a more specific override can win). None if unknown."""
    m = (model or '').lower()
    for key in sorted(rates, key=len, reverse=True):
        if key.lower() in m:
            return rates[key]
    return None


def load_rate_overrides(path: str | None) -> dict[str, dict[str, float]]:
    """Merge a --rates JSON file over the built-in MODEL_RATES (shallow per
    model key). The file maps model-substring -> {input, output, cache_write,
    cache_read} (USD per 1M)."""
    merged = {k: dict(v) for k, v in MODEL_RATES.items()}
    if not path:
        return merged
    try:
        override = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as e:
        print(f'bad --rates file {path}: {e}', file=sys.stderr)
        return merged
    for k, v in (override or {}).items():
        if isinstance(v, dict):
            merged.setdefault(k, {}).update({kk: float(vv) for kk, vv in v.items()})
    return merged


def price_tokens(
    model: str,
    input_t: int,
    output_t: int,
    cache_read: int,
    cache_create: int,
    rates: dict[str, dict[str, float]],
) -> float | None:
    """Notional cost for a token bundle, or None if the model's rate is unknown
    (graceful degradation — the efficiency layer still works without this)."""
    r = rate_for(model, rates)
    if r is None:
        return None
    return (
        input_t / 1e6 * r['input']
        + output_t / 1e6 * r['output']
        + cache_read / 1e6 * r['cache_read']
        + cache_create / 1e6 * r['cache_write']
    )


def efficiency_ratios(entries: list[UsageEntry]) -> dict:
    """Pricing-independent efficiency signals over a set of entries.

    - cache_read_per_output: context drag per generated token (lower better)
    - amortization: cache_read / cache_create (how many times cached context is
      reused; higher better)
    - output_share: output / total tokens (signal-to-overhead; higher better)
    - input_per_turn: mean uncached input per turn (the 1M-context tell)
    """
    out_t = sum(e.output_tokens for e in entries)
    cr = sum(e.cache_read for e in entries)
    cc = sum(e.cache_create for e in entries)
    in_t = sum(e.input_tokens for e in entries)
    total = in_t + out_t + cr + cc
    return {
        'cache_read_per_output': (cr / out_t) if out_t else None,
        'amortization': (cr / cc) if cc else None,
        'output_share': (out_t / total) if total else None,
        'input_per_turn': (in_t / len(entries)) if entries else 0.0,
        'output_tokens': out_t,
        'cache_read_tokens': cr,
        'cache_creation_tokens': cc,
        'input_tokens': in_t,
    }


def summarize_by_model(
    entries: list[UsageEntry], rates: dict[str, dict[str, float]], pricing: str
) -> list[dict]:
    """Per-model rows: tokens, efficiency ratios, and (when pricing!=none and
    the rate is known) notional cost + cost-per-Mtok-out. Sorted by output
    tokens descending. Entries with an empty model name bucket under '(unknown)'.
    """
    by_model: dict[str, list[UsageEntry]] = defaultdict(list)
    for e in entries:
        by_model[e.model or '(unknown)'].append(e)
    rows: list[dict] = []
    for model, es in by_model.items():
        eff = efficiency_ratios(es)
        cost = None
        if pricing != 'none':
            cost = price_tokens(
                model,
                eff['input_tokens'],
                eff['output_tokens'],
                eff['cache_read_tokens'],
                eff['cache_creation_tokens'],
                rates,
            )
        cost_per_mout = (
            (cost / eff['output_tokens'] * 1e6)
            if (cost is not None and eff['output_tokens'])
            else None
        )
        rows.append({
            'model': model,
            'turns': len(es),
            **eff,
            'cost_usd': cost,
            'cost_per_moutput': cost_per_mout,
        })
    rows.sort(key=lambda r: r['output_tokens'], reverse=True)
    return rows


def _project_key(label: str) -> str:
    """Roll a decoded project label up to a meaningful project bucket so the
    per-project view isn't fragmented into hundreds of worktrees / temp runs:
    - ephemeral run dirs (var/folders, tmp) → '(ephemeral runs)'
    - worktree paths (contain a 'worktrees' segment) → their repo (1st segment)
    - everything else → kept as-is so distinct repos stay distinct.
    """
    if (
        'var/folders' in label
        or label in ('tmp', 'var')
        or label.startswith(('private/var', 'private/tmp', 'tmp/', 'var/'))
    ):
        return '(ephemeral runs)'
    parts = label.split('/')
    if 'worktrees' in (p.lower() for p in parts):
        return parts[0] or label
    return label


def summarize_by_project(
    entries: list[UsageEntry], rates: dict[str, dict[str, float]], pricing: str
) -> list[dict]:
    """Per-project rows (CLI): sessions, efficiency ratios, top model, and
    notional cost. Projects are rolled up via ``_project_key`` (worktrees fold
    into their repo; temp runs bucket together). Sorted by output tokens
    descending so the heaviest project reads first.
    """
    by_proj: dict[str, list[UsageEntry]] = defaultdict(list)
    for e in entries:
        label = e.group[4:] if e.group.startswith('cli:') else e.group
        by_proj[_project_key(label)].append(e)
    rows: list[dict] = []
    for proj, es in by_proj.items():
        eff = efficiency_ratios(es)
        by_model = _group_by_model(es)
        cost = 0.0 if pricing != 'none' else None
        if pricing != 'none':
            # Sum priced cost across the project's models (unknown models add 0
            # but are surfaced via the per-model table).
            for model, mes in by_model.items():
                me = efficiency_ratios(mes)
                c = price_tokens(
                    model, me['input_tokens'], me['output_tokens'],
                    me['cache_read_tokens'], me['cache_creation_tokens'], rates,
                )
                if c is not None:
                    cost += c
        # Dominant model by output for an at-a-glance "what runs here".
        top_model = max(
            by_model.items(),
            key=lambda kv: sum(x.output_tokens for x in kv[1]),
            default=('', []),
        )[0]
        rows.append({
            'project': proj,
            'sessions': len({e.session_id for e in es}),
            **eff,
            'top_model': top_model,
            'cost_usd': cost,
        })
    rows.sort(key=lambda r: r['output_tokens'], reverse=True)
    return rows


def _group_by_model(entries: list[UsageEntry]) -> dict[str, list[UsageEntry]]:
    g: dict[str, list[UsageEntry]] = defaultdict(list)
    for e in entries:
        g[e.model or '(unknown)'].append(e)
    return g


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


def _fmt_ratio(x, suffix='x', digits=0) -> str:
    return f'{x:,.{digits}f}{suffix}' if x is not None else '—'


def format_per_model(title: str, rows: list[dict], pricing: str) -> list[str]:
    """Render the per-model efficiency + cost table. Pricing-independent ratios
    always show; the $ columns show '—' when the model's rate is unknown."""
    # Drop models that produced no output — they carry no efficiency signal
    # (e.g. '<synthetic>' placeholders, 1-token proxy probes).
    rows = [r for r in rows if r['output_tokens'] > 0]
    if not rows:
        return []
    lines = [f'  -- {title}: per-model efficiency --']
    header = (
        f'    {"model":<26}{"output":>12}{"CR:out":>9}'
        f'{"amort":>8}{"out%":>7}'
    )
    if pricing != 'none':
        header += f'{"$/Mout":>9}{"cost$":>10}'
    lines.append(header)
    for r in rows:
        line = (
            f'    {r["model"][:26]:<26}'
            f'{format_number(r["output_tokens"]):>12}'
            f'{_fmt_ratio(r["cache_read_per_output"]):>9}'
            f'{_fmt_ratio(r["amortization"]):>8}'
            f'{(r["output_share"] * 100 if r["output_share"] is not None else 0):>6.1f}%'
        )
        if pricing != 'none':
            cpm = r.get('cost_per_moutput')
            cost = r.get('cost_usd')
            line += f'{(f"${cpm:,.0f}" if cpm is not None else "—"):>9}'
            line += f'{(f"${cost:,.2f}" if cost is not None else "—"):>10}'
        lines.append(line)
    if pricing != 'none' and any(r.get('cost_usd') is None for r in rows):
        lines.append(
            '    (— = no rate for this model; efficiency ratios still valid. '
            'Add one via --rates.)'
        )
    return lines


def format_projects(
    rows: list[dict], pricing: str, top: int = 25
) -> list[str]:
    """Per-project table (CLI), heaviest first. Caps at `top` rows and states
    how many were omitted (no silent truncation) — use --json for all."""
    rows = [r for r in rows if r['output_tokens'] > 0]
    if not rows:
        return []
    lines = ['  -- Per-project (CLI), heaviest first --']
    header = (
        f'    {"project":<30}{"sess":>6}{"output":>12}{"CR:out":>9}'
        f'{"amort":>8}{"top model":>16}'
    )
    if pricing != 'none':
        header += f'{"cost$":>11}'
    lines.append(header)
    shown = rows[:top]
    for r in shown:
        top_model = (r['top_model'] or '').replace('claude-', '')[:15]
        line = (
            f'    {r["project"][:30]:<30}{r["sessions"]:>6}'
            f'{format_number(r["output_tokens"]):>12}'
            f'{_fmt_ratio(r["cache_read_per_output"]):>9}'
            f'{_fmt_ratio(r["amortization"]):>8}{top_model:>16}'
        )
        if pricing != 'none':
            cost = r.get('cost_usd')
            line += f'{(f"${cost:,.2f}" if cost is not None else "—"):>11}'
        lines.append(line)
    if len(rows) > top:
        omitted = rows[len(shown):]
        omitted_out = sum(r['output_tokens'] for r in omitted)
        lines.append(
            f'    … +{len(omitted)} more project(s), '
            f'{format_number(omitted_out)} output tokens (use --json for all)'
        )
    return lines


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
    groups: list[str],
    since,
    until,
    include_cli: bool = True,
    project_filters: list[str] | None = None,
    rates: dict[str, dict[str, float]] | None = None,
    pricing: str = 'notional',
) -> dict:
    rates = rates if rates is not None else MODEL_RATES
    container_entries = load_usage(groups, since, until)
    cli_entries = (
        load_cli_usage(since, until, project_filters) if include_cli else []
    )
    tools = load_tool_sizes(groups, since, until)
    quality_rows = load_interactions(groups, since, until)
    return {
        'container_usage': summarize_usage(container_entries),
        'cli_usage': summarize_usage(cli_entries),
        'container_per_model': summarize_by_model(
            container_entries, rates, pricing
        ),
        'cli_per_model': summarize_by_model(cli_entries, rates, pricing),
        'cli_per_project': summarize_by_project(cli_entries, rates, pricing),
        'container_efficiency': efficiency_ratios(container_entries),
        'cli_efficiency': efficiency_ratios(cli_entries),
        'tools': summarize_tool_sizes(tools),
        'quality': summarize_quality(quality_rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Analyze Deus token-usage and quality logs.'
    )
    ap.add_argument('--group', action='append', help='Restrict to this CONTAINER group folder (repeatable). Default: all. Orthogonal to --project.')
    ap.add_argument('--project', action='append', help='Filter CLI transcripts to project dirs whose RAW ENCODED name (e.g. -Users-<u>-myrepo, not the short label) contains this substring (repeatable, case-insensitive). Default: all projects.')
    ap.add_argument('--since', help='Start of window (YYYY-MM-DD, inclusive).')
    ap.add_argument('--until', help='End of window (YYYY-MM-DD, inclusive).')
    ap.add_argument('--baseline-until', help='For before/after: last day of baseline period.')
    ap.add_argument('--compare-from', help='For before/after: first day of comparison period.')
    ap.add_argument('--pricing', choices=['none', 'notional'], default='notional', help='Cost column: "notional" prices tokens via the built-in rate table (for an API-direct user this IS the bill); "none" shows tokens/efficiency only.')
    ap.add_argument('--rates', help='Path to a JSON file overriding/extending the built-in per-model rates (USD per 1M: {input,output,cache_write,cache_read}).')
    ap.add_argument('--json', action='store_true', help='Emit JSON instead of text report.')
    ap.add_argument(
        '--cli-project-dir',
        help=(
            'Override to a SINGLE Claude Code transcript project dir (disables '
            'the all-projects scan). Set this when running from a worktree or '
            'to scope to one repo.'
        ),
    )
    args = ap.parse_args()

    # Allow overriding to a single transcript dir (disables all-projects scan).
    if args.cli_project_dir:
        global CLI_TRANSCRIPTS_DIR
        CLI_TRANSCRIPTS_DIR = Path(args.cli_project_dir).expanduser()

    rates = load_rate_overrides(args.rates)

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
    # CLI data is still loaded even when no channel groups exist. Fail only when
    # there is neither a channel group nor any CLI transcript dir to scan.
    if not groups and not _iter_project_dirs(args.project):
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
        baseline = analyze(groups, since, baseline_until, project_filters=args.project, rates=rates, pricing=args.pricing)
        compare = analyze(groups, compare_from, until, project_filters=args.project, rates=rates, pricing=args.pricing)
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
    result = analyze(groups, since, until, project_filters=args.project, rates=rates, pricing=args.pricing)
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
    # Per-project breakdown FIRST (understand each project), then the
    # all-projects per-model totals (the layered view).
    out_lines: list[str] = []
    out_lines += format_projects(result['cli_per_project'], args.pricing)
    pm = format_per_model('CLI total (all projects)', result['cli_per_model'], args.pricing)
    if pm:
        if out_lines:
            out_lines.append('')
        out_lines += pm
    if result['container_per_model']:
        cm = format_per_model('Container', result['container_per_model'], args.pricing)
        if cm:
            if out_lines:
                out_lines.append('')
            out_lines += cm
    if out_lines:
        print()
        print('\n'.join(out_lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
