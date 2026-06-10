"""
Generic per-task metrics for the evolution loop.

Metrics are schemaless JSON attached to interactions: flat dicts whose values
are scalars or lists of scalars. A soft registry (WELL_KNOWN_METRICS) warns on
unknown keys but never rejects them — callers can track anything. The judge
stays blind to metrics (anti-gaming); reflection generation sees them.

Analysis functions are pure: they take rows as returned by
StorageProvider.get_metrics_rows() so they can be tested without a database.
Use fetch_metrics_rows() to get rows from the active storage provider.
"""
import json
import logging
from collections import Counter, defaultdict
from typing import Any, Optional

from .storage import get_storage

log = logging.getLogger(__name__)

# Hard cap on the serialized metrics payload per interaction. Prevents a
# runaway caller from bloating the interactions table (prompt compaction
# exists precisely because rows get re-read in bulk).
MAX_METRICS_BYTES = 16384

# Soft registry: documented keys with expected shapes. validate_metrics()
# warns on keys missing from this registry but never rejects them.
WELL_KNOWN_METRICS = {
    "tests_passed": "int — tests passing after the task",
    "tests_failed": "int — tests failing after the task",
    "tests_added": "int — new tests written for the task",
    "breaks": "list[str] — break categories observed (regression|expected|suspicious|integration)",
    "confidence": "float 0-1 — agent's stated confidence in the result",
    "warden_rounds": "int — review rounds before SHIP",
    "regressions_caught": "int — regressions caught before merge",
    "files_changed": "int — files touched by the task",
    "duration_minutes": "float — wall-clock task duration",
    "task_type": "str — free-form task classification",
}

BREAK_CATEGORIES = ("regression", "expected", "suspicious", "integration")

_SCALAR_TYPES = (str, int, float, bool)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def validate_metrics(metrics: dict) -> dict:
    """Validate a metrics dict. Returns it unchanged on success.

    Raises ValueError on: non-dict input, non-string keys, nested objects,
    lists containing non-scalars, None values, or serialized size above
    MAX_METRICS_BYTES. Unknown keys only log a warning (soft registry).
    """
    if not isinstance(metrics, dict):
        raise ValueError(f"metrics must be a dict, got {type(metrics).__name__}")
    for key, value in metrics.items():
        if not isinstance(key, str):
            raise ValueError(f"metric keys must be strings, got {key!r}")
        if isinstance(value, list):
            if not all(_is_scalar(v) for v in value):
                raise ValueError(
                    f"metric {key!r}: lists may only contain scalars "
                    "(str/int/float/bool)"
                )
        elif not _is_scalar(value):
            raise ValueError(
                f"metric {key!r}: value must be a scalar or a list of scalars, "
                f"got {type(value).__name__}"
            )
        if key not in WELL_KNOWN_METRICS:
            log.warning(
                "metric %r is not in WELL_KNOWN_METRICS — accepted, but "
                "consider a registered key for cross-task aggregation", key,
            )
    raw = json.dumps(metrics).encode("utf-8")
    if len(raw) > MAX_METRICS_BYTES:
        raise ValueError(
            f"metrics payload is {len(raw)} bytes; max is {MAX_METRICS_BYTES}"
        )
    return metrics


