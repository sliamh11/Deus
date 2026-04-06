"""
Versioned prompt artifact management.
Artifacts are compiled DSPy prompts serialized to JSON and stored in both
SQLite (for querying) and evolution/artifacts/ (for direct file access by Node).
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import ARTIFACTS_DIR
from ..storage import get_storage

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def save_artifact(
    module: str,
    content: str,
    baseline_score: Optional[float] = None,
    optimized_score: Optional[float] = None,
    sample_count: Optional[int] = None,
) -> str:
    """
    Save a new prompt artifact and mark it as active (deactivates previous for same module).
    Returns the artifact ID.
    """
    aid = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    store = get_storage()
    store.save_artifact(
        artifact_id=aid,
        module=module,
        content=content,
        created_at=ts,
        baseline_score=baseline_score,
        optimized_score=optimized_score,
        sample_count=sample_count,
    )

    # Write to filesystem for Node.js to read without Python
    _write_file(module, content, aid, ts, baseline_score, optimized_score)
    return aid


def get_active(module: str) -> Optional[dict]:
    """Return the currently active artifact for a module, or None."""
    store = get_storage()
    return store.get_active_artifact(module)


def list_artifacts(module: Optional[str] = None, limit: int = 10) -> list[dict]:
    store = get_storage()
    return store.list_artifacts(module=module, limit=limit)


def _write_file(
    module: str,
    content: str,
    artifact_id: str,
    created_at: str,
    baseline_score: Optional[float],
    optimized_score: Optional[float],
) -> None:
    data = {
        "id": artifact_id,
        "module": module,
        "created_at": created_at,
        "baseline_score": baseline_score,
        "optimized_score": optimized_score,
        "content": content,
    }
    # Write latest symlink-style file (Node reads this on startup)
    (ARTIFACTS_DIR / f"{module}-latest.json").write_text(
        json.dumps(data, indent=2)
    )
    # Also write versioned copy
    safe_ts = created_at.replace(":", "-").replace(".", "-")[:19]
    (ARTIFACTS_DIR / f"{module}-{safe_ts}.json").write_text(
        json.dumps(data, indent=2)
    )
