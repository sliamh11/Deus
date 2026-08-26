"""The ONE definition of how deep retrieval is scored (LIA-126).

Every benchmark, gate and suite that scores retrieval must use the depth the
UserPromptSubmit hook actually injects at. Restating that number anywhere else
is how it drifted: on 2026-08-23 `--bench-snapshot` reported 95.0% PASS at k=5
while five of eight identity queries had lost top-1, because the hook only ever
delivers `TOP_K = 3`.

Three sites carried the wrong depth, and only one of them was greppable:

* ``scripts/drift_check.py`` -- a literal ``k=5``
* ``scripts/bench/suites/memory_tree.py`` -- k by OMISSION, taking
  ``memory_tree.DEFAULT_TOP_K`` (5). No literal to search for, which is why it
  hid the longest.
* ``scripts/bench/suites/memory.py`` -- ``score_k = 5 if 5 in recall``, which
  computes recall@3 and then discards it.

Keep this module dependency-free so a bench suite can import it without pulling
in sqlite-vec or Ollama.
"""

from __future__ import annotations

from pathlib import Path

# What the hook falls back to if its module cannot be imported. Deliberately the
# hook's CURRENT value and never the old 5, so a broken import cannot silently
# restore the blindness this module exists to remove.
_FALLBACK_TOP_K = 3


def hook_top_k_with_source() -> tuple[int, str]:
    """The hook's injection depth, plus where the number came from.

    Returning the source matters: a fallback reported as if it were read live is
    the same provenance blindness in miniature. Callers that print a depth
    should print this label with it.
    """
    try:
        import sys

        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from memory_retrieval_hook import TOP_K

        return int(TOP_K), "hook TOP_K"
    except Exception as exc:  # noqa: BLE001 - any import failure must be visible
        return _FALLBACK_TOP_K, f"FALLBACK, import failed: {exc}"


def hook_top_k() -> int:
    """The hook's injection depth. Use this instead of any literal."""
    return hook_top_k_with_source()[0]
