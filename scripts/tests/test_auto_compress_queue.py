"""Tests for scripts/session_end_hook.py (enqueue + debounced worker) and
scripts/maintenance/compress_sweep.py (the stale-entry safety net).

All auto_compress.py subprocess dispatches are stubbed — these tests never
invoke the real worker script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


stop_hook = _load("stop_hook", _ROOT / "scripts" / "stop_hook.py")
seh = _load("session_end_hook", _ROOT / "scripts" / "session_end_hook.py")
sweep_mod = _load("compress_sweep", _ROOT / "scripts" / "maintenance" / "compress_sweep.py")


@pytest.fixture(autouse=True)
def isolate_queue_and_log(tmp_path, monkeypatch):
    queue_dir = tmp_path / "compress_queue"
    log_path = tmp_path / "auto_compress.log"
    monkeypatch.setattr(seh, "QUEUE_DIR", queue_dir)
    monkeypatch.setattr(seh, "LOG_PATH", log_path)
    monkeypatch.setattr(sweep_mod, "QUEUE_DIR", queue_dir)
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    monkeypatch.delenv("DEUS_AUTO_COMPRESS", raising=False)


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("DEUS_VAULT_PATH", str(v))
    return v


def _real_transcript(tmp_path, n_turns=6):
    p = tmp_path / "transcript.jsonl"
    lines = []
    for i in range(n_turns):
        lines.append(json.dumps({"type": "user" if i % 2 == 0 else "assistant", "timestamp": f"2026-01-01T00:0{i}:00Z"}))
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ── Parent-side enqueue gates ─────────────────────────────────────────────────

def test_main_skips_bg_session(tmp_path, fake_vault, monkeypatch):
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(tmp_path))
    transcript = _real_transcript(tmp_path)
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)})})())
    seh.main()
    assert not seh.QUEUE_DIR.exists() or not list(seh.QUEUE_DIR.glob("*.json"))


def test_main_skips_on_opt_out(tmp_path, fake_vault, monkeypatch):
    monkeypatch.setenv("DEUS_AUTO_COMPRESS", "0")
    transcript = _real_transcript(tmp_path)
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)})})())
    seh.main()
    assert not seh.QUEUE_DIR.exists() or not list(seh.QUEUE_DIR.glob("*.json"))


def test_main_skips_trivial_session(tmp_path, fake_vault, monkeypatch):
    transcript = _real_transcript(tmp_path, n_turns=2)  # below BG_COMPRESS_MIN_TURNS
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)})})())
    seh.main()
    assert not seh.QUEUE_DIR.exists() or not list(seh.QUEUE_DIR.glob("*.json"))


def test_main_skips_when_no_vault(tmp_path, monkeypatch):
    # Stub the resolver rather than just unsetting the env var -- the real
    # dev machine's global ~/.config/deus/config.json would otherwise still
    # resolve a real vault, defeating the test's intent.
    monkeypatch.setattr(seh, "_load_vault_root", lambda cwd=None: None)
    transcript = _real_transcript(tmp_path)
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)})})())
    seh.main()
    assert not seh.QUEUE_DIR.exists() or not list(seh.QUEUE_DIR.glob("*.json"))


def test_main_skips_when_compress_already_ran(tmp_path, fake_vault, monkeypatch):
    p = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"type": "user", "timestamp": f"2026-01-01T00:0{i}:00Z"}) for i in range(6)]
    lines.append(json.dumps({
        "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "compress"}}]},
    }))
    p.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "s1", "transcript_path": str(p), "cwd": str(tmp_path)})})())
    monkeypatch.setattr(seh, "_spawn_worker", lambda sid: pytest.fail("should not spawn"))
    seh.main()
    assert not seh.QUEUE_DIR.exists() or not list(seh.QUEUE_DIR.glob("*.json"))


def test_main_enqueues_and_spawns_on_valid_session(tmp_path, fake_vault, monkeypatch):
    transcript = _real_transcript(tmp_path)
    spawned = []
    monkeypatch.setattr(seh, "_spawn_worker", lambda sid: spawned.append(sid))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps(
        {"session_id": "sess-abc", "transcript_path": str(transcript), "cwd": str(tmp_path)})})())
    seh.main()
    assert spawned == ["sess-abc"]
    entry = seh.QUEUE_DIR / "sess-abc.json"
    assert entry.exists()
    payload = json.loads(entry.read_text())
    assert payload["session_id"] == "sess-abc"
    assert payload["attempts"] == 0


def test_enqueue_overwrite_resets_queued_at(tmp_path):
    seh._enqueue("s1", str(tmp_path / "t.jsonl"), str(tmp_path))
    first = json.loads(seh._entry_path("s1").read_text())
    time.sleep(0.01)
    seh._enqueue("s1", str(tmp_path / "t.jsonl"), str(tmp_path))
    second = json.loads(seh._entry_path("s1").read_text())
    assert second["queued_at"] != first["queued_at"] or second["queued_at"] >= first["queued_at"]


# ── Worker: claim race, mtime re-arm, success/failure ────────────────────────

@pytest.fixture
def stub_auto_compress_success(monkeypatch):
    calls = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, timeout=None, capture_output=True, text=True):
        calls.append(argv)
        return _FakeCompleted()

    monkeypatch.setattr(seh.subprocess, "run", _fake_run)
    return calls


@pytest.fixture
def stub_auto_compress_failure(monkeypatch):
    class _FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(seh.subprocess, "run", lambda *a, **k: _FakeCompleted())


def test_worker_body_success_deletes_entry(tmp_path, stub_auto_compress_success):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    seh._enqueue("sid1", str(transcript), str(tmp_path))
    seh._worker_body("sid1", debounce_s=0, ceiling=5)
    assert not seh._entry_path("sid1").exists()
    assert not seh._entry_path("sid1").with_suffix(".json.running").exists()
    assert stub_auto_compress_success  # auto_compress.py was actually invoked


def test_worker_body_failure_increments_attempts(tmp_path, stub_auto_compress_failure):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    seh._enqueue("sid1", str(transcript), str(tmp_path))
    seh._worker_body("sid1", debounce_s=0, ceiling=5)
    entry = seh._entry_path("sid1")
    assert entry.exists()
    payload = json.loads(entry.read_text())
    assert payload["attempts"] == 1


def test_worker_body_rearms_on_mtime_change(tmp_path, stub_auto_compress_success):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("v1", encoding="utf-8")
    seh._enqueue("sid1", str(transcript), str(tmp_path))
    time.sleep(0.02)
    transcript.write_text("v2 -- session reopened", encoding="utf-8")  # mtime changes
    seh._worker_body("sid1", debounce_s=0, ceiling=5)
    assert not stub_auto_compress_success  # never invoked
    entry = seh._entry_path("sid1")
    assert entry.exists()  # re-armed, not claimed


def test_worker_body_missing_entry_is_noop(tmp_path, stub_auto_compress_success):
    seh._worker_body("nonexistent", debounce_s=0, ceiling=5)
    assert not stub_auto_compress_success


def test_worker_body_claim_race_only_one_proceeds(tmp_path, stub_auto_compress_success):
    """Simulate: worker A claims (renames to .running); worker B then tries the
    same entry and must lose the race (FileNotFoundError -> no-op)."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    seh._enqueue("sid1", str(transcript), str(tmp_path))

    entry = seh._entry_path("sid1")
    import os
    running = entry.with_suffix(".json.running")
    os.rename(entry, running)  # simulate worker A having already claimed it

    # Worker B's body would find no entry.json at all now.
    seh._worker_body("sid1", debounce_s=0, ceiling=5)
    assert not stub_auto_compress_success
    assert running.exists()  # worker A's claim untouched by B


