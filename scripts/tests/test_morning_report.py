"""Tests for the morning memory report (scripts/maintenance/morning_report.py).

Hermetic: health/maintenance sources are fixture files under tmp_path, the
control-group DB is a throwaway sqlite file, and delivery is an injected
recorder — nothing shells out, no Ollama, no real chat. The IPC delivery
contract is asserted against the same shape the in-process watcher validates
(IpcMessageFileSchema: {type, chatJid?, text?}).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "morning_report.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("morning_report", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["morning_report"] = mod
    spec.loader.exec_module(mod)
    return mod


mr = _load()

NOW = 1_750_000_000.0  # fixed epoch seconds for deterministic ts/date


def _health(**kw) -> dict:
    base = {"date": "2026-06-24", "atoms": 2949, "avg_confidence": 0.702,
            "sessions": 811, "entities": 1513, "articles": 324, "articles_stale": 50}
    base.update(kw)
    return base


# ── _read_health ──────────────────────────────────────────────────────────────

def test_read_health_latest_and_prev(tmp_path: Path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in [
        _health(date="2026-06-22", atoms=2900),
        _health(date="2026-06-23", atoms=2940),
        _health(date="2026-06-24", atoms=2949),
    ]))
    latest, prev = mr._read_health(p)
    assert latest["atoms"] == 2949 and prev["atoms"] == 2940  # last two only


def test_read_health_recovers_prev_past_malformed_tail(tmp_path: Path):
    # A corrupted trailing line must NOT drop the valid previous snapshot (the
    # overnight delta would silently vanish). Scan-from-end skips it.
    p = tmp_path / "h.jsonl"
    p.write_text(
        json.dumps(_health(date="2026-06-23", atoms=2900)) + "\n"
        + json.dumps(_health(date="2026-06-24", atoms=2940)) + "\n"
        + "{truncated write\n"
    )
    latest, prev = mr._read_health(p)
    assert latest["atoms"] == 2940 and prev["atoms"] == 2900


def test_read_health_missing_file(tmp_path: Path):
    assert mr._read_health(tmp_path / "nope.jsonl") == (None, None)


def test_read_health_skips_malformed(tmp_path: Path):
    p = tmp_path / "h.jsonl"
    p.write_text("{bad json\n" + json.dumps(_health()) + "\n")
    latest, prev = mr._read_health(p)
    assert latest["atoms"] == 2949 and prev is None


# ── _parse_last_maintenance_run ─────────────────────────────────────────────────

_LOG = """\
=== Deus maintenance — 2026-06-23 04:30 ===

── Daily ──
  [memory_gc] OK
  [health] OK
=== Done: 5 OK, 0 failed ===

=== Deus maintenance — 2026-06-24 04:30 ===

── Daily ──
  [memory_gc] OK
  [credential_probe] running...
    [codex] WARN — only 8min to expiry (refresher stalled?)
    credential_probe: 2 OK, 0 WARN, 0 skipped
  [credential_probe] FAILED (exit 1)
  [health] OK
── Weekly ──
  [judge_calibration] running...
    [WARN] quality Pearson 0.40 < 0.580 floor — local judge calibration REGRESSION
  [judge_calibration] FAILED (exit 1)
