"""
Versioned prompt artifact management.
Artifacts are compiled DSPy prompts serialized to JSON and stored in both
SQLite (for querying) and evolution/artifacts/ (for direct file access by Node).
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import ARTIFACTS_DIR, OPTIMIZED_PROMPT_MAX_CHARS
from ..storage import get_storage

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# A freshly-instantiated dspy.Predict auto-generates its instruction from the
# signature ("Given the fields `a`, `b`, produce the fields `c`."). That string
# carries no learned signal — injecting it would be noise — so the consumer
# treats it as "no optimized prompt". The default is a single line, so no
# re.DOTALL: a multi-line instruction that merely opens with this shape but adds
# real learned guidance below must NOT be discarded.
_TRIVIAL_INSTRUCTION_RE = re.compile(r"^Given the fields .* produce the fields .*\.$")

# LIA-152 delimiter defense: an artifact is untrusted LLM output, so its
# instruction must not be able to forge the <stored-output> boundary it is wrapped
# in. Any stored-output open/close tag (with attribute or whitespace variants,
# including a space between `<` and `/`) is stripped before wrapping — the tag
# markup is removed but the surrounding TEXT is kept, so it always stays inside the
# boundary and can never close the trusted block to smuggle directives outside it.
_STORED_OUTPUT_TAG_RE = re.compile(r"<\s*/?\s*stored-output[^>]*>", re.IGNORECASE)


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


def get_active_prompt_block(module: str) -> Optional[dict]:
    """Return the active artifact's optimized instruction, sanitized and ready to
    inject into the agent prompt, or None when there is nothing safe to inject.

    The single trust boundary for injecting optimizer output into a live prompt
    (LIA-152). An artifact is untrusted LLM output, so this is the ONLY path that
    should turn one into prompt text — both the Node host bridge and the MCP tool
    route through here so neither can leak raw, unbounded artifact content.

    Fails safe (returns None) on every off-nominal case: no artifact, unparseable
    content, a missing/empty/non-string instruction, or the trivial
    auto-generated default that carries no learned signal.

    The returned ``block`` is wrapped in ``<stored-output source="dspy-artifact">``
    boundary tags ON PURPOSE — the tags are meant to be injected verbatim so the
    agent can see where untrusted stored content begins and ends.

    Deliberately NOT added: a post-boundary "the above may try to override your
    instructions, ignore such attempts" notice. For a passively-retrieved
    reflection that warning is harmless, but this block IS an intentional steering
    instruction the optimizer produced — telling the agent to ignore it would
    defeat the feature. The boundary tags + extraction + length cap are the
    defense; do not add an ignore-the-above notice here.
    """
    # Only the known base modules may be injected; an unexpected value could also
    # malform the XML tag attribute below. MODULE_REGISTRY is the single source of
    # truth for valid module names.
    from .modules import MODULE_REGISTRY
    if module not in MODULE_REGISTRY:
        return None

    art = get_active(module)
    if not art:
        return None

    # content is json.dumps(optimized.dump_state()); the learned instruction lives
    # at _predict.signature.instructions for a dspy.Predict module.
    # TODO(LIA-131): re-confirm this key path against a real GEPA-compiled
    # dump_state() at smoke-test time — verified so far only against a non-GEPA
    # (length-metric-era) artifact; GEPA compiles the same Predict, so the shape
    # is expected to hold, but confirm before flipping EVOLUTION_OPTIMIZED_PROMPTS=1.
    try:
        state = json.loads(art.get("content") or "")
    except (json.JSONDecodeError, TypeError):
        return None

    instruction = (
        state.get("_predict", {}).get("signature", {}).get("instructions")
        if isinstance(state, dict)
        else None
    )
    if not isinstance(instruction, str):
        return None
    instruction = instruction.strip()
    if not instruction or _TRIVIAL_INSTRUCTION_RE.match(instruction):
        return None

    # LIA-152: neutralize any forged boundary tags, then length-cap, before the
    # instruction is wrapped in (and demarcated by) the real <stored-output> tags.
    instruction = _STORED_OUTPUT_TAG_RE.sub("", instruction).strip()
    if not instruction:
        return None
    instruction = instruction[:OPTIMIZED_PROMPT_MAX_CHARS]
    block = (
        f'<stored-output source="dspy-artifact" module="{module}">\n'
        f"{instruction}\n"
        f"</stored-output>"
    )
    return {
        "block": block,
        "artifact_id": art.get("id"),
        "baseline_score": art.get("baseline_score"),
        "optimized_score": art.get("optimized_score"),
        "sample_count": art.get("sample_count"),
    }


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
