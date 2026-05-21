#!/usr/bin/env python3
"""M1c benchmark — attention-dilution probe via pairwise blind judging.

Measures whether loading the standards-pack DILUTES Claude Sonnet's attention
on a real-world code-review task. Three budget arms, one frozen task (PR #402
diff), pairwise blind judging by Gemini, force-balanced order to eliminate
position bias at N=30.

  arm     format     budget   note
  ---     ------     ------   ----
  zero    None       0        no standards loaded (control)
  tight   name_desc  800      sub-saturation (drops 3 atoms)
  prod    name_desc  1500     current production budget (saturated at ~876 tok)

For each (trial, arm) tuple: run `claude -p --model sonnet --permission-mode
plan` with the arm's standards-text concatenated before the frozen task.
Then for each of the 3 unordered arm-pairs, ask Gemini "which response is
better" with force-balanced AB/BA ordering (15 each per pair at N=30).

Pre-registered hypotheses (decision gates for M3):
  H1: prod  win-rate vs zero  >= 20/30 (67%, one-sided binomial p=0.0494)
  H2: tight win-rate vs prod  within +/-10pp of 50% (non-inferiority)
  H3: tight win-rate vs zero  >= 20/30 (67%, p=0.0494)

H2 is the M3 go/no-go signal: if sub-saturation budget is non-inferior to
current production, name_only @ 800 can ship.

Results land at scripts/bench/results/attention_dilution_YYYY-MM-DD.json.

NOTE: Live-LLM tests are NOT CI-gated; only the deterministic unit tests
in scripts/tests/test_attention_dilution_probe.py run in CI.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import itertools
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

# Anchor every path to __file__ (M1a/M1b pattern — see standards_format_sweep.py
# and rule_following_judge.py:55-57).
_BENCH_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _BENCH_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SP_PATH = _SCRIPTS_DIR / "standards_pack.py"
_FIXTURES_DIR = _BENCH_DIR / "fixtures"
_DEFAULT_OUTPUT_DIR = _BENCH_DIR / "results"
_PAIRWISE_CACHE_PATH = _BENCH_DIR / "attention_dilution_pairwise_cache.json"
_RECALL_CACHE_PATH = _BENCH_DIR / "attention_dilution_recall_cache.json"

# Locked arm config — guarded by test_arms_locked_to_five.
ARMS: list[dict[str, Any]] = [
    {"name": "zero", "format": None, "budget": 0},
    {"name": "minimal", "format": "name_desc", "budget": 400},
    {"name": "tight", "format": "name_desc", "budget": 800},
    {"name": "prod", "format": "name_desc", "budget": 1500},
    {"name": "bloated", "format": "name_desc", "budget": 3000},
]

# Pre-registered binomial threshold (H1/H3/H4): 60/100 wins clears p<0.05
# one-sided. P(X>=60 | N=100, p=0.5) = 0.02844.
H1_H3_THRESHOLD_WINS = 60
H1_H3_THRESHOLD_N = 100

# Non-inferiority margin for H2 (tight vs prod): pairwise win-rate must land
# within +/-10pp of 50%.
H2_MARGIN_PP = 10

# H5/H6 recall-difference margin (H6 = (prod-bloated) recall must exceed 10pp).
H6_MARGIN = 0.10

# Position-bias correction threshold: residual AB/BA asymmetry above this
# flags the affected pair as inconclusive.
POSITION_BIAS_THRESHOLD_PP = 8

# Subprocess auth-failure detection (M1b precedent at
# rule_following_judge.py:222).
_AUTH_ERROR_MARKERS = ("not authenticated", "credentials", "login", "oauth")

# Pairwise blind judge prompt — single Gemini call per (trial, pair).
_JUDGE_PROMPT = """You are an expert code reviewer comparing two PR-review responses.

The task given to the assistant:
{task}

--- RESPONSE A ---
{response_a}

--- RESPONSE B ---
{response_b}

Which response is better on three criteria, weighted equally:
1. Correctness - flags real bugs, no hallucinated ones
2. Security - surfaces auth/permissions/data-leak risks
3. Edge cases - names specific inputs/states that break

Return JSON exactly: {{"winner": "A" | "B" | "TIE", "reasoning": "<one sentence>"}}
"""


# ---------------------------------------------------------------------------
# Standards-pack subprocess invocation
# ---------------------------------------------------------------------------


def _resolve_main_repo() -> Path:
    """Find the main (non-worktree) repo root via `git rev-parse --git-common-dir`.

    Worktree subprocesses inherit CLAUDE_PROJECT_DIR pointing at the
    worktree path. But standards_pack.py derives auto-memory from
    CLAUDE_PROJECT_DIR-encoded path (~/.claude/projects/<encoded>/memory),
    which won't exist for worktrees. The git common-dir resolves to the
    main repo's .git dir regardless of where we are.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = (_REPO_ROOT / common_dir).resolve()
            # Common dir is `<main_repo>/.git`; parent is the main repo.
            return common_dir.parent
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _REPO_ROOT