def parse_metrics(raw: Optional[str]) -> Optional[dict]:
    """Parse a stored metrics JSON string. Returns None on missing/invalid."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("unparseable metrics payload skipped: %.80r", raw)
        return None
    return parsed if isinstance(parsed, dict) else None


def update_metrics(
    interaction_id: str,
    metrics: dict,
    *,
    merge: bool = True,
) -> dict:
    """Attach or update metrics on an existing interaction (post-hoc path).

    With merge=True (default), new keys are merged over any previously stored
    metrics. With merge=False, the stored payload is replaced wholesale.
    Returns the final stored dict. Raises ValueError if the interaction does
    not exist or validation fails.
    """
    validate_metrics(metrics)
    store = get_storage()
    row = store.get_interaction(interaction_id)
    if row is None:
        raise ValueError(f"interaction {interaction_id!r} not found")
    if merge:
        existing = parse_metrics(row.get("metrics")) or {}
        final = {**existing, **metrics}
    else:
        final = metrics
    validate_metrics(final)  # merged payload must also respect the size cap
    store.update_interaction(interaction_id, metrics=json.dumps(final))
    return final


def fetch_metrics_rows(
    *,
    group_folder: Optional[str] = None,
    days: int = 30,
    limit: int = 1000,
) -> list[dict]:
    """Fetch metrics-bearing interaction rows from the active storage provider."""
    return get_storage().get_metrics_rows(
        group_folder=group_folder, days=days, limit=limit,
    )


# ── Analysis (pure functions over get_metrics_rows() output) ─────────────────


def _parsed_rows(rows: list[dict]) -> list[tuple[dict, dict]]:
    """Yield (row, parsed_metrics) pairs, skipping unparseable payloads."""
    out = []
    for row in rows:
        parsed = parse_metrics(row.get("metrics"))
        if parsed:
            out.append((row, parsed))
    return out


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def summarize_metrics(rows: list[dict], key: Optional[str] = None) -> dict:
    """Per-key summary across rows.

    Numeric values get count/mean/min/max/sum; strings, bools, and list
    elements get categorical value counts. Pass key to restrict the summary
    to a single metric.
    """
    numeric: dict[str, list[float]] = defaultdict(list)
    categorical: dict[str, Counter] = defaultdict(Counter)
    parsed = _parsed_rows(rows)

    for _, metrics in parsed:
        for k, v in metrics.items():
            if key is not None and k != key:
                continue
            if _is_numeric(v):
                numeric[k].append(v)
            elif isinstance(v, list):
                categorical[k].update(str(e) for e in v)
            else:
                categorical[k][str(v)] += 1

    keys: dict[str, dict] = {}
    for k, values in numeric.items():
        keys[k] = {
            "type": "numeric",
            "count": len(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "sum": sum(values),
        }
    for k, counter in categorical.items():
        entry = keys.setdefault(k, {"type": "categorical", "count": 0})
        entry["values"] = dict(counter.most_common())
        entry["count"] = entry.get("count", 0) + sum(counter.values())

    return {"interactions": len(parsed), "keys": keys}


def metric_trend(rows: list[dict], key: str) -> list[dict]:
    """Daily average of a numeric metric. Non-numeric values are skipped.

    Returns [{"day", "avg", "count"}, ...] ordered by day ascending.
    """
    by_day: dict[str, list[float]] = defaultdict(list)
    for row, metrics in _parsed_rows(rows):
        value = metrics.get(key)
        if _is_numeric(value):
            by_day[str(row.get("timestamp", ""))[:10]].append(value)
    return [
        {"day": day, "avg": sum(vals) / len(vals), "count": len(vals)}
        for day, vals in sorted(by_day.items())
    ]


# Confidence bands for calibration: (label, inclusive lo, exclusive hi).
_CONFIDENCE_BANDS = (
    ("low", 0.0, 0.5),
    ("medium", 0.5, 0.8),
    ("high", 0.8, 1.0 + 1e-9),  # epsilon so confidence == 1.0 lands in "high"
)


def confidence_calibration(rows: list[dict], key: str = "confidence") -> dict:
    """Compare self-reported confidence against judge scores.

    Only rows carrying both a numeric confidence metric and a judge_score
    participate. gap = avg_confidence - avg_judge_score per band; a positive
    gap means overconfidence.
    """
    pairs: list[tuple[float, float]] = []
    for row, metrics in _parsed_rows(rows):
        confidence = metrics.get(key)
        score = row.get("judge_score")
        if _is_numeric(confidence) and _is_numeric(score):
            pairs.append((confidence, score))

    buckets = []
    for label, lo, hi in _CONFIDENCE_BANDS:
        band = [(c, s) for c, s in pairs if lo <= c < hi]
        if not band:
            continue
        avg_c = sum(c for c, _ in band) / len(band)
        avg_s = sum(s for _, s in band) / len(band)
        buckets.append({
            "band": label,
            "n": len(band),
            "avg_confidence": avg_c,
            "avg_judge_score": avg_s,
            "gap": avg_c - avg_s,
        })

    overall_gap = (
        sum(c - s for c, s in pairs) / len(pairs) if pairs else None
    )
    return {"n": len(pairs), "buckets": buckets, "overall_gap": overall_gap}


def break_report(rows: list[dict]) -> dict:
    """Aggregate the 'breaks' metric (list of break-category strings).

    A bare string value is treated as a single-category list. Categories
    outside BREAK_CATEGORIES are counted too (soft registry philosophy) —
    they show up alongside the known ones.
    """
    by_category: Counter = Counter()
    interactions_with_breaks = 0
    parsed = _parsed_rows(rows)
    for _, metrics in parsed:
        breaks = metrics.get("breaks")
        if isinstance(breaks, str):
            breaks = [breaks]
        if not isinstance(breaks, list) or not breaks:
            continue
        interactions_with_breaks += 1
        by_category.update(str(b) for b in breaks)
    return {
        "interactions": len(parsed),
        "interactions_with_breaks": interactions_with_breaks,
        "total_breaks": sum(by_category.values()),
        "by_category": dict(by_category.most_common()),
    }
