import importlib.util
import re
import sys
import time
from pathlib import Path

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_MB_PATH = _SCRIPTS_DIR / "memory_benchmark.py"


def _load_mb():
    if "memory_benchmark" in sys.modules:
        return sys.modules["memory_benchmark"]
    spec = importlib.util.spec_from_file_location("memory_benchmark", _MB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


def run_claude_md_hygiene() -> dict:
    mb = _load_mb()
    vault_root = mb._load_vault_root()
    claude_md = (
        (vault_root / "CLAUDE.md")
        if vault_root
        else Path(__file__).resolve().parent.parent.parent.parent / "CLAUDE.md"
    )

    pending_items = 0
    all_checkbox_format = True
    pending_issues: list[str] = []

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        pending_section = re.search(
            r"(?i)#+\s*(pending|todo|backlog).*?\n(.*?)(?=\n#+|\Z)",
            content,
            re.DOTALL,
        )
        if pending_section:
            section_body = pending_section.group(2)
            items = re.findall(r"^\s*-\s+(.+)$", section_body, re.MULTILINE)
            pending_items = len(items)
            for item in items:
                if not re.match(r"\[[ x]\]", item):
                    all_checkbox_format = False
                    pending_issues.append(item[:60])

    return {
        "items": pending_items,
        "within_limit": pending_items <= 10,
        "all_checkbox_format": all_checkbox_format,
        "issues": pending_issues[:3],
    }


@register("hygiene")
def run_hygiene(argv: list[str]) -> RunResult:
    t_start = time.monotonic()
    pa = run_claude_md_hygiene()
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    within_limit = pa["within_limit"]
    all_checkbox = pa["all_checkbox_format"]
    both_passed = within_limit and all_checkbox

    cases = [
        CaseResult(
            case_id="pending_items_within_limit",
            score=1.0 if within_limit else 0.0,
            passed=within_limit,
            meta={"items": pa["items"]},
        ),
        CaseResult(
            case_id="pending_all_checkbox_format",
            score=1.0 if all_checkbox else 0.0,
            passed=all_checkbox,
            meta={"issues": pa["issues"]},
        ),
    ]

    return RunResult(
        suite="hygiene",
        score=1.0 if both_passed else 0.0,
        cases=cases,
        latency_ms=elapsed_ms,
        meta={"items": pa["items"], "issues": pa["issues"]},
    )