def _build_standards_text(arm: dict[str, Any], auto_mem_dir: Path | None) -> str:
    """Build the standards-text prefix for one arm.

    Zero-arm short-circuits to '' WITHOUT invoking standards_pack (test
    test_standards_text_for_zero_arm_is_empty enforces this contract).

    Other arms invoke `scripts/standards_pack.py` as a subprocess with
    DEUS_STANDARDS_FORMAT and DEUS_STANDARDS_TOKEN_BUDGET set per the arm.
    Mirrors rule_following_judge.py:155-214 pattern.

    When `auto_mem_dir` is None, sets CLAUDE_PROJECT_DIR in the subprocess
    env to the MAIN repo (resolved via git common-dir), so worktree-
    invocations still load production atoms — not a worktree-specific
    encoded path that doesn't exist. User-agnostic: no hardcoded paths.
    """
    if arm["budget"] == 0 or arm["format"] is None:
        return ""

    env = os.environ.copy()
    env["DEUS_STANDARDS_FORMAT"] = arm["format"]
    env["DEUS_STANDARDS_TOKEN_BUDGET"] = str(arm["budget"])
    if auto_mem_dir is not None:
        env["DEUS_AUTO_MEMORY_DIR"] = str(auto_mem_dir)
    else:
        # Override CLAUDE_PROJECT_DIR to the main repo so the
        # standards_pack.py resolver finds production atoms even when
        # this bench runs from a worktree.
        env["CLAUDE_PROJECT_DIR"] = str(_resolve_main_repo())

    result = subprocess.run(
        ["python3", str(_SP_PATH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        input="",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"standards_pack.py exited {result.returncode} for arm {arm['name']}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Frozen task — gh pr diff fetch
# ---------------------------------------------------------------------------


def _fetch_pr_diff(
    pr_number: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Fetch a merged PR diff via `gh pr diff <N>`. Raises on non-zero exit.

    `runner` is injectable for testability (default is subprocess.run).
    """
    result = runner(
        ["gh", "pr", "diff", str(pr_number)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr diff {pr_number} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# MUT subprocess wrapper (mirror of rule_following_judge.py:225-276)
# ---------------------------------------------------------------------------


def _run_mut(
    task_prompt: str,
    standards_text: str,
    *,
    model: str = "sonnet",
    timeout: int = 90,
) -> dict[str, Any]:
    """Invoke `claude -p` with standards-text prepended to the task prompt.

    Returns {ok, response, error, latency_s, prompt_len}.
    """
    if standards_text:
        full_prompt = f"{standards_text}\n\n---\n\n{task_prompt}"
    else:
        full_prompt = task_prompt

    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    full_prompt,
                    "--model",
                    model,
                    "--permission-mode",
                    "plan",
                    "--dangerously-skip-permissions",
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "response": "",
                "error": f"timeout after {timeout}s",
                "latency_s": time.time() - started,
                "prompt_len": len(full_prompt),
            }
    latency_s = time.time() - started
    stderr_low = (result.stderr or "").lower()
    if any(m in stderr_low for m in _AUTH_ERROR_MARKERS):
        return {
            "ok": False,
            "response": "",
            "error": f"auth_error: {result.stderr.strip()[:200]}",
            "latency_s": latency_s,
            "prompt_len": len(full_prompt),
        }
    if result.returncode != 0:
        return {
            "ok": False,
            "response": result.stdout or "",
            "error": f"exit {result.returncode}: {result.stderr.strip()[:200]}",
            "latency_s": latency_s,
            "prompt_len": len(full_prompt),
        }
    return {
        "ok": True,
        "response": result.stdout or "",
        "error": None,
        "latency_s": latency_s,
        "prompt_len": len(full_prompt),
    }


# ---------------------------------------------------------------------------
# Order assignment - force-balanced 15 AB + 15 BA per pair
# ---------------------------------------------------------------------------


def _assign_orders(n_trials: int) -> list[str]:
    """Return a force-balanced order list ['AB','AB',...,'BA','BA',...].

    Pre-registered: force-balanced (not random) to eliminate the 5-10pp
    sampling-noise that random 50/50 produces at N=30 (e.g. 11/19 by chance
    would swamp a 10pp dilution effect). N must be even.
    """
    if n_trials % 2 != 0:
        raise ValueError(
            f"n_trials must be even for force-balanced ordering, got {n_trials}"
        )
    half = n_trials // 2
    return ["AB"] * half + ["BA"] * half


# ---------------------------------------------------------------------------
# Pairwise judge - JSON parser + Gemini call
# ---------------------------------------------------------------------------


def _parse_pairwise_response(raw: str) -> dict[str, Any]:
    """Parse Gemini pairwise-judge JSON. Malformed -> {winner: 'TIE', ...}.

    Fallback rationale: graceful degradation. A malformed judge response
    counted as TIE (= 0.5 wins each) is far better than poisoning the run
    with an exception that loses partial results.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"winner": "TIE", "reasoning": "parse_error: invalid JSON"}
    winner = parsed.get("winner") if isinstance(parsed, dict) else None
    if winner not in ("A", "B", "TIE"):
        return {
            "winner": "TIE",
            "reasoning": f"parse_error: unknown winner {winner!r}",
        }
    return {
        "winner": winner,
        "reasoning": str(parsed.get("reasoning", ""))[:300],
    }


# ---------------------------------------------------------------------------
# Bug-recall judge — single-issue mention check
# ---------------------------------------------------------------------------


_RECALL_PROMPT = """You are evaluating whether a code-review response addresses a specific issue.

The response under evaluation:
{response}

The issue to check for:
TITLE: {issue_title}
DESCRIPTION: {issue_description}

Does the response mention or address this issue, even paraphrased? Be strict:
a vague gesture in the right area does NOT count. The response must explicitly
identify the issue or describe the same root cause.

Return JSON exactly: {{"mentions": true | false, "evidence": "<one-line quote from response, or empty>"}}
"""


def _parse_recall_response(raw: str) -> dict[str, Any]:
    """Parse Gemini recall-judge JSON. Malformed → {mentions: False, evidence: 'parse_error...'}."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"mentions": False, "evidence": "parse_error: invalid JSON"}
    if not isinstance(parsed, dict):
        return {"mentions": False, "evidence": "parse_error: not a dict"}
    mentions = parsed.get("mentions")
    if not isinstance(mentions, bool):
        return {"mentions": False, "evidence": f"parse_error: bad mentions={mentions!r}"}
    return {"mentions": mentions, "evidence": str(parsed.get("evidence", ""))[:300]}


def _load_rubric(path: Path) -> dict[str, Any]:
    """Load + validate the PR-rubric JSON fixture."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Rubric fixture not found: {path}")
    rubric = json.loads(path.read_text(encoding="utf-8"))
    if "issues" not in rubric or not isinstance(rubric["issues"], list):
        raise ValueError(f"Rubric missing 'issues' list: {path}")
    for issue in rubric["issues"]:
        for k in ("id", "title", "description", "category"):
            if k not in issue:
                raise ValueError(f"Rubric issue missing '{k}': {issue}")
        if issue["category"] not in ("correctness", "security", "edge_case", "style"):
            raise ValueError(
                f"Rubric issue has unknown category: {issue['category']!r}"
            )
    return rubric


def _judge_recall(
    client,
    gen_models: list[str],
    response: str,
    issue: dict[str, Any],
    genai_types,
    cache: dict[str, dict[str, Any]],
    exhausted: set[str],
    *,
    cache_key: str,
) -> dict[str, Any]:
    """One bug-recall judge call. Same GEN_MODELS chain pattern as _judge_pair."""
    if cache_key in cache:
        return cache[cache_key]

    prompt = _RECALL_PROMPT.format(
        response=response[:3000],
        issue_title=issue["title"],
        issue_description=issue["description"],
    )

    last_err = None
    for model in gen_models:
        if model in exhausted:
            continue
        try:
            api_response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=200,
                    response_mime_type="application/json",
                ),
            )
        except Exception as e:
            msg = str(e)
            last_err = msg
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if "PerDay" in msg:
                    exhausted.add(model)
                else:
                    delay_match = re.search(r"retryDelay.*?(\d+)", msg)
                    if delay_match:
                        time.sleep(int(delay_match.group(1)))
                continue
            continue
        raw = (api_response.text or "").strip()
        parsed = _parse_recall_response(raw)
        cache[cache_key] = parsed
        return parsed

    fallback = {"mentions": False, "evidence": f"all_models_exhausted: {last_err}"[:300]}
    cache[cache_key] = fallback
    return fallback


