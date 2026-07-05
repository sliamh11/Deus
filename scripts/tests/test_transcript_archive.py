"""Tests for scripts/transcript_archive.py (LIA-374 rank-1).

Content-addressed cold-source retention: archive session transcripts to
~/.deus/archive/transcripts/<sha256>.jsonl.zst so lossy /compress summaries
keep a decompress-back-to-source path. Best-effort mode never blocks.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _load("transcript_archive")


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "archive"
    monkeypatch.setenv("DEUS_TRANSCRIPT_ARCHIVE_DIR", str(d))
    return d


@pytest.fixture
def transcript(tmp_path):
    t = tmp_path / "session.jsonl"
    t.write_text('{"type":"user","text":"hello"}\n{"type":"assistant"}\n', encoding="utf-8")
    return t


class TestArchive:
    def test_content_addressed_and_idempotent(self, store, transcript):
        r1 = ta.archive(transcript)
        assert r1["ok"] is True
        dest1 = Path(r1["dest"])
        assert dest1.exists()
        assert dest1.name.startswith(r1["sha256"])
        mtime = dest1.stat().st_mtime
        r2 = ta.archive(transcript)  # second call: skip, same dest
        assert r2["ok"] is True and r2["dest"] == r1["dest"]
        assert Path(r2["dest"]).stat().st_mtime == mtime
        assert r2["skipped"] is True

    def test_sha_changes_with_content(self, store, transcript, tmp_path):
        r1 = ta.archive(transcript)
        other = tmp_path / "other.jsonl"
        other.write_text("different\n", encoding="utf-8")
        r2 = ta.archive(other)
        assert r1["sha256"] != r2["sha256"]

    def test_roundtrip_byte_equality(self, store, transcript):
        r = ta.archive(transcript)
        restored = ta.decompress(Path(r["dest"]))
        assert restored == transcript.read_bytes()

    def test_gzip_fallback_when_zstd_missing(self, store, transcript, monkeypatch):
        monkeypatch.setattr(ta, "_zstd_bin", lambda: None)
        r = ta.archive(transcript)
        assert r["ok"] is True
        assert r["dest"].endswith(".jsonl.gz")
        assert ta.decompress(Path(r["dest"])) == transcript.read_bytes()

    def test_best_effort_never_raises_on_missing_transcript(self, store, tmp_path):
        r = ta.archive(tmp_path / "nope.jsonl", best_effort=True)
        assert r["ok"] is False and "error" in r

    def test_strict_mode_raises_on_missing_transcript(self, store, tmp_path):
        with pytest.raises(FileNotFoundError):
            ta.archive(tmp_path / "nope.jsonl")


class TestResolve:
    def test_resolves_via_session_registry(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        projects = tmp_path / "projects"
        sessions.mkdir()
        cwd = "/Users/x/proj"
        slug = cwd.replace("/", "-")
        tdir = projects / slug
        tdir.mkdir(parents=True)
        (tdir / "sess-1.jsonl").write_text("{}\n", encoding="utf-8")
        (sessions / "123.json").write_text(
            json.dumps({"pid": 123, "sessionId": "sess-1", "cwd": cwd}), encoding="utf-8"
        )
        monkeypatch.setattr(ta, "_sessions_dir", lambda: sessions)
        monkeypatch.setattr(ta, "_projects_dir", lambda: projects)
        assert ta.resolve_transcript(cwd) == tdir / "sess-1.jsonl"

    def test_falls_back_to_newest_jsonl(self, tmp_path, monkeypatch):
        import os
        import time

        sessions = tmp_path / "sessions"
        projects = tmp_path / "projects"
        sessions.mkdir()
        cwd = "/Users/x/proj"
        tdir = projects / cwd.replace("/", "-")
        tdir.mkdir(parents=True)
        old = tdir / "old.jsonl"
        new = tdir / "new.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        new.write_text("{}\n", encoding="utf-8")
        stale = time.time() - 1000
        os.utime(old, (stale, stale))
        monkeypatch.setattr(ta, "_sessions_dir", lambda: sessions)  # no registry match
        monkeypatch.setattr(ta, "_projects_dir", lambda: projects)
        assert ta.resolve_transcript(cwd) == new

    def test_claude_session_id_wins_over_registry_and_mtime(
        self, tmp_path, monkeypatch
    ):
        # Concurrent same-cwd sessions: CLAUDE_SESSION_ID must pin OUR
        # transcript even when a sibling session's file is newer / registered.
        sessions = tmp_path / "sessions"
        projects = tmp_path / "projects"
        sessions.mkdir()
        cwd = "/Users/x/proj"
        tdir = projects / cwd.replace("/", "-")
        tdir.mkdir(parents=True)
        (tdir / "ours.jsonl").write_text("{}\n", encoding="utf-8")
        (tdir / "sibling.jsonl").write_text("{}\n", encoding="utf-8")
        (sessions / "9.json").write_text(
            json.dumps({"pid": 9, "sessionId": "sibling", "cwd": cwd}),
            encoding="utf-8",
        )
        monkeypatch.setattr(ta, "_sessions_dir", lambda: sessions)
        monkeypatch.setattr(ta, "_projects_dir", lambda: projects)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "ours")
        assert ta.resolve_transcript(cwd) == tdir / "ours.jsonl"
        monkeypatch.delenv("CLAUDE_SESSION_ID")
        assert ta.resolve_transcript(cwd) == tdir / "sibling.jsonl"

    def test_dotted_cwd_segments_encode_to_dashes(self, tmp_path, monkeypatch):
        # Claude Code slugs map '.' to '-' as well: /x/.claude/wt -> -x--claude-wt.
        sessions = tmp_path / "sessions"
        projects = tmp_path / "projects"
        sessions.mkdir()
        cwd = "/x/.claude/worktrees/wt-1"
        tdir = projects / "-x--claude-worktrees-wt-1"
        tdir.mkdir(parents=True)
        (tdir / "s.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(ta, "_sessions_dir", lambda: sessions)
        monkeypatch.setattr(ta, "_projects_dir", lambda: projects)
        assert ta.resolve_transcript(cwd) == tdir / "s.jsonl"

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ta, "_sessions_dir", lambda: tmp_path / "s")
        monkeypatch.setattr(ta, "_projects_dir", lambda: tmp_path / "p")
        assert ta.resolve_transcript("/no/such/cwd") is None


class TestCli:
    def test_json_best_effort_exit_zero_on_error(self, store, tmp_path, capsys):
        rc = ta.main(["--transcript", str(tmp_path / "nope.jsonl"), "--json", "--best-effort"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False

    def test_json_happy_path(self, store, transcript, capsys):
        rc = ta.main(["--transcript", str(transcript), "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["sha256"] and out["dest"]

    def test_missing_transcript_without_best_effort_is_clean_error(
        self, store, tmp_path, capsys
    ):
        # Both CLI branches must degrade identically — no raw traceback.
        rc = ta.main(["--transcript", str(tmp_path / "nope.jsonl")])
        assert rc == 1
        assert "transcript not found" in capsys.readouterr().err
