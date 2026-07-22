"""Tests for scripts/auto_compress.py — the deterministic (no-LLM-turn)
SessionEnd auto-save worker.

No real subprocess/LLM calls: child dispatches are monkeypatched with a
recording stub so tests assert on exact argv, never on live network/LLM
behavior.
"""

from __future__ import annotations

import importlib.util
import json
import sys
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
ac = _load("auto_compress", _ROOT / "scripts" / "auto_compress.py")


# ── extract_transcript_facts ─────────────────────────────────────────────────

def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_extract_facts_counts_turns_and_tools(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}},
        {
            "type": "assistant", "timestamp": "2026-01-01T00:01:00Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b.py"}},
            ]},
        },
        {"type": "user", "timestamp": "2026-01-01T00:02:00Z", "message": {"content": "more"}},
        {
            "type": "assistant", "timestamp": "2026-01-01T00:03:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
        },
    ])
    facts = ac.extract_transcript_facts(str(transcript))
    assert facts["user_turns"] == 2
    assert facts["assistant_turns"] == 2
    assert facts["tool_counts"] == {"Bash": 2, "Edit": 1}
    assert facts["file_paths"] == ["/a/b.py"]
    assert facts["first_ts"] == "2026-01-01T00:00:00Z"
    assert facts["last_ts"] == "2026-01-01T00:03:00Z"


def test_extract_facts_missing_transcript_returns_zeros(tmp_path):
    facts = ac.extract_transcript_facts(str(tmp_path / "nope.jsonl"))
    assert facts == {
        "user_turns": 0, "assistant_turns": 0, "tool_counts": {},
        "file_paths": [], "first_ts": None, "last_ts": None,
    }


def test_extract_facts_skips_malformed_lines(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("not json\n" + json.dumps({"type": "user"}) + "\n\n", encoding="utf-8")
    facts = ac.extract_transcript_facts(str(transcript))
    assert facts["user_turns"] == 1


# ── tool-name bucketing + sanitization (the injected-content-safety layer) ──

def test_bucket_tool_name_known_and_unknown():
    assert ac._bucket_tool_name("Bash") == "Bash"
    assert ac._bucket_tool_name("mcp__linear__linear_createIssue") == "mcp__linear__linear_createIssue"
    assert ac._bucket_tool_name("SomeAdversarialToolName") == "other"


def test_sanitize_strips_control_chars_and_caps_length():
    dirty = "a\nb\rc" + "x" * 400
    clean = ac._sanitize(dirty)
    assert "\n" not in clean
    assert "\r" not in clean
    assert len(clean) <= 300


def test_extract_facts_sanitizes_embedded_newline_in_file_path(tmp_path):
    """Adversarial: a crafted file_path with an embedded newline + fake YAML
    key must not survive into extracted data unsanitized — this is what
    prevents frontmatter corruption in render_log."""
    transcript = tmp_path / "t.jsonl"
    evil_path = "/tmp/evil.py\nfake_key: not_really_a_yaml_key"
    _write_transcript(transcript, [
        {
            "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": evil_path}}]},
        },
    ])
    facts = ac.extract_transcript_facts(str(transcript))
    assert len(facts["file_paths"]) == 1
    assert "\n" not in facts["file_paths"][0]


# ── render_log: frontmatter stays well-formed under adversarial input ───────
# (code-review round 1 caught two reproduced bugs here: _bucket_tool_name
# returned mcp__-prefixed names verbatim/unsanitized, and an unescaped `"` in
# project_path broke the quoted scalar — plain substring checks missed both.
# The parser below is deliberately minimal/self-contained rather than a real
# YAML library (pyyaml isn't a declared project dependency) — it parses
# exactly render_log's own known, fixed key set as scalar `key: value` lines
# plus the `tldr: |` block literal, which is sufficient to prove no extra
# key was injected and no known key's value was overwritten by injected
# content, without adding a new dependency for a test-only concern.)

_FRONTMATTER_KEYS = {"type", "date", "auto_generated", "topics", "project_path", "source_transcript"}


def _parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    assert lines[0] == "---"
    close_idx = lines[1:].index("---") + 1
    body = lines[1:close_idx]

    result = {}
    i = 0
    while i < len(body):
        line = body[i]
        if line == "tldr: |":
            i += 1
            # tldr's block-literal content is everything indented under it;
            # a genuinely injected line would appear here at column 0
            # instead (proving it broke out), which the caller can assert on.
            block = []
            while i < len(body) and (body[i].startswith("  ") or body[i] == ""):
                block.append(body[i])
                i += 1
            result["tldr"] = "\n".join(block)
            continue
        if ": " in line:
            key, _, value = line.partition(": ")
            # A raw key must be one of the known fixed set -- anything else
            # is exactly the injection this parser exists to catch.
            assert key in _FRONTMATTER_KEYS, f"unexpected/injected key: {key!r}"
            result[key] = value
        i += 1
    return result


def test_render_log_frontmatter_well_formed_with_adversarial_file_path():
    facts = {
        "user_turns": 3, "assistant_turns": 4,
        "tool_counts": {"Bash": 2, "other": 1},
        "file_paths": [ac._sanitize("/tmp/evil.py\nfake_key: x")],
        "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-01-01T00:05:00Z",
    }
    text = ac.render_log("abc12345", "/some/cwd", facts, "deadbeef" * 8, "2026-01-01")
    parsed = _parse_frontmatter(text)
    assert parsed["auto_generated"] == "true"
    assert "fake_key" not in parsed
    assert parsed["source_transcript"] == "deadbeef" * 8


