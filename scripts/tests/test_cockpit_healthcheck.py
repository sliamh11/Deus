"""Tests for the cockpit healthcheck (LIA-552).

The recurring theme: this tool exists because a subsystem reported healthy while
being dead, so most of these tests assert that some *plausible* path to OK is
refused. A checker that can be talked into green is worse than no checker.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cockpit_healthcheck as cock  # noqa: E402


NOW = 1_786_800_000.0


def _evolution_db(path: Path, interactions=(), health=()):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, timestamp TEXT)")
    con.execute(
        "CREATE TABLE subsystem_health (component TEXT PRIMARY KEY, last_status TEXT,"
        " last_reason TEXT, last_attempt_at TEXT, last_skipped_at TEXT, last_ok_at TEXT,"
        " consecutive_failures INTEGER, first_failed_at TEXT)"
    )
    con.executemany("INSERT INTO interactions VALUES (?,?)", interactions)
    con.executemany(
        "INSERT INTO subsystem_health VALUES (?,?,?,?,?,?,?,?)", health
    )
    con.commit()
    con.close()
    return path


# ── the capability probe cannot be talked into OK ─────────────────────────────


def test_optimizer_is_failed_when_dspy_is_unimportable(monkeypatch):
    """Today's real state, and the ticket's acceptance criterion."""
    monkeypatch.setattr(cock.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "", "stderr": "No module named 'dspy'"})())

    r = cock.probe_optimizer(NOW)
    assert r.status == cock.FAILED
    assert "dspy" in r.observed
    assert r.remedy, "a FAILED verdict must tell the reader what to do"


def test_importable_dspy_alone_does_not_make_the_optimizer_ok(monkeypatch, tmp_path):
    """The co-gate finding: importability is a precondition, not a capability.

    Two blockers are live in the real source (GEPA without reflection_lm; the
    metric returning a dict). If merely installing dspy flipped this to OK, the
    cockpit would manufacture the false-green it exists to prevent.
    """
    monkeypatch.setattr(cock.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": "3.1.3", "stderr": ""})())
    monkeypatch.setattr(cock, "_last_optimizer_success", lambda: None)

    r = cock.probe_optimizer(NOW)
    assert r.status == cock.UNKNOWN, "no completed run on record must not be OK"
    assert r.status != cock.OK
    assert "known_open_blockers" in r.detail


def test_a_run_older_than_the_code_cannot_vindicate_it(monkeypatch):
    """A real 2026-03-30 artifact exists; it must not certify today's code."""
    monkeypatch.setattr(cock.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": "3.1.3", "stderr": ""})())
    monkeypatch.setattr(cock, "_last_optimizer_success", lambda: NOW - 10_000)
    monkeypatch.setattr(cock, "_newest_source_mtime", lambda d: NOW)

    r = cock.probe_optimizer(NOW)
    assert r.status == cock.UNKNOWN
    assert "predates" in r.observed


def test_optimizer_ok_requires_a_run_newer_than_the_sources(monkeypatch):
    monkeypatch.setattr(cock.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": "3.1.3", "stderr": ""})())
    monkeypatch.setattr(cock, "_last_optimizer_success", lambda: NOW)
    monkeypatch.setattr(cock, "_newest_source_mtime", lambda d: NOW - 10_000)

    assert cock.probe_optimizer(NOW).status == cock.OK


# ── demand gating: neither cry-wolf nor complacent ────────────────────────────





def test_ingest_reports_reachability_not_a_demand_verdict(tmp_path, monkeypatch):
    """Re-scoped after three review rounds each found a different semantic gap
    in the demand comparison. The probe now states what it can prove: the store
    is reachable. It must NOT manufacture a FAILED from log-inferred demand."""
    import datetime as dt
    old_ts = dt.datetime.fromtimestamp(NOW - 300_000, dt.timezone.utc).isoformat()
    db = _evolution_db(tmp_path / "e.db", interactions=[("i1", old_ts)])
    monkeypatch.setenv("DEUS_EVOLUTION_DB", str(db))
    heavy = cock.DemandWindow(count=99, newest_epoch=NOW, window_start=NOW - 3600,
                              window_sec=3600)

    r = cock.probe_ingest(NOW, heavy)
    assert r.status == cock.OK, "unserved-demand inference is deliberately gone"
    assert "readable" in r.observed
    # the raw signals stay visible as evidence, just not as a verdict
    assert r.detail["demand_events_in_window"] == 99


def test_missing_interactions_table_is_unknown_not_ok(tmp_path, monkeypatch):
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    monkeypatch.setenv("DEUS_EVOLUTION_DB", str(bare))
    demand = cock.DemandWindow(count=0, newest_epoch=0.0, window_start=NOW - 3600, window_sec=3600)

    r = cock.probe_ingest(NOW, demand)
    assert r.status == cock.UNKNOWN, "an idle-looking but broken DB must not read as idle-OK"



def test_one_request_counts_once_not_once_per_lifecycle_event(tmp_path):
    """Measured on the live log, the lifecycle markers fire 58 / 312 / 120 times
    — summing them inflated demand ~8x and would have reported healthy traffic
    as DEGRADED permanently. Only the arrival event counts."""
    log = tmp_path / "deus.log"
    (tmp_path / "archives").mkdir()
    recs = [
        {"time": NOW * 1000, "msg": "New messages"},
        {"time": NOW * 1000, "msg": "Spawning container agent"},
        {"time": NOW * 1000, "msg": "Container completed"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in recs))

    assert cock.read_demand(log, 3600, NOW).count == 1


def test_service_probe_is_skipped_off_macos(monkeypatch):
    """The scheduled job installs on Linux and Windows too. A launchctl-only
    probe returning UNKNOWN there would pin every healthy run at exit 4."""
    monkeypatch.setattr(cock.sys, "platform", "linux")
    assert cock.probe_service("com.deus", NOW) is None, "not-applicable, not blind"

    monkeypatch.setattr(cock.sys, "platform", "darwin")
    assert cock.probe_service("com.deus", NOW) is not None


def test_run_probes_omits_service_probes_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(cock.sys, "platform", "linux")
    monkeypatch.setattr(cock, "probe_optimizer", lambda n: _res(cock.OK, "opt"))
    monkeypatch.setattr(cock, "probe_ingest", lambda n, d: _res(cock.OK, "ing"))
    monkeypatch.setattr(cock, "probe_memory", lambda n: _res(cock.OK, "mem"))
    monkeypatch.setattr(cock, "probe_opa_policy", lambda n, m, u: _res(cock.OK, "opa"))
    monkeypatch.setattr(cock, "read_demand",
                        lambda *a: cock.DemandWindow(0, 0.0, NOW - 60, 60))

    names = [r.probe for r in cock.run_probes(NOW, tmp_path / "l", 60, "m", "u")]
    assert not any(n.startswith("service.com.deus") for n in names)
    assert cock.exit_code(cock.run_probes(NOW, tmp_path / "l", 60, "m", "u")) == cock.EXIT_OK


def test_bookkeeping_components_do_not_count_as_a_completed_run(tmp_path, monkeypatch):
    """evolution/cli.py:224,240 write registry/storage OK on every cycle, even
    below-threshold ones that optimize nothing. Treating those as a completed
    run would let routine bookkeeping certify a dead optimizer."""
    import datetime as dt
    recent = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat()
    db = _evolution_db(tmp_path / "e.db", health=[
        ("evolution.optimizer.registry", "OK", None, recent, None, recent, 0, None),
        ("evolution.optimizer.storage", "OK", None, recent, None, recent, 0, None),
    ])
    monkeypatch.setenv("DEUS_EVOLUTION_DB", str(db))
    assert cock._last_optimizer_success() is None, "bookkeeping is not a run"

    db2 = _evolution_db(tmp_path / "e2.db", health=[
        ("evolution.optimizer.registry", "OK", None, recent, None, recent, 0, None),
        ("evolution.optimizer.qa", "OK", None, recent, None, recent, 0, None),
    ])
    monkeypatch.setenv("DEUS_EVOLUTION_DB", str(db2))
    assert cock._last_optimizer_success() is not None, "a real module run counts"


def test_demand_reads_gzipped_archives(tmp_path):
    """Rotation writes logs/archives/*.log.gz. Opening those as plain text
    silently drops their records while the live file still marks the source
    readable — missing demand would then look like idleness."""
    import gzip as _gz
    log = tmp_path / "deus.log"
    arch = tmp_path / "archives"
    arch.mkdir()
    log.write_text(json.dumps({"time": NOW * 1000, "msg": "New messages"}))
    with _gz.open(arch / "deus.2026-01-01.log.gz", "wt") as fh:
        fh.write(json.dumps({"time": (NOW - 100) * 1000, "msg": "New messages"}))

    assert cock.read_demand(log, 3600, NOW).count == 2, "gzipped archive must be read"


def test_opa_probe_is_skipped_off_macos(monkeypatch):
    """OPA is an optional macOS/launchd install; Linux packaging is out of
    scope. UNKNOWN there would alarm every healthy run forever."""
    monkeypatch.setattr(cock.sys, "platform", "linux")
    assert cock.probe_opa_policy(NOW, "m", "http://127.0.0.1:9/x") is None


def test_demand_excludes_health_polls_and_webhooks(tmp_path):
    """1,226 GitHub webhooks and 23 /health polls once made a three-day-idle
    system look busy. Only chat/agent events count."""
    log = tmp_path / "deus.log"
    (tmp_path / "archives").mkdir()
    recs = [
        {"time": NOW * 1000, "msg": "ingress request", "path": "/health"},
        {"time": NOW * 1000, "msg": "ingress request", "path": "/github"},
        {"time": NOW * 1000, "msg": "New messages"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in recs))

    d = cock.read_demand(log, 3600, NOW)
    assert d.count == 1, "only the chat event is demand"


def test_demand_reads_across_rotation(tmp_path):
    log = tmp_path / "deus.log"
    arch = tmp_path / "archives"
    arch.mkdir()
    log.write_text(json.dumps({"time": NOW * 1000, "msg": "New messages"}))
    (arch / "deus.log.1").write_text(json.dumps({"time": (NOW - 100) * 1000, "msg": "New messages"}))

    assert cock.read_demand(log, 3600, NOW).count == 2


# ── memory reuse must not inherit the fail-open contract ──────────────────────


def test_corrupt_tree_db_is_not_ok_even_if_the_library_says_ok(tmp_path, monkeypatch):
    """quick_check reports corruption as a returned row, not an exception, so a
    probe that only catches exceptions would honour memory_health's fail-open OK."""
    db = tmp_path / "memory_tree.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE nodes (path TEXT)")
    con.commit()
    con.close()

    import memory_health
    monkeypatch.setattr(memory_health, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(memory_health, "assess_memory_health", lambda *a, **k: (True, "ok", []))

    real_ro = cock._ro

    class _Cursor:
        """Stands in for a sqlite cursor reporting corruption the way SQLite
        really does: a returned row, not a raised exception."""
        def fetchone(self): return ("*** in database main ***\nPage 4 is never used",)

    class _Con:
        def __init__(self, inner): self._inner = inner
        def execute(self, sql, *a):
            if "quick_check" in sql:
                return _Cursor()
            return self._inner.execute(sql, *a)
        def close(self): self._inner.close()

    monkeypatch.setattr(cock, "_ro", lambda p: _Con(real_ro(p)))

    r = cock.probe_memory(NOW)
    assert r.status == cock.FAILED
    assert "quick_check" in r.observed


def test_missing_tree_db_is_failed(tmp_path, monkeypatch):
    import memory_health
    monkeypatch.setattr(memory_health, "DEFAULT_DB_PATH", tmp_path / "gone.db")
    assert cock.probe_memory(NOW).status == cock.FAILED


# ── exit codes: no path may silently mean "fine" ──────────────────────────────


def _res(status, name="p"):
    return cock.Result(name, status)


def test_exit_codes_cover_every_combination():
    assert cock.exit_code([_res(cock.OK)]) == cock.EXIT_OK
    assert cock.exit_code([_res(cock.OK), _res(cock.DEGRADED, "b")]) == cock.EXIT_OK
    assert cock.exit_code([_res(cock.FAILED)]) == cock.EXIT_FAILED
    assert cock.exit_code([_res(cock.UNKNOWN)]) == cock.EXIT_ALL_UNKNOWN
    # partial blindness must not read as success
    assert cock.exit_code([_res(cock.OK), _res(cock.UNKNOWN, "b")]) == cock.EXIT_PARTIAL_UNKNOWN
    # a known failure outranks an unknown: it is the actionable one
    assert cock.exit_code([_res(cock.FAILED), _res(cock.UNKNOWN, "b")]) == cock.EXIT_FAILED
    assert cock.exit_code([]) == cock.EXIT_ALL_UNKNOWN


def test_artifact_write_failure_is_reported_not_swallowed(tmp_path, monkeypatch, capsys):
    """A checker that cannot record its own result must say so — LIA-556's lesson."""
    monkeypatch.setattr(cock, "ARTIFACT_JSON", tmp_path / "nope" / "x.json")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", tmp_path / "nope" / "x.line")
    monkeypatch.setattr(cock, "run_probes", lambda *a, **k: [_res(cock.OK)])

    def boom(*a, **k):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(Path, "mkdir", boom)

    assert cock.main([]) == cock.EXIT_WRITE_FAILED
    assert "could not write artifact" in capsys.readouterr().err


# ── regression vs steady state ────────────────────────────────────────────────


def test_bare_invocation_is_the_run_mode(tmp_path, monkeypatch):
    """The scheduled job passes no arguments (SCHEDULED_JOBS carries none), so a
    bare call must probe and write. Guards against a future edit gating the
    probe branch on a flag and silently disabling the daily run."""
    monkeypatch.setattr(cock, "ARTIFACT_JSON", tmp_path / "c.json")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", tmp_path / "c.line")
    monkeypatch.setattr(cock, "run_probes", lambda *a, **k: [_res(cock.OK, "x")])

    assert cock.main([]) == cock.EXIT_OK
    assert (tmp_path / "c.json").exists(), "a bare call must write the artifact"
    assert (tmp_path / "c.line").read_text().strip() == "OK"


def test_first_failure_is_a_regression_and_repeats_are_not():
    first = cock.merge_history([_res(cock.FAILED, "x")], {}, NOW)
    assert first["probes"][0]["is_regression"] is True
    assert first["probes"][0]["consecutive_bad_runs"] == 1

    second = cock.merge_history([_res(cock.FAILED, "x")], first, NOW + 3600)
    assert second["probes"][0]["is_regression"] is False, "don't shout twice"
    assert second["probes"][0]["consecutive_bad_runs"] == 2
    assert second["probes"][0]["first_bad_at"] == NOW, "streak start is preserved"


def test_recovery_clears_the_streak():
    bad = cock.merge_history([_res(cock.FAILED, "x")], {}, NOW)
    good = cock.merge_history([_res(cock.OK, "x")], bad, NOW + 60)
    assert good["probes"][0]["consecutive_bad_runs"] == 0
    assert good["probes"][0]["first_bad_at"] is None


# ── the shell-facing path ─────────────────────────────────────────────────────


def test_brief_is_silent_when_healthy(tmp_path, monkeypatch, capsys):
    line = tmp_path / "l"
    line.write_text("OK\n")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", line)
    assert cock.main(["--brief"]) == cock.EXIT_OK
    assert capsys.readouterr().out == "", "quiet when healthy, or it becomes noise"


def test_brief_speaks_up_when_not_healthy(tmp_path, monkeypatch, capsys):
    line = tmp_path / "l"
    line.write_text("FAILED evolution.optimizer (NEW) — cannot import dspy\n")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", line)
    cock.main(["--brief"])
    assert "evolution.optimizer" in capsys.readouterr().out


def test_brief_reports_a_missing_artifact_rather_than_nothing(tmp_path, monkeypatch, capsys):
    """Silence when the checker is broken is the exact bug this tool exists for."""
    monkeypatch.setattr(cock, "ARTIFACT_LINE", tmp_path / "absent")
    assert cock.main(["--brief"]) == cock.EXIT_ALL_UNKNOWN
    assert "no healthcheck result" in capsys.readouterr().out


def test_brief_reports_a_stale_artifact(tmp_path, monkeypatch, capsys):
    line = tmp_path / "l"
    line.write_text("OK\n")
    import os
    old = time.time() - (cock.ARTIFACT_MAX_AGE_SEC + 3600)
    os.utime(line, (old, old))
    monkeypatch.setattr(cock, "ARTIFACT_LINE", line)
    cock.main(["--brief"])
    assert "may not be running" in capsys.readouterr().out


def test_brief_rejects_an_empty_cached_verdict(tmp_path, monkeypatch, capsys):
    """A truncated artifact is a checker that died mid-write. Reading it as
    healthy would be a false green produced by the health tool itself."""
    line = tmp_path / "l"
    line.write_text("")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", line)
    assert cock.main(["--brief"]) == cock.EXIT_ALL_UNKNOWN
    assert "empty" in capsys.readouterr().out


def test_a_failed_artifact_write_preserves_the_previous_result(tmp_path, monkeypatch):
    """Non-atomic writes truncate first; a failure would destroy the prior
    verdict and leave an empty file that reads as healthy."""
    target = tmp_path / "c.line"
    target.write_text("FAILED evolution.optimizer (NEW) — previous evidence\n")

    real_replace = cock.os.replace
    monkeypatch.setattr(cock.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cock._atomic_write(target, "new content")
    monkeypatch.setattr(cock.os, "replace", real_replace)

    assert "previous evidence" in target.read_text(), "prior verdict must survive"


def test_a_new_regression_outranks_an_ongoing_failure_of_equal_severity():
    """Otherwise today's news stays hidden behind yesterday's known breakage."""
    state = {"probes": [
        {"probe": "old", "status": cock.FAILED, "observed": "known", "is_regression": False},
        {"probe": "new", "status": cock.FAILED, "observed": "just broke", "is_regression": True},
    ]}
    assert "new" in cock.render_line(state)
    assert "NEW" in cock.render_line(state)


def test_optional_components_absent_are_skipped_not_failed(monkeypatch):
    """A valid install simply may not run OPA. Calling that FAILED would alarm
    daily about something deliberately never set up."""
    monkeypatch.setattr(cock.sys, "platform", "darwin")
    monkeypatch.setattr(cock, "_launch_agent_installed", lambda label: False)
    assert cock.probe_service("com.deus.warden-opa", NOW) is None
    assert cock.probe_opa_policy(NOW, "m", "http://127.0.0.1:9/x") is None


def test_ingest_does_not_claim_the_write_path(tmp_path, monkeypatch):
    """Read-only SELECTs cannot prove writes work; the wording must not imply it."""
    db = _evolution_db(tmp_path / "e.db")
    monkeypatch.setenv("DEUS_EVOLUTION_DB", str(db))
    r = cock.probe_ingest(NOW, cock.DemandWindow(0, 0.0, NOW - 60, 60))
    assert "write path" not in r.observed
    assert "readable" in r.observed


def test_brief_never_probes(tmp_path, monkeypatch):
    """The shell path must not open a DB, spawn a process, or hit the network —
    that is what keeps `deus` launch instant without needing a timeout."""
    line = tmp_path / "l"
    line.write_text("OK\n")
    monkeypatch.setattr(cock, "ARTIFACT_LINE", line)

    def forbidden(*a, **k):
        raise AssertionError("--brief must not probe")
    monkeypatch.setattr(cock.sqlite3, "connect", forbidden)
    monkeypatch.setattr(cock.subprocess, "run", forbidden)
    monkeypatch.setattr(cock.urllib.request, "urlopen", forbidden)

    assert cock.main(["--brief"]) == cock.EXIT_OK