# ---------------------------------------------------------------------------
# Pre-commit padding-leak audit
# ---------------------------------------------------------------------------


def _scan_for_padding_leaks(
    padding_dir: Path,
    check_paths: list[Path],
) -> list[str]:
    """Scan check_paths for padding atom filenames or content prefixes.

    Returns a list of leak descriptions (empty if clean). For each padding atom:
      1. Filename match: grep basename across check_paths.
      2. Content prefix match: sha256 isn't directly searchable, so instead
         take first 100 bytes of body-after-frontmatter and grep that exact
         substring. Catches verbatim quotes of padding prose.
    """
    padding_dir = Path(padding_dir)
    leaks: list[str] = []
    if not padding_dir.exists():
        return leaks

    for atom in padding_dir.rglob("*.md"):
        atom_name = atom.name
        try:
            atom_text = atom.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Extract body prefix (after the second '---' divider).
        parts = atom_text.split("---", 2)
        body_prefix = parts[2][:100].strip() if len(parts) >= 3 else atom_text[:100]

        for check_path in check_paths:
            check_path = Path(check_path)
            if not check_path.exists():
                continue
            targets: list[Path] = []
            if check_path.is_file():
                targets = [check_path]
            elif check_path.is_dir():
                targets = list(check_path.rglob("*"))
            for target in targets:
                if not target.is_file():
                    continue
                try:
                    content = target.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if atom_name in content:
                    leaks.append(f"filename:{atom_name} in {target}")
                # Content prefix check: only if prefix is substantial (avoid common phrases).
                if len(body_prefix) >= 30 and body_prefix in content:
                    leaks.append(
                        f"content_prefix:{atom_name} ({body_prefix[:40]}...) in {target}"
                    )
    return leaks


# ---------------------------------------------------------------------------
# H5/H6 — bug-recall hypothesis evaluators with bootstrap CI
# ---------------------------------------------------------------------------


