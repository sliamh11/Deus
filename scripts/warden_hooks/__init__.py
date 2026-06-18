"""Warden-hooks capsule package (LIA-306).

Focused capsules extracted from the ~4200-line ``codex_warden_hooks.py`` gate
engine. Two kinds: pure leaves (stdlib only, no shared module state — ``globs``,
``command_parse``, ``ci_status``) and injection-seam modules that resolve a few
monkeypatched entry-module helpers through a ``bind_entry`` reference at call
time (``verdict_store``). Either way the entry module re-imports the symbols so
the runtime + test surface stay byte-identical. The package resolves the same way
``warden_review`` does: ``scripts/`` is on ``sys.path`` for hook invocation
(script dir), ``python3 -m pytest`` from the repo root (CI), and the
spec-loaded test context.

Submodules are imported explicitly (``from warden_hooks.globs import ...``),
mirroring the ``from warden_review.constants import ...`` precedent.
"""
