"""Tests for scripts/recall_source.py (LIA-374 rank-1) — summary→source recall."""

from __future__ import annotations

import importlib.util
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
rs = _load("recall_source")


@pytest.fixture
def archived(tmp_path, monkeypatch):
    monkeypatch.setenv("DEUS_TRANSCRIPT_ARCHIVE_DIR", str(tmp_path / "archive"))
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"user"}\n{"type":"assistant"}\n', encoding="utf-8")
    result = ta.archive(transcript)
    return transcript, result


class TestRecallSource:
    def test_roundtrip_from_session_log(self, tmp_path, archived, capsysbinary):
        transcript, result = archived
        log = tmp_path / "log.md"
        log.write_text(
            f"---\ntype: session\ndate: 2026-07-05\nsource_transcript: {result['sha256']}\n---\nbody\n",
            encoding="utf-8",
        )
        rc = rs.main(["--source", str(log)])
        assert rc == 0
        assert capsysbinary.readouterr().out == transcript.read_bytes()

    def test_bare_sha_mode(self, archived, capsysbinary):
        transcript, result = archived
        rc = rs.main(["--source", result["sha256"]])
        assert rc == 0
        assert capsysbinary.readouterr().out == transcript.read_bytes()

    def test_out_flag_writes_file(self, tmp_path, archived):
        transcript, result = archived
        out = tmp_path / "restored.jsonl"
        rc = rs.main(["--source", result["sha256"], "--out", str(out)])
        assert rc == 0
        assert out.read_bytes() == transcript.read_bytes()

    def test_missing_frontmatter_key_errors_clearly(self, tmp_path, capsys):
        log = tmp_path / "log.md"
        log.write_text("---\ntype: session\n---\nbody\n", encoding="utf-8")
        rc = rs.main(["--source", str(log)])
        assert rc == 1
        assert "source_transcript" in capsys.readouterr().err

    def test_missing_archive_errors_clearly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DEUS_TRANSCRIPT_ARCHIVE_DIR", str(tmp_path / "empty"))
        rc = rs.main(["--source", "a" * 64])
        assert rc == 1
        assert "archive" in capsys.readouterr().err.lower()