=== Done: 4 OK, 2 failed ===
"""


def test_parse_last_run_only_last_block(tmp_path: Path):
    p = tmp_path / "maintenance.log"
    p.write_text(_LOG)
    m = mr._parse_last_maintenance_run(p)
    assert m["ran"] is True
    assert "2026-06-24" in m["header"]
    assert set(m["failed"]) == {"credential_probe", "judge_calibration"}
    assert m["ok"] == 4
    # WARN/REGRESSION lines surfaced
    assert any("expiry" in w for w in m["warns"])
    assert any("REGRESSION" in w for w in m["warns"])
    # ...but a healthy count-summary ("2 OK, 0 WARN, 0 skipped") is NOT a warning.
    assert not any("skipped" in w for w in m["warns"])


def test_parse_missing_log(tmp_path: Path):
    assert mr._parse_last_maintenance_run(tmp_path / "nope.log") is None


# ── _format_digest ──────────────────────────────────────────────────────────────

def test_format_digest_deltas_and_warns():
    latest = _health(atoms=2949, avg_confidence=0.702)
    prev = _health(date="2026-06-23", atoms=2940, avg_confidence=0.690)
    maint = {"ran": True, "ok": 4, "failed": ["credential_probe"],
             "warns": ["[codex] WARN — only 8min to expiry"], "done": "Done: 4 OK, 1 failed"}
    out = mr._format_digest(latest, prev, maint, "2026-06-24")
    assert "+9" in out  # atom delta 2949-2940
    assert "+0.012" in out  # confidence delta
    assert "1 failed: credential_probe" in out
    assert "⚠️" in out and "expiry" in out


def test_format_digest_stale_snapshot_flagged():
    latest = _health(date="2026-06-22")  # older than 'today'
    out = mr._format_digest(latest, None, {"ran": True, "ok": 5, "failed": [], "warns": []}, "2026-06-24")
    assert "2026-06-22" in out and "no fresh" in out


def test_format_digest_no_data_fallbacks():
    out = mr._format_digest(None, None, None, "2026-06-24")
    assert "no health snapshot" in out and "no overnight run" in out


# ── _find_control_group ─────────────────────────────────────────────────────────

def _make_db(tmp_path: Path, *, with_main: bool) -> Path:
    db = tmp_path / "messages.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE registered_groups (folder TEXT, jid TEXT, is_main INTEGER)")
    con.execute("INSERT INTO registered_groups VALUES ('other','other@g.us',0)")
    if with_main:
        con.execute("INSERT INTO registered_groups VALUES ('main','main@g.us',1)")
    con.commit()
    con.close()
    return db


def test_find_control_group_present(tmp_path: Path):
    assert mr._find_control_group(_make_db(tmp_path, with_main=True)) == ("main", "main@g.us")


def test_find_control_group_absent(tmp_path: Path):
    assert mr._find_control_group(_make_db(tmp_path, with_main=False)) is None


def test_find_control_group_missing_db(tmp_path: Path):
    assert mr._find_control_group(tmp_path / "nope.db") is None


# ── _deliver ────────────────────────────────────────────────────────────────────

def test_deliver_writes_schema_valid_ipc_file(tmp_path: Path):
    ok = mr._deliver(tmp_path, "main", "main@g.us", "hello\nworld", ts=123)
    assert ok is True
    files = list((tmp_path / "ipc" / "main" / "messages").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    # IpcMessageFileSchema contract: type required; chatJid/text the carried fields.
    assert payload["type"] == "message"
    assert payload["chatJid"] == "main@g.us"
    assert payload["text"] == "hello\nworld"


def test_deliver_rejects_path_traversal_folder(tmp_path: Path):
    # Defense-in-depth: a folder that could escape data/ipc/ is refused, no write.
    assert mr._deliver(tmp_path, "../../etc", "j@g.us", "x", ts=1) is False
    assert not (tmp_path / "ipc").exists()


# ── main (injected deliverer/notifier) ──────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return True


def _setup_sources(tmp_path: Path, *, with_main=True):
    health = tmp_path / "h.jsonl"
    health.write_text(json.dumps(_health(date="2026-06-24")) + "\n")
    log = tmp_path / "maintenance.log"
    log.write_text(_LOG)
    db = _make_db(tmp_path, with_main=with_main)
    # --cockpit is pinned to tmp_path so no test ever reads the developer's real
    # ~/.deus/cockpit_health.json. Paired with cockpit_expected=lambda: False at
    # each call site, which keeps the real launchd plist out of the test too.
    return ["--health", str(health), "--maint-log", str(log),
            "--db", str(db), "--data-dir", str(tmp_path / "data"),
            "--cockpit", str(tmp_path / "no-cockpit.json")]


def test_main_delivers_to_control_group(tmp_path: Path):
    argv = _setup_sources(tmp_path, with_main=True)
    deliver = _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=_Recorder(), now=NOW,
                   cockpit_expected=lambda: False)
    assert code == 0
    assert len(deliver.calls) == 1
    data_dir, folder, jid, text, ts = deliver.calls[0]
    assert folder == "main" and jid == "main@g.us"
    assert "While you slept" in text


def test_main_no_control_group_skips_and_notifies(tmp_path: Path):
    argv = _setup_sources(tmp_path, with_main=False)
    deliver, notify = _Recorder(), _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=notify, now=NOW,
                   cockpit_expected=lambda: False)
    assert code == 0
    assert deliver.calls == []        # no chat delivery
    assert len(notify.calls) == 1     # desktop fallback instead


def test_main_no_data_is_benign_skip(tmp_path: Path):
    argv = ["--health", str(tmp_path / "nope.jsonl"),
            "--maint-log", str(tmp_path / "nope.log"),
            "--db", str(tmp_path / "nope.db"), "--data-dir", str(tmp_path / "data"),
            "--cockpit", str(tmp_path / "nope.json")]
    deliver = _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=_Recorder(), now=NOW,
                   cockpit_expected=lambda: False)
    assert code == 0
    assert deliver.calls == []


# ── cockpit verdict (LIA-552) ───────────────────────────────────────────────────

def _cockpit(**kw) -> dict:
    base = {"checked_at": NOW, "probes": [{"probe": "memory", "status": "OK"}]}
    base.update(kw)
    return base


def _write_cockpit(tmp_path: Path, obj) -> Path:
    p = tmp_path / "cockpit_health.json"
    p.write_text(json.dumps(obj))
    return p


class TestReadCockpit:
    """A malformed artifact must never take down the unattended report."""

    def test_reads_valid_artifact(self, tmp_path: Path):
        assert mr._read_cockpit(_write_cockpit(tmp_path, _cockpit())) is not None

    def test_missing_file(self, tmp_path: Path):
        assert mr._read_cockpit(tmp_path / "nope.json") is None

    def test_malformed_json(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text("{not json")
        assert mr._read_cockpit(p) is None

    def test_non_dict_top_level(self, tmp_path: Path):
        assert mr._read_cockpit(_write_cockpit(tmp_path, [1, 2])) is None

    def test_string_checked_at_rejected(self, tmp_path: Path):
        assert mr._read_cockpit(_write_cockpit(tmp_path, _cockpit(checked_at="yesterday"))) is None

    def test_bool_checked_at_rejected(self, tmp_path: Path):
        # bool subclasses int, so a bare isinstance(..., (int, float)) would
        # accept JSON `true` and compute an age off 1.0.
        assert mr._read_cockpit(_write_cockpit(tmp_path, _cockpit(checked_at=True))) is None

    def test_non_list_probes_rejected(self, tmp_path: Path):
        assert mr._read_cockpit(_write_cockpit(tmp_path, _cockpit(probes={"a": 1}))) is None

    def test_non_dict_probe_entry_rejects_whole_artifact(self, tmp_path: Path):
        # Skipping the bad entry instead would still count it toward the probe
        # total, manufacturing a false-clean "N OK" out of garbage.
        bad = _cockpit(probes=[{"probe": "memory", "status": "OK"}, "garbage"])
        assert mr._read_cockpit(_write_cockpit(tmp_path, bad)) is None


class TestCockpitExpected:
    def test_true_for_each_posix_marker(self, tmp_path: Path, monkeypatch):
        # Each platform's install marker must count on its own — checking only
        # the macOS plist would silently omit every verdict on Linux installs.
        for i in range(3):
            marker = tmp_path / f"marker{i}"
            marker.write_text("")
            markers = [tmp_path / "absent-a", tmp_path / "absent-b", tmp_path / "absent-c"]
            markers[i] = marker
            monkeypatch.setattr(mr, "COCKPIT_JOB_MARKERS", tuple(markers))
            assert mr._cockpit_expected() is True

    def test_false_when_no_marker_present(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(mr, "COCKPIT_JOB_MARKERS", (tmp_path / "absent",))
        monkeypatch.setattr(sys, "platform", "linux")
        assert mr._cockpit_expected() is False

    def test_windows_falls_back_to_task_scheduler(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(mr, "COCKPIT_JOB_MARKERS", (tmp_path / "absent",))
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(mr.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(returncode=0))
        assert mr._cockpit_expected() is True

    def test_windows_query_failure_reports_not_expected(self, tmp_path: Path, monkeypatch):
        # Cannot tell -> say "not expected" rather than emit a daily warning we
        # are not sure is warranted.
        monkeypatch.setattr(mr, "COCKPIT_JOB_MARKERS", (tmp_path / "absent",))
        monkeypatch.setattr(sys, "platform", "win32")

        def _boom(*a, **k):
            raise OSError("schtasks missing")

        monkeypatch.setattr(mr.subprocess, "run", _boom)
        assert mr._cockpit_expected() is False


class TestFormatCockpit:
    def test_not_expected_renders_nothing(self):
        assert mr._format_cockpit(None, False, NOW) == []
        assert mr._format_cockpit(_cockpit(), False, NOW) == []

    def test_expected_but_missing_warns(self):
        out = mr._format_cockpit(None, True, NOW)
        assert len(out) == 1 and "no verdict on record" in out[0]

    def test_every_line_is_attributable_to_the_cockpit(self):
        # An unlabelled top-level line renders under "Maintenance (04:30)",
        # whose own convention is `  ⚠️ {warn}` — so a bare warning would read
        # as a maintenance warning. Every line must carry the label or be
        # indented beneath one that does.
        stale = _cockpit(checked_at=NOW - (mr.COCKPIT_MAX_AGE_SEC + 3600),
                         probes=[{"probe": "a", "status": "FAILED"}])
        for case in (mr._format_cockpit(None, True, NOW),
                     mr._format_cockpit(stale, True, NOW),
                     mr._format_cockpit(_cockpit(), True, NOW)):
            assert case, "expected verdict must render something"
            assert case[0].startswith("Cockpit (06:45):"), case[0]
            for extra in case[1:]:
                assert extra.startswith("  "), extra

    def test_all_ok_summary(self):
        out = "\n".join(mr._format_cockpit(_cockpit(), True, NOW))
        assert "Cockpit (06:45): 1 OK" in out
        assert "not OK" not in out

    def test_non_ok_probe_listed_with_raw_status(self):
        c = _cockpit(probes=[
            {"probe": "memory", "status": "OK"},
            {"probe": "evolution.optimizer", "status": "FAILED",
             "observed": "python3 cannot import dspy"},
        ])
        out = "\n".join(mr._format_cockpit(c, True, NOW))
        assert "Cockpit (06:45): 1 OK, 1 not OK" in out
        assert "FAILED evolution.optimizer (ONGOING) — python3 cannot import dspy" in out

    def test_degraded_and_unknown_keep_their_own_status(self):
        # The cockpit ranks OK/DEGRADED/UNKNOWN/FAILED distinctly; collapsing
        # every non-OK into one label would discard that.
        c = _cockpit(probes=[{"probe": "a", "status": "DEGRADED"},
                             {"probe": "b", "status": "UNKNOWN"}])
        out = "\n".join(mr._format_cockpit(c, True, NOW))
        assert "DEGRADED a" in out and "UNKNOWN b" in out

    def test_regression_tagged_new(self):
        c = _cockpit(probes=[{"probe": "a", "status": "FAILED", "is_regression": True}])
        assert "(NEW)" in "\n".join(mr._format_cockpit(c, True, NOW))

    def test_stale_verdict_warns(self):
        c = _cockpit(checked_at=NOW - (mr.COCKPIT_MAX_AGE_SEC + 3600))
        out = "\n".join(mr._format_cockpit(c, True, NOW))
        assert "verdict is" in out and "old" in out

    def test_fresh_verdict_does_not_warn(self):
        out = "\n".join(mr._format_cockpit(_cockpit(), True, NOW))
        assert "old (healthcheck" not in out


class TestCockpitInMain:
    def test_missing_verdict_does_not_trigger_benign_skip(self, tmp_path: Path):
        # The whole point: when a verdict was expected and is absent, the report
        # must still run. Gating the early return on presence would exit exactly
        # then — the one case that most needs reporting.
        argv = ["--health", str(tmp_path / "nope.jsonl"),
                "--maint-log", str(tmp_path / "nope.log"),
                "--db", str(tmp_path / "nope.db"), "--data-dir", str(tmp_path / "data"),
                "--cockpit", str(tmp_path / "nope.json")]
        notify = _Recorder()
        code = mr.main(argv=argv, deliverer=_Recorder(), notifier=notify, now=NOW,
                       cockpit_expected=lambda: True)
        assert code == 0
        assert len(notify.calls) == 1
        assert "Cockpit (06:45): ⚠️ no verdict on record" in notify.calls[0][1]

    def test_verdict_reaches_the_delivered_digest(self, tmp_path: Path):
        argv = _setup_sources(tmp_path, with_main=True)
        c = _cockpit(probes=[{"probe": "evolution.optimizer", "status": "FAILED",
                              "observed": "python3 cannot import dspy"}])
        argv[argv.index("--cockpit") + 1] = str(_write_cockpit(tmp_path, c))
        deliver = _Recorder()
        code = mr.main(argv=argv, deliverer=deliver, notifier=_Recorder(), now=NOW,
                       cockpit_expected=lambda: True)
        assert code == 0
        text = deliver.calls[0][3]
        assert "Cockpit (06:45): 0 OK, 1 not OK" in text
        assert "FAILED evolution.optimizer" in text
