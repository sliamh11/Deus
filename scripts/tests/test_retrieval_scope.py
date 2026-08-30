"""LIA-138: retrieval scope has one definition, and no caller can omit it.

The defect this guards is the sibling of LIA-126's. That one was a depth
restated in three places; this one was a scope absent from four call sites --
every one of them a MEASUREMENT surface, so the numbers gating this system
described a path no live session takes. Same DB, same 82 queries, migrated
373-node corpus::

    unscoped   0.817  (67/82)
    scoped     0.866  (71/82)

The suite would have called that improvement a regression, and a number that
disagrees with production is worse than no number because it gets acted on.

Two things had to be true for this to hide, and both are guarded below:

1. ``project_scope`` DEFAULTED to ``None``. A default that is wrong for most
   callers cannot be fixed by fixing callers -- so it is now required, and
   :class:`TestScopeIsRequiredAtTheBoundary` drives the real function to prove
   the sentinel raises rather than trusting the signature.

2. The audit that found the four sites was a DIRECT-caller audit, and two more
   callers (``calibrate_sweep``, ``benchmark_ablation``) reach ``retrieve()``
   through ``benchmark()``. An audit of who calls the boundary structurally
   cannot see a caller that arrives via an intermediary -- the same shape as
   "k by omission", where there is no literal to grep for either.
   :class:`TestNoCallerOmitsScope` walks the AST instead, so a NEVER-SEEN new
   site is caught, not merely a regression at a site someone already knew about.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _retrieval_scope as rs  # noqa: E402

# Every function that either IS the retrieval boundary or reaches it. The last
# six are the indirect layer: they take a dataset and score it, so a scope they
# do not carry is a scope their retrieval silently drops.
_SCOPE_REQUIRING = frozenset({
    "retrieve",
    "retrieve_with_policy",
    "calibrate",
    "calibrate_sweep",
    "benchmark",
    "benchmark_ablation",
    "benchmark_tiered",
    "benchmark_loo",
})

# Only these two CHOOSE a scoring depth; the rest forward a `k` they were given.
_DEPTH_CHOOSING = frozenset({"retrieve", "retrieve_with_policy"})


def _non_test_sources() -> list[Path]:
    """Every non-test .py under scripts/, tests and worktrees excluded."""
    out = []
    for p in sorted(_SCRIPTS.rglob("*.py")):
        parts = p.parts
        if "tests" in parts or ".claude" in parts:
            continue
        out.append(p)
    return out


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _bare_names_from_memory_tree(tree: ast.AST) -> set[str]:
    """Names this file pulled in via `from memory_tree import X`.

    Matching on name ALONE over-matches, and the false positive is instructive:
    `code_search.py` has its own unrelated `benchmark()`. Flagging it would be a
    guard that cries wolf, which is how a guard gets switched off. Caught here
    by running this test red before trusting it green -- a guard that has never
    reported a false positive has also never been calibrated.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "memory_tree":
            out.update(a.asname or a.name for a in node.names)
    return out


def _loads_memory_tree(src: str, tree: ast.AST) -> bool:
    """Does this file hold a memory_tree MODULE object to call attributes on?

    Either a real import, or `spec_from_file_location(..., memory_tree.py)` the
    way every bench suite loads it.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "memory_tree" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "memory_tree":
            return True
    # Substring, so a file merely MENTIONING memory_tree.py in a docstring
    # counts (code_search.py does). Harmless today because its same-named
    # `benchmark()` is called bare and is therefore gated by `bare_ok` instead;
    # it would only matter if such a file gained an attribute-style call.
    return "memory_tree.py" in src


def _offending_calls(want: str, names: frozenset[str]) -> list[str]:
    offenders: list[str] = []
    for path in _non_test_sources():
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover - a broken file is a different failure
            continue
        is_mt_itself = path.name == "memory_tree.py"
        bare_ok = _bare_names_from_memory_tree(tree)
        attr_ok = is_mt_itself or _loads_memory_tree(src, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in names:
                continue
            # An ATTRIBUTE call (`mt.retrieve`) needs a memory_tree module in
            # scope; a BARE call needs the name to have come from memory_tree,
            # or to be inside memory_tree itself. Anything else is some other
            # module's function that merely shares a name.
            if isinstance(node.func, ast.Attribute):
                if not attr_ok:
                    continue
            elif not (is_mt_itself or name in bare_ok):
                continue
            # `f(**kw)` — the scope may be inside the dict, which is not
            # decidable statically, so this ASSUMES the caller that built `kw`
            # is itself guarded. There are zero such calls today; if one
            # appears, that assumption needs re-checking rather than trusting.
            if any(kw.arg is None for kw in node.keywords):
                continue
            if any(kw.arg == want for kw in node.keywords):
                continue
            rel = path.relative_to(_SCRIPTS)
            offenders.append(f"{rel}:{node.lineno} {name}() has no explicit {want}=")
    return offenders


class TestScopeIsRequiredAtTheBoundary:
    """Drive the real functions. A signature can be read; a raise must be shown."""

    def test_retrieve_refuses_an_omitted_scope(self):
        import memory_tree as mt

        with pytest.raises(TypeError) as exc:
            mt.retrieve(None, "any query")
        assert "project_scope" in str(exc.value)
        assert "LIA-138" in str(exc.value)

    def test_retrieve_with_policy_refuses_an_omitted_scope(self):
        import memory_tree as mt

        with pytest.raises(TypeError) as exc:
            mt.retrieve_with_policy(None, "any query")
        assert "project_scope" in str(exc.value)

    @pytest.mark.parametrize(
        "func", ["calibrate", "calibrate_sweep", "benchmark",
                 "benchmark_ablation", "benchmark_tiered", "benchmark_loo"],
    )
    def test_every_scoring_function_refuses_an_omitted_scope(self, func):
        """Including the two that reach retrieve() only through benchmark()."""
        import memory_tree as mt

        fn = getattr(mt, func)
        args = [None, []] + ([set()] if func == "benchmark_tiered" else [])
        with pytest.raises(TypeError) as exc:
            fn(*args)
        assert "project_scope" in str(exc.value)

    def test_an_explicit_none_is_accepted_as_a_decision(self):
        """Unscoped must stay REACHABLE. It is what the live hook still runs
        while DEUS_PROJECT_SCOPE is off, so a guard that made it unexpressible
        would remove the only way to measure today's actual behaviour."""
        import memory_tree as mt

        assert mt._require_scope("retrieve", None) is None
        assert mt._require_scope("retrieve", "deus") == "deus"


