"""Tests for scripts/cross_family_review.py — the pure, network-free logic.

No llama-server is contacted: these cover the verdict classifier, diff splitting,
the size-guard line counter, language detection, endpoint/model resolution, and the
two typed-exit-code paths in main() that short-circuit before any model call
(missing --diff-file → NOT_FOUND; empty diff → ABSTAIN).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is importable.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import cross_family_review as cfr
from _exit_codes import ABSTAIN, INTERNAL_ERROR, NOT_FOUND


# ── is_flagged: clean iff the LAST non-empty line contains the sentinel ──────────

def test_flagged_clean_exact_sentinel():
    assert cfr.is_flagged("NO ISSUES FOUND") is False


def test_flagged_clean_with_markdown_and_punctuation():
    assert cfr.is_flagged("**NO ISSUES FOUND.**") is False


def test_flagged_clean_after_deliberation_then_verdict():
    # reasoning-off output deliberates in the body, then concludes with the sentinel.
    review = (
        "Let me check the added lines.\n"
        "The guard handles the None case and the loop bound is correct.\n"
        "NO ISSUES FOUND\n"
    )
    assert cfr.is_flagged(review) is False


def test_flagged_when_issue_reported():
    assert cfr.is_flagged("MAJOR: null deref at foo.py:12 — config may be None.") is True


def test_flagged_embedded_sentinel_midtext_but_final_line_is_a_flag():
    # The refinement over a full-text substring scan: a mid-text mention of the
    # sentinel must NOT mask a real flag on the final line.
    review = (
        "I first thought this was NO ISSUES FOUND, but on closer reading:\n"
        "MAJOR: off-by-one at gate.ts:42 truncates the last element.\n"
    )
    assert cfr.is_flagged(review) is True


def test_flagged_empty_is_defensively_flagged():
    # Empty content is handled as an error upstream; the classifier must never
    # call it "clean".
    assert cfr.is_flagged("") is True
    assert cfr.is_flagged("   \n  \n") is True


# ── split_by_file ───────────────────────────────────────────────────────────────

_TWO_FILE_DIFF = """diff --git a/scripts/foo.py b/scripts/foo.py
index 111..222 100644
--- a/scripts/foo.py
+++ b/scripts/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
diff --git a/src/bar.ts b/src/bar.ts
index 333..444 100644
--- a/src/bar.ts
+++ b/src/bar.ts
@@ -1 +1,2 @@
 const a = 1;
