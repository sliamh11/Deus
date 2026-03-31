"""
DSPy-based prompt optimizer.
Loads scored interactions from the interaction log, splits into train/dev,
and runs MIPROv2 to find better prompts for each module.

Requires: pip install dspy-ai
Minimum samples: DSPY_MIN_SAMPLES (default 20) per module.
"""
import json
from typing import Optional

from ..config import (
    DSPY_MAX_BOOTSTRAPPED,
    DSPY_MAX_LABELED,
    DSPY_MIN_DOMAIN_SAMPLES,
    DSPY_MIN_SAMPLES,
    JUDGE_MODEL,
    load_api_key,
)
from ..ilog.interaction_log import get_recent
from .artifacts import save_artifact
from .modules import MODULE_REGISTRY, _require_dspy


def _setup_dspy(model: str = JUDGE_MODEL) -> None:
    _require_dspy()
    import dspy
    import os
    # Prefer Ollama (no quota) for optimizer; fall back to Gemini.
    # qwen3.5:4b with think=False to disable slow thinking mode.
    ollama_model = os.environ.get("DSPY_OLLAMA_MODEL", "qwen3.5:4b")
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            lm = dspy.LM(
                f"ollama/{ollama_model}",
                api_base="http://localhost:11434",
                think=False,
            )
            dspy.configure(lm=lm)
            return
    except Exception:
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


def _judge_metric(example, prediction, trace=None):
    """Metric function: checks that prediction is non-empty and reasonable."""
    try:
        pred_str = str(prediction.answer if hasattr(prediction, "answer") else prediction)
        return len(pred_str.strip()) > 20
    except Exception:
        return False


def optimize(
    module: str = "qa",
    group_folder: Optional[str] = None,
    min_samples: int = DSPY_MIN_SAMPLES,
    model: str = JUDGE_MODEL,
    domain: Optional[str] = None,
) -> Optional[str]:
    """
    Run DSPy MIPROv2 optimization on logged interactions.
    When domain is specified, uses weighted inclusion: primary pool (domain-specific)
    plus secondary pool (top cross-domain interactions) for generalization.
    Returns the artifact ID on success, None if insufficient samples.
    """
    _require_dspy()
    import dspy

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

    # Split train/dev (80/20)
    split = max(1, int(len(examples) * 0.8))
    trainset = examples[:split]
    devset = examples[split:]

    # Baseline score (average judge_score of dev set)
    dev_scores = [
        float(scored[i].get("judge_score", 0.5))
        for i in range(split, min(len(scored), len(examples)))
    ]
    baseline = sum(dev_scores) / len(dev_scores) if dev_scores else 0.5

    # Instantiate module
    ModuleCls = MODULE_REGISTRY[module]
    program = ModuleCls()

    # Run MIPROv2 — auto="light" sets num_candidates/num_trials internally,
    # so we can't pass num_candidates alongside it (DSPy v3 requirement).
    teleprompter = dspy.MIPROv2(
        metric=_judge_metric,
        auto="light",
        max_bootstrapped_demos=DSPY_MAX_BOOTSTRAPPED,
        max_labeled_demos=DSPY_MAX_LABELED,
        verbose=True,
    )
    optimized = teleprompter.compile(
        program,
        trainset=trainset,
        minibatch_size=min(10, len(trainset)),
    )

    # Extract compiled prompt
    try:
        prompt_content = json.dumps(optimized.dump_state(), indent=2)
    except Exception:
        prompt_content = str(optimized)

    # Estimate post-optimization score on dev set
    opt_scores = []
    for ex in devset[:10]:
        try:
            pred = optimized.forward(**{k: ex[k] for k in ex.inputs()})
            opt_scores.append(1.0 if _judge_metric(None, ex, pred) else 0.0)
        except Exception:
            pass
    optimized_score = sum(opt_scores) / len(opt_scores) if opt_scores else baseline

    # Save artifact — domain-keyed if domain-specific optimization
    artifact_module = f"{module}:{domain}" if domain else module
    aid = save_artifact(
        module=artifact_module,
        content=prompt_content,
        baseline_score=baseline,
        optimized_score=optimized_score,
        sample_count=len(examples),
    )

    delta = optimized_score - baseline
    print(
        f"[evolution] Optimized {artifact_module}: "
        f"baseline={baseline:.3f} → {optimized_score:.3f} "
        f"({'+'if delta>=0 else ''}{delta:.3f}) | artifact={aid[:8]}"
    )
    return aid
