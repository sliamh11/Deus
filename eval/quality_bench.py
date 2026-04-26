"""
Lightweight quality benchmark — Claude vs Codex side-by-side.

Uses CLIs directly (claude -p / codex exec) instead of containers.
Judges each response with local Ollama (Gemma 4).

Usage:
    python3 eval/quality_bench.py --smoke   # 1 case
    python3 eval/quality_bench.py           # all 5 cases
    python3 eval/quality_bench.py --dry     # show cases only
"""

import argparse
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CASES = [
    {
        "id": "cqa_001",
        "input": "What are three key differences between TCP and UDP?",
        "expected": "TCP is connection-oriented, reliable, and ordered. UDP is connectionless, best-effort, and does not guarantee ordering.",
        "dimension": "factual",
    },
    {
        "id": "cqa_004",
        "input": "A train leaves Station A at 9am at 60 mph. Another leaves Station B (300 miles away) at 10am at 90 mph toward Station A. When do they meet?",
        "expected": "After 1 hour the first train covers 60 miles. The remaining 240 miles closes at 150 mph combined, taking 1.6 hours. They meet at approximately 11:36am, 156 miles from Station A.",
        "dimension": "reasoning",
    },
    {
        "id": "cqa_003",
        "input": "Summarize this in exactly 3 bullet points: Machine learning is a subset of AI. It uses statistical methods. Models learn from data. There are supervised and unsupervised approaches. Deep learning uses neural networks.",
        "expected": "Three bullet points covering: ML is a subset of AI using statistics; models learn from data; approaches include supervised, unsupervised, and deep learning.",
        "dimension": "instruction_following",
    },
    {
        "id": "cqa_015",
        "input": "A bag has 3 red and 5 blue balls. You draw 2 without replacement. What is the probability both are red?",
        "expected": "P = (3/8) * (2/7) = 6/56 = 3/28 ≈ 0.107.",
        "dimension": "math",
    },
    {
        "id": "cqa_013",
        "input": "Respond in exactly 2 sentences: What is Docker and why is it useful?",
        "expected": "Two sentences describing Docker as a containerization platform that packages applications with their dependencies. Should mention portability or isolation as key benefits.",
        "dimension": "format_constraint",
    },
]

JUDGE_PROMPT = """You are a strict judge comparing two AI responses to the same question.

Question: {question}

Reference answer: {expected}

--- Response A (Claude) ---
{response_a}

--- Response B (Codex) ---
{response_b}

Score each response on these dimensions (0.0 to 1.0):
1. correctness: Is the answer factually correct and complete?
2. relevance: Does it directly address the question asked?
3. conciseness: Is it appropriately concise without unnecessary padding?
4. instruction_compliance: Does it follow any format constraints in the question?

Return ONLY valid JSON, no markdown fences:
{{"a": {{"correctness": 0.0, "relevance": 0.0, "conciseness": 0.0, "instruction_compliance": 0.0}}, "b": {{"correctness": 0.0, "relevance": 0.0, "conciseness": 0.0, "instruction_compliance": 0.0}}, "preferred": "a or b or tie", "reason": "one sentence"}}"""


@dataclass
class CaseResult:
    case_id: str
    dimension: str
    claude_response: str
    codex_response: str
    claude_latency_ms: float
    codex_latency_ms: float
    scores: dict | None = None
    judge_error: str | None = None


