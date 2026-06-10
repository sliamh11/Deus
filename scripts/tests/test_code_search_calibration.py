"""Unit tests for code_search retrieval_confidence math (GH #717).

Deterministic — no embedding backend required. Exercises the pure
`_retrieval_confidence` helper extracted from `search()` so the
percentile -> confidence mapping is testable without Ollama, including the
regression invariant that a natural-language-shaped calibration distribution
no longer pins correct NL queries to ~0 confidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import code_search  # noqa: E402

rc = code_search._retrieval_confidence


def test_no_calibration_linear_fallback():
    # No calibration distribution -> linear 1 - dist/2; fts support -> no halving.
    assert rc(0.0, None, True) == 1.0
    assert rc(1.0, None, True) == 0.5
    assert rc(2.0, [], True) == 0.0  # empty list also triggers the fallback


def test_percentile_monotonic():
    cal = [0.2, 0.3, 0.4, 0.5, 0.6]
    assert rc(0.1, cal, True) == 1.0  # closer than every calibration query
    assert rc(0.7, cal, True) == 0.0  # farther than every calibration query
    # A smaller distance must never yield lower confidence.
    confs = [rc(d, cal, True) for d in (0.15, 0.35, 0.55)]
    assert confs[0] > confs[1] > confs[2]


def test_fts_halving_for_weak_vector_only_match():
    cal = [0.2, 0.3, 0.4, 0.5, 0.6]
    d = 0.55  # high percentile -> sub-0.5 confidence
    with_fts = rc(d, cal, True)
    without_fts = rc(d, cal, False)
    assert with_fts < 0.5
    # No FTS support + sub-0.5 confidence -> halved.
    assert without_fts == round(with_fts * 0.5, 3)


def test_717_nl_calibration_beats_symbol_self_lookup():
    """A correct in-domain NL query should not be pinned to ~0 confidence.

    The bug: symbol-name self-lookup calibration is artificially tight/low, so
    a correct NL match at ~0.46 lands in its upper tail -> near-zero confidence.
    The fix: a generic-NL calibration spans the real NL-query regime.
    """
    symbol_cal = [0.19, 0.25, 0.30, 0.34, 0.37, 0.40, 0.43, 0.46, 0.49, 0.53]
    nl_cal = [0.38, 0.42, 0.45, 0.48, 0.50, 0.53, 0.56, 0.58, 0.61, 0.64]
    nl_query_dist = 0.46  # a correct in-domain NL match
    assert rc(nl_query_dist, nl_cal, True) > rc(nl_query_dist, symbol_cal, True)
    # Genuinely out-of-domain queries still rank far -> low confidence.
    assert rc(0.70, nl_cal, True) <= 0.1
