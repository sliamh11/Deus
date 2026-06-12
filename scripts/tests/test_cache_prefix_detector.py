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


# --- diff mode (the authoritative reuse-ceiling signal) ---------------------


def test_diff_identical_fully_reusable():
    rep = cpd.diff_prefixes("same prefix text", "same prefix text")
    assert rep["prefixes_identical"] is True
    assert rep["reuse_ceiling"] is None
    assert rep["reuse_ceiling_fraction"] == 1.0
    assert "divergence" not in rep


def test_diff_first_divergence_offset():
    rep = cpd.diff_prefixes("abcDEF", "abcXEF")
    assert rep["prefixes_identical"] is False
    assert rep["reuse_ceiling"] == 3
    assert rep["divergence"]["offset"] == 3


def test_diff_one_is_prefix_of_other():
    rep = cpd.diff_prefixes("abc", "abcdef")
    assert rep["prefixes_identical"] is False
    assert rep["one_is_prefix_of_other"] is True
    assert rep["reuse_ceiling"] == 3
    assert rep["reuse_ceiling_fraction"] == round(3 / 6, 4)


def test_diff_shaped_kind_at_divergence():
    # The date's last digit changes -> the divergence is INSIDE the iso8601
    # token's span (mid-token), so it must still be attributed to "iso8601".
    rep = cpd.diff_prefixes("date: 2026-06-11 end", "date: 2026-06-12 end")
    assert rep["divergence"]["shaped"] is True
    assert rep["divergence"]["kind"] == "iso8601"
    assert rep["reuse_ceiling"] == rep["divergence"]["offset"]


def test_diff_unstructured_divergence():
    # A bare digit count changing is not a shaped token -> kind None.
    rep = cpd.diff_prefixes("x: 3 sessions", "x: 4 sessions")
    assert rep["divergence"]["shaped"] is False
    assert rep["divergence"]["kind"] is None
    assert rep["shaped_false_alarms"] == 0


def test_diff_shaped_false_alarms_counted():
    # The LOAD-BEARING test: a stable UUID identical in both prefixes sits
    # before the real (date) divergence. Shaped, but reusable -> a false alarm
    # the single-prefix heuristic would wrongly flag. The diff proves the actual
    # churn is the downstream date, not the UUID.
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    rep = cpd.diff_prefixes(f"id {uuid} d 2026-06-11", f"id {uuid} d 2026-06-12")
    assert rep["shaped_false_alarms"] >= 1
    assert rep["divergence"]["kind"] == "iso8601"  # the date is the real cause
    assert rep["reuse_ceiling"] > 3 + len(uuid)  # past the stable UUID


def test_diff_both_empty():
    rep = cpd.diff_prefixes("", "")
    assert rep["prefixes_identical"] is True
    assert rep["reuse_ceiling"] is None
    # b_len==0 guard wins over the identical->1.0 rule.
    assert rep["reuse_ceiling_fraction"] == 0.0


def test_diff_b_empty_a_nonempty():
    rep = cpd.diff_prefixes("abc", "")
    assert rep["prefixes_identical"] is False
    assert rep["one_is_prefix_of_other"] is True
    assert rep["reuse_ceiling"] == 0
    assert rep["reuse_ceiling_fraction"] == 0.0


def test_diff_a_empty_b_nonempty():
    # Mirror of the above: empty FILE_A, non-empty FILE_B. b_len>0 so the
    # fraction is the natural 0/3, not the b_len==0 guard.
    rep = cpd.diff_prefixes("", "abc")
    assert rep["prefixes_identical"] is False
    assert rep["one_is_prefix_of_other"] is True
    assert rep["reuse_ceiling"] == 0
    assert rep["reuse_ceiling_fraction"] == 0.0


def test_diff_window_escapes_newline():
    # A newline inside the context window must be escaped so it cannot corrupt
    # the single-line human output.
    rep = cpd.diff_prefixes("x\nK: 2026-06-11", "x\nK: 2026-06-12")
    ctx = rep["divergence"]["context_before"]
    assert "\n" not in ctx
    assert "\\n" in ctx


# --- diff mode: agent-native CLI --------------------------------------------


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_diff_cli_json(tmp_path, capsys):
    a = _write(tmp_path, "a.txt", "date 2026-06-11")
    b = _write(tmp_path, "b.txt", "date 2026-06-12")
    rc = cpd.main(["--diff", a, b, "--json"])
    assert rc == cpd.SUCCESS
    obj = json.loads(capsys.readouterr().out)
    expected = {
        "prefixes_identical",
        "a_len",
        "b_len",
        "reuse_ceiling",
        "reuse_ceiling_fraction",
        "one_is_prefix_of_other",
        "shaped_false_alarms",
    }
    assert expected <= set(obj)
    assert obj["prefixes_identical"] is False


def test_diff_cli_missing_file_not_found(tmp_path):
    a = _write(tmp_path, "a.txt", "present")
    rc = cpd.main(["--diff", a, str(tmp_path / "nope.txt")])
    assert rc == cpd.NOT_FOUND


def test_diff_cli_conflicts_with_stdin(tmp_path):
    a = _write(tmp_path, "a.txt", "x")
    b = _write(tmp_path, "b.txt", "y")
    # --diff + --stdin -> exactly-one-mode guard rejects before any file read.
    assert cpd.main(["--diff", a, b, "--stdin"]) == cpd.USAGE_ERROR


def test_diff_cli_conflicts_with_positional(tmp_path):
    a = _write(tmp_path, "a.txt", "x")
    b = _write(tmp_path, "b.txt", "y")
    f = _write(tmp_path, "f.txt", "z")
    assert cpd.main([f, "--diff", a, b]) == cpd.USAGE_ERROR


def test_diff_cli_human(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DEUS_AGENT_NATIVE", raising=False)
    a = _write(tmp_path, "a.txt", "date 2026-06-11")
    b = _write(tmp_path, "b.txt", "date 2026-06-12")
    rc = cpd.main(["--diff", a, b])  # no --json -> human branch
    assert rc == cpd.SUCCESS
    assert "reuse ceiling" in capsys.readouterr().out


def test_diff_cli_compact_keeps_identical_bool(tmp_path, capsys):
    a = _write(tmp_path, "a.txt", "identical prefix")
    rc = cpd.main(["--diff", a, a, "--json", "--compact"])
    assert rc == cpd.SUCCESS
    obj = json.loads(capsys.readouterr().out)
    # compact_json strips the None reuse_ceiling; the authoritative bool survives
    # and the divergence block is absent (never emitted when identical).
    assert obj["prefixes_identical"] is True
    assert "reuse_ceiling" not in obj
    assert "divergence" not in obj