def invoke_claude(prompt: str) -> tuple[str, float]:
    t0 = time.monotonic()
    result = subprocess.run(
        ["claude", "-p", prompt, "--max-turns", "1"],
        capture_output=True, text=True, timeout=120,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    text = result.stdout.strip() if result.returncode == 0 else f"ERROR: {result.stderr.strip()}"
    return text, elapsed_ms


def invoke_codex(prompt: str, model: str) -> tuple[str, float]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        out_path = f.name

    t0 = time.monotonic()
    result = subprocess.run(
        ["codex", "exec", "-m", model, "-o", out_path, prompt],
        capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    try:
        text = Path(out_path).read_text().strip()
    except FileNotFoundError:
        text = f"ERROR: no output file. stderr: {result.stderr.strip()[:200]}"
    finally:
        Path(out_path).unlink(missing_ok=True)

    if not text and result.returncode != 0:
        text = f"ERROR (rc={result.returncode}): {result.stderr.strip()[:200]}"

    return text, elapsed_ms


def judge_with_ollama(question: str, expected: str, resp_a: str, resp_b: str) -> dict:
    import re
    prompt = JUDGE_PROMPT.format(
        question=question, expected=expected, response_a=resp_a, response_b=resp_b,
    )
    result = subprocess.run(
        ["ollama", "run", "gemma4:e4b", "--nowordwrap"],
        input=prompt, capture_output=True, text=True, timeout=120,
    )
    raw = result.stdout.strip()
    raw = re.sub(r'\x1b\[[^m]*m|\x1b\[\?[0-9]+[hl]|\x1b\[[0-9]*[A-Za-z]', '', raw)
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1:
        raise ValueError(f"No JSON found in judge output: {raw[:200]}")
    return json.loads(raw[start:end + 1])


def print_table(results: list[CaseResult]) -> None:
    dims = ["correctness", "relevance", "conciseness", "instruction_compliance"]
    header = f"{'Case':<12} {'Dim':<18} "
    for d in dims:
        header += f"{'C:'+d[:6]:>9} {'X:'+d[:6]:>9} "
    header += f"{'C_ms':>8} {'X_ms':>8} {'Pref':>5}"
    print("\n" + header)
    print("-" * len(header))

    claude_totals = {d: 0.0 for d in dims}
    codex_totals = {d: 0.0 for d in dims}
    counted = 0

    for r in results:
        if not r.scores:
            print(f"{r.case_id:<12} {r.dimension:<18} JUDGE ERROR: {r.judge_error}")
            continue
        row = f"{r.case_id:<12} {r.dimension:<18} "
        sa = r.scores.get("a", {})
        sb = r.scores.get("b", {})
        for d in dims:
            va = sa.get(d, 0)
            vb = sb.get(d, 0)
            row += f"{va:>9.2f} {vb:>9.2f} "
            claude_totals[d] += va
            codex_totals[d] += vb
        pref = r.scores.get("preferred", "?")
        row += f"{r.claude_latency_ms:>8.0f} {r.codex_latency_ms:>8.0f} {pref:>5}"
        print(row)
        counted += 1

    if counted > 0:
        print("-" * len(header))
        avg_row = f"{'AVG':<12} {'':<18} "
        for d in dims:
            avg_row += f"{claude_totals[d]/counted:>9.2f} {codex_totals[d]/counted:>9.2f} "
        print(avg_row)

    print("\n" + "=" * 80)
    print("RAW RESPONSES")
    print("=" * 80)
    for r in results:
        print(f"\n--- {r.case_id} ({r.dimension}) ---")
        print(f"[Claude] {r.claude_response[:500]}")
        print(f"[Codex]  {r.codex_response[:500]}")
        if r.scores:
            print(f"[Judge]  preferred={r.scores.get('preferred')} reason={r.scores.get('reason','')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run only 1 case")
    parser.add_argument("--dry", action="store_true", help="Show cases without running")
    parser.add_argument("--model", default="gpt-5.4", help="Codex model (default: gpt-5.4)")
    args = parser.parse_args()

    cases = CASES[:1] if args.smoke else CASES

    if args.dry:
        for c in cases:
            print(f"  {c['id']}: [{c['dimension']}] {c['input'][:60]}...")
        print(f"\n{len(cases)} cases x 2 backends = {len(cases)*2} CLI invocations")
        return

    print(f"Model: Claude (claude -p) vs Codex ({args.model})")
    print(f"Cases: {len(cases)} | Judge: Ollama gemma4:e4b (local)")

    results: list[CaseResult] = []

    for i, case in enumerate(cases):
        print(f"\n[{i+1}/{len(cases)}] {case['id']} ({case['dimension']})...")

        print("  Claude...", end="", flush=True)
        claude_text, claude_ms = invoke_claude(case["input"])
        status = "OK" if not claude_text.startswith("ERROR") else "ERR"
        print(f" {claude_ms:.0f}ms {status}")

        print("  Codex...", end="", flush=True)
        codex_text, codex_ms = invoke_codex(case["input"], args.model)
        status = "OK" if not codex_text.startswith("ERROR") else "ERR"
        print(f" {codex_ms:.0f}ms {status}")

        print("  Judging...", end="", flush=True)
        cr = CaseResult(
            case_id=case["id"],
            dimension=case["dimension"],
            claude_response=claude_text,
            codex_response=codex_text,
            claude_latency_ms=claude_ms,
            codex_latency_ms=codex_ms,
        )
        try:
            cr.scores = judge_with_ollama(case["input"], case["expected"], claude_text, codex_text)
            print(f" preferred={cr.scores.get('preferred', '?')}")
        except Exception as e:
            cr.judge_error = str(e)
            print(f" ERROR: {e}")

        results.append(cr)

    out_path = Path(__file__).parent / ".quality_bench_results.json"
    out_path.write_text(json.dumps(
        [{"case_id": r.case_id, "dimension": r.dimension,
          "claude": r.claude_response, "codex": r.codex_response,
          "claude_ms": r.claude_latency_ms, "codex_ms": r.codex_latency_ms,
          "scores": r.scores, "judge_error": r.judge_error} for r in results],
        indent=2,
    ))

    print_table(results)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
