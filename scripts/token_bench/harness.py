#!/usr/bin/env python3
"""Token-usage measurement harness for Deus.

Measures static-context char + estimated token budget for:
- Root CLAUDE.md (loaded in every host CC session)
- groups/global/CLAUDE.md.template (appended to non-main-group system prompts)
- groups/*/CLAUDE.md (loaded via settingSources['project'])
- Selected pattern files and docs

Char-to-token conversion uses 1 tok ≈ 3.7 chars (Claude BPE approximation for
English technical text). For Hebrew-heavy files the ratio is ~3.0, but we use
a single ratio since we care about deltas, not absolute counts.

Output: JSON snapshot at results/<label>.json.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent  # worktree root
CHARS_PER_TOKEN = 3.7


def est_tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path.relative_to(REPO)), "exists": False}
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(REPO)),
        "exists": True,
        "chars": len(data),
        "lines": len(data.splitlines()),
        "est_tokens": est_tokens(len(data)),
        "sha256_8": hashlib.sha256(data).hexdigest()[:8],
    }


STATIC_CONTEXT_TARGETS = [
    # Host-side (loaded when CC runs in ~/deus)
    ("host_claude_md", "CLAUDE.md"),
    # Container-side shared
    ("global_template", "groups/global/CLAUDE.md.template"),
    ("main_template", "groups/main/CLAUDE.md.template"),
    # Container-side per-group (live)
    ("whatsapp_main", "groups/whatsapp_main/CLAUDE.md"),
    ("telegram_main", "groups/telegram_main/CLAUDE.md"),
    # Routing
    ("router", ".mex/ROUTER.md"),
    ("pattern_general", "patterns/general-code.md"),
]

# Simulated turn-1 context scenarios: what gets loaded together.
SCENARIOS = {
    "host_cc_session": ["host_claude_md"],
    "container_whatsapp_main_turn1": [
        "global_template",
        "whatsapp_main",
    ],
    "container_telegram_main_turn1": [
        "global_template",
        "telegram_main",
    ],
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True, help="snapshot label (e.g. baseline, phase0)")
    p.add_argument("--out-dir", default=str(REPO / "scripts/token_bench/results"))
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, Any]] = {}
    for key, rel in STATIC_CONTEXT_TARGETS:
        files[key] = file_info(REPO / rel)

    scenarios: dict[str, dict[str, Any]] = {}
    for name, keys in SCENARIOS.items():
        total_chars = sum(files[k].get("chars", 0) for k in keys if files[k].get("exists"))
        scenarios[name] = {
            "components": keys,
            "total_chars": total_chars,
            "est_tokens": est_tokens(total_chars),
        }

    snapshot = {
        "label": args.label,
        "chars_per_token": CHARS_PER_TOKEN,
        "files": files,
        "scenarios": scenarios,
    }

    out_path = out_dir / f"{args.label}.json"
    out_path.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"wrote {out_path}")
    for name, s in scenarios.items():
        print(f"  {name}: {s['total_chars']:>6} chars, ~{s['est_tokens']:>5} tok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
