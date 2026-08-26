"""LIA-126: one definition of retrieval-scoring depth, and a guard that keeps it.

The original defect was not that a number was wrong. It was that the number
existed in three places and only one of them was greppable, so two of the three
survived a fix aimed at all of them:

* ``scripts/drift_check.py`` -- a literal ``k=5``
* ``scripts/bench/suites/memory_tree.py`` -- k by OMISSION, silently taking
  ``memory_tree.DEFAULT_TOP_K`` (5)
* ``scripts/bench/suites/memory.py`` -- ``score_k = 5 if 5 in recall``

The last two were found by a second reviewer after the first fix was already
gated and about to ship. These tests exist so a fourth site cannot appear
quietly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _retrieval_depth as rd  # noqa: E402


def _code_only(path: Path) -> str:
    """The file with comments and string literals blanked, line numbers kept.

    A prose explanation of the defect must not trip the guard that prevents it.
    """
    import io
    import tokenize

    src = path.read_text()
    out = src.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            line = out[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            out[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "\n".join(out)


class TestHookTopK:
    def test_reads_the_hooks_own_constant(self):
        from memory_retrieval_hook import TOP_K

        assert rd.hook_top_k() == TOP_K

    def test_reports_its_source_so_a_fallback_is_never_mislabelled(self):
        value, source = rd.hook_top_k_with_source()
        assert isinstance(value, int)
        assert source == "hook TOP_K"

    def test_fallback_is_the_hooks_current_value_not_the_old_five(self, monkeypatch):
        """A broken import must not silently restore the blindness. Falling back
        to 5 would re-create the exact defect while looking like a safe default."""
        assert rd._FALLBACK_TOP_K == 3

        def _boom(*_a, **_kw):
            raise ImportError("simulated")

        monkeypatch.setitem(sys.modules, "memory_retrieval_hook", None)
        monkeypatch.setattr(rd, "hook_top_k_with_source", rd.hook_top_k_with_source)
        # Drive the real function with the import poisoned.
        sys.modules.pop("memory_retrieval_hook", None)
        monkeypatch.setattr(
            "builtins.__import__",
            lambda name, *a, **kw: _boom() if name == "memory_retrieval_hook" else __import__(name, *a, **kw),
        )
        value, source = rd.hook_top_k_with_source()
        assert value == 3
        assert "FALLBACK" in source


class TestNoSiteRestatesTheDepth:
    """The regression guard the original fix lacked.

    Greps the scoring sites for a re-stated depth. A new suite that scores
    retrieval must import from ``_retrieval_depth``, not write its own number
    and not rely on ``DEFAULT_TOP_K`` by omission.
    """

    SCORING_SITES = (
        "drift_check.py",
        "bench/suites/memory_tree.py",
        "bench/suites/memory.py",
    )

    @pytest.mark.parametrize("rel", SCORING_SITES)
    def test_site_imports_the_shared_depth(self, rel):
        src = (_SCRIPTS / rel).read_text()
        assert "_retrieval_depth" in src, (
            f"{rel} scores retrieval but does not derive its depth from "
            "scripts/_retrieval_depth. Restating the number is the defect."
        )

    @pytest.mark.parametrize("rel", SCORING_SITES)
    def test_site_has_no_live_k_equals_five(self, rel):
        """Literal `k=5` in executable code.

        Strips comments AND string literals before matching -- a docstring
        explaining why k=5 was wrong must not trip the guard that enforces it.
        """
        offenders = [
            (n, line)
            for n, line in enumerate(_code_only(_SCRIPTS / rel).splitlines(), 1)
            if re.search(r"\bk\s*=\s*5\b", line)
        ]
        assert not offenders, f"{rel} still hardcodes k=5: {offenders}"

    def test_bench_suite_passes_k_explicitly(self):
        """The site that hid longest: `mt.retrieve(db, q)` with k omitted takes
        DEFAULT_TOP_K, so there is no literal for a grep to find."""
        src = _code_only(_SCRIPTS / "bench/suites/memory_tree.py")
        assert re.search(r"mt\.retrieve\(\s*db,\s*q\s*\)", src) is None, (
            "bench/suites/memory_tree.py calls mt.retrieve without an explicit k, "
            "which silently scores at DEFAULT_TOP_K instead of the hook's depth"
        )
        assert "hook_top_k()" in (_SCRIPTS / "bench/suites/memory_tree.py").read_text()

    def test_memory_suite_does_not_fall_back_to_the_highest_k(self):
        """`max(recall)` reintroduces the optimistic bias when the hook's depth
        is not among the requested ks. The safe fallback is the lowest."""
        body = _code_only(_SCRIPTS / "bench/suites/memory.py")
        assert "max(recall)" not in body
        assert "min(recall)" in body