def _bootstrap_recall_diff_ci(
    recall_a: list[int],
    recall_b: list[int],
    *,
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap 95% CI on (mean(a) - mean(b)) for Bernoulli observations.

    Each list is 0/1 mention flags per (trial, issue). Resampling is paired
    over indices: re-sample N indices with replacement, take mean of a and b
    at those indices, compute the difference. Repeat n_resamples times.
    """
    if not recall_a or not recall_b or len(recall_a) != len(recall_b):
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(recall_a)
    diffs: list[float] = []
    for _ in range(n_resamples):
        idxs = [rng.randrange(n) for _ in range(n)]
        mean_a = sum(recall_a[i] for i in idxs) / n
        mean_b = sum(recall_b[i] for i in idxs) / n
        diffs.append(mean_a - mean_b)
    diffs.sort()
    lo = diffs[int(n_resamples * (alpha / 2))]
    hi = diffs[int(n_resamples * (1 - alpha / 2))]
    return (lo, hi)


def _collect_recall_pair(
    raw_recall: list[dict[str, Any]], arm_a: str, arm_b: str
) -> tuple[list[int], list[int]]:
    """Build paired-by-(trial,issue) 0/1 lists for two arms."""
    by_a: dict[tuple[int, str], int] = {}
    by_b: dict[tuple[int, str], int] = {}
    for r in raw_recall:
        key = (r["trial"], r["issue_id"])
        flag = 1 if r["mentions"] else 0
        if r["arm"] == arm_a:
            by_a[key] = flag
        elif r["arm"] == arm_b:
            by_b[key] = flag
    common = sorted(set(by_a.keys()) & set(by_b.keys()))
    return [by_a[k] for k in common], [by_b[k] for k in common]


def _evaluate_h5(
    raw_recall: list[dict[str, Any]],
    *,
    prod_arm: str = "prod",
    zero_arm: str = "zero",
) -> dict[str, Any]:
    """H5: bug-recall(prod) > bug-recall(zero) AND bootstrap 95% CI excludes 0."""
    recall_prod, recall_zero = _collect_recall_pair(raw_recall, prod_arm, zero_arm)
    if not recall_prod:
        return {"verdict": "FAIL", "reason": "no data", "ci95": [0.0, 0.0]}
    mean_prod = sum(recall_prod) / len(recall_prod)
    mean_zero = sum(recall_zero) / len(recall_zero)
    diff = mean_prod - mean_zero
    ci_lo, ci_hi = _bootstrap_recall_diff_ci(recall_prod, recall_zero)
    verdict = "PASS" if (diff > 0 and ci_lo > 0) else "FAIL"
    return {
        "verdict": verdict,
        "recall_prod": mean_prod,
        "recall_zero": mean_zero,
        "diff": diff,
        "ci95": [ci_lo, ci_hi],
    }


def _evaluate_h6(
    raw_recall: list[dict[str, Any]],
    *,
    prod_arm: str = "prod",
    bloated_arm: str = "bloated",
) -> dict[str, Any]:
    """H6: bug-recall(bloated) < bug-recall(prod) − 10pp AND CI lower > 10pp."""
    recall_prod, recall_bloated = _collect_recall_pair(
        raw_recall, prod_arm, bloated_arm
    )
    if not recall_prod:
        return {"verdict": "FAIL", "reason": "no data", "ci95": [0.0, 0.0]}
    mean_prod = sum(recall_prod) / len(recall_prod)
    mean_bloated = sum(recall_bloated) / len(recall_bloated)
    diff = mean_prod - mean_bloated
    ci_lo, ci_hi = _bootstrap_recall_diff_ci(recall_prod, recall_bloated)
    verdict = "PASS" if (diff > H6_MARGIN and ci_lo > H6_MARGIN) else "FAIL"
    return {
        "verdict": verdict,
        "recall_prod": mean_prod,
        "recall_bloated": mean_bloated,
        "diff": diff,
        "margin": H6_MARGIN,
        "ci95": [ci_lo, ci_hi],
    }


def _judge_pair(
    client,
    gen_models: list[str],
    task: str,
    a_response: str,
    b_response: str,
    genai_types,
    cache: dict[str, dict[str, Any]],
    exhausted: set[str],
    *,
    cache_key: str,
) -> dict[str, Any]:
    """One pairwise judge call. Mirrors rule_following_judge.py:319-398.

    Iterates GEN_MODELS until one succeeds; on 429 RESOURCE_EXHAUSTED
    adds model to `exhausted` (if PerDay) or sleeps + retries. Cache hits
    short-circuit.
    """
    if cache_key in cache:
        return cache[cache_key]

    prompt = _JUDGE_PROMPT.format(
        task=task, response_a=a_response, response_b=b_response
    )

    last_err = None
    for model in gen_models:
        if model in exhausted:
            continue
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=300,
                    response_mime_type="application/json",
                ),
            )
        except Exception as e:
            msg = str(e)
            last_err = msg
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if "PerDay" in msg:
                    exhausted.add(model)
                else:
                    delay_match = re.search(r"retryDelay.*?(\d+)", msg)
                    if delay_match:
                        time.sleep(int(delay_match.group(1)))
                continue
            continue
        raw = (response.text or "").strip()
        parsed = _parse_pairwise_response(raw)
        cache[cache_key] = parsed
        return parsed

    # All models exhausted.
    fallback = {
        "winner": "TIE",
        "reasoning": f"all_models_exhausted: {last_err}"[:300],
    }
    cache[cache_key] = fallback
    return fallback


# ---------------------------------------------------------------------------
# Cache I/O (mirrors rule_following_judge.py:511-528)
# ---------------------------------------------------------------------------


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _cache_key(
    trial: int, pair: str, order: str, a_response: str, b_response: str
) -> str:
    raw = f"{trial}|||{pair}|||{order}|||{a_response[:200]}|||{b_response[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Aggregation - win-matrix + per-arm + pairwise + hypotheses
# ---------------------------------------------------------------------------


def _bootstrap_win_rate_ci(
    win_record: list[float],
    *,
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap 95% CI on a list of per-trial win values (1.0/0.5/0.0).

    Reuses the resampling shell from rule_following_judge.py:427-441; inner
    statistic is the simple mean (binary win-rate), not a CSR delta.
    Copying the M1b inner delta math would silently produce wrong CIs.
    """
    if not win_record:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(win_record)
    boot_means: list[float] = []
    for _ in range(n_resamples):
        sample = [win_record[rng.randrange(n)] for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    lo = boot_means[int(n_resamples * (alpha / 2))]
    hi = boot_means[int(n_resamples * (1 - alpha / 2))]
    return (lo, hi)


def _aggregate(
    raw_judge: list[dict[str, Any]], arm_names: list[str]
) -> dict[str, Any]:
    """Win-matrix -> per-arm win-rate + pairwise CIs + hypothesis verdicts."""
    # Per-arm tally
    per_arm: dict[str, dict[str, Any]] = {
        a: {"wins": 0, "ties": 0, "losses": 0} for a in arm_names
    }
    # Pairwise tally (key = sorted "X__vs__Y")
    pair_records: dict[str, list[tuple[str, float, str]]] = {}
    # Position-bias tally
    position_records: dict[str, list[float]] = {"AB": [], "BA": []}

    for r in raw_judge:
        a_arm = r["a_arm"]
        b_arm = r["b_arm"]
        winner = r["winner"]
        order = r["order"]
        # Determine which arm won (or tied)
        if winner == "A":
            per_arm[a_arm]["wins"] += 1
            per_arm[b_arm]["losses"] += 1
            a_score = 1.0
        elif winner == "B":
            per_arm[b_arm]["wins"] += 1
            per_arm[a_arm]["losses"] += 1
            a_score = 0.0
        else:  # TIE
            per_arm[a_arm]["ties"] += 1
            per_arm[b_arm]["ties"] += 1
            a_score = 0.5
        position_records[order].append(a_score)

        # Pair key (sorted for stability across trials with different orders)
        pair_key = r["pair"]
        # Track winner from FIRST arm's perspective using sorted arms
        # (so zero__vs__tight always means "did zero win?")
        sorted_arms = sorted([a_arm, b_arm])
        first_arm = sorted_arms[0]
        if winner == "TIE":
            first_score = 0.5
        elif (winner == "A" and a_arm == first_arm) or (
            winner == "B" and b_arm == first_arm
        ):
            first_score = 1.0
        else:
            first_score = 0.0
        pair_records.setdefault(pair_key, []).append(
            (first_arm, first_score, order)
        )

    # Compute per-arm win_rate
    for arm in arm_names:
        wins = per_arm[arm]["wins"]
        ties = per_arm[arm]["ties"]
        losses = per_arm[arm]["losses"]
        games = wins + ties + losses
        per_arm[arm]["win_rate"] = (
            (wins + 0.5 * ties) / games if games else 0.0
        )

    # Compute pairwise CIs + first_arm win-rate
    pairwise: dict[str, dict[str, Any]] = {}
    for pair_key, records in pair_records.items():
        scores = [s for _, s, _ in records]
        first_arm_wins = sum(1 for s in scores if s == 1.0)
        ties_p = sum(1 for s in scores if s == 0.5)
        first_arm_losses = sum(1 for s in scores if s == 0.0)
        first_arm = records[0][0]
        win_rate_first = sum(scores) / len(scores) if scores else 0.0
        ci_lo, ci_hi = _bootstrap_win_rate_ci(scores)
        pairwise[pair_key] = {
            "first_arm": first_arm,
            "first_arm_wins": first_arm_wins,
            "ties": ties_p,
            "first_arm_losses": first_arm_losses,
            "win_rate_first_arm": win_rate_first,
            "ci95": [ci_lo, ci_hi],
            "n": len(scores),
        }

    # Position-bias
    ab_scores = position_records["AB"]
    ba_scores = position_records["BA"]
    ab_a_win = sum(ab_scores) / len(ab_scores) if ab_scores else 0.5
    ba_a_win = sum(ba_scores) / len(ba_scores) if ba_scores else 0.5
    asymmetry_pp = abs(ab_a_win - ba_a_win) * 100.0
    position_bias = {
        "order_AB_a_win_rate": ab_a_win,
        "order_BA_a_win_rate": ba_a_win,
        "asymmetry_pp": asymmetry_pp,
        "inconclusive": asymmetry_pp > POSITION_BIAS_THRESHOLD_PP,
    }

    # Hypothesis verdicts
    hypotheses = _compute_hypotheses(per_arm, pair_records, arm_names)

    return {
        "per_arm": per_arm,
        "pairwise": pairwise,
        "position_bias": position_bias,
        "hypotheses": hypotheses,
    }


def _compute_hypotheses(
    per_arm: dict[str, dict[str, Any]],
    pair_records: dict[str, list[tuple[str, float, str]]],
    arm_names: list[str],
) -> dict[str, str]:
    """Apply the pre-registered H1/H2/H3/H4 decision gates.

    All thresholds at N=100: 60/100 wins clears one-sided binomial p<0.05.
    H1: prod-wins-vs-zero >= 60/100.
    H2: tight win-rate vs prod within +/-10pp of 50% (non-inferiority).
    H3: tight-wins-vs-zero >= 60/100 (same threshold as H1).
    H4: prod-wins-vs-bloated >= 60/100 (bloated is statistically WORSE).

    H5 and H6 (recall-based) are computed OUTSIDE this function because they
    need per-(trial, issue) Bernoulli observations from raw_recall, not the
    pair-records aggregate. main() fills them in via _evaluate_h5/_h6.
    """

    def _arm_wins_in_pair(pair_key: str, arm: str) -> int:
        records = pair_records.get(pair_key, [])
        if not records:
            return 0
        wins = 0
        for first_arm, score, _order in records:
            if first_arm == arm and score == 1.0:
                wins += 1
            elif first_arm != arm and score == 0.0:
                # Other arm scored as first; second arm (which IS `arm`) won
                wins += 1
        return wins

    # H1: prod beats zero >= 20/30
    h1_pair = "zero__vs__prod"
    prod_wins = _arm_wins_in_pair(h1_pair, "prod")
    h1_n = len(pair_records.get(h1_pair, []))
    h1_pass = (
        prod_wins >= H1_H3_THRESHOLD_WINS and h1_n >= H1_H3_THRESHOLD_N
    )

    # H3: tight beats zero >= threshold/N
    h3_pair = "zero__vs__tight"
    tight_wins_vs_zero = _arm_wins_in_pair(h3_pair, "tight")
    h3_n = len(pair_records.get(h3_pair, []))
    h3_pass = (
        tight_wins_vs_zero >= H1_H3_THRESHOLD_WINS and h3_n >= H1_H3_THRESHOLD_N
    )

    # H4 (v2 NEW): bloated worse than prod — prod beats bloated >= threshold/N.
    # Pair key follows ARMS order (prod comes before bloated): "prod__vs__bloated".
    h4_pair = "prod__vs__bloated"
    prod_wins_vs_bloated = _arm_wins_in_pair(h4_pair, "prod")
    h4_n = len(pair_records.get(h4_pair, []))
    h4_pass = (
        prod_wins_vs_bloated >= H1_H3_THRESHOLD_WINS and h4_n >= H1_H3_THRESHOLD_N
    )

    # H2: tight vs prod within +/-10pp of 50%
    h2_pair = "tight__vs__prod"
    h2_records = pair_records.get(h2_pair, [])
    if h2_records:
        # win-rate of tight (arbitrary choice — symmetric metric). Flip
        # the score when first_arm is prod: a prod-win (1.0) means tight
        # lost (0.0). 1.0 - 0.5 == 0.5, so tie maps to tie automatically.
        tight_score = 0.0
        for first_arm, score, _order in h2_records:
            if first_arm == "tight":
                tight_score += score
            else:
                tight_score += 1.0 - score
        tight_win_rate = tight_score / len(h2_records)
        h2_pass = abs(tight_win_rate - 0.5) * 100.0 <= H2_MARGIN_PP
    else:
        h2_pass = False

    return {
        "H1": "PASS" if h1_pass else "FAIL",
        "H2": "PASS" if h2_pass else "FAIL",
        "H3": "PASS" if h3_pass else "FAIL",
        "H4": "PASS" if h4_pass else "FAIL",
        # H5/H6 are filled in by main() from raw_recall data (computed
        # outside this function because they need per-trial-per-issue
        # Bernoulli observations, not pair_records aggregates).
    }


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _load_gemini():
    """Lazy-import Gemini SDK + load API key + GEN_MODELS chain."""
    sys.path.insert(0, str(_REPO_ROOT))
    from evolution.config import GEN_MODELS, load_api_key

    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=load_api_key())
    return client, list(GEN_MODELS), genai_types


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _stub_judge() -> dict[str, Any]:
    """Deterministic stub used by --dry-run."""
    return {"winner": "A", "reasoning": "stub: dry-run mode"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--trials", type=int, default=100,
        help="trials per arm (default 100, must be even)",
    )
    parser.add_argument(
        "--pr", type=int, default=402, help="PR number for frozen task diff",
    )
    parser.add_argument(
        "--max-workers", type=int, default=8,
        help="MUT parallelism (default 8; --safe-serial forces 1)",
    )
    parser.add_argument(
        "--judge-workers", type=int, default=8,
        help="Gemini judge parallelism (default 8)",
    )
    parser.add_argument("--safe-serial", action="store_true", help="force MUT max_workers=1")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="stub Gemini judge; verify MUT + plumbing only",
    )
    parser.add_argument(
        "--pilot", action="store_true",
        help="N=10 fast pilot to catch auth + JSON parse issues",
    )
    parser.add_argument("--mut-timeout", type=int, default=90)
    parser.add_argument(
        "--auto-mem-dir", type=str, default=None,
        help="override DEUS_AUTO_MEMORY_DIR for default arms",
    )
    parser.add_argument(
        "--padding-dir", type=str, default=None,
        help="auto-mem dir for the bloated arm (prod atoms + kind-flipped padding). "
        "If omitted, bloated arm degenerates to prod (same content).",
    )
    parser.add_argument(
        "--rubric", type=str,
        default=str(_FIXTURES_DIR / "pr402_rubric.json"),
        help="Path to PR rubric JSON for bug-recall scoring.",
    )
    parser.add_argument(
        "--skip-recall", action="store_true",
        help="Skip the bug-recall judge pass (H5/H6 unevaluated).",
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(_DEFAULT_OUTPUT_DIR),
    )
    args = parser.parse_args(argv)

    if args.pilot:
        args.trials = 10

    if args.trials % 2 != 0:
        print(
            f"ERROR: --trials must be even (got {args.trials}) for force-balanced "
            "AB/BA ordering. Use --trials 100 (default) or another even value.",
            file=sys.stderr,
        )
        return 2

    auto_mem_dir = Path(args.auto_mem_dir) if args.auto_mem_dir else None
    padding_dir = Path(args.padding_dir) if args.padding_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Per-arm auto-mem-dir map: bloated uses padding_dir, others use auto_mem_dir.
    arm_auto_mem_dir: dict[str, Path | None] = {a["name"]: auto_mem_dir for a in ARMS}
    if padding_dir is not None:
        arm_auto_mem_dir["bloated"] = padding_dir

    # Load rubric (unless skipped).
    rubric: dict[str, Any] | None = None
    if not args.skip_recall:
        try:
            rubric = _load_rubric(Path(args.rubric))
            print(
                f"[m1c] rubric loaded: {len(rubric['issues'])} issues from {args.rubric}",
                file=sys.stderr,
            )
        except (FileNotFoundError, ValueError) as e:
            print(
                f"[m1c] WARNING: rubric load failed ({e}); recall pass disabled.",
                file=sys.stderr,
            )
            rubric = None

    print(f"[m1c] fetching PR #{args.pr} diff ...", file=sys.stderr)
    diff_text = _fetch_pr_diff(args.pr)
    diff_line_count = diff_text.count("\n")
    print(
        f"[m1c]   diff: {diff_line_count} lines, {len(diff_text)} bytes",
        file=sys.stderr,
    )
    task_prompt = (
        "Review this pull-request diff for correctness, security, and edge cases. "
        "Output exactly 3-5 bullet points, one per issue. Be specific - cite the "
        "line you'd change.\n\n--- DIFF ---\n"
        + diff_text
    )

    # ----- MUT pass: trials x arms -----
    print(
        f"[m1c] MUT pass: {args.trials} trials x {len(ARMS)} arms = "
        f"{args.trials * len(ARMS)} calls",
        file=sys.stderr,
    )
    standards_by_arm: dict[str, str] = {}
    for arm in ARMS:
        per_arm_mem_dir = arm_auto_mem_dir[arm["name"]]
        standards_by_arm[arm["name"]] = _build_standards_text(arm, per_arm_mem_dir)
        print(
            f"[m1c]   arm={arm['name']}: standards_text="
            f"{len(standards_by_arm[arm['name']])} bytes "
            f"(auto_mem_dir={per_arm_mem_dir})",
            file=sys.stderr,
        )

    workers = 1 if args.safe_serial else max(1, args.max_workers)
    mut_tasks = []
    for trial in range(args.trials):
        for arm in ARMS:
            mut_tasks.append((trial, arm["name"]))

    raw_mut: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_meta = {
            ex.submit(
                _run_mut,
                task_prompt,
                standards_by_arm[arm_name],
                timeout=args.mut_timeout,
            ): (trial, arm_name)
            for (trial, arm_name) in mut_tasks
        }
        for fut in as_completed(future_to_meta):
            trial, arm_name = future_to_meta[fut]
            result = fut.result()
            record = {
                "trial": trial,
                "arm": arm_name,
                "response": result["response"],
                "ok": result["ok"],
                "error": result.get("error"),
                "latency_s": result["latency_s"],
                "prompt_len": result["prompt_len"],
            }
            raw_mut.append(record)
            if not result["ok"] and result.get("error", "").startswith("auth_error"):
                print(
                    f"[m1c] AUTH FAIL: {result['error']!r} — shutting down. "
                    "Run `claude auth status` and retry with --safe-serial.",
                    file=sys.stderr,
                )
                ex.shutdown(wait=False, cancel_futures=True)
                return 3

    # Sort by (trial, arm) for stable downstream iteration
    raw_mut.sort(key=lambda r: (r["trial"], r["arm"]))

    # Index for quick pairwise pickup
    mut_by_trial_arm: dict[tuple[int, str], dict[str, Any]] = {
        (r["trial"], r["arm"]): r for r in raw_mut
    }

    # ----- Judge pass: trials x 3 pairs -----
    arm_names = [a["name"] for a in ARMS]
    pairs = list(itertools.combinations(arm_names, 2))
    order_assignment_by_pair = {p: _assign_orders(args.trials) for p in pairs}

    if args.dry_run:
        client, gen_models, genai_types = (None, ["stub"], None)
        cache: dict[str, dict[str, Any]] = {}
        exhausted: set[str] = set()
    else:
        client, gen_models, genai_types = _load_gemini()
        cache = _load_cache(_PAIRWISE_CACHE_PATH)
        exhausted = set()

    print(
        f"[m1c] Pairwise pass: {args.trials} trials x {len(pairs)} pairs = "
        f"{args.trials * len(pairs)} judge calls "
        f"({'8-way parallel' if not args.dry_run else 'stub'})",
        file=sys.stderr,
    )
    # Build the full task list.
    # Note on failed MUT calls: when claude -p times out or auth-fails, the
    # record is still kept in raw_mut with response="" + ok=False. The pairwise
    # judge then sees "" vs <real response>, which Gemini typically scores as a
    # win for the non-empty side (NOT as TIE). This biases failed-arm win-rates
    # downward — a known confound when MUT failure rates are non-trivial.
    # Documented in the results JSON limitations section.
    judge_tasks: list[dict[str, Any]] = []
    for arm_a_name, arm_b_name in pairs:
        pair_key = f"{arm_a_name}__vs__{arm_b_name}"
        orders = order_assignment_by_pair[(arm_a_name, arm_b_name)]
        for trial in range(args.trials):
            order = orders[trial]
            if order == "AB":
                first_arm, second_arm = arm_a_name, arm_b_name
            else:
                first_arm, second_arm = arm_b_name, arm_a_name
            first_response = mut_by_trial_arm[(trial, first_arm)]["response"]
            second_response = mut_by_trial_arm[(trial, second_arm)]["response"]
            ck = _cache_key(trial, pair_key, order, first_response, second_response)
            judge_tasks.append({
                "trial": trial, "pair": pair_key, "order": order,
                "first_arm": first_arm, "second_arm": second_arm,
                "first_response": first_response, "second_response": second_response,
                "cache_key": ck,
            })

    def _run_one_pairwise(task: dict[str, Any]) -> dict[str, Any]:
        if args.dry_run:
            parsed = _stub_judge()
        else:
            parsed = _judge_pair(
                client, gen_models, task_prompt,
                task["first_response"], task["second_response"],
                genai_types, cache, exhausted, cache_key=task["cache_key"],
            )
        return {
            "trial": task["trial"], "pair": task["pair"], "order": task["order"],
            "a_arm": task["first_arm"], "b_arm": task["second_arm"],
            "winner": parsed["winner"], "reasoning": parsed["reasoning"],
        }

    raw_judge: list[dict[str, Any]] = []
    judge_workers = 1 if args.dry_run else max(1, args.judge_workers)
    completed = 0
    with ThreadPoolExecutor(max_workers=judge_workers) as ex:
        futures = [ex.submit(_run_one_pairwise, t) for t in judge_tasks]
        for fut in as_completed(futures):
            raw_judge.append(fut.result())
            completed += 1
            if completed % 50 == 0 and not args.dry_run:
                _save_cache(_PAIRWISE_CACHE_PATH, cache)
                print(
                    f"[m1c]   pairwise progress: {completed}/{len(judge_tasks)}",
                    file=sys.stderr,
                )
            if exhausted and len(exhausted) == len(gen_models):
                print(
                    "[m1c] All judge models exhausted — cancelling remaining.",
                    file=sys.stderr,
                )
                ex.shutdown(wait=False, cancel_futures=True)
                break
    if not args.dry_run:
        _save_cache(_PAIRWISE_CACHE_PATH, cache)

    # ----- Recall pass: arms x trials x issues -----
    raw_recall: list[dict[str, Any]] = []
    h5_result: dict[str, Any] | None = None
    h6_result: dict[str, Any] | None = None
    if rubric is not None and not args.dry_run:
        recall_cache = _load_cache(_RECALL_CACHE_PATH)
        issues = rubric["issues"]
        recall_tasks = []
        for trial in range(args.trials):
            for arm_name in arm_names:
                response = mut_by_trial_arm[(trial, arm_name)]["response"]
                for issue in issues:
                    raw_key = f"{response[:500]}|||{issue['id']}"
                    ck_r = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
                    recall_tasks.append({
                        "trial": trial, "arm": arm_name,
                        "response": response, "issue": issue, "cache_key": ck_r,
                    })
        print(
            f"[m1c] Recall pass: {len(recall_tasks)} calls "
            f"({len(arm_names)} arms x {args.trials} trials x {len(issues)} issues)",
            file=sys.stderr,
        )

        def _run_one_recall(task: dict[str, Any]) -> dict[str, Any]:
            parsed = _judge_recall(
                client, gen_models, task["response"], task["issue"],
                genai_types, recall_cache, exhausted, cache_key=task["cache_key"],
            )
            return {
                "trial": task["trial"], "arm": task["arm"],
                "issue_id": task["issue"]["id"], "category": task["issue"]["category"],
                "mentions": parsed["mentions"], "evidence": parsed["evidence"],
            }

        completed_r = 0
        with ThreadPoolExecutor(max_workers=judge_workers) as ex:
            futures = [ex.submit(_run_one_recall, t) for t in recall_tasks]
            for fut in as_completed(futures):
                raw_recall.append(fut.result())
                completed_r += 1
                if completed_r % 200 == 0:
                    _save_cache(_RECALL_CACHE_PATH, recall_cache)
                    print(
                        f"[m1c]   recall progress: {completed_r}/{len(recall_tasks)}",
                        file=sys.stderr,
                    )
                if exhausted and len(exhausted) == len(gen_models):
                    print(
                        "[m1c] All judge models exhausted in recall — cancelling.",
                        file=sys.stderr,
                    )
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
        _save_cache(_RECALL_CACHE_PATH, recall_cache)
        h5_result = _evaluate_h5(raw_recall)
        h6_result = _evaluate_h6(raw_recall)

    # ----- Aggregate -----
    aggregate = _aggregate(raw_judge, arm_names)
    if h5_result is not None:
        aggregate["hypotheses"]["H5"] = h5_result["verdict"]
        aggregate["h5_detail"] = h5_result
    if h6_result is not None:
        aggregate["hypotheses"]["H6"] = h6_result["verdict"]
        aggregate["h6_detail"] = h6_result
    # Per-arm recall %
    if raw_recall:
        recall_by_arm: dict[str, list[int]] = {a: [] for a in arm_names}
        for r in raw_recall:
            recall_by_arm[r["arm"]].append(1 if r["mentions"] else 0)
        for arm in arm_names:
            samples = recall_by_arm[arm]
            aggregate["per_arm"][arm]["bug_recall_pct"] = (
                sum(samples) / len(samples) if samples else 0.0
            )
    # Per-arm mean latency + tokens
    for arm in arm_names:
        arm_records = [r for r in raw_mut if r["arm"] == arm]
        if arm_records:
            aggregate["per_arm"][arm]["mean_latency_s"] = (
                sum(r["latency_s"] for r in arm_records) / len(arm_records)
            )
            aggregate["per_arm"][arm]["mean_prompt_len"] = (
                sum(r["prompt_len"] for r in arm_records) / len(arm_records)
            )

    # ----- Emit JSON -----
    today = datetime.date.today().isoformat()
    out_path = output_dir / f"attention_dilution_{today}.json"
    payload = {
        "run_date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "frozen_task": {
            "source": f"gh pr diff {args.pr}",
            "diff_line_count": diff_line_count,
            "task_prompt_head": task_prompt[:500],
        },
        "rubric_path": str(args.rubric) if rubric else None,
        "n_trials": args.trials,
        "arms": ARMS,
        "mut_model": "sonnet",
        "judge_models": gen_models,
        "padding": {
            "padding_dir": str(padding_dir) if padding_dir else None,
            "applied_to_arms": ["bloated"] if padding_dir else [],
        },
        "raw_mut": raw_mut,
        "raw_judge": raw_judge,
        "raw_recall": raw_recall,
        "aggregate": aggregate,
        "limitations": {
            "criterion_anchoring": (
                "Judge prompt names 'correctness/security/edge cases' as "
                "weighted criteria. Standards-packed arms may surface more "
                "security-flagged content (e.g. atoms about credentials or "
                "auth), so the security criterion can favor verbose arms "
                "independently of actual reviewer utility. Treat criterion "
                "weighting as a known confound when interpreting close "
                "results (e.g. H2 boundary cases)."
            ),
            "cross_platform": "macOS/Linux only (claude -p subprocess pattern).",
            "failed_mut_bias": (
                "Failed MUT calls (timeout / auth) yield response=\"\" but are "
                "still scored by the pairwise judge. Gemini typically picks the "
                "non-empty response as winner, biasing failed-arm win-rates "
                "downward. Mitigation: raise --mut-timeout and/or lower "
                "--max-workers if per-arm OK rate < 90%."
            ),
        },
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[m1c] wrote {out_path}", file=sys.stderr)

    # Console summary
    print("\n=== M1c verdict ===", file=sys.stderr)
    for arm in arm_names:
        pa = aggregate["per_arm"][arm]
        recall_str = (
            f"  recall={pa['bug_recall_pct']:.3f}" if "bug_recall_pct" in pa else ""
        )
        latency_str = (
            f"  lat={pa['mean_latency_s']:.1f}s" if "mean_latency_s" in pa else ""
        )
        print(
            f"  {arm:8s}: win-rate={pa['win_rate']:.3f}  "
            f"(W={pa['wins']} T={pa['ties']} L={pa['losses']}){recall_str}{latency_str}",
            file=sys.stderr,
        )
    for h in ("H1", "H2", "H3", "H4", "H5", "H6"):
        v = aggregate["hypotheses"].get(h, "n/a")
        print(f"  {h}: {v}", file=sys.stderr)
    if h5_result:
        print(
            f"  H5 detail: prod={h5_result['recall_prod']:.3f} "
            f"zero={h5_result['recall_zero']:.3f} "
            f"diff={h5_result['diff']:+.3f} "
            f"CI95=[{h5_result['ci95'][0]:+.3f}, {h5_result['ci95'][1]:+.3f}]",
            file=sys.stderr,
        )
    if h6_result:
        print(
            f"  H6 detail: prod={h6_result['recall_prod']:.3f} "
            f"bloated={h6_result['recall_bloated']:.3f} "
            f"diff={h6_result['diff']:+.3f} "
            f"CI95=[{h6_result['ci95'][0]:+.3f}, {h6_result['ci95'][1]:+.3f}]",
            file=sys.stderr,
        )
    pb = aggregate["position_bias"]
    print(
        f"  position_bias: AB_a_win={pb['order_AB_a_win_rate']:.3f}  "
        f"BA_a_win={pb['order_BA_a_win_rate']:.3f}  "
        f"asymmetry={pb['asymmetry_pp']:.1f}pp"
        f"  {'(INCONCLUSIVE)' if pb['inconclusive'] else ''}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
