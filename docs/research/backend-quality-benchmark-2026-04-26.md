# Backend Quality Benchmark: Claude vs Codex

**Date:** 2026-04-26
**Method:** CLI-based (claude -p / codex exec), judged by local Ollama Gemma 4 (e4b)
**Models:** Claude Opus 4.6 vs GPT-5.4 (high reasoning effort)
**Script:** `eval/quality_bench.py`

## Results

| Case | Dimension | C:correct | X:correct | C:relevance | X:relevance | C:concise | X:concise | C:comply | X:comply | C_ms | X_ms | Preferred |
|------|-----------|-----------|-----------|-------------|-------------|-----------|-----------|----------|----------|------|------|-----------|
| cqa_001 | factual | 1.00 | 1.00 | 1.00 | 1.00 | 0.90 | 0.80 | 1.00 | 1.00 | 8024 | 15131 | Claude |
| cqa_004 | reasoning | 1.00 | 1.00 | 1.00 | 1.00 | 0.90 | 0.80 | 1.00 | 1.00 | 10000 | 7613 | Tie |
| cqa_003 | instruction | 1.00 | 1.00 | 1.00 | 1.00 | 0.90 | 0.90 | 1.00 | 1.00 | 6361 | 4523 | Tie |
| cqa_015 | math | 1.00 | 1.00 | 1.00 | 1.00 | 0.90 | 1.00 | 1.00 | 1.00 | 7678 | 4516 | Codex |
| cqa_013 | format | 1.00 | 1.00 | 1.00 | 1.00 | 0.90 | 1.00 | 1.00 | 1.00 | 7140 | 4712 | Codex |
| **AVG** | | **1.00** | **1.00** | **1.00** | **1.00** | **0.90** | **0.90** | **1.00** | **1.00** | **7841** | **7299** | **C:1 X:2 T:2** |

## Raw Responses

### cqa_001 (factual) - TCP vs UDP

**Claude:** Structured with bold headers (Connection, Reliability, Overhead), mentioned 3-way handshake, flow control, use cases.

**Codex:** Used inline code formatting for protocol names, similar coverage, mentioned flow/congestion control.

**Judge:** Claude preferred for clear segmented structure with headers.

### cqa_004 (reasoning) - Train meeting point

**Claude:** Clean derivation: 240/150 = 1.6 hours, answer 11:36 AM, 156 miles from A.

**Codex:** Initially stated "11:00 AM" then self-corrected to 11:36 AM in the explanation. Same math.

**Judge:** Tie - both correct despite Codex's initial misstatement (self-corrected).

### cqa_003 (instruction following) - 3 bullet points

**Claude:** Three bullets covering ML-as-subset, supervised/unsupervised, deep learning.

**Codex:** Three bullets, nearly identical content, slightly different phrasing.

**Judge:** Tie - both perfectly meet constraints.

### cqa_015 (math) - Probability

**Claude:** Answer first (3/28 ~10.7%), then derivation.

**Codex:** LaTeX notation, cleaner mathematical presentation.

**Judge:** Codex preferred for clearer mathematical notation.

### cqa_013 (format constraint) - Docker in 2 sentences

**Claude:** Two sentences, richer ("spin up reproducible environments in seconds").

**Codex:** Two sentences, tighter phrasing, no padding.

**Judge:** Codex preferred for conciseness.

## Key Findings

1. **Perfect correctness parity** - both models scored 1.0 on correctness and relevance across all cases.
2. **Stylistic differences drive preferences** - Claude favors structured formatting (headers, richer explanations); Codex favors concise, direct output.
3. **Latency comparable** - Codex faster on 4/5 cases (avg 7.3s vs 7.8s), but within noise for CLI invocations.
4. **Backend choice is ecosystem-driven, not quality-driven** - for Deus, the deciding factors are auth flow, session management, tool support, and pricing rather than raw model quality.

## Limitations

- 5 cases is a small sample. Results are directional, not statistically significant.
- Single-turn QA only. Multi-turn conversation, tool use, and persona consistency were not tested.
- Judge (Gemma 4 local) may have its own biases. No human ground truth.
- Claude had access to project context (CLAUDE.md loaded by CLI); Codex ran in a fresh workspace. This may affect persona-heavy tasks but is unlikely to matter for factual QA.

## Reproduction

```bash
python3 eval/quality_bench.py --model gpt-5.4        # full run
python3 eval/quality_bench.py --smoke --model gpt-5.4 # single case
python3 eval/quality_bench.py --dry                    # show cases only
```
