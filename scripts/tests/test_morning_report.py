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
    return ["--health", str(health), "--maint-log", str(log),
            "--db", str(db), "--data-dir", str(tmp_path / "data")]


def test_main_delivers_to_control_group(tmp_path: Path):
    argv = _setup_sources(tmp_path, with_main=True)
    deliver = _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=_Recorder(), now=NOW)
    assert code == 0
    assert len(deliver.calls) == 1
    data_dir, folder, jid, text, ts = deliver.calls[0]
    assert folder == "main" and jid == "main@g.us"
    assert "While you slept" in text


def test_main_no_control_group_skips_and_notifies(tmp_path: Path):
    argv = _setup_sources(tmp_path, with_main=False)
    deliver, notify = _Recorder(), _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=notify, now=NOW)
    assert code == 0
    assert deliver.calls == []        # no chat delivery
    assert len(notify.calls) == 1     # desktop fallback instead


def test_main_no_data_is_benign_skip(tmp_path: Path):
    argv = ["--health", str(tmp_path / "nope.jsonl"),
            "--maint-log", str(tmp_path / "nope.log"),
            "--db", str(tmp_path / "nope.db"), "--data-dir", str(tmp_path / "data")]
    deliver = _Recorder()
    code = mr.main(argv=argv, deliverer=deliver, notifier=_Recorder(), now=NOW)
    assert code == 0
    assert deliver.calls == []
