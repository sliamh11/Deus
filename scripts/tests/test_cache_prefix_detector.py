"""Tests for the cache-prefix-stability detector (LIA-205).

Covers the four structural parsers (UUID / ISO-8601 / JWT / hex), their
no-false-positive boundaries, the `analyze_prefix` aggregate metric, the
agent-native CLI (`--json` / `--compact` / `--stdin`, typed exit codes), and
determinism.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, rel: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cpd = _load("cache_prefix_detector", "cache_prefix_detector.py")


def _kinds(text: str):
    return [t.kind for t in cpd.detect_volatile_tokens(text)]


# --- positive detection -----------------------------------------------------


def test_detects_canonical_uuid():
    toks = cpd.detect_volatile_tokens("ref 550e8400-e29b-41d4-a716-446655440000 end")
    assert [t.kind for t in toks] == ["uuid"]
    assert toks[0].offset == 4


def test_detects_iso_date():
    toks = cpd.detect_volatile_tokens("updated: 2026-04-20")
    assert [t.kind for t in toks] == ["iso8601"]
    assert toks[0].sample == "2026-04-20"


def test_detects_iso_datetime_anchors_on_date():
    toks = cpd.detect_volatile_tokens("ts 2026-06-12T00:55:00 z")
    assert toks[0].kind == "iso8601"
    # the time portion is irrelevant -- the date head is the volatile offset
    assert toks[0].sample == "2026-06-12"
    assert toks[0].offset == 3


def test_detects_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.abc123_-XYZ"
    assert _kinds(f"auth {jwt} ok") == ["jwt"]


def test_detects_hex_hashes():
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    for h in (md5, sha1, sha256):
        assert _kinds(f"hash {h} x") == ["hex"], h


# --- no false positives -----------------------------------------------------


def test_semver_is_not_jwt():
    assert cpd.detect_volatile_tokens("version 1.2.3 released") == []


def test_short_hex_words_not_flagged():
    # 8 and 4 hex chars -- neither is a 32/40/64 hash length
    assert cpd.detect_volatile_tokens("the deadbeef cafe") == []


def test_dashless_md5_is_hex_not_uuid():
    # 32-char dashless form collides with MD5 -> ceded to the hex detector
    assert _kinds("x 550e8400e29b41d4a716446655440000 y") == ["hex"]


def test_36_char_non_uuid_not_flagged():
    assert cpd.detect_volatile_tokens("zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz") == []


def test_impossible_date_not_flagged():
    assert cpd.detect_volatile_tokens("bogus 9999-99-99 date") == []


def test_prose_date_words_not_flagged():
    assert cpd.detect_volatile_tokens("on June 11 we met") == []


# --- aggregate metric -------------------------------------------------------


def test_analyze_offset_and_fraction():
    text = "x" * 10 + "550e8400-e29b-41d4-a716-446655440000"
    rep = cpd.analyze_prefix(text)
    assert rep["prefix_stable"] is False
    assert rep["first_volatile_offset"] == 10
    assert rep["total_len"] == 46
    assert rep["volatile_tail_fraction"] == round(36 / 46, 4)
    assert rep["tokens"][0]["kind"] == "uuid"


def test_analyze_clean_is_stable():
    rep = cpd.analyze_prefix("no volatile tokens here at all")
    assert rep["prefix_stable"] is True
    assert rep["first_volatile_offset"] is None
    assert rep["volatile_tail_fraction"] == 0.0
    assert rep["tokens"] == []


# --- agent-native CLI -------------------------------------------------------


def test_cli_json_clean_compact_keeps_prefix_stable(tmp_path, capsys):
    f = tmp_path / "clean.txt"
    f.write_text("no volatile tokens here at all", encoding="utf-8")
    rc = cpd.main([str(f), "--json", "--compact"])
    assert rc == cpd.SUCCESS
    obj = json.loads(capsys.readouterr().out)
    # compact_json strips the None offset; the authoritative bool must survive
    assert obj["prefix_stable"] is True
    assert "first_volatile_offset" not in obj


def test_cli_json_dirty_has_expected_keys(tmp_path, capsys):
    f = tmp_path / "dirty.txt"
    f.write_text("id: 550e8400-e29b-41d4-a716-446655440000", encoding="utf-8")
    rc = cpd.main([str(f), "--json"])
    assert rc == cpd.SUCCESS
    obj = json.loads(capsys.readouterr().out)
    assert obj["prefix_stable"] is False
    expected = {
        "prefix_stable",
        "total_len",
        "first_volatile_offset",
        "volatile_tail_fraction",
        "tokens",
    }
    assert expected <= set(obj)
    assert obj["tokens"][0]["kind"] == "uuid"


def test_cli_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("clean text only"))
    rc = cpd.main(["--stdin", "--json"])
    assert rc == cpd.SUCCESS
    assert json.loads(capsys.readouterr().out)["prefix_stable"] is True


def test_cli_missing_file_returns_not_found(tmp_path):
    assert cpd.main([str(tmp_path / "nope.txt")]) == cpd.NOT_FOUND


def test_cli_no_input_returns_usage_error(capsys):
    assert cpd.main([]) == cpd.USAGE_ERROR


def test_cli_human_output(tmp_path, capsys):
    f = tmp_path / "d.txt"
    f.write_text("id 550e8400-e29b-41d4-a716-446655440000", encoding="utf-8")
    rc = cpd.main([str(f)])  # no --json -> human-readable branch
    assert rc == cpd.SUCCESS
    out = capsys.readouterr().out
    assert "reuse ceiling" in out
    assert "uuid" in out


def test_cli_file_and_stdin_conflict_is_usage_error(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi", encoding="utf-8")
    assert cpd.main([str(f), "--stdin"]) == cpd.USAGE_ERROR


# --- determinism ------------------------------------------------------------


def test_determinism():
    text = "a 2026-04-20 b 550e8400-e29b-41d4-a716-446655440000 c"
    assert cpd.detect_volatile_tokens(text) == cpd.detect_volatile_tokens(text)


def test_mixed_input_sorted_by_offset():
    # Three different token kinds at increasing offsets -> the merge+sort must
    # return them strictly ascending by offset (exercises the (offset, kind,
    # length) sort key and overlap suppression across parsers).
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.sig_-Xy"
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    text = f"{jwt} then 2026-04-20 then {md5}"
    toks = cpd.detect_volatile_tokens(text)
    seq = [(t.offset, t.kind) for t in toks]
    assert seq == sorted(seq)  # strictly ascending by offset
    assert [t.kind for t in toks] == ["jwt", "iso8601", "hex"]
