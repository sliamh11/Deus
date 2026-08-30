"""The ONE definition of the project scope a benchmark scores under (LIA-138).

Sibling of :mod:`_retrieval_depth`, and it exists for the same reason. That
module fixed a *depth* that lived in three places; this one fixes a *scope* that
was absent from four.

The live hook passes ``project_scope`` on every call. Four non-test callers of
``memory_tree.retrieve()`` did not, and every one of them is a MEASUREMENT
surface, so the numbers that gate this system described a path no session takes.
Measured 2026-08-26 on the migrated 373-node corpus, same DB, same 82 queries::

    unscoped      0.817  (67/82)   <- what the suites shipped
    scoped        0.866  (71/82)   <- the path being migrated to

The suite would have reported a REGRESSION on a change that improved live
retrieval. A number that disagrees with production is worse than no number,
because it gets acted on.

``retrieve()`` now REQUIRES ``project_scope`` (a sentinel default that raises),
so a fifth site cannot take the wrong path by omission the way these four did.
This module is what those measurement sites pass.

Keep this module dependency-free apart from ``auto_memory_dir`` so a bench suite
can import it without pulling in sqlite-vec or Ollama.
"""

from __future__ import annotations

import sys
from pathlib import Path

# What the scope resolves to if `auto_memory_dir` cannot be imported. This repo's
# own project id, deliberately, because that is the corpus every benchmark and
# canary in this repo scores against -- never None, which would silently restore
# the unscoped read this module exists to remove.
_FALLBACK_SCOPE = "deus"


def bench_project_scope_with_source() -> tuple[str, str]:
    """The scope a benchmark scores under, plus where the value came from.

    Returning the source matters for the same reason it does in
    :func:`_retrieval_depth.hook_top_k_with_source`: a fallback reported as if
    it were read live is the provenance blindness in miniature. Callers that
    print a scope should print this label with it.

    This is deliberately NOT ``auto_memory_dir.resolve_project_id()``. That
    function answers "which project is the CURRENT SESSION in", which is the
    right question for the hook and the wrong one for a benchmark: a suite run
    from another repo's session would then score the Deus fixture corpus under
    that repo's scope and every canary would change meaning. The corpus under
    test is fixed, so the scope is too.
    """
    try:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from auto_memory_dir import DEUS_PROJECT_ID

        return str(DEUS_PROJECT_ID), "auto_memory_dir.DEUS_PROJECT_ID"
    except Exception as exc:  # noqa: BLE001 - any import failure must be visible
        return _FALLBACK_SCOPE, f"FALLBACK, import failed: {exc}"


def bench_project_scope() -> str:
    """The scope a benchmark scores under. Use this instead of any literal."""
    return bench_project_scope_with_source()[0]
