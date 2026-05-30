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
    activate: bool = True,
) -> str:
    """
    Save a new prompt artifact. Returns the artifact ID.

    When activate=True (default), mark it active (deactivating the previous active
    artifact for the module) and refresh the {module}-latest.json file that a
    consumer treats as "the active prompt". When activate=False (ship-if-better
    gate rejected it), persist for audit only: the row is stored inactive and
    {module}-latest.json is NOT clobbered, so the current active artifact stands.
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
        active=activate,
    )

    # Write to filesystem for Node.js to read without Python.
    _write_file(module, content, aid, ts, baseline_score, optimized_score,
                update_latest=activate)
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
    update_latest: bool = True,
) -> None:
    data = {
        "id": artifact_id,
        "module": module,
        "created_at": created_at,
        "baseline_score": baseline_score,
        "optimized_score": optimized_score,
        "content": content,
    }
    payload = json.dumps(data, indent=2)
    # The {module}-latest.json file is what a consumer reads as "the active
    # prompt". Only refresh it when this artifact is being activated; a shelved
    # artifact must not overwrite the standing active one.
    if update_latest:
        (ARTIFACTS_DIR / f"{module}-latest.json").write_text(payload)
    # Always write the versioned copy (full audit trail, active or shelved).
    safe_ts = created_at.replace(":", "-").replace(".", "-")[:19]
    (ARTIFACTS_DIR / f"{module}-{safe_ts}.json").write_text(payload)
