#!/usr/bin/env python3
"""
Deus daily maintenance — runs KB health tasks automatically.

Intended to be called by a system scheduler (launchd/systemd/Task Scheduler).
Each task runs independently; one failure does not block others.

Daily tasks: memory_gc, prune, decay, health
Weekly tasks (Sunday only): compress-digests, compile entities, compression benchmark, vault integrity

Usage:
    python3 scripts/maintenance.py              # daily tasks only
    python3 scripts/maintenance.py --weekly     # force weekly tasks regardless of day
    python3 scripts/maintenance.py --dry-run    # preview without changes
"""
import subprocess
import sys
from pathlib import Path

# Local helpers — _time.py lives next to this script.
sys.path.insert(0, str(Path(__file__).parent))
from _time import local_now  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_task(name: str, args: list[str], dry_run: bool = False, timeout: int = 300) -> bool:
    """Run a single maintenance task. Returns True on success."""
    cmd = [PYTHON] + args
    if dry_run:
        print(f"  [{name}] dry-run: {' '.join(args)}")
        return True
    print(f"  [{name}] running...", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
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
        print(f"  [{name}] TIMEOUT ({timeout}s)")
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

    is_sunday = local_now().weekday() == 6
    run_weekly = args.weekly or is_sunday
    dry_run = args.dry_run

    indexer = str(SCRIPTS_DIR / "memory_indexer.py")
    gc = str(SCRIPTS_DIR / "memory_gc.py")

    print(f"=== Deus maintenance — {local_now().strftime('%Y-%m-%d %H:%M')} ===")
    if dry_run:
        print("(dry-run mode)\n")

    results: dict[str, bool] = {}

    # ── Daily tasks ──────────────────────────────────────────────────────────

    print("\n── Daily ──")
    results["memory_gc"] = run_task("memory_gc", [gc], dry_run)

    # LIA-137: index the project-memory store nightly.
    #
    # Read this before assuming it protects `--prune` below: it does NOT, and
    # an earlier version of this comment wrongly claimed it did. These are two
    # SEPARATE stores. `reindex-external` populates `nodes` in
    # memory_tree.db from ~/.claude/projects/*/memory/. `--prune` evaluates
    # `entries` in memory.db, holding atoms under the vault's Atoms/ dir.
    # Reindexing one cannot re-register the other's rows.
    #
    # What this step is actually for: `nodes` had NO automatic indexing at
    # all, which is why 311 files across 18 projects were invisible to
    # retrieval until today. Running it nightly makes new and moved
    # project-memory files self-registering instead of waiting for someone to
    # notice.
    #
    # What protects `entries` from the 847-atom move-vs-delete loss is the
    # bulk-orphan guard inside cmd_prune, plus `--migrate-prefix` for
    # recovery. Not this line.
    #
    # It still runs BEFORE prune: adding before removing is the right default
    # even across stores, and the ordering test pins it.
    #
    # Caveat, disclosed rather than glossed: this step also REMOVES. The walk
    # orphans nodes as `missing_file` (memory_tree.py), so calling it "the
    # adding half" is incomplete. By reading, the sweep is scoped by
    # expected_tag and gated by _confirm_orphan (LIA-336), so a project dir
    # vanishing from the projects root is never walked and its nodes are never
    # swept -- the 847-atom shape should not reproduce here. An in-project
    # move WOULD sweep, and `nodes` has no bulk guard equivalent to the one
    # cmd_prune just gained for `entries`. That gap is UNVERIFIED: a
    # verification pass could not build a driving fixture (the project
    # resolver rejects synthetic dirs). Treat as a known unknown, not a
    # cleared risk.
    tree = str(SCRIPTS_DIR / "memory_tree.py")
    results["reindex_external"] = run_task(
        "reindex_external",
        [tree, "reindex-external", "--all-projects", "--no-id-writeback"],
        dry_run,
    )

    results["prune"] = run_task("prune", [indexer, "--prune"], dry_run)
    results["decay"] = run_task("decay", [indexer, "--decay"], dry_run)
    results["health"] = run_task("health", [indexer, "--health"], dry_run)

    prune_baks = str(SCRIPTS_DIR / "maintenance" / "prune_warden_backups.py")
    results["prune_warden_baks"] = run_task(
        "prune_warden_baks", [prune_baks, "--keep", "10"], dry_run
    )

    rotate_qlog = str(SCRIPTS_DIR / "maintenance" / "rotate_query_log.py")
    results["rotate_query_log"] = run_task(
        "rotate_query_log", [rotate_qlog], dry_run
    )

    # LIA-128: the SQLite half of the same query log. rotate_query_log above bounds
    # the JSONL twin; this bounds the `queries_log` table, which had grown to 98% of
    # memory_tree.db by size. Runs WITHOUT --vacuum: steady-state trims free a handful
    # of pages that SQLite reuses, so a nightly VACUUM would rewrite the whole file for
    # nothing. The one-off VACUUM after the first large trim is run by hand.
    prune_qlog = str(SCRIPTS_DIR / "maintenance" / "prune_queries_log.py")
    results["prune_queries_log"] = run_task(
        "prune_queries_log", [prune_qlog], dry_run
    )

    # run_task prepends the Python interpreter, so this (like every sibling
    # maintenance script) runs as `python3 credential_probe.py` and stays 644.
    cred_probe = str(SCRIPTS_DIR / "maintenance" / "credential_probe.py")
    results["credential_probe"] = run_task(
        "credential_probe", [cred_probe], dry_run
    )

    # SessionEnd auto-save safety net: recovers queue entries a crashed/killed
    # detached worker missed. Up to 3 entries x compress_sweep's own worker
    # ceiling (120s default) + slack.
    compress_sweep = str(SCRIPTS_DIR / "maintenance" / "compress_sweep.py")
    results["compress_sweep"] = run_task(
        "compress_sweep", [compress_sweep], dry_run, timeout=600,
    )

    # LIA-135: reap what nothing else owns stopping. The host hit load 50-74 on
    # 2026-08-25 with no runaway process -- just long-lived things (a Langfuse
    # stack up 6 days, ten `claude agents` viewers aged 1-25 days) that nobody
    # was responsible for ending.
    #
    # Deliberately asymmetric, and do not "tidy" this into one posture: the
    # stack half ACTS (docker compose down is reversible, volumes survive),
    # the process half only REPORTS -- killing is irreversible, and age alone
    # cannot tell a stale viewer from one a human is watching right now. A
    # report-only run raises a desktop banner rather than printing into a log
    # nobody reads, which is the failure this whole ticket is about.
    #
    # Passing --kill here would make the nightly job kill processes
    # unattended on age alone. That needs Liam's explicit sign-off and an idle
    # signal the reaper does not yet have -- see reap_stale.py's docstring.
    #
    # Timeout: the reaper's own internal budget can exceed run_task's 300s
    # default -- a single stack costs up to 60s (compose ps) + 60s (inspect) +
    # 300s (down), and more than one stack may be listed. A hard TIMEOUT here
    # would kill it mid-teardown and report FAILED for a run that was merely
    # slow, so give it headroom rather than letting the wrapper win the race.
    reap_stale = str(SCRIPTS_DIR / "maintenance" / "reap_stale.py")
    results["reap_stale"] = run_task("reap_stale", [reap_stale], dry_run, timeout=900)

    # LIA-527 Phase 2: reclaims cc-write-queue job files a crashed/killed detached worker
    # missed. Single-best-effort-no-retry design -- see cc_write_queue_sweep.py's docstring.
    cc_write_queue_sweep = str(SCRIPTS_DIR / "maintenance" / "cc_write_queue_sweep.py")
    results["cc_write_queue_sweep"] = run_task(
        "cc_write_queue_sweep", [cc_write_queue_sweep], dry_run,
    )

    # ── Weekly tasks (Sunday or --weekly) ────────────────────────────────────

    if run_weekly:
        print("\n── Weekly ──")
        results["digests"] = run_task("digests", [indexer, "--compress-digests", "weekly"], dry_run)
        results["compile"] = run_task("compile", [indexer, "--compile"], dry_run)

        compression_bench = str(SCRIPTS_DIR / "compression_benchmark.py")
        results["compression_check"] = run_task(
            "compression_check",
            [compression_bench, "--auto"],
            dry_run,
            timeout=900,  # LLM-based: multiple Ollama calls
        )
        results["vault_integrity"] = run_task(
            "vault_integrity",
            [compression_bench, "--vault-integrity"],
            dry_run,
        )

        # Local judge calibration watchdog (LIA-261): anchor the local gemma4:e4b
        # evolution judge to the pinned Gemini ground truth; WARN on a quality-
        # Pearson regression. 130min ceiling > the watchdog's own 7200s bench
        # timeout, so the watchdog returns a clean INCONCLUSIVE (exit 0) before a
        # hard maintenance TIMEOUT could mark it FAILED on pure infra slowness.
        # run_task prepends the Python interpreter, so this (like every sibling
        # maintenance script) runs as `python3 judge_calibration.py` and stays 644.
        judge_calib = str(SCRIPTS_DIR / "maintenance" / "judge_calibration.py")
        results["judge_calibration"] = run_task(
            "judge_calibration", [judge_calib], dry_run, timeout=7800,
        )
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