+const b = 2;
"""


def test_split_by_file_two_files():
    pairs = cfr.split_by_file(_TWO_FILE_DIFF)
    assert [p for p, _ in pairs] == ["scripts/foo.py", "src/bar.ts"]
    assert pairs[0][1].startswith("diff --git a/scripts/foo.py")
    assert "const b = 2;" in pairs[1][1]


def test_split_by_file_empty():
    assert cfr.split_by_file("") == []
    assert cfr.split_by_file("   \n") == []


def test_split_by_file_skips_git_show_commit_preamble():
    # `git show` prepends a commit header before the first `diff --git`. That
    # preamble must NOT become a phantom <unknown> "file" sent to the reviewer.
    git_show = (
        "commit deadbeef1234\n"
        "Author: Someone <s@example.com>\n"
        "Date:   Mon Jun 9 12:00:00 2026 +0000\n"
        "\n"
        "    feat: add a thing\n"
        "\n"
        + _TWO_FILE_DIFF
    )
    pairs = cfr.split_by_file(git_show)
    assert [p for p, _ in pairs] == ["scripts/foo.py", "src/bar.ts"]
    assert all(p != "<unknown>" for p, _ in pairs)


# ── added_code_lines (size-guard counter) ───────────────────────────────────────

def test_added_code_lines_counts_only_real_added_code():
    chunk = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"          # must NOT count (starts with +++)
        "@@ -1 +1,5 @@\n"
        " context_line\n"        # context, not added
        "-removed_line\n"        # removed, not added
        "+real = 1\n"            # +1
        "+    another = 2\n"     # +1
        "+# a comment\n"         # comment-only, excluded
        "+\n"                    # blank, excluded
        "+x = 3  # trailing\n"   # +1 (code with trailing comment counts)
    )
    assert cfr.added_code_lines(chunk) == 3


# ── lang_of ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,lang", [
    ("scripts/foo.py", "Python"),
    ("src/bar.ts", "TypeScript"),
    ("src/widget.tsx", "TypeScript"),
    ("web/app.js", "JavaScript"),
    ("README.md", "code"),
    ("Makefile", "code"),
])
def test_lang_of(path, lang):
    assert cfr.lang_of(path) == lang


# ── endpoint / model resolution + footgun precedence ────────────────────────────

_ENV_VARS = (
    "LLAMA_CPP_REVIEW_BASE_URL", "LLAMA_CPP_BASE_URL", "LLAMA_CPP_PORT",
    "LLAMA_CPP_REVIEW_MODEL", "LLAMA_CPP_MODEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


def test_resolve_base_url_prefers_review_specific(clean_env):
    clean_env.setenv("LLAMA_CPP_REVIEW_BASE_URL", "http://127.0.0.1:8099/v1/")
    clean_env.setenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080/v1")
    assert cfr.resolve_base_url() == "http://127.0.0.1:8099/v1"  # trailing slash stripped


def test_resolve_base_url_falls_back_to_shared(clean_env):
    clean_env.setenv("LLAMA_CPP_BASE_URL", "http://host:9000/v1")
    assert cfr.resolve_base_url() == "http://host:9000/v1"


def test_resolve_base_url_default_is_8080(clean_env):
    # The footgun default — nothing configured.
    assert cfr.resolve_base_url() == "http://127.0.0.1:8080/v1"


def test_resolve_model_precedence(clean_env):
    clean_env.setenv("LLAMA_CPP_MODEL", "catch-all")
    assert cfr.resolve_model() == "catch-all"
    clean_env.setenv("LLAMA_CPP_REVIEW_MODEL", "reviewer-12b")
    assert cfr.resolve_model() == "reviewer-12b"
    assert cfr.resolve_model() != "catch-all"


def test_resolve_model_empty_when_unset(clean_env):
    assert cfr.resolve_model() == ""


def test_review_endpoint_explicit(clean_env):
    assert cfr.review_endpoint_explicit(None) is False
    assert cfr.review_endpoint_explicit("http://x/v1") is True  # cli override counts
    clean_env.setenv("LLAMA_CPP_REVIEW_BASE_URL", "http://127.0.0.1:8099/v1")
    assert cfr.review_endpoint_explicit(None) is True           # env override counts


# ── main() typed exit codes on the pre-network short-circuit paths ───────────────

def test_main_missing_diff_file_returns_not_found(capsys):
    rc = cfr.main(["--diff-file", "/nonexistent/path/to.diff"])
    assert rc == NOT_FOUND
    assert "not found" in capsys.readouterr().err.lower()


def test_main_empty_diff_file_returns_abstain(tmp_path, capsys):
    empty = tmp_path / "empty.diff"
    empty.write_text("")
    rc = cfr.main(["--diff-file", str(empty)])
    assert rc == ABSTAIN
    assert "nothing to review" in capsys.readouterr().err.lower()


# ── review() orchestrator (call_model mocked — no server) ────────────────────────

_ONE_FILE_DIFF = """diff --git a/scripts/foo.py b/scripts/foo.py
index 111..222 100644
--- a/scripts/foo.py
+++ b/scripts/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
"""


def _big_diff(path: str = "src/big.py", n: int = 70) -> str:
    header = (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n"
              f"--- a/{path}\n+++ b/{path}\n@@ -1 +1,{n} @@\n")
    body = "".join(f"+line_{i} = {i}\n" for i in range(n))
    return header + body


def _cr(content: str, finish: str = "stop") -> "cfr.CallResult":
    return cfr.CallResult(True, content=content, finish_reason=finish)


def _cfg(**kw) -> "cfr.ReviewConfig":
    return cfr.ReviewConfig(base_url="http://x/v1", model="m", **kw)


def test_review_classifies_each_file():
    with patch.object(cfr, "call_model",
                      side_effect=[_cr("NO ISSUES FOUND"), _cr("MAJOR: bug at bar.ts:1")]):
        result = cfr.review(_TWO_FILE_DIFF, _cfg())
    meta = result["meta"]
    assert meta["files_reviewed"] == 2
    assert meta["files_flagged"] == 1
    by_file = {r["file"]: r for r in result["results"]}
    assert by_file["scripts/foo.py"]["flagged"] is False
    assert by_file["src/bar.ts"]["flagged"] is True


def test_review_skip_large_does_not_call_model():
    with patch.object(cfr, "call_model") as mock_call:
        result = cfr.review(_big_diff("src/big.py", 70), _cfg(max_diff_loc=60, skip_large=True))
    mock_call.assert_not_called()
    r = result["results"][0]
    assert r["skipped"] is True and r["flagged"] is None and r["added_loc"] == 70
    assert result["meta"]["files_reviewed"] == 0
    assert result["meta"]["files_skipped_large"] == [{"file": "src/big.py", "added_loc": 70}]


def test_review_oversize_tagged_when_not_skipping():
    with patch.object(cfr, "call_model", side_effect=[_cr("NO ISSUES FOUND")]):
        result = cfr.review(_big_diff("src/big.py", 70), _cfg(max_diff_loc=60, skip_large=False))
    r = result["results"][0]
    assert r["oversize"] is True and r["skipped"] is False
    assert r["flagged"] is False


def test_review_empty_content_fails_loud():
    with patch.object(cfr, "call_model", side_effect=[_cr("")]):
        with pytest.raises(cfr.ReviewError) as ei:
            cfr.review(_ONE_FILE_DIFF, _cfg())
    assert ei.value.code == INTERNAL_ERROR


def test_review_connection_error_fails_loud():
    with patch.object(cfr, "call_model",
                      side_effect=[cfr.CallResult(False, error="connection error to x")]):
        with pytest.raises(cfr.ReviewError) as ei:
            cfr.review(_ONE_FILE_DIFF, _cfg())
    assert ei.value.code == INTERNAL_ERROR


def test_review_marks_output_truncated_on_length_finish():
    with patch.object(cfr, "call_model", side_effect=[_cr("MAJOR: partial...", finish="length")]):
        result = cfr.review(_ONE_FILE_DIFF, _cfg())
    assert result["results"][0]["output_truncated"] is True
