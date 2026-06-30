"""Tests for scripts/maintenance/subagent_fanout_report.py (LIA-343)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD_PATH = (
    Path(__file__).resolve().parent.parent / "maintenance" / "subagent_fanout_report.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("subagent_fanout_report", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _rec(name, ts="2026-06-28T10:00:00.000Z"):
    return {"ts": ts, "name": name, "is_error": False}


def test_summarize_run_counts_task_and_agent_as_spawns():
    records = [
        _rec("Task"),
        _rec("Task"),
        _rec("Agent"),
        _rec("Bash"),
        _rec("Read"),
        _rec("Glob"),
    ]
    s = mod.summarize_run(records, "g", "g-1")
    assert s["spawns"] == 3
    assert s["total_calls"] == 6
    assert s["task_outputs"] == 0
    assert s["logging_gap"] is False
    assert s["first_ts"] == "2026-06-28T10:00:00.000Z"


def test_summarize_run_orphan_taskoutput_is_logging_gap():
    # The real baseline anomaly: a subagent ran (TaskOutput) but no spawn logged.
    records = [_rec("TaskOutput"), _rec("Bash"), _rec("Read")]
    s = mod.summarize_run(records, "g", "g-1")
    assert s["spawns"] == 0
    assert s["task_outputs"] == 1
    assert s["logging_gap"] is True


def test_aggregate_counts_activity_and_gaps():
    runs = [
        {"spawns": 2, "task_outputs": 0, "logging_gap": False},  # fan-out
        {"spawns": 0, "task_outputs": 1, "logging_gap": True},  # gap (activity)
        {"spawns": 0, "task_outputs": 0, "logging_gap": False},  # no activity
    ]
    a = mod.aggregate(runs)
    assert a["runs"] == 3
    assert a["total_spawns"] == 2
    assert a["runs_with_spawn"] == 1
    assert a["runs_with_subagent_activity"] == 2  # spawn run + gap run
    assert a["logging_gaps"] == 1
    assert a["mean_spawn_per_run"] == round(2 / 3, 3)


def test_aggregate_empty():
    a = mod.aggregate([])
    assert a["runs"] == 0
    assert a["mean_spawn_per_run"] == 0.0


def _write_run(groups_dir: Path, group: str, iid: str, recs: list[dict]):
    d = groups_dir / group / "logs" / "tool-calls"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{iid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs), encoding="utf-8"
    )


def test_build_report_since_split(tmp_path):
    gd = tmp_path / "groups"
    # before the cutoff: no spawns
    _write_run(gd, "linear-dispatch", "ld-1", [_rec("Bash", "2026-06-10T00:00:00Z")])
    # after the cutoff: one Task spawn
    _write_run(
        gd,
        "linear-dispatch",
        "ld-2",
        [_rec("Task", "2026-06-28T12:00:00Z"), _rec("Bash", "2026-06-28T12:00:01Z")],
    )
    report = mod.build_report(gd, None, mod._parse_since("2026-06-20T00:00:00Z"))
    ld = report["groups"]["linear-dispatch"]
    assert ld["before"]["runs"] == 1
    assert ld["before"]["total_spawns"] == 0
    assert ld["after"]["runs"] == 1
    assert ld["after"]["total_spawns"] == 1


def test_main_json_and_abstain(tmp_path, capsys):
    # No tool-calls anywhere -> ABSTAIN, empty groups.
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = mod.main(["--groups-dir", str(empty), "--json"])
    assert rc == mod.ABSTAIN
    out = json.loads(capsys.readouterr().out)
    assert out["groups"] == {}

    # With data -> SUCCESS, valid JSON.
    gd = tmp_path / "groups"
    _write_run(gd, "linear-dispatch", "ld-1", [_rec("Task"), _rec("Bash")])
    rc = mod.main(["--groups-dir", str(gd), "--json"])
    assert rc == mod.SUCCESS
    out = json.loads(capsys.readouterr().out)
    assert out["groups"]["linear-dispatch"]["total_spawns"] == 1


def test_main_bad_since_is_usage_error(tmp_path):
    rc = mod.main(["--groups-dir", str(tmp_path), "--since", "not-a-date"])
    assert rc == mod.USAGE_ERROR
