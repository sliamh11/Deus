"""
Backend parity report — compares eval metrics between two backends.

Reads a pytest-json-report output file and groups test results by backend
parameter. Outputs a comparison table with per-metric score deltas.

Usage:
    python3 parity_report.py [--report .report.json] [--threshold 0.1]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def parse_report(report_path: Path) -> dict[str, dict[str, list]]:
    """Parse pytest-json-report and group results by (test_name, backend)."""
    data = json.loads(report_path.read_text())
    tests = data.get("tests", [])

    # Group: {test_base_name: {"claude": [outcomes], "openai": [outcomes]}}
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for test in tests:
        nodeid = test.get("nodeid", "")
        outcome = test.get("outcome", "unknown")
        duration = test.get("call", {}).get("duration", 0) if isinstance(test.get("call"), dict) else 0

        # Extract backend from parametrize: test_name[backend-case_id]
        backend = "claude"
        if "[openai-" in nodeid:
            backend = "openai"
        elif "[claude-" in nodeid:
            backend = "claude"

        # Strip parametrize suffix to get base test name
        base = nodeid.split("[")[0] if "[" in nodeid else nodeid

        grouped[base][backend].append({
            "outcome": outcome,
            "duration": duration,
            "nodeid": nodeid,
        })

    return dict(grouped)


def compute_pass_rate(outcomes: list[dict]) -> float:
    if not outcomes:
        return 0.0
    passed = sum(1 for o in outcomes if o["outcome"] == "passed")
    return passed / len(outcomes)


def report(grouped: dict, threshold: float) -> bool:
    """Print comparison table. Returns True if all deltas are within threshold."""
    all_ok = True

    print(f"\n{'Test':<50} {'Claude':>8} {'OpenAI':>8} {'Delta':>8} {'Status':>8}")
    print("-" * 86)

    for test_name in sorted(grouped.keys()):
        backends = grouped[test_name]
        claude_rate = compute_pass_rate(backends.get("claude", []))
        openai_rate = compute_pass_rate(backends.get("openai", []))
        delta = openai_rate - claude_rate

        short_name = test_name.split("::")[-1] if "::" in test_name else test_name
        if len(short_name) > 48:
            short_name = short_name[:45] + "..."

        status = "OK" if abs(delta) <= threshold else "DRIFT"
        if status == "DRIFT":
            all_ok = False

        print(f"{short_name:<50} {claude_rate:>7.0%} {openai_rate:>7.0%} {delta:>+7.0%} {status:>8}")

    print("-" * 86)
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Backend parity report")
    parser.add_argument(
        "--report", default=".report.json",
        help="Path to pytest-json-report output (default: .report.json)",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.1,
        help="Max acceptable pass-rate delta between backends (default: 0.1 = 10%%)",
    )
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"Report file not found: {report_path}", file=sys.stderr)
        sys.exit(1)

    grouped = parse_report(report_path)

    if not grouped:
        print("No test results found in report.", file=sys.stderr)
        sys.exit(1)

    all_ok = report(grouped, args.threshold)

    if all_ok:
        print(f"\nAll metrics within {args.threshold:.0%} threshold.")
    else:
        print(f"\nSome metrics exceed {args.threshold:.0%} threshold — review needed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
