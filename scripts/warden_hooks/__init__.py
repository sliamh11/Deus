"""Warden-hooks capsule package (LIA-306).

Focused, zero-coupling leaf modules extracted from the ~4200-line
``codex_warden_hooks.py`` gate engine. Each capsule is pure (stdlib only, no
shared module state), so the entry module re-imports the symbols and the
runtime + test surface stay byte-identical. The package resolves the same way
``warden_review`` does: ``scripts/`` is on ``sys.path`` for hook invocation
(script dir), ``python3 -m pytest`` from the repo root (CI), and the
spec-loaded test context.

Submodules are imported explicitly (``from warden_hooks.globs import ...``),
mirroring the ``from warden_review.constants import ...`` precedent.
"""