class TestNoCallerOmitsScope:
    """The guard the LIA-126 depth guard could not be: it names no sites.

    `test_retrieval_depth.py` enumerates three known files. That catches a
    regression at a site someone already found and cannot catch a brand-new
    file that never imported the shared module -- which is exactly what
    `context_sufficiency.py` was. This walks every non-test source instead.
    """

    def test_no_non_test_caller_omits_project_scope(self):
        offenders = _offending_calls("project_scope", _SCOPE_REQUIRING)
        assert not offenders, (
            "these calls reach retrieval without saying which project's memory "
            "they are reading, so whatever they measure is not what the reader "
            "sees (LIA-138):\n  " + "\n  ".join(offenders)
        )

    def test_no_non_test_caller_omits_k(self):
        """LIA-126's fourth site was `mt.retrieve(db, q)` -- wrong by omission,
        so there was no literal to grep for. Only the two boundary functions
        choose a depth; the benchmark family forwards one it was given."""
        offenders = _offending_calls("k", _DEPTH_CHOOSING)
        assert not offenders, (
            "these calls score at DEFAULT_TOP_K instead of the hook's depth "
            "(LIA-126); derive it from scripts/_retrieval_depth:\n  "
            + "\n  ".join(offenders)
        )


class TestBenchScopeHasOneDefinition:
    def test_reports_its_source_so_a_fallback_is_never_mislabelled(self):
        value, source = rs.bench_project_scope_with_source()
        assert value == "deus"
        assert source == "auto_memory_dir.DEUS_PROJECT_ID"

    def test_fallback_is_a_scope_not_none(self):
        """A failed import must not degrade to the unscoped read this module
        exists to remove. None would be silent and would look like a default."""
        assert rs._FALLBACK_SCOPE == "deus"

    def test_drift_check_derives_its_bench_scope_rather_than_restating_it(self):
        src = (_SCRIPTS / "drift_check.py").read_text()
        assert "_retrieval_scope" in src
        assert '_BENCH_SCOPE = "deus"' not in src

    @pytest.mark.parametrize(
        "rel", ["bench/suites/memory_tree.py", "bench/suites/context_sufficiency.py"],
    )
    def test_bench_suites_derive_the_scope(self, rel):
        assert "_retrieval_scope" in (_SCRIPTS / rel).read_text()


class TestHookScopeDerivationHasNotDrifted:
    """A source-equivalence check on the hook's own scope expression.

    WHY THIS SHAPE, deliberately, and not a shared import: the hook computes
    its scope INLINE, and `scripts/memory_retrieval_hook.py` is owned by a
    concurrent work lane, so extracting that expression into
    `_retrieval_scope` was not mine to do. Comparing the parsed expression is
    the drift detector that needs no edit to the hook.

    The follow-up that REPLACES this test is one line in the hook: import the
    derivation from `_retrieval_scope` and delete the inline expression. Until
    then, this failing is the signal that the bench and the hook have parted
    ways -- which is the entire defect LIA-138 is about.
    """

    RECORDED = (
        "amd.resolve_project_id() if os.environ.get('DEUS_PROJECT_SCOPE', '')"
        ".strip() == '1' else None"
    )

    def test_the_hooks_scope_expression_is_the_one_we_recorded(self):
        hook = _SCRIPTS / "memory_retrieval_hook.py"
        tree = ast.parse(hook.read_text())
        found = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "project_scope"
                for t in node.targets
            )
        ]
        assert found, (
            "memory_retrieval_hook.py no longer assigns `project_scope` at all. "
            "Find where the hook now derives its scope, then either update "
            "RECORDED here or extract that derivation into "
            "scripts/_retrieval_scope and import it from both."
        )
        assert found[0] == self.RECORDED, (
            "the hook's project_scope derivation CHANGED, so the benchmarks may "
            "now score a different path than the reader takes -- which is the "
            f"defect LIA-138 fixed.\n  hook now: {found[0]}\n  recorded: "
            f"{self.RECORDED}\nEither update RECORDED to match (if the change is "
            "intended), or better, extract the expression into "
            "scripts/_retrieval_scope and have the hook import it, which "
            "deletes this test."
        )
