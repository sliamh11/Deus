#!/usr/bin/env python3
"""Shared Gemini quota-error predicate.

Extracted from duplicated `"429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)`
checks scattered across scripts/drift_check.py, scripts/trec_atom_benchmark.py,
scripts/memory_indexer.py, scripts/bench/rule_following_judge.py, and
scripts/bench/attention_dilution_probe.py. Intentionally scoped to ONLY this
predicate — the surrounding retry/fallback bodies differ meaningfully per
file (temperatures, token limits, exhaustion-tracking, retry cadence) and are
NOT consolidated here.
"""


def is_quota_error(exc: Exception) -> bool:
    """Return True if `exc` looks like a Gemini per-minute/per-day quota error."""
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg
