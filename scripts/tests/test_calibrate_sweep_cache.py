"""Regression test for the calibrate-sweep embed-cache identity trap (#767).

`calibrate_sweep` pre-embeds each distinct query once, then patches the module's
`embed_text` with a cache-backed wrapper so the 1,440-combo grid reuses those
vectors instead of re-embedding live per combo. The bug: it patched
`import memory_tree as _self`. Under CLI invocation the script runs as
`__main__`, so that import bound a SECOND module object and the patch landed
where `benchmark()` / `retrieve()` never resolve `embed_text`. The cache was
silently bypassed and every combo re-embedded (a ~92s sweep became 7h / ~107k
live Ollama calls).

The bug is invisible to a naive in-process test: when the executing module IS
named "memory_tree", `import memory_tree as _self` happens to return the same
object, so the patch works. To reproduce the CLI split we load a SECOND copy
under a different name — inside it `import memory_tree` resolves to the conftest
copy, exactly the __main__-vs-memory_tree divergence. The fix
(`_self = sys.modules[__name__]`) patches the executing module under either name.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_MT_PATH = _ROOT / "scripts" / "memory_tree.py"


def _embed(text: str, dim: int) -> list[float]:
    """Deterministic, Ollama-free bag-of-words embedder (token overlap → cosine)."""
    vec = [0.0] * dim
    for tok in set(re.findall(r"\w+", text.lower())):
        if len(tok) < 2:
            continue
        h = hashlib.sha256(tok.encode()).digest()
        for i in range(8):
            vec[int.from_bytes(h[i * 2 : i * 2 + 2], "big") % dim] += 1.0
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def _load_cli_copy(name: str):
    """Load a fresh memory_tree under a NON-'memory_tree' name and leave it in
    sys.modules. Reproduces CLI `__main__` identity (executing name != the name
    `import memory_tree` resolves to) and keeps the module registered so the fix's
    `sys.modules[__name__]` lookup succeeds during the sweep."""
    spec = importlib.util.spec_from_file_location(name, _MT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_vault(root: Path) -> Path:
    (root / "Persona").mkdir(parents=True)
    (root / "MEMORY_TREE.md").write_text(
        "---\nid: root000000000000000000000000000001\n"
        "type: memory-tree-root\ntitle: Root\n"
        "description: Root map routing personal-fact queries to persona nodes.\n"
        "level: 0\nchildren:\n  - Persona/household.md\n  - Persona/movies.md\n---\n",
        encoding="utf-8",
    )
    (root / "Persona" / "household.md").write_text(
        "---\nid: household000000000000000000000003\n"
        "type: persona-node\ntitle: Household\n"
        "description: Who Liam lives with at home, his roommates and family.\n"
        "level: 1\n---\n",
        encoding="utf-8",
    )
    (root / "Persona" / "movies.md").write_text(
        "---\nid: movies000000000000000000000000004\n"
        "type: persona-node\ntitle: Movies\n"
        "description: Liam's film taste in stylish crime and director-driven cinema.\n"
        "level: 1\n---\n",
        encoding="utf-8",
    )
    return root


def test_calibrate_sweep_reuses_query_cache_under_cli_identity(tmp_path, monkeypatch):
    # Approach angles off → query-time retrieval embeds only the query itself, so
    # the embed-call count during the sweep is exactly the warm-up count when the
    # cache is honored.
    monkeypatch.setenv("DEUS_APPROACH_ANGLES", "0")

    name = "memory_tree_cli_767"
    cli = _load_cli_copy(name)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tree.db", raising=False)
    monkeypatch.setattr(cli, "_LOG_PATH", tmp_path / "q.jsonl", raising=False)
    monkeypatch.setattr(cli, "_AUDIT_PATH", tmp_path / "a.jsonl", raising=False)

    calls = {"n": 0}

    def counting(text):
        calls["n"] += 1
        return _embed(text, cli.EMBED_DIM)

    monkeypatch.setattr(cli, "embed_text", counting)
    monkeypatch.setattr(
        cli, "embed_batch_text", lambda texts: [counting(t) for t in texts], raising=False
    )

    vault = _write_vault(tmp_path / "vault")
    db = cli.open_db(tmp_path / "tree.db")
    try:
        cli.build_tree(vault, db)

        # Shrink the grid to keep the test fast: one value per float param →
        # entity_overlap [1,2,3] still yields >1 combo so a cache miss would scale.
        monkeypatch.setattr(cli, "_frange", lambda start, stop, step: iter([round(start, 2)]))

        dataset = [
            {"query": "who does Liam live with", "expected_path": "Persona/household.md"},
            {"query": "what films does Liam like", "expected_path": "Persona/movies.md"},
        ]
        distinct = len({d["query"] for d in dataset})

        calls["n"] = 0  # count only sweep-time embeds (build embeds already warmed)
        result = cli.calibrate_sweep(db, dataset)
    finally:
        db.close()
        sys.modules.pop(name, None)

    # The grid actually executed (>1 combo)…
    assert result["total_combos"] >= 3
    # …yet each distinct query was embedded exactly once (the warm-up). Pre-fix
    # this was distinct + total_combos * len(dataset) live embeds because the
    # cache patch missed the executing module.
    assert calls["n"] == distinct
