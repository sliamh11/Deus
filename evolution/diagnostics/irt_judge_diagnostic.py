#!/usr/bin/env python3
"""
IRT-GRM Judge Reliability Diagnostic

Applies Item Response Theory (Graded Response Model) to the judge's scored corpus
to diagnose whether score gaps are calibration issues (fixable with affine transform)
or validity issues (require model improvement).

Usage:
    python3 -m evolution.diagnostics.irt_judge_diagnostic
    python3 evolution/diagnostics/irt_judge_diagnostic.py --help
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import pearsonr


DB_PATH = Path(os.environ.get("DEUS_EVOLUTION_DB", Path.home() / ".deus" / "evolution.db"))
DIMENSIONS = ["quality", "safety", "tool_use", "personalization"]
MIN_N_FOR_STABLE_IRT = 200


def load_scores(db_path: Path) -> dict[str, np.ndarray]:
    """Load per-dimension judge scores from the evolution database."""
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT judge_dims FROM interactions WHERE judge_dims IS NOT NULL AND parse_error = 0"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError as e:
        print(f"Error: Cannot read database at {db_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("Error: No scored interactions found in database.", file=sys.stderr)
        sys.exit(1)

    scores: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    for (dims_json,) in rows:
        try:
            dims = json.loads(dims_json)
            for d in DIMENSIONS:
                if d in dims:
                    scores[d].append(float(dims[d]))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    result = {}
    for d in DIMENSIONS:
        arr = np.array(scores[d])
        if len(arr) < MIN_N_FOR_STABLE_IRT:
            print(f"Warning: {d} has only {len(arr)} records (< {MIN_N_FOR_STABLE_IRT}). "
                  f"Discrimination estimates will have wide confidence intervals.", file=sys.stderr)
        result[d] = arr

    return result


def discretize_scores(scores: np.ndarray, n_categories: int = 5) -> np.ndarray:
    """Bin continuous [0,1] scores into ordinal categories for GRM."""
    bins = np.linspace(0, 1, n_categories + 1)
    return np.digitize(scores, bins[1:-1])  # 0-indexed categories


def grm_log_likelihood(params: np.ndarray, responses: np.ndarray, n_categories: int) -> float:
    """
    Negative log-likelihood for a single-item GRM.

    params[0] = discrimination (a)
    params[1:] = difficulty thresholds (b_1, ..., b_{K-1}) where K = n_categories
    """
    a = params[0]
    bs = params[1:]
    n = len(responses)

    # 21-point Gauss-Hermite quadrature over theta ~ N(0,1) -- fast approximation
    # that avoids full EM while being adequate for n > 200.
    theta_points = np.linspace(-3, 3, 21)
    theta_weights = np.exp(-theta_points**2 / 2) / np.sqrt(2 * np.pi)
    theta_weights /= theta_weights.sum()

    ll = 0.0
    for i in range(n):
        cat = min(int(responses[i]), n_categories - 1)

        p_item = 0.0
        for t, w in zip(theta_points, theta_weights):
            cum_probs = np.zeros(n_categories + 1)
            cum_probs[0] = 1.0
            for k in range(len(bs)):
                cum_probs[k + 1] = 1.0 / (1.0 + np.exp(-a * (t - bs[k])))
            cum_probs[-1] = 0.0

            p_cat = cum_probs[cat] - cum_probs[cat + 1]
            p_cat = max(p_cat, 1e-10)
            p_item += w * p_cat

        ll += np.log(max(p_item, 1e-10))

    return -ll


def fit_grm(scores: np.ndarray, n_categories: int = 5) -> dict:
    """Fit a single-item GRM and return parameters."""
    responses = discretize_scores(scores, n_categories)

    # Initial params: a=1, b's evenly spaced
    n_thresholds = n_categories - 1
    init_params = np.zeros(1 + n_thresholds)
    init_params[0] = 1.0  # discrimination
    init_params[1:] = np.linspace(-1.5, 1.5, n_thresholds)  # difficulties

    result = minimize(
        grm_log_likelihood,
        init_params,
        args=(responses, n_categories),
        method="L-BFGS-B",
        bounds=[(0.1, 5.0)] + [(-4.0, 4.0)] * n_thresholds,
        options={"maxiter": 500},
    )

    return {
        "discrimination": float(result.x[0]),
        "difficulties": result.x[1:].tolist(),
        "converged": result.success,
        "neg_log_likelihood": float(result.fun),
        "n_observations": len(scores),
    }


def compute_calibration_transform(scores: np.ndarray) -> dict:
    """
    Compute optimal affine transform y = a*x + b that maps scores
    to a uniform-ish distribution (target: well-calibrated judge).

    Uses the score distribution's deviation from uniform as the signal.
    """
    # Compare empirical CDF to uniform CDF
    sorted_scores = np.sort(scores)
    n = len(sorted_scores)
    empirical_cdf = np.arange(1, n + 1) / n

    # Fit affine: find a, b such that a*sorted_scores + b ~ empirical_cdf
    # This is just linear regression of empirical_cdf on sorted_scores
    A = np.vstack([sorted_scores, np.ones(n)]).T
    result = np.linalg.lstsq(A, empirical_cdf, rcond=None)
    a_coeff, b_coeff = result[0]

    # Residual tells us how well affine transform can fix the calibration
    predicted = a_coeff * sorted_scores + b_coeff
    residual_std = np.std(empirical_cdf - predicted)

    return {
        "a": float(a_coeff),
        "b": float(b_coeff),
        "residual_std": float(residual_std),
        "calibration_fixable": residual_std < 0.1,  # threshold: affine explains >90% of deviation
    }


def diagnose(scores_by_dim: dict[str, np.ndarray]) -> dict:
    """Run full IRT-GRM diagnostic and return structured results."""
    results = {}

    for dim, scores in scores_by_dim.items():
        if len(scores) == 0:
            results[dim] = {"error": "no data"}
            continue

        # Fit GRM
        grm = fit_grm(scores)

        # Compute calibration transform
        calibration = compute_calibration_transform(scores)

        # Score distribution stats
        stats = {
            "n": len(scores),
            "mean": float(np.mean(scores)),
            "std": float(np.std(scores)),
            "min": float(np.min(scores)),
            "max": float(np.max(scores)),
            "pct_at_bounds": float(((scores <= 0.05) | (scores >= 0.95)).mean()),
        }

        # Diagnosis
        if grm["discrimination"] < 0.5:
            diagnosis = "validity"
            recommendation = f"Low discrimination ({grm['discrimination']:.2f}) — {dim} dimension does not reliably separate quality levels. Consider rewriting the rubric criteria or using a more capable judge model."
        elif not calibration["calibration_fixable"]:
            diagnosis = "validity"
            recommendation = f"Affine transform cannot fix calibration (residual std={calibration['residual_std']:.3f}). Non-linear bias detected — the judge may be using a different quality concept than intended."
        else:
            diagnosis = "calibration"
            recommendation = f"Apply affine transform: score_calibrated = {calibration['a']:.3f} * score + {calibration['b']:.3f}. This closes the calibration gap without model changes."

        results[dim] = {
            "grm": grm,
            "calibration": calibration,
            "stats": stats,
            "diagnosis": diagnosis,
            "recommendation": recommendation,
        }

    return results


def print_report(results: dict) -> None:
    """Print human-readable diagnostic report."""
    print("=" * 60)
    print("IRT-GRM Judge Reliability Diagnostic")
    print("=" * 60)
    print()

    for dim, data in results.items():
        if "error" in data:
            print(f"  {dim}: {data['error']}")
            continue

        grm = data["grm"]
        cal = data["calibration"]
        stats = data["stats"]

        print(f"## {dim} (n={stats['n']})")
        print(f"  Distribution: mean={stats['mean']:.3f}, std={stats['std']:.3f}, bounds={stats['pct_at_bounds']*100:.1f}%")
        print(f"  GRM discrimination: {grm['discrimination']:.3f} {'(converged)' if grm['converged'] else '(DID NOT CONVERGE)'}")
        print(f"  GRM difficulties: {[f'{d:.2f}' for d in grm['difficulties']]}")
        print(f"  Calibration transform: y = {cal['a']:.3f}x + {cal['b']:.3f} (residual={cal['residual_std']:.4f})")
        print(f"  Diagnosis: **{data['diagnosis'].upper()}**")
        print(f"  Recommendation: {data['recommendation']}")
        print()

    # Summary
    diagnoses = [d["diagnosis"] for d in results.values() if "diagnosis" in d]
    validity_count = diagnoses.count("validity")
    calibration_count = diagnoses.count("calibration")

    print("-" * 60)
    print(f"Summary: {calibration_count} calibration issues, {validity_count} validity issues")
    if validity_count == 0:
        print("All gaps are calibration — apply per-dimension affine transforms. No retraining needed.")
    elif calibration_count == 0:
        print("All gaps are validity — judge model/rubric needs improvement on all dimensions.")
    else:
        print(f"Mixed: calibration fix for {calibration_count} dims, model improvement needed for {validity_count} dims.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="IRT-GRM Judge Reliability Diagnostic — diagnoses calibration vs validity gaps"
    )
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to evolution database")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of human-readable")
    parser.add_argument("--categories", type=int, default=5, help="Number of GRM categories (default: 5)")
    args = parser.parse_args()

    scores = load_scores(args.db)
    results = diagnose(scores)

    if args.json:
        # Convert numpy types for JSON serialization
        print(json.dumps(results, indent=2, default=lambda x: float(x) if hasattr(x, 'item') else x))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
