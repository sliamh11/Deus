#!/usr/bin/env python3
"""Report parallel-subagent fan-out per container run (LIA-343 validation).

Counts subagent spawns per engineering-context run from the existing per-
interaction tool-call logs written by `container/agent-runner/src/tool-call-log.ts`
(LIA-154) at `groups/<group>/logs/tool-calls/<interaction_id>.jsonl`. One file =
one run; each line is one tool call `{ts, name, ...}`.

Used to validate the Layer-A subagent fan-out nudge: did the engineering agent
start fanning out after the nudge shipped? Run with `--since <deploy-ISO>` to
split runs into before/after the deploy boundary.

Counting methodology:
  - spawn        = a record with name in {"Task", "Agent"} (tool-call-log.ts:86
                   groups 'Agent' || 'Task'; counting only "Task" undercounts).
  - task_output  = a record with name == "TaskOutput" (a subagent's result-push,
                   NOT a spawn) — tallied separately as corroborating evidence.
  - logging_gap  = a run with task_outputs > 0 but spawns == 0: a subagent
                   demonstrably ran but its spawn record was not logged. Surfaced
                   explicitly so the report never reports a misleadingly clean 0.

Read-only. Never modifies logs. Agent-native (see docs/decisions/printing-press-adoption.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from _exit_codes import SUCCESS, ABSTAIN, USAGE_ERROR, INTERNAL_ERROR  # noqa: E402
from _agent_io import agent_output, is_agent_context  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent
_DEFAULT_GROUPS_DIR = _REPO_ROOT / "groups"

SPAWN_NAMES = frozenset({"Task", "Agent"})
TASK_OUTPUT_NAME = "TaskOutput"


def summarize_run(records: list[dict], group: str, interaction_id: str) -> dict:
    """Reduce one run's tool-call records to fan-out counts. Pure."""
    spawns = 0
    task_outputs = 0
    first_ts = None
    for rec in records:
        name = rec.get("name")
        if name in SPAWN_NAMES:
            spawns += 1
        elif name == TASK_OUTPUT_NAME:
            task_outputs += 1
        if first_ts is None and isinstance(rec.get("ts"), str):
            first_ts = rec["ts"]
    return {
        "group": group,
        "interaction_id": interaction_id,
        "first_ts": first_ts,
        "total_calls": len(records),
        "spawns": spawns,
        "task_outputs": task_outputs,
        "logging_gap": task_outputs > 0 and spawns == 0,
    }


def aggregate(runs: list[dict]) -> dict:
    """Aggregate run summaries. Pure."""
    n = len(runs)
    total_spawns = sum(r["spawns"] for r in runs)
    return {
        "runs": n,
        "total_spawns": total_spawns,
        "runs_with_spawn": sum(1 for r in runs if r["spawns"] > 0),
        # Honest fan-out-occurred signal: any explicit spawn OR a TaskOutput.
        "runs_with_subagent_activity": sum(
            1 for r in runs if r["spawns"] > 0 or r["task_outputs"] > 0
        ),
        "logging_gaps": sum(1 for r in runs if r["logging_gap"]),
        "mean_spawn_per_run": round(total_spawns / n, 3) if n else 0.0,
    }


def _parse_since(value: str) -> datetime:
    """Parse an ISO-8601 cutoff into an aware UTC datetime. Raises ValueError."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_dt(run: dict) -> datetime | None:
    ts = run.get("first_ts")
    if not isinstance(ts, str):
        return None
    try:
        return _parse_since(ts)
    except ValueError:
        return None


def _load_records(path: Path) -> list[dict]:
    """Load one interaction file, skipping blank/malformed lines."""
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _iter_run_files(groups_dir: Path, groups_filter: set[str] | None):
    """Yield (group, file) for every tool-calls JSONL under groups_dir."""
    if not groups_dir.is_dir():
        return
    for group_dir in sorted(groups_dir.iterdir()):
        if not group_dir.is_dir():
            continue
        group = group_dir.name
        if groups_filter is not None and group not in groups_filter:
            continue
        tc_dir = group_dir / "logs" / "tool-calls"
        if not tc_dir.is_dir():
            continue
        for f in sorted(tc_dir.glob("*.jsonl")):
            yield group, f


def build_report(
    groups_dir: Path,
    groups_filter: set[str] | None,
    since: datetime | None,
) -> dict:
    """Collect run summaries per group, optionally split before/after `since`."""
    by_group: dict[str, list[dict]] = {}
    for group, f in _iter_run_files(groups_dir, groups_filter):
        run = summarize_run(_load_records(f), group, f.stem)
        by_group.setdefault(group, []).append(run)

    report: dict = {"groups_dir": str(groups_dir), "groups": {}}
    if since is not None:
        report["since"] = since.isoformat()
    for group, runs in sorted(by_group.items()):
        if since is None:
            report["groups"][group] = aggregate(runs)
            continue
        before, after, undated = [], [], []
        for run in runs:
            dt = _run_dt(run)
            if dt is None:
                undated.append(run)
            elif dt < since:
                before.append(run)
            else:
                after.append(run)
        entry = {"before": aggregate(before), "after": aggregate(after)}
        if undated:
            entry["undated"] = aggregate(undated)
        report["groups"][group] = entry
    return report


def _fmt_agg(a: dict) -> str:
    return (
        f"runs={a['runs']} spawns={a['total_spawns']} "
        f"runs_with_spawn={a['runs_with_spawn']} "
        f"subagent_activity={a['runs_with_subagent_activity']} "
        f"logging_gaps={a['logging_gaps']} mean={a['mean_spawn_per_run']}"
    )


def _print_human(report: dict) -> None:
    since = report.get("since")
    print(f"Subagent fan-out report  (groups_dir={report['groups_dir']})")
    if since:
        print(f"Split at --since {since}")
    if not report["groups"]:
        print("  (no tool-call logs found)")
        return
    for group, data in report["groups"].items():
        print(f"\n{group}")
        if since:
            for bucket in ("before", "after", "undated"):
                if bucket in data:
                    print(f"  {bucket:8} {_fmt_agg(data[bucket])}")
        else:
            print(f"  {_fmt_agg(data)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--groups-dir",
        default=str(_DEFAULT_GROUPS_DIR),
        help="Path to the groups/ directory (default: <repo>/groups).",
    )
    parser.add_argument(
        "--groups",
        default=None,
        help="Comma-separated group names to include (default: all).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 cutoff; split runs into before/after (e.g. the deploy time).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--compact", action="store_true", help="Token-efficient JSON (strip nulls)."
    )
    parser.add_argument(
        "--select", default=None, help="Comma-separated field paths to project."
    )
    args = parser.parse_args(argv)

    since = None
    if args.since is not None:
        try:
            since = _parse_since(args.since)
        except ValueError:
            print(f"error: --since is not a valid ISO-8601 timestamp: {args.since}", file=sys.stderr)
            return USAGE_ERROR

    groups_filter = None
    if args.groups:
        groups_filter = {g.strip() for g in args.groups.split(",") if g.strip()}

    try:
        report = build_report(Path(args.groups_dir), groups_filter, since)
    except OSError as exc:
        print(f"error: failed to read logs: {exc}", file=sys.stderr)
        return INTERNAL_ERROR

    use_json = args.json or is_agent_context()
    out = agent_output(
        report, use_json=use_json, compact=args.compact, select=args.select
    )
    if out is not None:
        print(out)
    else:
        _print_human(report)

    return SUCCESS if report["groups"] else ABSTAIN


if __name__ == "__main__":
    raise SystemExit(main())
