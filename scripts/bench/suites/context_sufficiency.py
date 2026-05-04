"""Context sufficiency probe — does auto-load + retrieval carry enough info?

Tests whether the agent would have access to the expected answer after
the normal startup context-loading path runs. Used to validate that slimming
CLAUDE.md (moving state/persona/infra to on-demand leaves) does not
regress the agent's ability to reach critical facts.

Probe scopes:
- auto_load  — expected substring must appear in auto-loaded files only
               (CLAUDE.md by default).
- retrieval  — expected substring must appear in memory_tree top-k results
               for the probe's retrieval_query.
- either     — pass if the substring appears in auto-load OR retrieval results.

Fixture schema (JSONL):
    {
      "id": "unique-id",
      "scope": "auto_load" | "retrieval" | "either",
      "question": "human-readable question (documentation only)",
      "expected_substrings": ["string1", "string2"],
      "must_match": "any" | "all"  (default "any"),
      "retrieval_query": "query for memory_tree" (optional, defaults to question)
    }
"""
import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_MT_PATH = _SCRIPTS_DIR / "memory_tree.py"
_DEFAULT_DATASET = _SCRIPTS_DIR / "bench" / "tests" / "fixtures" / "context_sufficiency_universal.jsonl"
_DEFAULT_AUTO_LOAD = ["CLAUDE.md"]
_DEFAULT_RETRIEVAL_K = 3


def _resolve_vault() -> Path:
    env = os.environ.get("DEUS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    cfg = Path.home() / ".config/deus/config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text())
        vp = data.get("vault_path")
        if vp:
            return Path(vp).expanduser()
    raise SystemExit("vault path not configured; set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json")


def _load_mt():
    if "memory_tree" in sys.modules:
        return sys.modules["memory_tree"]
    spec = importlib.util.spec_from_file_location("memory_tree", _MT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_auto_load(vault: Path, filenames: list[str], aux_files: list[Path]) -> str:
    chunks = []
    for name in filenames:
        fp = vault / name
        if fp.exists():
            chunks.append(f"=== VAULT: {name} ===\n{fp.read_text()}")
    for fp in aux_files:
        if fp.exists():
            chunks.append(f"=== AUX: {fp.name} ===\n{fp.read_text()}")
    return "\n\n".join(chunks)


def _retrieve_context(mt: Any, db: Any, vault: Path, query: str, k: int) -> str:
    result = mt.retrieve(db, query)
    results = result.get("results", [])[:k]
    chunks = []
    for r in results:
        rel = r.get("path", "")
        # memory_tree stores paths relative to vault
        fp = vault / rel
        if fp.exists():
            chunks.append(f"=== TREE HIT: {rel} ===\n{fp.read_text()}")
        else:
            chunks.append(f"=== TREE HIT: {rel} ===\n(file not found)")
    return "\n\n".join(chunks)


def _check_substrings(text: str, substrings: list[str], must_match: str) -> tuple[bool, list[str]]:
    hits = [s for s in substrings if s.lower() in text.lower()]
    if must_match == "all":
        passed = len(hits) == len(substrings)
    else:
        passed = len(hits) > 0
    misses = [s for s in substrings if s.lower() not in text.lower()]
    return passed, misses


def _score_probe(
    mt: Any,
    db: Any,
    vault: Path,
    auto_load_text: str,
    probe: dict[str, Any],
    retrieval_k: int,
) -> CaseResult:
    probe_id = probe["id"]
    scope = probe.get("scope", "either")
    expected = probe.get("expected_substrings", [])
    must_match = probe.get("must_match", "any")
    query = probe.get("retrieval_query") or probe.get("question", "")

    if not expected:
        raise ValueError(f"probe {probe_id!r} has no expected_substrings")

    retrieval_text = ""
    if scope in ("retrieval", "either"):
        retrieval_text = _retrieve_context(mt, db, vault, query, retrieval_k)

    if scope == "auto_load":
        context = auto_load_text
    elif scope == "retrieval":
        context = retrieval_text
    else:  # either
        context = auto_load_text + "\n\n" + retrieval_text

    passed, misses = _check_substrings(context, expected, must_match)

    return CaseResult(
        case_id=probe_id,
        score=1.0 if passed else 0.0,
        passed=passed,
        meta={
            "scope": scope,
            "question": probe.get("question", ""),
            "misses": misses,
            "must_match": must_match,
        },
    )


@register("context_sufficiency")
def run_context_sufficiency(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="context_sufficiency")
    p.add_argument(
        "--dataset",
        type=Path,
        default=_DEFAULT_DATASET,
        help="Path to JSONL probe dataset",
    )
    p.add_argument(
        "--auto-load",
        type=str,
        default=",".join(_DEFAULT_AUTO_LOAD),
        help="Comma-separated vault filenames to treat as auto-loaded (default: CLAUDE.md)",
    )
    p.add_argument(
        "--retrieval-k",
        type=int,
        default=_DEFAULT_RETRIEVAL_K,
        help="Top-k memory_tree results to include in retrieval context",
    )
    p.add_argument(
        "--aux-files",
        type=str,
        default="",
        help="Comma-separated absolute paths to additional auto-loaded files (e.g., Claude Code auto-memory MEMORY.md).",
    )
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    vault = _resolve_vault()
    auto_load_files = [s.strip() for s in args.auto_load.split(",") if s.strip()]
    aux_files = [Path(s.strip()).expanduser() for s in args.aux_files.split(",") if s.strip()]
    auto_load_text = _read_auto_load(vault, auto_load_files, aux_files)

    dataset = _load_dataset(args.dataset)
    if args.limit is not None:
        dataset = dataset[: args.limit]

    mt = _load_mt()
    db = mt.open_db()

    t_start = time.monotonic()
    cases: list[CaseResult] = []
    for probe in dataset:
        cases.append(_score_probe(mt, db, vault, auto_load_text, probe, args.retrieval_k))
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    n = len(cases)
    hits = sum(1 for c in cases if c.passed)
    score = hits / n if n else 0.0

    per_scope: dict[str, dict[str, int]] = {}
    for c in cases:
        s = c.meta.get("scope", "either")
        bucket = per_scope.setdefault(s, {"n": 0, "hits": 0})
        bucket["n"] += 1
        if c.passed:
            bucket["hits"] += 1

    return RunResult(
        suite="context_sufficiency",
        score=score,
        cases=cases,
        latency_ms=elapsed_ms,
        meta={
            "n": n,
            "hits": hits,
            "per_scope": per_scope,
            "auto_load": auto_load_files,
            "aux_files": [str(p) for p in aux_files],
            "dataset": str(args.dataset),
            "vault": str(vault),
            "auto_load_chars": len(auto_load_text),
        },
    )
