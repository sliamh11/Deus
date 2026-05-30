"""
DSPy-based prompt optimizer.
Loads scored interactions from the interaction log, splits into train/dev,
and runs GEPA to find better prompts for each module.

Requires: pip install dspy
Minimum samples: DSPY_MIN_SAMPLES (default 20) per module.
"""
import json
from typing import Optional

from ..config import (
    DSPY_MIN_DOMAIN_SAMPLES,
    DSPY_MIN_SAMPLES,
    DSPY_OLLAMA_MODEL,
    DSPY_SHIP_MARGIN,
    JUDGE_MODEL,
    load_api_key,
)
from ..ilog.interaction_log import get_recent
from ..judge import NoProviderAvailableError, make_runtime_judge
from .artifacts import get_active, save_artifact
from .modules import MODULE_REGISTRY, _require_dspy


def _setup_dspy(model: str = JUDGE_MODEL) -> None:
    _require_dspy()
    import dspy
    import os
    # Prefer Ollama (no quota) for optimizer; fall back to Gemini.
    from ..judge.provider import JudgeRegistry
    try:
        ollama_provider = JudgeRegistry.default().get("ollama")
        if ollama_provider.is_available():
            from ..config import OLLAMA_HOST
            lm = dspy.LM(
                f"ollama/{DSPY_OLLAMA_MODEL}",
                api_base=OLLAMA_HOST,
                think=False,
            )
            dspy.configure(lm=lm)
            return
    except KeyError:
        pass
    # Fallback: Gemini
    os.environ.setdefault("GEMINI_API_KEY", load_api_key())
    model_id = model.replace("models/", "")
    lm = dspy.LM(f"gemini/{model_id}", api_key=load_api_key())
    dspy.configure(lm=lm)


def _build_examples(module: str, interactions: list[dict]) -> list:
    """
    Convert logged interactions into DSPy Example objects.
    Interactions with positive user signals are duplicated (2x weight)
    so the optimizer gives them more influence during training.
    """
    import dspy
    examples = []
    for row in interactions:
        try:
            prompt = row["prompt"]
            response = row["response"] or ""
            dims = json.loads(row.get("judge_dims") or "{}")

            if module == "qa":
                ex = dspy.Example(
                    query=prompt,
                    context="",
                    reflections="",
                    answer=response,
                ).with_inputs("query", "context", "reflections")

            elif module == "tool_selection":
                tools_used = json.loads(row.get("tools_used") or "[]")
                ex = dspy.Example(
                    query=prompt,
                    available_tools="send_message, schedule_task, list_tasks",
                    selected_tools=", ".join(tools_used),
                    rationale="",
                ).with_inputs("query", "available_tools")

            elif module == "summarization":
                ex = dspy.Example(
                    conversation_history=prompt,
                    summary=response[:500],
                ).with_inputs("conversation_history")

            else:
                continue

            examples.append(ex)
            # 2x weight for user-praised interactions
            if row.get("user_signal") == "positive":
                examples.append(ex)
        except Exception:
            continue
    return examples


# Per-module IO mapping: (example prompt field, example context field,
# prediction response field). Keep in sync with _build_examples and the module
# signatures in modules.py — a new module needs an entry in both places.
_MODULE_IO = {
    "qa": ("query", "context", "answer"),
    "tool_selection": ("query", "available_tools", "selected_tools"),
    "summarization": ("conversation_history", "", "summary"),
}


def _make_judge_metric(judge, module: str):
    """Build a GEPA-protocol metric that scores a prediction with the real
    runtime judge instead of the old length heuristic.

    The judge is captured once via closure and reused across every GEPA metric
    call, so a candidate is never charged a fresh judge construction. Returns
    {"score": float, "feedback": str} per the GEPAFeedbackMetric protocol.
    """
    prompt_field, context_field, response_field = _MODULE_IO.get(
        module, ("query", "context", "answer")
    )

    def metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
        try:
            prompt = str(example.get(prompt_field, "") or "")
            context = str(example.get(context_field, "") or "") if context_field else ""
            response = str(getattr(prediction, response_field, "") or "")
            result = judge.evaluate(
                prompt=prompt, response=response, context=context or None
            )
            # A parse-error result carries a fallback score that is not a real
            # quality signal — treat it as failure so the gate can never mistake
            # unparseable judge output for a good prompt.
            if getattr(result, "is_parse_error", False):
                return {"score": 0.0, "feedback": "judge parse error — scored 0.0"}
            return {"score": float(result.score), "feedback": result.rationale or ""}
        except Exception as exc:
            return {
                "score": 0.0,
                "feedback": f"metric error: {type(exc).__name__}: {exc}",
            }

    return metric


def _score_program(program, devset, metric, limit: int = 10) -> Optional[float]:
    """Run a DSPy program over the holdout and return its mean judge score.
    A per-example failure contributes 0.0 (consistent with the metric's own
    exception handling) so a broken program can't outscore a working one.
    Returns None when there is nothing to score.
    """
    scores = []
    for ex in devset[:limit]:
        try:
            pred = program.forward(**{k: ex[k] for k in ex.inputs()})
            scores.append(metric(ex, pred)["score"])
        except Exception:
            scores.append(0.0)
    return sum(scores) / len(scores) if scores else None


