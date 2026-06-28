"""Tests for scripts/session_preflight_hook.py — the SessionStart preflight hook.

The hook is a thin, fail-safe wrapper around session_preflight.py: read the
session_id from stdin, run the detector with --critical-only, and emit a WARN
banner only on a CONFLICT. Every failure path must exit 0 and emit nothing. Tests
drive main() with monkeypatched stdin + subprocess.
"""
import io
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import session_preflight_hook as hook  # noqa: E402


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _fake_proc(stdout, returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# The real detector exists on disk; point _DETECTOR at it so .is_file() is True
# without monkeypatching a method on an (immutable) Path instance.
_REAL_DETECTOR = Path(_SCRIPTS_DIR) / "session_preflight.py"
_MISSING_DETECTOR = Path(_SCRIPTS_DIR) / "does_not_exist_preflight.py"


CONFLICT_JSON = json.dumps(
    {
        "status": "CONFLICT",
        "exit_code": 6,
        "toplevel": "/repo/top",
        "findings": [
            {"severity": "CRITICAL", "code": "live_session_same_tree", "detail": "session abc live"},
            {"severity": "CRITICAL", "code": "branch_in_other_worktree", "detail": "branch x at /wt"},
        ],
    }
)
OK_JSON = json.dumps({"status": "OK", "exit_code": 0, "toplevel": "/repo/top", "findings": []})


# ── _read_session_id ─────────────────────────────────────────────────────────
class TestReadSessionId:
    def test_extracts_session_id(self, monkeypatch):
        _stdin(monkeypatch, json.dumps({"session_id": "uuid-123", "source": "startup"}))
        assert hook._read_session_id() == "uuid-123"

    def test_empty_stdin(self, monkeypatch):
        _stdin(monkeypatch, "")
        assert hook._read_session_id() == ""

    def test_malformed_json(self, monkeypatch):
        _stdin(monkeypatch, "{not json")
        assert hook._read_session_id() == ""

    def test_missing_field(self, monkeypatch):
        _stdin(monkeypatch, json.dumps({"source": "resume"}))
        assert hook._read_session_id() == ""

    def test_non_dict_payload(self, monkeypatch):
        _stdin(monkeypatch, json.dumps(["a", "b"]))
        assert hook._read_session_id() == ""


# ── _run_detector ────────────────────────────────────────────────────────────
class TestRunDetector:
    def test_passes_self_and_critical_only(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["timeout"] = kw.get("timeout")
            return _fake_proc(OK_JSON)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        hook._run_detector("uuid-9")
        assert "--critical-only" in captured["cmd"]
        assert "--json" in captured["cmd"]
        assert "--self" in captured["cmd"] and "uuid-9" in captured["cmd"]
        assert captured["timeout"] == hook._DETECTOR_TIMEOUT

    def test_omits_self_when_blank(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _fake_proc(OK_JSON)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        hook._run_detector("")
        assert "--self" not in captured["cmd"]

    def test_conflict_returncode_still_parsed(self, monkeypatch):
        # detector exits 6 on a real conflict -- non-zero is expected, not an error
        monkeypatch.setattr(hook.subprocess, "run", lambda cmd, **kw: _fake_proc(CONFLICT_JSON, returncode=6))
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        assert hook._run_detector("x")["status"] == "CONFLICT"

    def test_missing_detector_returns_none(self, monkeypatch):
        monkeypatch.setattr(hook, "_DETECTOR", _MISSING_DETECTOR)
        assert hook._run_detector("x") is None

    def test_timeout_returns_none(self, monkeypatch):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 6)

        monkeypatch.setattr(hook.subprocess, "run", boom)
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        assert hook._run_detector("x") is None

    def test_oserror_returns_none(self, monkeypatch):
        def boom(cmd, **kw):
            raise OSError("exec failed")

        monkeypatch.setattr(hook.subprocess, "run", boom)
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        assert hook._run_detector("x") is None

    def test_nonjson_stdout_returns_none(self, monkeypatch):
        monkeypatch.setattr(hook.subprocess, "run", lambda cmd, **kw: _fake_proc("not json"))
        monkeypatch.setattr(hook, "_DETECTOR", _REAL_DETECTOR)
        assert hook._run_detector("x") is None


# ── _banner ──────────────────────────────────────────────────────────────────
class TestBanner:
    def test_conflict_banner_lists_findings(self):
        out = hook._banner(json.loads(CONFLICT_JSON))
        assert out is not None
        assert "PREFLIGHT" in out and "2 other live session" in out
        assert "session abc live" in out and "branch x at /wt" in out
        assert "/repo/top" in out
        assert "deus preflight" in out

    def test_non_conflict_returns_none(self):
        assert hook._banner(json.loads(OK_JSON)) is None

    def test_conflict_without_findings_returns_none(self):
        assert hook._banner({"status": "CONFLICT", "findings": []}) is None

    def test_findings_not_list_returns_none(self):
        assert hook._banner({"status": "CONFLICT", "findings": "oops"}) is None


# ── main() — end-to-end, always exits cleanly ────────────────────────────────
class TestMain:
    def test_conflict_emits_banner(self, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"session_id": "me"}))
        monkeypatch.setattr(hook, "_run_detector", lambda sid: json.loads(CONFLICT_JSON))
        hook.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "PREFLIGHT" in payload["hookSpecificOutput"]["additionalContext"]

    def test_clean_emits_nothing(self, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"session_id": "me"}))
        monkeypatch.setattr(hook, "_run_detector", lambda sid: json.loads(OK_JSON))
        hook.main()
        assert capsys.readouterr().out == ""

    def test_detector_none_emits_nothing(self, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"session_id": "me"}))
        monkeypatch.setattr(hook, "_run_detector", lambda sid: None)
        hook.main()
        assert capsys.readouterr().out == ""

    def test_optout_skips_detector(self, monkeypatch, capsys):
        monkeypatch.setenv("DEUS_PREFLIGHT_HOOK", "0")
        _stdin(monkeypatch, json.dumps({"session_id": "me"}))
        called = {"k": False}

        def boom(sid):
            called["k"] = True
            raise AssertionError("detector must not run when opted out")

        monkeypatch.setattr(hook, "_run_detector", boom)
        hook.main()
        assert called["k"] is False
        assert capsys.readouterr().out == ""

    def test_self_id_threaded_to_detector(self, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"session_id": "uuid-xyz"}))
        seen = {}

        def fake_detector(sid):
            seen["sid"] = sid
            return json.loads(OK_JSON)

        monkeypatch.setattr(hook, "_run_detector", fake_detector)
        hook.main()
        assert seen["sid"] == "uuid-xyz"
