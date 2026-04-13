#!/usr/bin/env python3
"""
Deus daily maintenance — runs KB health tasks automatically.

Intended to be called by a system scheduler (launchd/systemd/Task Scheduler).
Each task runs independently; one failure does not block others.

Daily tasks: memory_gc, prune, decay, health
Weekly tasks (Sunday only): compress-digests, compile entities

Usage:
    python3 scripts/maintenance.py              # daily tasks only
    python3 scripts/maintenance.py --weekly     # force weekly tasks regardless of day
    python3 scripts/maintenance.py --dry-run    # preview without changes
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_task(name: str, args: list[str], dry_run: bool = False) -> bool:
    """Run a single maintenance task. Returns True on success."""
    cmd = [PYTHON] + args
    if dry_run:
        print(f"  [{name}] dry-run: {' '.join(args)}")
        return True
    print(f"  [{name}] running...", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                print(f"    {line}")
        if result.returncode != 0:
            print(f"  [{name}] FAILED (exit {result.returncode})")
            if result.stderr.strip():
                for line in result.stderr.strip().splitlines()[:5]:
                    print(f"    stderr: {line}")
            return False
        print(f"  [{name}] OK")
        return True
    except subprocess.TimeoutExpired:
        print(f"  [{name}] TIMEOUT (300s)")
        return False
    except Exception as e:
        print(f"  [{name}] ERROR: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Deus daily maintenance")
    parser.add_argument("--weekly", action="store_true", help="Force weekly tasks regardless of day")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    is_sunday = datetime.now().weekday() == 6
    run_weekly = args.weekly or is_sunday
    dry_run = args.dry_run

    indexer = str(SCRIPTS_DIR / "memory_indexer.py")
    gc = str(SCRIPTS_DIR / "memory_gc.py")

    print(f"=== Deus maintenance — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    if dry_run:
        print("(dry-run mode)\n")

    results: dict[str, bool] = {}

    # ── Daily tasks ──────────────────────────────────────────────────────────

    print("\n── Daily ──")
    results["memory_gc"] = run_task("memory_gc", [gc], dry_run)
    results["prune"] = run_task("prune", [indexer, "--prune"], dry_run)
    results["decay"] = run_task("decay", [indexer, "--decay"], dry_run)
    results["health"] = run_task("health", [indexer, "--health"], dry_run)

    # ── Weekly tasks (Sunday or --weekly) ────────────────────────────────────

    if run_weekly:
        print("\n── Weekly ──")
        results["digests"] = run_task("digests", [indexer, "--compress-digests", "weekly"], dry_run)
        results["compile"] = run_task("compile", [indexer, "--compile"], dry_run)
    else:
        print(f"\n── Weekly tasks skipped (not Sunday, use --weekly to force) ──")

    # ── Summary ──────────────────────────────────────────────────────────────

    ok = sum(1 for v in results.values() if v)
    fail = sum(1 for v in results.values() if not v)
    print(f"\n=== Done: {ok} OK, {fail} failed ===")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