def _should_activate(
    optimized_score: float,
    baseline: float,
    active_artifact: Optional[dict],
    margin: float,
) -> bool:
    """Activate only when the optimized score beats the better of the
    un-optimized baseline and the current active artifact by `margin` — this is
    what keeps the loop from ever activating a regression. Falls back to
    `baseline` when there is no active artifact or its recorded score is
    missing/None, so the comparison never sees None.
    """
    current_active_score = baseline
    if active_artifact:
        current_active_score = active_artifact.get("optimized_score") or baseline
    threshold = max(baseline, current_active_score) + margin
    return optimized_score >= threshold


def optimize(
    module: str = "qa",
    group_folder: Optional[str] = None,
    min_samples: int = DSPY_MIN_SAMPLES,
    model: str = JUDGE_MODEL,
    domain: Optional[str] = None,
) -> Optional[str]:
    """
    Run DSPy GEPA optimization on logged interactions.
    When domain is specified, uses weighted inclusion: primary pool (domain-specific)
    plus secondary pool (top cross-domain interactions) for generalization.
    Returns the artifact ID on success, None if insufficient samples.
    """
    _require_dspy()
    import dspy

    # Build the judge once; the GEPA metric closes over it. Abort if no judge
    # provider is available — silently optimizing for length is the exact bug we
    # are removing, so there is deliberately no length fallback.
    try:
        judge = make_runtime_judge()
    except NoProviderAvailableError as exc:
        print(f"[evolution] No judge provider available — skipping {module}: {exc}")
        return None

    if domain:
        min_samples = DSPY_MIN_DOMAIN_SAMPLES

    # Load scored interactions across all eval suites (runtime + backfill)
    if domain:
        # Primary pool: domain-specific interactions
        primary = get_recent(
            group_folder=group_folder,
            limit=200,
            min_score=0.0,
            eval_suite=None,
            domain=domain,
        )
        primary = [i for i in primary if i.get("judge_score") is not None]

        # Secondary pool: top cross-domain interactions for generalization
        all_interactions = get_recent(
            group_folder=group_folder,
            limit=200,
            min_score=0.7,  # Only high-quality cross-domain
            eval_suite=None,
        )
        primary_ids = {i["id"] for i in primary}
        secondary = [
            i for i in all_interactions
            if i.get("judge_score") is not None and i["id"] not in primary_ids
        ]
        # Cap secondary at half of min_samples to keep domain data dominant
        secondary = sorted(secondary, key=lambda x: x.get("judge_score", 0), reverse=True)
        secondary = secondary[:min_samples // 2]

        scored = primary + secondary
    else:
        interactions = get_recent(
            group_folder=group_folder,
            limit=200,
            min_score=0.0,
            eval_suite=None,
        )
        scored = [i for i in interactions if i.get("judge_score") is not None]

    if len(scored) < min_samples:
        print(
            f"[evolution] Not enough samples for {module}: "
            f"{len(scored)} < {min_samples} required"
        )
        return None

    _setup_dspy(model)

    # Build examples
    examples = _build_examples(module, scored)
    if len(examples) < min_samples // 2:
        print(f"[evolution] Not enough usable examples for {module}: {len(examples)}")
        return None

    # Split train/dev (80/20). The dev split is the holdout used for both the
    # baseline and the optimized scoring and the ship-if-better gate.
    split = max(1, int(len(examples) * 0.8))
    trainset = examples[:split]
    devset = examples[split:]

    # Score candidates with the same quality signal the rest of the loop uses,
    # replacing the old length heuristic.
    metric = _make_judge_metric(judge, module)

    # Instantiate the un-optimized module and score it on the holdout — this is
    # the baseline the optimized prompt must beat. Scoring the program's own
    # output (not the historical logged response) keeps baseline and optimized
    # on the same population, scale, and metric.
    ModuleCls = MODULE_REGISTRY[module]
    program = ModuleCls()
    baseline = _score_program(program, devset, metric)
    if baseline is None:
        baseline = 0.5  # empty holdout — neutral prior; gate stays conservative

    teleprompter = dspy.GEPA(
        metric=metric,
        auto="light",
        track_stats=True,
    )
    optimized = teleprompter.compile(
        program,
        trainset=trainset,
    )

    # Extract compiled prompt
    try:
        prompt_content = json.dumps(optimized.dump_state(), indent=2)
    except Exception:
        prompt_content = str(optimized)

    # Score the optimized program on the same holdout, same metric.
    optimized_score = _score_program(optimized, devset, metric)
    if optimized_score is None:
        optimized_score = baseline

    # Activate only when the optimized score beats the better of baseline /
    # current active artifact by DSPY_SHIP_MARGIN. Below the margin the artifact
    # is still persisted (audit) but not activated.
    artifact_module = f"{module}:{domain}" if domain else module
    active_artifact = get_active(artifact_module)
    activate = _should_activate(
        optimized_score, baseline, active_artifact, DSPY_SHIP_MARGIN
    )

    aid = save_artifact(
        module=artifact_module,
        content=prompt_content,
        baseline_score=baseline,
        optimized_score=optimized_score,
        sample_count=len(examples),
        activate=activate,
    )

    delta = optimized_score - baseline
    decision = "ACTIVATED" if activate else "SHELVED (below margin)"
    print(
        f"[evolution] Optimized {artifact_module}: "
        f"baseline={baseline:.3f} → {optimized_score:.3f} "
        f"({'+' if delta >= 0 else ''}{delta:.3f}) | "
        f"n={len(examples)} | artifact={aid[:8]} | {decision}"
    )
    return aid
