"""Tests for memory_tree `scaffold-root` recovery (#768).

When MEMORY_TREE.md (the navigation root) is lost, the graph loses its
reachability anchor. `scaffold_root` regenerates a starter root that adopts every
parentless surviving node, so a follow-up `build_tree` restores reachability.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

if "memory_tree" in sys.modules:
    mt = sys.modules["memory_tree"]
else:
    _spec = importlib.util.spec_from_file_location(
        "memory_tree", _ROOT / "scripts" / "memory_tree.py"
    )
    mt = importlib.util.module_from_spec(_spec)
    sys.modules["memory_tree"] = mt
    _spec.loader.exec_module(mt)


def _embed(text: str) -> list[float]:
    """Deterministic, Ollama-free bag-of-words embedder."""
    vec = [0.0] * mt.EMBED_DIM
    for tok in set(re.findall(r"\w+", text.lower())):
        if len(tok) < 2:
            continue
        h = hashlib.sha256(tok.encode()).digest()
        for i in range(8):
            vec[int.from_bytes(h[i * 2 : i * 2 + 2], "big") % mt.EMBED_DIM] += 1.0
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


@pytest.fixture
def stub_embed(monkeypatch):
    monkeypatch.setattr(mt, "embed_text", _embed)
    monkeypatch.setattr(
        mt, "embed_batch_text", lambda texts: [_embed(t) for t in texts], raising=False
    )


@pytest.fixture
def rootless_vault(tmp_path):
    """A small forest (INDEX→household, plus STUDY) with NO MEMORY_TREE.md."""
    (tmp_path / "Persona").mkdir(parents=True)
    (tmp_path / "Persona" / "INDEX.md").write_text(
        "---\nid: idx0000000000000000000000000001\ntype: persona-index\n"
        "title: Persona\ndescription: Index of Liam personal facts and nodes.\n"
        "level: 1\nchildren:\n  - Persona/household.md\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "Persona" / "household.md").write_text(
        "---\nid: hh00000000000000000000000000002\ntype: persona-node\n"
        "title: Household\ndescription: Who Liam lives with at home.\nlevel: 2\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "STUDY.md").write_text(
        "---\nid: study00000000000000000000000003\ntype: project-node\n"
        "title: Study\ndescription: Liam study routine and course tooling.\nlevel: 1\n---\n",
        encoding="utf-8",
    )
    return tmp_path


def test_scaffold_root_restores_reachability(rootless_vault, tmp_path, stub_embed):
    db = mt.open_db(tmp_path / "tree.db")
    try:
        mt.build_tree(rootless_vault, db)
        before = mt.check_tree(db, rootless_vault)
        assert before["ok"] is False
        assert any("MEMORY_TREE.md not found" in i for i in before["issues"])

        info = mt.scaffold_root(db, rootless_vault, force=True)
        assert info["is_root"] is True
        # Parentless forest roots are adopted; the child (household) is NOT.
        assert set(info["children"]) == {"Persona/INDEX.md", "STUDY.md"}
        assert (rootless_vault / "MEMORY_TREE.md").exists()

        # Re-walk to materialize the root's child edges; reachability now holds.
        mt.build_tree(rootless_vault, db)
        after = mt.check_tree(db, rootless_vault)
        assert not any("unreachable from root" in i for i in after["issues"])
        assert not any("not found as a node" in i for i in after["issues"])
    finally:
        db.close()


def test_scaffold_root_is_non_destructive(rootless_vault, tmp_path, stub_embed):
    db = mt.open_db(tmp_path / "tree.db")
    try:
        mt.build_tree(rootless_vault, db)
        (rootless_vault / "MEMORY_TREE.md").write_text("EXISTING ROOT", encoding="utf-8")

        info = mt.scaffold_root(db, rootless_vault)  # force defaults to False
        assert info["is_root"] is False
        assert info["written"].endswith("MEMORY_TREE.md.scaffold")
        # Existing root left untouched; preview written alongside.
        assert (rootless_vault / "MEMORY_TREE.md").read_text() == "EXISTING ROOT"
        assert (rootless_vault / "MEMORY_TREE.md.scaffold").exists()
    finally:
        db.close()


def test_scaffold_root_stdout_writes_nothing(rootless_vault, tmp_path, stub_embed):
    db = mt.open_db(tmp_path / "tree.db")
    try:
        mt.build_tree(rootless_vault, db)
        info = mt.scaffold_root(db, rootless_vault, to_stdout=True)
        assert info["written"] is None
        assert "type: memory-tree-root" in info["content"]
        assert not (rootless_vault / "MEMORY_TREE.md").exists()
        assert not (rootless_vault / "MEMORY_TREE.md.scaffold").exists()
    finally:
        db.close()


def test_scaffold_root_force_backs_up_existing(rootless_vault, tmp_path, stub_embed):
    db = mt.open_db(tmp_path / "tree.db")
    try:
        mt.build_tree(rootless_vault, db)
        (rootless_vault / "MEMORY_TREE.md").write_text("CURATED ROOT", encoding="utf-8")

        info = mt.scaffold_root(db, rootless_vault, force=True)
        assert info["is_root"] is True
        assert info["backup"] is not None
        # Previous root is preserved in the backup; the real root is replaced.
        assert (rootless_vault / "MEMORY_TREE.md.bak").read_text() == "CURATED ROOT"
        assert "type: memory-tree-root" in (rootless_vault / "MEMORY_TREE.md").read_text()
    finally:
        db.close()
