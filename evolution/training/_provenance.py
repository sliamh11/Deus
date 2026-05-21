"""Shared provenance helpers for evolution/training/* artifacts.

Origin: extracted verbatim from build_judge_lora_dataset.py (step-1 dataset
builder, commit 5ca45f0) so the step-2 training driver can reuse them without
duplicating logic. Honors the "Never duplicate content across files" rule.

All three functions are intentionally tolerant of non-git environments
(return None) so they can be called from contexts where git may not be
available (CI containers, fresh clones, tarball installs).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# evolution/training/_provenance.py -> evolution/training -> evolution -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def git_sha() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None


def git_dirty() -> bool | None:
    """Whether the working tree has uncommitted changes. None if not in a git repo.

    Pair this flag with git_sha when consuming the manifest — a dirty tree means
    the recorded SHA does not point at the exact code that produced the dataset,
    which silently breaks manifest reproducibility downstream.
    """
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return len(out.strip()) > 0
    except Exception:
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