def test_render_log_frontmatter_well_formed_with_adversarial_mcp_tool_name():
    """Reproduces code-review's exact finding: an mcp__-prefixed tool_use
    name containing an embedded newline + fake YAML key must not flip
    auto_generated or inject a new key, once routed through _bucket_tool_name
    (which the real extract_transcript_facts path always does)."""
    evil_name = "mcp__evil__x\nauto_generated: false\nfake_key: injected"
    bucketed = ac._bucket_tool_name(evil_name)
    facts = {
        "user_turns": 1, "assistant_turns": 1,
        "tool_counts": {bucketed: 1},
        "file_paths": [], "first_ts": None, "last_ts": None,
    }
    text = ac.render_log("sid", "/cwd", facts, None, "2026-01-01")
    parsed = _parse_frontmatter(text)
    assert parsed["auto_generated"] == "true"
    assert "fake_key" not in parsed


def test_render_log_frontmatter_well_formed_with_quote_in_cwd():
    """Reproduces code-review's second finding: an embedded `"` in cwd must
    not break the quoted project_path scalar."""
    facts = {"user_turns": 1, "assistant_turns": 1, "tool_counts": {}, "file_paths": [], "first_ts": None, "last_ts": None}
    text = ac.render_log("sid", '/some/"weird/cwd', facts, None, "2026-01-01")
    parsed = _parse_frontmatter(text)
    assert json.loads(parsed["project_path"]) == '/some/"weird/cwd'


def test_bucket_tool_name_sanitizes_mcp_prefixed_names():
    evil = "mcp__x\nfake: y"
    result = ac._bucket_tool_name(evil)
    assert "\n" not in result


def test_render_log_omits_source_transcript_when_none():
    facts = {"user_turns": 1, "assistant_turns": 1, "tool_counts": {}, "file_paths": [], "first_ts": None, "last_ts": None}
    text = ac.render_log("sid", "/cwd", facts, None, "2026-01-01")
    assert "source_transcript:" not in text


# ── run(): end-to-end with stubbed subprocess calls ──────────────────────────

@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("DEUS_VAULT_PATH", str(v))
    return v


@pytest.fixture
def recorded_calls(monkeypatch):
    calls = []

    class _FakeCompleted:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _fake_run(argv, cwd=None, env=None, timeout=None, capture_output=True, text=True):
        calls.append({"argv": argv, "cwd": cwd, "env": env})
        script = Path(argv[1]).name
        if script == "transcript_archive.py":
            return _FakeCompleted(0, json.dumps({"ok": True, "sha256": "cafef00d" * 8}))
        return _FakeCompleted(0, "")

    monkeypatch.setattr(ac.subprocess, "run", _fake_run)
    return calls


def test_run_writes_log_and_calls_children_with_no_extract(fake_vault, recorded_calls, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}])

    rc = ac.run("session123", str(transcript), str(tmp_path))
    assert rc == 0

    session_logs = list((fake_vault / "Session-Logs").glob("*/auto-session1.md"))
    assert len(session_logs) == 1
    content = session_logs[0].read_text()
    assert "source_transcript: cafef00d" in content

    scripts_called = [Path(c["argv"][1]).name for c in recorded_calls]
    assert "transcript_archive.py" in scripts_called
    assert "sync_linear_pending.py" in scripts_called
    assert "memory_indexer.py" in scripts_called

    indexer_call = next(c for c in recorded_calls if Path(c["argv"][1]).name == "memory_indexer.py")
    assert "--no-extract" in indexer_call["argv"]
    assert "--add" in indexer_call["argv"]

    archive_call = next(c for c in recorded_calls if Path(c["argv"][1]).name == "transcript_archive.py")
    assert "--transcript" in archive_call["argv"]
    assert "--cwd" not in archive_call["argv"]
    assert str(transcript) in archive_call["argv"]


def test_run_propagates_resolved_vault_to_every_child_env(fake_vault, recorded_calls, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}])
    ac.run("session123", str(transcript), str(tmp_path))
    assert len(recorded_calls) >= 3
    for call in recorded_calls:
        assert call["env"]["DEUS_VAULT_PATH"] == str(fake_vault)


def test_run_fails_when_no_vault_resolvable(tmp_path, monkeypatch):
    # Stub the resolver itself rather than just unsetting the env var — the
    # real dev machine's global ~/.config/deus/config.json would otherwise
    # still resolve a real vault, defeating the test's intent (the resolver's
    # own tiers are covered separately in test_stop_hook_vault_resolver.py).
    monkeypatch.setattr(ac, "_load_vault_root", lambda cwd=None: None)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    rc = ac.run("sid", str(transcript), str(tmp_path))
    assert rc == 1


def test_run_does_not_delete_checkpoints(fake_vault, recorded_calls, tmp_path):
    """Round-6 fix C: the automated worker must never touch Checkpoints/ at all."""
    checkpoints = fake_vault / "Checkpoints"
    checkpoints.mkdir()
    marker = checkpoints / "2026-01-01-00.md"
    marker.write_text("real in-progress content", encoding="utf-8")

    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}])
    ac.run("sid", str(transcript), str(tmp_path))

    assert marker.exists()
    assert marker.read_text() == "real in-progress content"
