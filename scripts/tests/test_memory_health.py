"""Tests for the session-start memory health check (#769).

Covers `memory_health.assess_memory_health` (catastrophic-only severity) and the
`vault_context_hook.main()` integration that surfaces a DEGRADED banner on the
vault-missing path instead of returning silently (the incident's failure mode).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, rel: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mh = _load("memory_health", "memory_health.py")
mt = _load("memory_tree", "memory_tree.py")


def _embed(text: str) -> list[float]:
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
def _stub_embed(monkeypatch):
    monkeypatch.setattr(mt, "embed_text", _embed)
    monkeypatch.setattr(
        mt, "embed_batch_text", lambda texts: [_embed(t) for t in texts], raising=False
    )


def _build_db(vault: Path, db_path: Path) -> None:
    db = mt.open_db(db_path)
    try:
        mt.build_tree(vault, db)
    finally:
        db.close()


@pytest.fixture
def healthy_vault_db(tmp_path, _stub_embed):
    vault = tmp_path / "vault"
    (vault / "Persona").mkdir(parents=True)
    (vault / "MEMORY_TREE.md").write_text(
        "---\nid: root000000000000000000000000000001\ntype: memory-tree-root\n"
        "title: Root\ndescription: Root map routing queries to persona nodes.\n"
        "level: 0\nchildren:\n  - Persona/INDEX.md\n---\n",
        encoding="utf-8",
    )
    (vault / "Persona" / "INDEX.md").write_text(
        "---\nid: idx0000000000000000000000000002\ntype: persona-index\n"
        "title: Persona\ndescription: Index of Liam personal facts.\nlevel: 1\n---\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "tree.db"
    _build_db(vault, db_path)
    return vault, db_path


# ── assess_memory_health ────────────────────────────────────────────────────

def test_healthy_tree_is_silent(healthy_vault_db):
    vault, db_path = healthy_vault_db
    ok, severity, lines = mh.assess_memory_health(vault, db_path)
    assert ok is True
    assert severity == "ok"
    assert lines == []


def test_missing_db_is_degraded(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    ok, _sev, lines = mh.assess_memory_health(vault, tmp_path / "absent.db")
    assert ok is False
    assert any("DB missing" in line for line in lines)


def test_unmounted_vault_is_degraded(healthy_vault_db, tmp_path):
    _vault, db_path = healthy_vault_db
    ok, _sev, lines = mh.assess_memory_health(tmp_path / "ghost", db_path)
    assert ok is False
    assert any("not mounted" in line for line in lines)


def test_unconfigured_vault_is_degraded(tmp_path):
    ok, _sev, lines = mh.assess_memory_health(None, tmp_path / "absent.db")
    assert ok is False
    assert any("not configured" in line for line in lines)


def test_missing_root_is_degraded(tmp_path, _stub_embed):
    # Build a tree with nodes but NO MEMORY_TREE.md → root node absent.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(
        "---\nid: aaaa000000000000000000000000001\ntype: persona-node\n"
        "title: A\ndescription: standalone node a.\nlevel: 1\n---\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "tree.db"
    _build_db(vault, db_path)
    ok, _sev, lines = mh.assess_memory_health(vault, db_path)
    assert ok is False
    assert any("root MEMORY_TREE.md is missing" in line for line in lines)


def test_trivial_hygiene_stays_silent(healthy_vault_db, tmp_path, _stub_embed):
    # Add one node with a description but no parent link (a single stray orphan).
    # That is routine hygiene, NOT catastrophic → must stay silent.
    vault, db_path = healthy_vault_db
    (vault / "stray.md").write_text(
        "---\nid: stray00000000000000000000000009\ntype: persona-node\n"
        "title: Stray\ndescription: an unreferenced but described node.\nlevel: 1\n---\n",
        encoding="utf-8",
    )
    _build_db(vault, db_path)  # rebuild incrementally (stray now present, unreferenced)
    ok, _sev, lines = mh.assess_memory_health(vault, db_path)
    # Root present + edges present + nodes>0 → not catastrophic, even though
    # `check` would report the stray as unreachable.
    assert ok is True, f"unexpected degradation: {lines}"


def test_render_degraded_includes_remediation():
    text = mh.render_degraded_section(
        ["navigation root MEMORY_TREE.md is missing from the tree"]
    )
    assert "MEMORY SYSTEM DEGRADED" in text
    assert "scaffold-root" in text
    assert "memory_tree.py check" in text


# ── vault_context_hook.main() integration ───────────────────────────────────

def test_main_emits_degraded_on_missing_vault(tmp_path, monkeypatch, capsys):
    """The silent-return fix: a non-dir vault must surface DEGRADED, not nothing.

    Exercises the path where the two earlier main() guards do NOT short-circuit:
    DEUS_VAULT_PRELOADED unset, and cwd has no `.git` file (not a worktree)."""
    vch = _load("vault_context_hook", "vault_context_hook.py")
    monkeypatch.delenv("DEUS_VAULT_PRELOADED", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd/.git is absent → worktree guard passes
    ghost = tmp_path / "ghost-vault"
    monkeypatch.setenv("DEUS_VAULT_PATH", str(ghost))  # set but not a dir
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    vch.main()

    out = capsys.readouterr().out.strip()
    assert out, "expected the hook to emit a degraded banner, got nothing"
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "MEMORY SYSTEM DEGRADED" in ctx
    assert "ghost-vault" in ctx
