#!/usr/bin/env python3
"""
Judge calibration watchdog (LIA-261) — LOCAL weekly.

The evolution loop scores every interaction with a LOCAL Ollama judge
(gemma4:e4b). If that judge silently drifts out of agreement with the pinned
Gemini ground truth, every downstream reflection/metric is quietly poisoned and
nothing warns. The CI safety-recall gate (.github/workflows/judge-gate.yml,
LIA-326) cannot cover this: CI has no Ollama, so it runs the *Gemini* judge.
This watchdog is the local sibling that anchors the *local* judge.

It re-runs `evolution/benchmark_judge.py` against the committed Gemini-labeled
fixture and checks the QUALITY-dimension Pearson against a measured floor in
`evolution/eval/baselines.json` (sibling to `safety_recall`). Quality is the
load-bearing composite dimension; personalization is excluded (structurally
dead — the limiter is the judge model, not the rubric; see CLAUDE.md
perso-structural). tool_use/safety are out of v1 scope.

Contract (mirrors safety_redteam.py::_classify_gate_outcome — infra precedence):
  - Any judge-infra failure (Ollama down, bench crash/timeout, too few records,
    too many parse errors, or an unreadable Pearson) -> INCONCLUSIVE, exit 0.
    A degraded run can NEVER be misread as a regression: we never false-alarm.
  - Quality Pearson below the floor on a clean full run -> WARN, exit nonzero
    (+ best-effort macOS banner). This is the ONLY alert.
  - Otherwise -> OK, exit 0.

Wired into scripts/maintenance.py's WEEKLY (Sunday) block, not the daily one: an
n=200 Ollama bench takes ~12-130min depending on contention (eval-diagnostics
ollama-contention), too heavy for the 04:30 daily run.

The fixture (finetune/judge-bench/fixture-v1.jsonl) is gitignored / local-only;
if it is absent the bench exits and this watchdog returns INCONCLUSIVE — the
same "opt-in surface absent -> skip, never fail maintenance" stance as
credential_probe.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the shared maintenance notifier (no platform guard needed at call site).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _notify import macos_notify  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINES_PATH = _REPO_ROOT / "evolution" / "eval" / "baselines.json"

# Conservative fallback floor when baselines.json is missing/malformed: set well
# below any healthy observed value (~0.67) so a missing file never false-alarms
# — mirrors safety_redteam's 0.80 fallback sitting below its 0.86 file floor.
_DEFAULT_QUALITY_FLOOR = 0.50

_DIM = "quality"
# Require near-full fixture coverage; a short run is untrustworthy -> INCONCLUSIVE.
_MIN_N = 150
# A high local-judge JSON-parse-error rate means the judge output is garbage, so
# the Pearson can't be believed -> INCONCLUSIVE. Local analogue of the safety
# gate's n_errored>0 (which gates on Gemini *call* failures, a different mode).
_MAX_PARSE_ERROR_RATE = 0.10

# Machine-adaptive overrides (design rule: env override, no hardcoded limits).
DEFAULT_FIXTURE = Path(
    os.environ.get(  # LIA-261
        "DEUS_JUDGE_CALIB_FIXTURE",
        str(_REPO_ROOT / "finetune" / "judge-bench" / "fixture-v1.jsonl"),
    )
)
DEFAULT_MODEL = os.environ.get("DEUS_JUDGE_CALIB_MODEL", "gemma4:e4b")  # LIA-261
# Bench wall-clock can reach ~130min under contention; default 2h. maintenance.py
# wraps this task with a slightly larger timeout so THIS timeout fires first and
# yields a clean INCONCLUSIVE rather than a hard maintenance TIMEOUT.
DEFAULT_TIMEOUT_S = int(os.environ.get("DEUS_JUDGE_CALIB_TIMEOUT_S", "7200"))  # LIA-261


def _load_floor() -> float:
    """Return the quality-Pearson floor from baselines.json.

    Falls back to _DEFAULT_QUALITY_FLOOR (0.50) if the file is missing,
    malformed, or the key is absent/out-of-range, so the watchdog stays usable
    (and quiet) even when the baseline file is unavailable.
    """
    try:
        data = json.loads(_BASELINES_PATH.read_text(encoding="utf-8"))
        floor = data["quality_pearson"]["floor"]
        if isinstance(floor, (int, float)) and 0.0 <= float(floor) <= 1.0:
            return float(floor)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _DEFAULT_QUALITY_FLOOR


def _run_benchmark(
    fixture: Path, model: str, timeout_s: int, repo_root: Path = _REPO_ROOT,
) -> "dict | None":
    """Run benchmark_judge against the fixture for one model.

    Returns that model's result row from the json-out payload, or None on ANY
    infra failure (Ollama down -> bench exits 1; nonzero exit; timeout; missing
    fixture; missing/malformed json-out; no row matching `model`). None ->
    caller maps to INCONCLUSIVE. List-form args + shell=False: fixture/model are
    passed as distinct argv elements, never interpolated into a shell string.
    """
    if not fixture.exists():
        print(f"  fixture absent ({fixture}) — skipping", flush=True)
        return None
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "judge_calib.json"
        cmd = [
            sys.executable, "-m", "evolution.benchmark_judge",
            "--fixture", str(fixture),
            "--models", model,
            "--safety-probes", "",   # empty (falsy) -> bench skips safety probes
            "--json-out", str(out_path),
            "--quiet",
        ]
        try:
            result = subprocess.run(
                cmd, cwd=str(repo_root), capture_output=True, text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            print(f"  bench timed out after {timeout_s}s — inconclusive", flush=True)
            return None
        except OSError as e:
            print(f"  bench failed to launch: {e} — inconclusive", flush=True)
            return None
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            print(f"  bench exited {result.returncode} ({tail[0]}) — inconclusive", flush=True)
            return None
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            rows = data["results"]
        except (OSError, ValueError, KeyError, TypeError):
            print("  bench json-out missing/malformed — inconclusive", flush=True)
            return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("model") == model:
            return row
    print(f"  no result row for model {model} — inconclusive", flush=True)
    return None


def _classify_outcome(result: "dict | None", floor: float) -> "tuple[int, str, str]":
    """Map a bench result row to (exit_code, status, message).

    Priority chain (infra precedence, like safety_redteam._classify_gate_outcome):
    a partial/degraded run can never be read as a regression.
    """
    if result is None:
        return 0, "INCONCLUSIVE", "judge infra unavailable — not measured"
    n = int(result.get("n", 0))
    if n < _MIN_N:
        return 0, "INCONCLUSIVE", f"only {n} records (< {_MIN_N}) — partial run, not measured"
    parse_errors = int(result.get("parse_errors", 0))
    rate = parse_errors / n  # n >= _MIN_N > 0 here (guarded above)
    if rate > _MAX_PARSE_ERROR_RATE:
        return (
            0, "INCONCLUSIVE",
            f"{parse_errors}/{n} ({rate:.0%}) judge parse errors (> {_MAX_PARSE_ERROR_RATE:.0%}) "
            "— judge output untrustworthy, not measured",
        )
    dims = result.get("dims")
    quality = dims.get(_DIM) if isinstance(dims, dict) else None
    pearson = quality.get("pearson") if isinstance(quality, dict) else None
    if not isinstance(pearson, (int, float)):
        # None when benchmark_judge's _pearson got a degenerate (constant) column;
        # also covers a malformed/missing dims payload. Either way: not measurable.
        return 0, "INCONCLUSIVE", f"{_DIM} Pearson unreadable — not measured"
    pearson = float(pearson)
    if pearson < floor:
        return (
            1, "WARN",
            f"{_DIM} Pearson {pearson:.3f} < {floor:.3f} floor — local judge calibration "
            f"REGRESSION on {n} records",
        )
    return 0, "OK", f"{_DIM} Pearson {pearson:.3f} >= {floor:.3f} floor — calibrated ({n} records)"


def main(argv: "list[str] | None" = None, notifier=macos_notify, runner=_run_benchmark) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)

    floor = _load_floor()
    print(
        f"judge_calibration: anchoring {args.model} on {args.fixture.name} "
        f"({_DIM} Pearson floor {floor:.3f})",
        flush=True,
    )
    result = runner(args.fixture, args.model, args.timeout_s)
    # Free multi-dim early-warning: log the composite Pearson (not gated on) so a
    # broad judge drift is visible before it concentrates into the quality dim.
    if isinstance(result, dict) and isinstance(result.get("composite_pearson"), (int, float)):
        print(f"  composite Pearson {result['composite_pearson']:.3f} (FYI, not gated)")
    exit_code, status, message = _classify_outcome(result, floor)
    print(f"  [{status}] {message}")
    if status == "WARN":
        notifier("Deus judge calibration", message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