# ── Sweep ─────────────────────────────────────────────────────────────────────

def test_sweep_recovers_orphaned_running(tmp_path, stub_auto_compress_success, monkeypatch):
    seh.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    running = seh.QUEUE_DIR / "sid1.json.running"
    running.write_text(json.dumps({
        "session_id": "sid1", "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(tmp_path), "queued_at": "x", "transcript_mtime": 0, "attempts": 0,
    }), encoding="utf-8")
    old_time = time.time() - 100000
    import os
    os.utime(running, (old_time, old_time))

    sweep_mod.sweep(dry_run=False)
    assert not running.exists()
    recovered = seh.QUEUE_DIR / "sid1.json"
    assert recovered.exists()
    assert json.loads(recovered.read_text())["attempts"] == 1


def test_sweep_gives_up_after_max_attempts(tmp_path):
    seh.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    entry = seh.QUEUE_DIR / "sid1.json"
    entry.write_text(json.dumps({
        "session_id": "sid1", "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(tmp_path), "queued_at": "x", "transcript_mtime": 0, "attempts": 3,
    }), encoding="utf-8")
    old_time = time.time() - 100000
    import os
    os.utime(entry, (old_time, old_time))

    sweep_mod.sweep(dry_run=False)
    assert not entry.exists()
    assert (seh.QUEUE_DIR / "sid1.failed").exists()


def test_sweep_caps_entries_per_run(tmp_path, stub_auto_compress_success):
    seh.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    old_time = time.time() - 100000
    import os
    for i in range(5):
        entry = seh.QUEUE_DIR / f"sid{i}.json"
        entry.write_text(json.dumps({
            "session_id": f"sid{i}", "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(tmp_path), "queued_at": "x", "transcript_mtime": 0, "attempts": 0,
        }), encoding="utf-8")
        os.utime(entry, (old_time, old_time))

    sweep_mod.sweep(dry_run=False)
    remaining = list(seh.QUEUE_DIR.glob("*.json"))
    processed = 5 - len(remaining)
    assert processed == sweep_mod.MAX_ENTRIES_PER_RUN


def test_sweep_dry_run_touches_nothing(tmp_path):
    seh.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    entry = seh.QUEUE_DIR / "sid1.json"
    entry.write_text(json.dumps({
        "session_id": "sid1", "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(tmp_path), "queued_at": "x", "transcript_mtime": 0, "attempts": 0,
    }), encoding="utf-8")
    old_time = time.time() - 100000
    import os
    os.utime(entry, (old_time, old_time))

    sweep_mod.sweep(dry_run=True)
    assert entry.exists()
    payload = json.loads(entry.read_text())
    assert payload["attempts"] == 0


def test_sweep_no_queue_dir_is_noop():
    assert sweep_mod.sweep(dry_run=False) == 0
