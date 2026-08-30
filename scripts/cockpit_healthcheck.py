#!/usr/bin/env python3
"""Daily cockpit healthcheck — does each subsystem still do its job? (LIA-552)

`launchctl list` proves a process is loaded. It says nothing about whether
interactions are being judged. That gap let the evolution optimizer sit dead
from 2026-03 to 2026-08 with no external signal (LIA-551), so every probe here
reports on *evidence of work*, never on liveness alone.

Three probe classes, because they answer different questions:

  capability  can this subsystem work at all?   (deterministic, idle-insensitive)
  demand      was work produced when work was asked for?
  liveness    is the process up, serving the config we think it is?

Two rules keep it from reproducing the bug it detects:

  * A probe that cannot reach a verdict reports UNKNOWN. UNKNOWN is never OK,
    is counted separately, and has its own exit code.
  * OK is only ever asserted from positive evidence. The absence of known
    problems is not evidence of health.

Read-only, with one documented exception. Every database is opened with a
`mode=ro` URI, and the checker writes no subsystem storage — but the memory
probe delegates to `memory_health.assess_memory_health`, whose vault check is a
deliberate touch+unlink (`memory_health.py:33-53`). That write is the point of
that probe: `is_dir()` alone misses the macOS Full-Disk-Access case where the
vault reads fine and writes silently fail. It is kept rather than bypassed, and
named here so the read-only claim stays true as written.

Usage:
    cockpit_healthcheck.py           probe, write artifacts, exit per table below
    cockpit_healthcheck.py --json    same, machine-readable on stdout
    cockpit_healthcheck.py --brief   print the cached line, no probing

A bare invocation is the run mode, because that is exactly what the scheduled
job issues: SCHEDULED_JOBS entries carry no arguments (setup/service.ts), so a
flag guarding the probe branch would silently disable the daily run.

Exit codes:
    0  at least one real verdict, none FAILED, no UNKNOWN
    1  at least one FAILED (outranks UNKNOWN: a known failure is actionable)
    2  no probe reached a real verdict — the checker itself is untrustworthy
    3  artifact write failed
    4  at least one UNKNOWN, none FAILED — partial blindness
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import logging
import os
import plistlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

OK = "OK"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

#: Ranked worst-first so a run's overall verdict is a simple max().
_RANK = {OK: 0, DEGRADED: 1, UNKNOWN: 2, FAILED: 3}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ALL_UNKNOWN = 2
EXIT_WRITE_FAILED = 3
EXIT_PARTIAL_UNKNOWN = 4

DEUS_HOME = Path(os.environ.get("DEUS_HOME", "~/.deus")).expanduser()
ARTIFACT_JSON = DEUS_HOME / "cockpit_health.json"
ARTIFACT_LINE = DEUS_HOME / "cockpit_health.line"

REPO_ROOT = Path(__file__).resolve().parent.parent

#: How long a cached artifact stays meaningful to the shell display.
ARTIFACT_MAX_AGE_SEC = 36 * 3600

#: A demand served this recently is not yet evidence of a stuck pipeline.
INGEST_GRACE_SEC = 30 * 60


@dataclass
class Result:
    """One probe's verdict plus the evidence a reader needs to act on it."""

    probe: str
    status: str
    observed: str = ""
    expected: str = ""
    remedy: str = ""
    detail: dict = field(default_factory=dict)


# ── helpers ───────────────────────────────────────────────────────────────────


def _launch_agent_installed(label: str) -> bool:
    """Is this launchd job actually installed on this machine?

    Optional components are absent on a perfectly valid install, so "missing"
    must mean not-applicable rather than broken — otherwise the cockpit alarms
    daily about something the user deliberately never set up.
    """
    return (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").is_file()


# launchd job kinds. One "is there a PID" rule cannot serve all of these: a
# scheduled job that finished correctly looks identical to a daemon that died,
# and reporting the former as FAILED is the permanent false alarm this module
# exists to remove.
RESIDENT = "resident"    # KeepAlive: true -- must always be running
SCHEDULED = "scheduled"  # StartInterval/StartCalendarInterval
RUN_ONCE = "run_once"    # RunAtLoad only -- runs once at login, exits

# A dict-valued KeepAlive has NO bucket here, deliberately. It carries
# conditions (SuccessfulExit, Crashed, NetworkState, PathState...) whose current
# state decides whether being stopped is correct, and this probe cannot evaluate
# them. Three review rounds each produced a different confident rule for it --
# stopped+nonzero is FAILED, then UNKNOWN, then stopped+zero is OK -- and every
# one was wrong for a predicate somebody could name: `SuccessfulExit: true`
# requires relaunch AFTER a clean exit, so even exit 0 while stopped can mean
# broken. No such job exists on this host, so none of it was ever calibrated.
# Classifying it as unknown-shape is the honest answer and keeps the calibrated
# majority shippable. Evaluating the predicates is its own ticket.


def _never_ran(exit_status: str) -> bool:
    """Has this job never completed a run?

    `launchctl list` prints "-" in the Status column for a job that has never
    run. Any numeric value -- including a negative one, which is a signal
    termination -- means a run completed and its result is knowable.

    One helper rather than the same expression in each verdict branch: three
    branches consult it, and a change to the sentinel format that updated only
    two of them would be invisible until it produced a wrong verdict.
    """
    return not exit_status.lstrip("-").isdigit()


def _launch_agent_kind(label: str) -> str | None:
    """Classify a launchd job so its verdict is judged on the right thing.

    KeepAlive is the discriminator, NOT RunAtLoad. RunAtLoad is set on only 4 of
    the 7 jobs here and appears across every bucket, so it separates nothing.

    Returns None when the plist cannot be read or matches no known shape. The
    caller turns that into UNKNOWN rather than guessing a bucket -- a wrong
    bucket yields a confident verdict about the wrong proposition, which is the
    defect this probe was rewritten to stop producing.
    """
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    try:
        with path.open("rb") as fh:
            spec = plistlib.load(fh)
    # Measured: malformed XML raises xml.parsers.expat.ExpatError, which is NOT
    # a ValueError and so escapes the narrower tuple. A single bad plist would
    # then crash the whole healthcheck instead of degrading one probe to
    # UNKNOWN. A bare Exception is deliberate: every parse failure means the
    # same thing here (this job cannot be classified), and a checker that dies
    # is strictly worse than one that says it does not know.
    except Exception as exc:
        # Logged so a future refactor's own AttributeError cannot masquerade as
        # "malformed plist" forever. The user-facing behaviour stays UNKNOWN --
        # a checker that dies is strictly worse than one that says it does not
        # know -- but the diagnostic must not vanish with it.
        # .warning(), NOT .debug(): main() never calls logging.basicConfig, so
        # the root logger sits at its default WARNING level and a debug record
        # is dropped before any handler sees it. A diagnostic that only fires
        # under caplog is not a diagnostic -- it is a fix that does not fix.
        logging.getLogger(__name__).warning(
            "cannot classify %s: %r", path, exc)
        return None
    if not isinstance(spec, dict):
        # A valid plist whose root is an array or string loads without error and
        # then AttributeErrors on .get(). Not classifiable either.
        return None
    # KeepAlive is checked FIRST and deliberately wins over an interval key: a
    # job asking to be kept alive must be running whatever else it declares. No
    # current job sets both, so this precedence is a decision rather than an
    # accident of ordering -- and it is untested by the calibration set.
    keep_alive = spec.get("KeepAlive")
    # `KeepAlive: {}` is a legal but degenerate construct: a dict with no
    # conditions. Test isinstance BEFORE truthiness, or the empty dict is falsy
    # and silently falls through to the interval/RunAtLoad checks -- classifying
    # a keep-alive job as something that is meant to finish. No conditions to be
    # unmet means launchd keeps it running, so it belongs with RESIDENT.
    if isinstance(keep_alive, dict):
        # Unclassifiable by design -- see the note beside the kind constants.
        # Whether a stopped conditional job is healthy depends on a predicate
        # this probe cannot evaluate, so no verdict is available and UNKNOWN is
        # the honest report.
        return None
    if keep_alive is True:
        return RESIDENT
    if "StartInterval" in spec or "StartCalendarInterval" in spec:
        return SCHEDULED
    if spec.get("RunAtLoad"):
        return RUN_ONCE
    # e.g. a purely WatchPaths-triggered job. None exists here today, so there
    # is no positive evidence for any bucket -- and no calibration for one.
    return None


def _deus_service_labels() -> tuple[str, ...] | None:
    """Every installed com.deus job, discovered rather than hardcoded.

    A hardcoded tuple is how five of seven jobs went unwatched while one of them
    failed nightly (LIA-136). Discovery keeps new jobs covered by default.

    Returns None when the directory cannot be read. That is loss of probe
    coverage, not an occasion to fall back to a shorter list: falling back would
    let the cockpit report OK from two probes while an undiscovered job fails.
    An empty tuple is different -- it is a real observation that nothing is
    installed.

    The literal dot matters: a bare `com.deus*` glob also matches
    `com.deus-v2.plist`, which some installs carry but never run (see the note
    in run_probes). Checked against that decoy before adopting.
    """
    agents = Path.home() / "Library" / "LaunchAgents"
    # iterdir(), NOT glob(). Measured: Path.glob() returns [] for BOTH a missing
    # directory AND an unreadable one, and raises in neither case -- so an
    # OSError branch around glob is unreachable and every failure would present
    # as a successful empty discovery. iterdir() raises FileNotFoundError and
    # PermissionError respectively, which is the distinction this function
    # exists to make.
    try:
        entries = list(agents.iterdir())
    except OSError:
        return None
    # "com.deus.plist" itself satisfies startswith("com.deus.") and
    # endswith(".plist"), so the comprehension already yields the bare
    # "com.deus" label -- no separate insert, which would duplicate it. The
    # `com.deus.` prefix with its literal dot is what excludes the
    # "com.deus-v2.plist" decoy (see the note in run_probes).
    labels = sorted(
        e.name[: -len(".plist")]
        for e in entries
        # is_file() as well as the name: a DIRECTORY named com.deus.x.plist
        # would otherwise be discovered and probed as a job.
        if e.name.startswith("com.deus.") and e.name.endswith(".plist") and e.is_file()
    )
    return tuple(labels)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so readers never see a partial or
    truncated artifact, and a failed write leaves the previous one intact."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _ro(path: Path) -> sqlite3.Connection:
    """Open read-only. Never let a health check mutate what it inspects."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _resolved_python() -> str:
    """The interpreter that would run `evolution.cli`.

    Deliberately not a claim about the call graph: EVOLUTION_PYTHON is read by
    the Node host (src/evolution-client.ts:22) for its own spawns, and nothing
    threads it into `cli.py optimize`. This resolves it the same way the host
    does so the probe reports on the environment a run would actually get.
    """
    return os.environ.get("EVOLUTION_PYTHON") or "python3"


def _vault_path() -> Path | None:
    """Vault path from the standard config, the way the rest of the repo reads it
    (mirrors codex_warden_hooks.py). None when unset — memory_health handles that."""
    try:
        cfg = json.loads((Path("~/.config/deus/config.json").expanduser()).read_text())
    except (OSError, ValueError):
        return None
    raw = cfg.get("vault_path")
    return Path(raw).expanduser() if raw else None


def _newest_source_mtime(directory: Path) -> float:
    """Newest mtime among a package's sources. 0.0 when it cannot be read."""
    newest = 0.0
    try:
        for p in directory.rglob("*.py"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


# ── capability probes ─────────────────────────────────────────────────────────


def probe_optimizer(now: float) -> Result:
    """Can the DSPy optimizer run at all?

    OK requires positive evidence of a completed run that postdates the code it
    would be vindicating. Absence of known blockers is not evidence: enumerating
    blockers is how LIA-556 shipped three separate silent paths.

    The two blockers this docstring used to name -- GEPA constructed without
    reflection_lm, and the judge metric returning a dict rather than a
    dspy.Prediction -- are both fixed, so no blocker is currently KNOWN and
    `known_open_blockers` is an empty list. That makes this probe MORE
    important, not less: with nothing left to point at, "dspy imports" is the
    only remaining signal and it still says nothing about whether a run ever
    completed. A future blocker nobody has listed lands in the UNKNOWN branch
    rather than green, which is the whole point.
    """
    py = _resolved_python()
    try:
        proc = subprocess.run(
            [py, "-c", "import dspy; print(dspy.__version__)"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(
            "evolution.optimizer", UNKNOWN,
            observed=f"could not run {py!r}: {type(exc).__name__}",
            expected="an interpreter that can import dspy",
            remedy=f"check that {py!r} exists and is executable",
        )

    if proc.returncode != 0:
        return Result(
            "evolution.optimizer", FAILED,
            observed=f"{py} cannot import dspy",
            expected="dspy importable by the interpreter that runs evolution.cli",
            remedy=f"{py} -m pip install dspy  (or point EVOLUTION_PYTHON at an env that has it)",
            detail={"interpreter": py, "stderr": proc.stderr.strip()[-200:]},
        )

    version = proc.stdout.strip()
    code_mtime = _newest_source_mtime(REPO_ROOT / "evolution" / "optimizer")
    last_ok = _last_optimizer_success()

    if last_ok is None:
        return Result(
            "evolution.optimizer", UNKNOWN,
            observed=f"dspy {version} importable, but no completed run on record",
            expected="a completed optimize run newer than the optimizer sources",
            remedy="run the optimizer once and confirm it completes",
            detail={
                # Both previously-listed blockers (GEPA built without
                # reflection_lm; the metric returning a dict rather than a
                # dspy.Prediction) were fixed when the arm was activated. Keeping
                # them here would send the next reader after defects that are
                # gone. The list is empty because no blocker is currently KNOWN —
                # which is exactly why this stays UNKNOWN rather than OK: an
                # unlisted blocker is what this branch exists to catch.
                "known_open_blockers": [],
            },
        )

    if last_ok < code_mtime:
        return Result(
            "evolution.optimizer", UNKNOWN,
            observed="newest completed run predates the current optimizer sources",
            expected="a completed run newer than evolution/optimizer/*.py",
            remedy="run the optimizer against the current code",
            detail={"last_success_epoch": last_ok, "sources_mtime": code_mtime},
        )

    return Result(
        "evolution.optimizer", OK,
        observed=f"dspy {version}; completed run postdates the optimizer sources",
        expected="a completed run newer than evolution/optimizer/*.py",
    )


def _last_optimizer_success() -> float | None:
    """Epoch of the newest recorded optimizer success, or None.

    A run cannot vindicate code written after it, so the caller compares this
    against the sources' mtime. Returns None on any doubt.
    """
    import datetime as _dt

    db = Path(os.environ.get("DEUS_EVOLUTION_DB", DEUS_HOME / "evolution.db")).expanduser()
    if not db.exists():
        return None
    try:
        con = _ro(db)
        try:
            # registry/storage are bookkeeping: evolution/cli.py:224,240 write
            # them OK on every cycle, including below-threshold ones that
            # optimize nothing and cycles that then fail every module. Counting
            # them as "a completed run" would let routine bookkeeping certify a
            # dead optimizer — the exact false-green this probe exists to stop.
            row = con.execute(
                "SELECT MAX(last_ok_at) FROM subsystem_health "
                "WHERE component LIKE 'evolution.optimizer.%' "
                "  AND component NOT IN ('evolution.optimizer.registry',"
                "                        'evolution.optimizer.storage') "
                "  AND last_ok_at IS NOT NULL"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        return _dt.datetime.fromisoformat(row[0]).timestamp()
    except ValueError:
        return None


# ── demand-gated probe ────────────────────────────────────────────────────────


def probe_ingest(now: float, demand: "DemandWindow") -> Result:
    """Is the interaction store readable, and how fresh is it?

    Scope is deliberately narrow and the wording matters: these are read-only
    SELECTs, so this cannot and does not assert that ingest *writes* work. A
    disabled or no-op ingest path stays invisible to it. Proving the write path
    needs a canary insert, which the read-only contract forbids — named here so
    the limit is visible rather than implied away.

    Reports reachability as a verdict and freshness as *observation*. It
    deliberately does NOT derive a FAILED verdict from demand.

    Three review rounds each found a different reason the demand comparison was
    unsound: an unreadable log read as idle; the lifecycle markers fire 58/312/120
    times rather than 1:1; and "New messages" is logged before trigger checks,
    host-command handling and evolution opt-outs, so ordinary non-trigger group
    traffic legitimately produces no interaction. Three distinct semantic gaps in
    one rule is not bad luck — it means the log does not carry a trustworthy
    "this arrival should have produced an interaction" signal, and inferring one
    manufactures false alarms in the tool built to end them.

    So this probe states what it can prove. Judging unserved demand needs a
    first-class signal emitted at the point the decision is made, which is a
    change to the runtime rather than something a log reader can recover.
    """
    db = Path(os.environ.get("DEUS_EVOLUTION_DB", DEUS_HOME / "evolution.db")).expanduser()
    if not db.exists():
        return Result(
            "evolution.ingest", UNKNOWN,
            observed=f"{db} does not exist",
            expected="a readable evolution database",
            remedy="check DEUS_EVOLUTION_DB / run Deus once to create it",
        )
    try:
        con = _ro(db)
        try:
            con.execute("SELECT 1 FROM interactions LIMIT 1").fetchone()
            newest = con.execute("SELECT MAX(timestamp) FROM interactions").fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return Result(
            "evolution.ingest", UNKNOWN,
            observed=f"interactions unreadable: {type(exc).__name__}: {exc}",
            expected="a queryable interactions table",
            remedy="inspect the evolution database",
        )

    age = ""
    if newest:
        import datetime as _dt
        try:
            hours = (now - _dt.datetime.fromisoformat(newest).timestamp()) / 3600
            age = f", newest {hours:.1f}h old"
        except ValueError:
            pass
    return Result(
        "evolution.ingest", OK,
        observed=f"store readable, {total} interaction(s){age}",
        expected="a queryable interactions table",
        detail={"newest_interaction": newest, "demand_events_in_window": demand.count,
                "demand_log_readable": demand.readable},
    )


@dataclass
class DemandWindow:
    """Real chat/agent demand in a window.

    Deliberately excludes /health polls and GitHub/Linear webhooks: those run
    constantly and would make every window look busy, which is how an earlier
    version nearly concluded a three-day-idle system was under heavy load.
    """

    count: int
    newest_epoch: float
    window_start: float
    window_sec: float
    readable: bool = True


#: ONE canonical arrival event. The lifecycle emits several records per request
#: ("New messages" -> "Spawning container agent" -> "Container completed"), and
#: they are nowhere near 1:1 — measured over the live log: 58 / 312 / 120. Summing
#: them inflated demand ~8x and would have reported healthy traffic as DEGRADED
#: forever. Arrival is the right one: it is what must be *served*, so a request
#: that never reaches a container still counts as unmet demand.
_DEMAND_MARKER = "New messages"


def read_demand(log_path: Path, window_sec: float, now: float) -> DemandWindow:
    start = now - window_sec
    count = 0
    newest = 0.0
    readable = False
    # Rotation: the live file may not span the whole window.
    paths = [log_path] + [Path(p) for p in glob.glob(str(log_path.parent / "archives" / "*"))]
    for path in paths:
        try:
            # Rotation produces logs/archives/*.log.gz; opening those as plain
            # text silently yields nothing while the live file still marks the
            # source readable, so missing demand would look like idleness.
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", errors="replace") as fh:
                readable = True
                for line in fh:
                    if not line.startswith("{"):
                        continue
                    if _DEMAND_MARKER not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = rec.get("time", 0) / 1000
                    if ts < start:
                        continue
                    count += 1
                    newest = max(newest, ts)
        except OSError:
            continue
    return DemandWindow(count, newest, start, window_sec, readable)


# ── memory (reuses scripts/memory_health.py) ──────────────────────────────────


def probe_memory(now: float) -> Result:
    """Delegate to memory_health.py, but do not inherit its fail-open contract.

    That module deliberately swallows sqlite errors and returns healthy
    (memory_health.py:78) so a startup banner never blocks on a probe fault.
    Correct there, wrong here — it is the silent-failure shape this cockpit
    exists to catch. So the cockpit independently proves the DB is genuinely
    queryable before honouring any OK.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from memory_health import DEFAULT_DB_PATH, assess_memory_health
    except Exception as exc:
        return Result(
            "memory", UNKNOWN,
            observed=f"memory_health unavailable: {type(exc).__name__}: {exc}",
            expected="scripts/memory_health.py importable",
            remedy="check the repo checkout",
        )

    db = DEFAULT_DB_PATH
    if not db.exists():
        return Result(
            "memory", FAILED,
            observed=f"{db} is missing",
            expected="the memory tree database to exist",
            remedy="restore the tree DB or re-run the indexer",
        )

    # quick_check reports corruption as a returned row, not an exception, so
    # the value is what matters — catching exceptions alone would miss it.
    try:
        con = _ro(db)
        try:
            verdict = con.execute("PRAGMA quick_check").fetchone()[0]
            con.execute("SELECT 1 FROM nodes LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return Result(
            "memory", UNKNOWN,
            observed=f"tree DB unreadable: {type(exc).__name__}: {exc}",
            expected="a queryable memory tree database",
            remedy="inspect ~/.deus/memory_tree.db",
        )
    if str(verdict).lower() != "ok":
        return Result(
            "memory", FAILED,
            observed=f"PRAGMA quick_check returned {verdict!r}",
            expected="quick_check == 'ok'",
            remedy="restore the tree DB from a backup",
        )

    try:
        healthy, _label, lines = assess_memory_health(_vault_path())
    except Exception as exc:
        return Result(
            "memory", UNKNOWN,
            observed=f"assess_memory_health raised: {type(exc).__name__}: {exc}",
            expected="a memory health verdict",
            remedy="run scripts/memory_health.py directly",
        )

    if healthy:
        return Result("memory", OK, observed="tree DB queryable; no degradation reported")
    missing = [ln for ln in lines if "missing" in ln.lower() or "unreadable" in ln.lower()]
    return Result(
        "memory", FAILED if missing else DEGRADED,
        observed="; ".join(lines)[:300],
        expected="no memory degradation",
        remedy="python3 scripts/memory_health.py",
    )


# ── hook registration hygiene (LIA-129) ───────────────────────────────────────

#: Settings files whose hooks all merge into one execution set per event.
#: User scope and project scope are ADDITIVE -- the project copy does NOT
#: override the user copy, both are INVOKED. Verified by driving three
#: `claude -p` probes from three unrelated cwds; see
#: ~/.claude/rules/feedback_hook_scopes_merge_not_override.md.
#:
#: WHAT THIS PROBE DETECTS, precisely: a duplicate REGISTRATION, not a duplicate
#: EXECUTION. The two are not the same, and conflating them would make this
#: probe wrong about the very case it was written for. A script registered
#: twice is invoked twice, but it may still SELF-SUPPRESS on the second
#: invocation -- `memory_retrieval_hook` does exactly that today, its user-scope
#: registration exiting early when the project defines its own (verified by
#: driving the guard in both directions: it trips under the Deus project dir and
#: does not trip elsewhere, which is correct). So the duplicate registration is
#: real and this probe correctly reports it, while the route runs once.
#:
#: That is the whole reason an acknowledgement must NAME a guard and the probe
#: must VERIFY that guard still exists: "registered twice but guarded" is a
#: legitimate resting state, and the only thing separating it from "registered
#: twice and paying twice" is a guard nobody has deleted yet.
#: All four are OPTIONAL and all four are additive. The `.local.json` variants
#: are a standard scope, not a curiosity: a hook registered there co-fires with
#: one in any other scope, so omitting them is a false-negative surface.
_SETTINGS_FILES = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
    REPO_ROOT / ".claude" / "settings.json",
    REPO_ROOT / ".claude" / "settings.local.json",
)

#: Events whose matcher is a regex over the TOOL name. Every other event fires
#: once per occurrence with no tool dimension, so its matcher is not consulted.
_TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

#: Tool names a matcher can select. Only used to decide which registrations
#: land in the same co-firing set, so it needs to cover the tools in use, not
#: every tool that exists -- an unlisted tool simply yields no grouping for
#: itself, never a false duplicate.
_TOOL_NAMES = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Task",
    "Agent", "ExitPlanMode", "NotebookEdit", "WebFetch", "WebSearch",
    "apply_patch", "TodoWrite", "SlashCommand",
)

_NO_TOOL = "<no-tool>"

#: Bucket key for a registration whose co-firing set cannot be determined -- a
#: non-tool event carrying a matcher whose domain is undocumented. Never
#: compared for duplicates; surfaced as UNKNOWN instead. See _matcher_covers.
_UNDECIDABLE = "<undecidable>"

#: argv[0] basenames that are an INTERPRETER, not the thing being registered.
#: Identity must bind to the script, not to whatever runs it: an absolute
#: interpreter path (a venv pin, e.g. `~/.deus/langfuse/.venv/bin/python x.py`,
#: which is live on this host) stats successfully at index 0, so a
#: first-stat-wins resolver binds identity to the INTERPRETER with the script as
#: an argument. The same script registered elsewhere as `python3 x.py` -- where
#: the bare name does not stat -- binds to the script itself. Two identities for
#: one script, and the duplicate goes unreported. Checked by basename only, and
#: only at argv[0], so a script that merely happens to sit next to a config file
#: argument is unaffected.
_INTERPRETERS = frozenset({
    "python", "python3", "python3.11", "python3.12", "python3.13", "python3.14",
    "node", "deno", "bun", "ruby", "perl", "bash", "sh", "zsh", "env", "uv", "uvx",
})

#: Duplicates reviewed and knowingly accepted, keyed by (event, script basename).
#: Each entry MUST name the guard it relies on, and that guard is verified
#: present in the target before the acknowledgement is honoured -- an
#: acknowledgement that merely asserts "the target guards itself" stays green
#: after someone deletes the guard, which is a suppression outliving its reason.
#: EMPTY BY DESIGN while LIA-129's guard is parked: the live duplicate reports
#: DEGRADED, which is a true statement about a real unfixed defect.
_ACKNOWLEDGED_DUPLICATES: dict[tuple[str, str], dict[str, str]] = {}


def _expand_settings_token(token: str, settings_file: Path) -> str:
    """Resolve a command token relative to the settings file that declared it.

    ``${CLAUDE_PROJECT_DIR}`` is taken from the settings file's OWN location, not
    from the environment. The environment is wrong here: this runs as a daily
    scheduled job, which has no ``CLAUDE_PROJECT_DIR``, so the ``:-.`` default
    would resolve against the job's cwd. Measured -- with the variable unset from
    an unrelated cwd the project-scope registration did not resolve at all and
    the sweep reported no duplicate while the duplicate was firing every prompt.
    A settings file at ``<X>/.claude/settings.json`` states that the project dir
    is ``<X>``; the process does not.
    """
    # A USER-scope file has no project of its own -- `~/.claude/settings.json`'s
    # grandparent is the home directory, not a project root. A user-scope hook
    # written with ${CLAUDE_PROJECT_DIR} resolves against whatever project the
    # session is in, which for this probe is REPO_ROOT. Deriving it from the
    # file's parent would resolve such a hook to `~/scripts/...` and it would
    # then fail to match the project-scope registration of the same script --
    # hiding the very duplicate being looked for.
    if settings_file.parent == Path.home() / ".claude":
        project_dir = str(REPO_ROOT)
    else:
        project_dir = str(settings_file.parent.parent)
    token = token.replace("${CLAUDE_PROJECT_DIR:-.}", project_dir)
    token = token.replace("${CLAUDE_PROJECT_DIR}", project_dir)
    return os.path.expanduser(token)


def _hook_invocation(command: str, settings_file: Path) -> tuple[object, str]:
    """(identity, label) for one registered hook command.

    Identity is ``((st_dev, st_ino), args)``:

    * **inode, not path.** ``realpath()`` does NOT canonicalise case, so on a
      case-insensitive filesystem ``~/Deus/x`` and ``~/deus/x`` come back as two
      different strings for one file -- and that IS the duplicate this probe
      exists to catch. Measured: string-equal False, st_ino equal True.
    * **args included.** One dispatcher script is routinely registered many
      times in a single co-firing set with different subcommands (6x
      ``warden-shim.sh`` under one PostToolUse matcher here). Keying on the
      script alone reports all six as duplicates, and a false positive that
      large is how a detector gets switched off.

    Unresolvable commands fall back to the raw string, marked, so they are
    counted rather than silently dropped.

    DISCLOSED RESIDUAL - the first regular-file token wins, which is not the same
    as knowing which token the shell would EXECUTE. A command using a pipe or a
    redirection (`cat /tmp/in | /path/hook`) can bind identity to the operand
    rather than the hook. Closing that needs command-aware shell parsing, which
    is the same over-reach as computing regex intersections and is refused for
    the same reason: a daily healthcheck should make a narrow claim soundly
    rather than a broad one badly. No registration on this host uses a pipe,
    a redirection or a `&&` chain -- all 44 resolve to their real script.

    Returns ``(identity, label, script_path)``. The script path is returned from
    here rather than re-derived by a second parser: an earlier version had the
    unwrap-and-expand logic duplicated in two functions, and the interpreter bug
    above is exactly the kind of fix that would have landed in one copy only,
    desynchronising duplicate-detection from guard-verification.
    """
    unresolved = (f"unresolved:{command}", f"UNRESOLVED {command[:60]}", "")

    try:
        tokens = shlex.split(command)
    except ValueError:
        return unresolved

    # Unwrap one level of `bash -c '<inner>'`. Without this, shlex returns the
    # whole quoted inner command as a single non-file token: a first version
    # resolved ZERO of this host's ~14 warden-shim registrations and printed a
    # clean sweep while blind to most of the corpus.
    if len(tokens) >= 3 and os.path.basename(tokens[0]) in ("bash", "sh", "zsh") \
            and tokens[1] == "-c":
        try:
            tokens = shlex.split(tokens[2])
        except ValueError:
            return unresolved

    expanded = [_expand_settings_token(t, settings_file) for t in tokens]
    for i, token in enumerate(expanded):
        # argv[0] naming an interpreter is not the registered thing -- keep
        # looking for the script it runs. See _INTERPRETERS.
        if i == 0 and os.path.basename(token) in _INTERPRETERS:
            continue
        try:
            st = os.stat(token)
        except OSError:
            continue
        # Must be a regular FILE. `os.stat` succeeds on directories too, so
        # `cd /repo && ./hooks/a.sh` would bind identity to `/repo` and count as
        # resolved -- then fail to match the same script registered directly in
        # another scope. (An earlier split-out helper used `isfile`; the check
        # was lost when the two parsers were merged into one. Second time a guard
        # went missing during a consolidation.)
        if not stat.S_ISREG(st.st_mode):
            continue
        args = tuple(expanded[i + 1:])
        label = os.path.basename(token)
        if args:
            label += " " + " ".join(args)
        return ((st.st_dev, st.st_ino), args), label, token

    return unresolved


#: Characters that make an alternation branch a pattern rather than a name.
_REGEX_META = set(".^$*+?{}[]\\()|")


def _literal_branches(matcher: str) -> set[str]:
    """Plain tool names named literally anywhere in a matcher.

    Splitting on top-level ``|`` and keeping the metacharacter-free branches is
    what lets two matchers be compared on a tool NEITHER of them is alone: both
    ``Bash|mcp__x`` and ``Read|mcp__x`` contribute ``mcp__x``, so the pair is
    compared on it. Taking the whole matcher text as a witness cannot do that --
    ``Bash|mcp__x`` does not fullmatch the string ``Read|mcp__x``.

    RESIDUAL, disclosed rather than solved: this finds overlaps witnessed by a
    LITERAL name. Two matchers overlapping only through pattern regions
    (``mcp__a.*`` vs ``mcp__.*b``, which both fire on ``mcp__ab``) still miss
    each other. Deciding that in general is regex intersection, which is not
    what a daily healthcheck should be doing; every matcher on this host today
    is an alternation of literals.
    """
    return {b for b in matcher.split("|") if b and not (set(b) & _REGEX_META)}


def _fully_literal(matcher: str) -> bool:
    """Is EVERY top-level branch of this matcher a plain tool name?

    Not "has a literal branch". `Bash|mcp__a.*` has one, but its second branch is
    a pattern that can overlap another matcher's pattern branch on a tool neither
    names -- `Bash|mcp__a.*` and `Read|mcp__.*b` both fire for `mcp__ab`. Only a
    matcher whose branches are ALL literal is fully settled by bucketing.
    """
    branches = [b for b in matcher.split("|") if b]
    return bool(branches) and all(not (set(b) & _REGEX_META) for b in branches)


def _safe_pattern(matcher: str) -> "re.Pattern[str] | None":
    """Compile a matcher, or None if it is not a valid regex."""
    try:
        return re.compile(matcher)
    except re.error:
        return None


def _matcher_covers(event: str, matcher: str | None,
                    extra_tools: "frozenset[str] | set[str]" = frozenset()) -> list[str]:
    """Tool names this registration co-fires on.

    Grouping by the matcher STRING would be wrong: a matcher is a regex over the
    tool name, so two registrations whose matchers differ textually but overlap
    in coverage genuinely fire on the same call. Six such pairs exist on this
    host today (``Stop`` ``None`` vs ``''`` being the clearest -- identical
    semantics, different text). Expanding to the tools each one covers compares
    what actually co-fires and needs no pairwise regex analysis.
    """
    if event not in _TOOL_EVENTS:
        # A non-tool event with NO matcher is unambiguous: everything on that
        # event co-fires. One bucket.
        if matcher in (None, ""):
            return [_NO_TOOL]
        # A non-tool event WITH a matcher is genuinely undecidable here, and
        # guessing either way produces a wrong verdict:
        #   * if such matchers are honoured, two registrations under `startup`
        #     and `resume` never co-fire -- calling them a duplicate is a false
        #     positive, and a false positive is how a detector gets switched off
        #   * if they are ignored, both fire -- calling them clean is a false
        #     negative, and this probe exists to eliminate exactly that
        # The matcher domain for these events is not documented (checked against
        # the official hook-development skill: it specifies the domain for
        # PreToolUse/PostToolUse only). So the probe declines to decide, per this
        # module's own rule that a probe which cannot reach a verdict reports
        # UNKNOWN rather than manufacturing one. No such registration exists on
        # this host today, so this changes nothing here -- it is a guard for the
        # config that eventually has one.
        return [_UNDECIDABLE]
    universe = list(_TOOL_NAMES) + sorted(extra_tools)
    if matcher in (None, ""):
        # Matcher-less means ALL tools -- including any the caller discovered
        # from other registrations' matchers. Covering only _TOOL_NAMES here is
        # what let a universal registration and an MCP-scoped one co-fire
        # without ever sharing a bucket.
        return universe
    pattern = _safe_pattern(matcher)
    if pattern is None:
        return [_UNDECIDABLE]
    hits = [t for t in universe if pattern.fullmatch(t)]
    # A pattern-only matcher (`mcp__.*`) contributes no literal branch and may
    # match nothing in the universe. Returning [] would bucket the registration
    # NOWHERE, so even two byte-identical registrations of it would never be
    # compared and the probe would report OK on a real duplicate. Falling back
    # to the matcher's own text gives identical patterns a shared witness.
    # (Regression note: an earlier version had this guard, and it was lost when
    # the <unmatched> bucket was replaced by universe expansion.)
    return hits or [f"<pattern:{matcher}>"]


def _scope_tag(settings_file: Path) -> str:
    """A short name a reader can act on.

    NOT ``settings_file.name`` -- every settings file is literally called
    ``settings.json``, so the finding's source set collapses to one entry and
    cannot say which two scopes collided, which is the first thing anyone acting
    on a DEGRADED result wants to know.
    """
    if settings_file.parent == Path.home() / ".claude":
        return "user"
    return f"project:{settings_file.parent.parent.name}"


def _guard_present(script_path: str, guard: str) -> bool:
    """Is the named guard still in the target script?"""
    try:
        return guard in Path(script_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def probe_hook_registrations(now: float) -> Result:
    """Is any hook script registered more than once in the same co-firing set?

    Nothing else in this repo inspects hook registrations, and a prose rule
    documenting the hazard did not prevent it -- nobody re-reads a rule while
    editing a settings file. The check has to be mechanical.

    KNOWN LIMITATION, measured rather than assumed. ``REPO_ROOT`` follows
    whichever checkout this file lives in, so run from a linked worktree the
    probe reads THAT worktree's settings, and the project-scope registration
    resolves to the worktree's own copy of the script -- a different inode from
    the user-scope target, so no duplicate is reported. That is literally true
    (they are two files) but under-reports: both still fire, so the cost is
    doubled anyway. The probe is calibrated for how it actually ships -- the
    daily launchd job, running from the primary checkout, where both
    registrations resolve to one inode and the duplicate IS reported. Detecting
    the two-checkouts-of-one-repo variant needs different reasoning than inode
    identity and is deliberately out of scope here.

    SECOND KNOWN LIMITATION, same class: the interpreter skip fires only at
    argv[0], so a wrapper ahead of an absolute-path interpreter
    (``sudo /venv/bin/python x.py``, ``timeout 5 /venv/bin/python x.py``) shifts
    the interpreter to index 1 and identity binds to the wrapper. No such
    registration exists on this host today (calibration: 44 resolved, 0
    unresolved), so this is disclosed rather than handled.
    """
    parsed_files = 0
    commands = 0
    resolved = 0
    read_errors: list[str] = []
    # (event, tool) -> identity -> [(label, scope, script_path)]
    buckets: dict[tuple[str, str], dict[object, list[tuple[str, str, str]]]] = {}
    # Bucketing is a SECOND pass. The tool universe is not knowable until every
    # matcher has been seen: a matcher naming an MCP or otherwise unlisted tool
    # contributes that tool to the universe, and a matcher-less registration
    # (which means "all tools") has to cover it too. Expanding as we go put the
    # universal registration in the _TOOL_NAMES buckets and the MCP one in a
    # bucket of its own, so a script registered both ways co-fired on that tool
    # and was never compared.
    registrations: list[tuple[str, object, object, str, str, str]] = []
    event_matchers: dict[str, set[str]] = {}
    event_extra_tools: dict[str, set[str]] = {}
    undecidable: list[str] = []

    for settings_file in _SETTINGS_FILES:
        try:
            spec = json.loads(settings_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # An ABSENT optional scope is an empty scope, not a failed read.
            # Treating it as unreadable would park a perfectly normal install --
            # one that simply has no settings.local.json -- at UNKNOWN forever,
            # which is noise, and noise is how a probe gets ignored.
            continue
        except OSError as exc:
            read_errors.append(f"{settings_file}: {type(exc).__name__}")
            continue
        except ValueError as exc:
            read_errors.append(f"{settings_file}: unparseable ({exc})")
            continue
        parsed_files += 1

        for event, groups in (spec.get("hooks") or {}).items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for hook in group.get("hooks", []) or []:
                    command = (hook or {}).get("command", "")
                    if not command:
                        continue
                    commands += 1
                    identity, label, script_path = _hook_invocation(command, settings_file)
                    if isinstance(identity, tuple):
                        resolved += 1
                    matcher = group.get("matcher")
                    registrations.append(
                        (event, matcher, identity, label,
                         _scope_tag(settings_file), script_path))
                    if event in _TOOL_EVENTS and matcher not in (None, ""):
                        event_matchers.setdefault(event, set()).add(matcher)

    # Pass 2. Every matcher's literal branches join that event's tool universe,
    # so registrations naming a tool this probe never heard of are still compared
    # with each other.
    for event, matchers in event_matchers.items():
        extras = set()
        for m in matchers:
            extras.update(_literal_branches(m))
        extras -= set(_TOOL_NAMES)
        if extras:
            event_extra_tools[event] = extras

    # SCOPE OF THIS PROBE'S CLAIM, and the reason it is narrow.
    #
    # Comparison is by shared bucket key, and the keys are witnesses drawn from a
    # finite universe of tool NAMES. That is sound for matchers built entirely
    # from literal names, and unsound for anything else -- a matcher that is a
    # PATTERN cannot share a witness with a registration expanded over names,
    # because the two live in different key spaces. Widening the universe was
    # tried three times and failed three times; it relocates the seam rather than
    # closing it.
    #
    # So a registration whose matcher is not fully literal is NOT compared at
    # all. It goes to UNKNOWN, named. That removes the whole class by
    # construction instead of guarding it: the probe makes exactly one claim and
    # makes it soundly, rather than a broader claim it cannot support.
    #
    # Live cost: zero. Every matcher on this host is an alternation of literals.
    for event, matcher, identity, label, scope, script_path in registrations:
        if event in _TOOL_EVENTS and matcher not in (None, "") and not _fully_literal(matcher):
            undecidable.append(
                f"{event}: matcher {matcher!r} on {label.split(' ', 1)[0]} is not a "
                f"literal tool name, so its co-firing set cannot be decided here"
            )
            continue
        for tool in _matcher_covers(event, matcher, event_extra_tools.get(event, frozenset())):
            entry = buckets.setdefault((event, tool), {}).setdefault(identity, [])
            entry.append((label, scope, script_path))

    if parsed_files == 0:
        # No settings readable at all: coverage is unknown, not clean. Asserting
        # OK from an empty sweep is the failure this whole module exists to stop.
        return Result(
            "hook_registrations", UNKNOWN,
            observed="; ".join(read_errors) or "no settings files readable",
            expected="at least one settings.json parsed",
            remedy="check ~/.claude/settings.json and .claude/settings.json",
        )

    findings: list[str] = []
    stale_acks: list[str] = []
    honoured_acks: list[str] = []
    seen: set[tuple[str, object]] = set()

    for (event, tool), by_identity in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        if tool == _UNDECIDABLE:
            undecidable.append(
                f"{event} carries a matcher whose domain is undocumented "
                f"({sum(len(e) for e in by_identity.values())} registration(s))"
            )
            continue
        for identity, entries in by_identity.items():
            if len(entries) < 2:
                continue
            key = (event, identity)
            if key in seen:
                continue
            seen.add(key)
            label, _src, script_path = entries[0]
            basename = label.split(" ", 1)[0]
            sources = sorted({src for _l, src, _p in entries})
            ack = _ACKNOWLEDGED_DUPLICATES.get((event, basename))
            if ack:
                guard = ack.get("guard", "")
                if script_path and _guard_present(script_path, guard):
                    honoured_acks.append(
                        f"{basename} in {event}: {ack.get('reason', '')} "
                        f"(guard {guard!r} verified present)"
                    )
                else:
                    stale_acks.append(
                        f"{basename} in {event}: guard {guard!r} NO LONGER PRESENT "
                        f"in {script_path or 'unresolved target'}"
                    )
                continue
            findings.append(
                f"{basename} x{len(entries)} co-firing on {event}, from {', '.join(sources)}"
            )

    coverage = (
        f"{parsed_files} file(s), {commands} command(s), {resolved} resolved, "
        f"{commands - resolved} unresolved"
    )
    if read_errors:
        coverage += f"; unreadable: {'; '.join(read_errors)}"

    if stale_acks:
        return Result(
            "hook_registrations", DEGRADED,
            observed="stale acknowledgement - " + "; ".join(stale_acks),
            expected="each acknowledged duplicate's named guard still present",
            remedy="re-review the duplicate, or restore the guard it relies on",
            detail={"coverage": coverage, "duplicates": findings},
        )
    if findings:
        return Result(
            "hook_registrations", DEGRADED,
            observed="duplicate registration: " + "; ".join(findings),
            expected="each hook script registered once per co-firing set",
            remedy="register the hook in ONE scope, or add a reviewed acknowledgement naming its guard",
            detail={"coverage": coverage},
        )
    # ONE completeness gate, deliberately covering every way the sweep can be
    # partial. Three separate defects were filed against this probe in a row --
    # unreadable scope, undecidable matcher, unresolved command -- each the same
    # shape: incomplete coverage reported as OK. Anything that narrows what was
    # actually inspected belongs in this list, not in its own branch, so the
    # NEXT such input cannot be added while forgetting the verdict.
    #
    # Why any of them blocks OK: a duplicate needs TWO registrations to be seen.
    # Miss either half -- because its file would not parse, its co-firing set is
    # undecidable, or its command would not resolve (two shell spellings of one
    # script get different raw-string identities and never match) -- and the
    # duplicate is invisible. "No duplicates found" is then a statement about
    # the sweep, not about the config.
    incomplete: list[str] = list(undecidable)
    if read_errors:
        incomplete.append("could not read " + "; ".join(read_errors))
    if commands - resolved:
        incomplete.append(f"{commands - resolved} command(s) did not resolve to a script")

    if incomplete:
        return Result(
            "hook_registrations", UNKNOWN,
            observed="incomplete sweep - " + "; ".join(incomplete),
            expected="every scope readable, every command resolved, every co-firing set determinable",
            remedy="fix the unreadable settings file, or check the commands that did not resolve",
            detail={"coverage": coverage},
        )
    return Result(
        "hook_registrations", OK,
        # Say WHICH claim is being made. A daily reader should not have to infer
        # the probe's scope from its silence -- "no duplicates" and "no duplicates
        # among the registrations I can decide" are different statements, and only
        # the second one is true.
        observed=f"no duplicate literal-matcher registrations across {coverage}"
                 + (f"; acknowledged: {'; '.join(honoured_acks)}" if honoured_acks else ""),
        expected="each hook script registered once per co-firing set",
        detail={"coverage": coverage},
    )


# ── liveness + loaded config ──────────────────────────────────────────────────


def probe_service(label: str, now: float) -> Result | None:
    """launchd liveness. Returns None where launchd is not the service manager.

    None means "not applicable here", which is different from UNKNOWN. The
    scheduled job is installed on Linux and Windows too (SCHEDULED_JOBS in
    setup/service.ts), so reporting UNKNOWN off-macOS would mark every healthy
    run partially blind and pin the exit code at 4 forever — a permanent false
    alarm, which is the failure mode this tool exists to remove.
    """
    if sys.platform != "darwin":
        return None
    probe = f"service.{label}"
    if not _launch_agent_installed(label):
        # Optional components (OPA is one) are simply absent on a valid
        # install. Reporting a missing optional job as FAILED would alarm
        # every single day on a perfectly healthy machine.
        return None
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(probe, UNKNOWN, observed=f"launchctl unavailable: {exc}",
                      expected="launchctl to be runnable",
                      remedy="this probe assumes macOS launchd")
    kind = _launch_agent_kind(label)
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            pid = parts[0].strip()
            exit_status = parts[1].strip()
            running = pid != "-"

            if kind is None:
                # Unreadable plist, or a trigger shape with no bucket (e.g.
                # WatchPaths). There is no positive evidence for any verdict.
                return Result(probe, UNKNOWN,
                              observed=f"pid={pid} exit={exit_status}, unrecognised job shape",
                              expected="a classifiable launchd job",
                              remedy=f"inspect ~/Library/LaunchAgents/{label}.plist")

            if kind == RESIDENT:
                if running:
                    return Result(probe, OK, observed=f"running pid={pid}",
                                  expected="a live PID (KeepAlive)")
                # Deliberately asymmetric with every other kind: a never-run
                # status ("-") is FAILED here, not UNKNOWN. An unconditional
                # KeepAlive job that has never started IS the anomaly -- launchd
                # starts it on load and restarts it on exit, so "no PID and no
                # completed run" is the failure, not an absence of evidence.
                # The sibling kinds are all supposed to finish; this one is not.
                never_ran = _never_ran(exit_status)
                detail = "never started" if never_ran else f"exit {exit_status}"
                return Result(probe, FAILED,
                              observed=f"KeepAlive job not running ({detail})",
                              expected="a live PID",
                              remedy=f"launchctl kickstart -k gui/$(id -u)/{label}")

            # SCHEDULED and RUN_ONCE: the job is meant to finish, so an absent
            # PID is expected and says nothing on its own.
            #
            # `running` is checked FIRST, the same order the RESIDENT branch
            # uses above -- keep that convention for every kind. LastExitStatus
            # holds the PREVIOUS invocation's code until the current one exits;
            # launchd does not clear it while a job is mid-run. Asserting on it
            # while the job is running would report FAILED for a job that failed
            # once and is now legitimately re-running, which is the same false
            # alarm this rewrite exists to remove.
            #
            # NOTE: phase 1 does not check staleness -- exit 0 from a run that
            # last happened long ago still reports OK. A last-run signal needs a
            # per-job sentinel; log mtime cannot distinguish ran-and-was-silent
            # from did-not-run (judge_runner's log is 0 bytes). Tracked separately.
            if running:
                return Result(probe, OK,
                              observed=f"running pid={pid} (previous exit {exit_status})",
                              expected="the current run to be in progress")
            if exit_status == "0":
                return Result(probe, OK, observed="last exit 0 (idle between runs)",
                              expected="exit 0 from its last run")
            if _never_ran(exit_status):
                # `launchctl list` shows "-" in the Status column for a job that
                # has never run -- a freshly loaded one awaiting its first
                # invocation. There is no run to have failed, so FAILED would be
                # a verdict about an event that has not happened. OK would be
                # worse: it asserts health from no evidence at all.
                return Result(probe, UNKNOWN,
                              observed=f"never run (status {exit_status!r})",
                              expected="a completed run to judge",
                              remedy=f"wait for its schedule, or launchctl kickstart -k gui/$(id -u)/{label}")
            return Result(probe, FAILED, observed=f"last run exited {exit_status}",
                          expected="exit 0 from its last run",
                          remedy=f"check the job's log, then launchctl kickstart -k gui/$(id -u)/{label}")

    return Result(probe, FAILED, observed="not loaded in launchctl", expected="the job to be loaded",
                  remedy=f"launchctl load ~/Library/LaunchAgents/{label}.plist")


def probe_opa_policy(now: float, marker: str, url: str) -> Result | None:
    """Verify the policy the daemon has LOADED, not the file on disk.

    A live test once passed against a daemon serving a policy that did not
    contain the rule under test; the file on disk was current and the process
    was up, and both facts were irrelevant.
    """
    probe = "service.warden-opa.policy"
    if sys.platform != "darwin" or not _launch_agent_installed("com.deus.warden-opa"):
        # OPA is an optional macOS/launchd install; Linux packaging is out of
        # scope and plenty of valid installs simply do not run it. Probing it
        # unconditionally would alarm every healthy run forever.
        return None
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Result(probe, UNKNOWN, observed=f"{url} unreachable: {type(exc).__name__}",
                      expected="the OPA policy API to answer",
                      remedy="launchctl kickstart -k gui/$(id -u)/com.deus.warden-opa")
    raw = " ".join(p.get("raw", "") for p in body.get("result", []))
    if not raw:
        return Result(probe, UNKNOWN, observed="policy API returned no policy text",
                      expected="loaded policy source", remedy="check the OPA daemon")
    if marker not in raw:
        return Result(probe, DEGRADED,
                      observed=f"loaded policy does not contain {marker!r}",
                      expected=f"the loaded policy to contain {marker!r}",
                      remedy="launchctl kickstart -k gui/$(id -u)/com.deus.warden-opa")
    return Result(probe, OK, observed=f"loaded policy contains {marker!r}",
                  expected=f"the loaded policy to contain {marker!r}")


# ── run / render ──────────────────────────────────────────────────────────────


# No longer anyone's default: discovery is (LIA-136), and this hardcoded pair is
# how five of seven jobs went unwatched while one failed nightly. Kept as the
# historical list the suppression test names, not as a fallback -- a discovery
# failure reports UNKNOWN rather than quietly reverting to these two.
DEFAULT_SERVICE_LABELS = ("com.deus", "com.deus.warden-opa")


def run_probes(now: float, log_path: Path, window_sec: float,
               opa_marker: str, opa_url: str,
               service_labels: tuple[str, ...] | None = None) -> list[Result]:
    demand = read_demand(log_path, window_sec, now)
    results = [probe_optimizer(now), probe_ingest(now, demand), probe_memory(now),
               probe_hook_registrations(now)]
    # `com.deus` is the label actually loaded. A `com.deus-v2.plist` also exists
    # on some installs but is not the running job — probing it reports a
    # confident FAILED about a service nobody runs. `_deus_service_labels`
    # filters iterdir() entries on the literal `com.deus.` prefix, so that
    # decoy cannot match. It deliberately does NOT glob -- see the note on
    # _deus_service_labels for why glob() cannot be used here.
    if service_labels is None:
        # No explicit --service-labels: discover, so a newly installed job is
        # covered without anyone remembering to edit a tuple (LIA-136). The
        # sentinel is None rather than a value equal to DEFAULT_SERVICE_LABELS,
        # so a caller who deliberately passes exactly the default list still
        # suppresses discovery.
        discovered = _deus_service_labels()
        if discovered is None:
            # Cannot enumerate: probe coverage is unknown, not merely default.
            # Falling back to the short list here would let the cockpit report
            # OK while an undiscovered job fails — the original blind spot.
            results.append(Result(
                "service.discovery", UNKNOWN,
                observed="cannot enumerate ~/Library/LaunchAgents",
                expected="the LaunchAgents directory to be readable",
                remedy="check permissions on ~/Library/LaunchAgents",
            ))
            service_labels = ()
        else:
            service_labels = discovered
    for label in service_labels:
        service = probe_service(label, now)
        if service is not None:
            results.append(service)
    opa = probe_opa_policy(now, opa_marker, opa_url)
    if opa is not None:
        results.append(opa)
    return results


def merge_history(results: list[Result], previous: dict, now: float) -> dict:
    """Carry per-probe streaks so a regression is distinguishable from a rut.

    Requirement 6: a subsystem that has been broken for a week should stay in
    the report but stop shouting, or the user learns to skip the whole thing.
    """
    prev = {p["probe"]: p for p in previous.get("probes", [])}
    out = []
    for r in results:
        rec = asdict(r)
        was = prev.get(r.probe, {})
        bad = r.status != OK
        was_bad = was.get("status", OK) != OK
        rec["first_bad_at"] = (
            was.get("first_bad_at") if bad and was_bad else (now if bad else None)
        )
        rec["consecutive_bad_runs"] = (was.get("consecutive_bad_runs", 0) + 1) if bad and was_bad else (1 if bad else 0)
        rec["is_regression"] = bad and not was_bad
        out.append(rec)
    return {"checked_at": now, "probes": out}


def overall(results: list[Result]) -> str:
    return max((r.status for r in results), key=lambda s: _RANK[s], default=UNKNOWN)


def exit_code(results: list[Result]) -> int:
    statuses = [r.status for r in results]
    if not statuses:
        return EXIT_ALL_UNKNOWN
    if FAILED in statuses:
        return EXIT_FAILED
    if all(s == UNKNOWN for s in statuses):
        return EXIT_ALL_UNKNOWN
    if UNKNOWN in statuses:
        return EXIT_PARTIAL_UNKNOWN
    return EXIT_OK


def render_line(state: dict) -> str:
    bad = [p for p in state["probes"] if p["status"] != OK]
    if not bad:
        return "OK"
    # Rank first, then regressions win ties: an ongoing failure would otherwise
    # keep hiding a new one of equal severity behind it, which is precisely the
    # news the reader needs today.
    worst = max(bad, key=lambda p: (_RANK[p["status"]], bool(p["is_regression"])))
    tag = "NEW" if worst["is_regression"] else "ONGOING"
    return f"{worst['status']} {worst['probe']} ({tag}) — {worst['observed']}"


def render_report(state: dict) -> str:
    lines = ["=== Deus cockpit ==="]
    for p in state["probes"]:
        mark = "" if p["status"] == OK else (
            "  NEW" if p["is_regression"] else f"  ONGOING x{p['consecutive_bad_runs']}"
        )
        lines.append(f"[{p['status']:<8}] {p['probe']}{mark}")
        if p["status"] != OK:
            lines.append(f"             observed: {p['observed']}")
            if p["expected"]:
                lines.append(f"             expected: {p['expected']}")
            if p["remedy"]:
                lines.append(f"             fix:      {p['remedy']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    ap.add_argument("--brief", action="store_true", help="print the cached line; never probes")
    ap.add_argument("--log", type=Path, default=REPO_ROOT / "logs" / "deus.log")
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--opa-marker", default="package deus.wardens")
    ap.add_argument("--opa-url", default="http://127.0.0.1:8181/v1/policies")
    # default=None, NOT the joined default list: run_probes distinguishes
    # "no override given" (discover) from an explicit choice, and a caller who
    # deliberately passes exactly the default string must still suppress
    # discovery. A value-equal sentinel cannot tell those apart.
    ap.add_argument("--service-labels", default=None,
                    help="comma-separated launchd labels to probe "
                         "(default: discover every installed com.deus job)")
    args = ap.parse_args(argv)

    if args.brief:
        try:
            age = time.time() - ARTIFACT_LINE.stat().st_mtime
            line = ARTIFACT_LINE.read_text().strip()
        except OSError:
            # Silence here would be the very bug this tool exists to catch.
            print("cockpit: no healthcheck result on record")
            return EXIT_ALL_UNKNOWN
        if not line:
            # A truncated/empty artifact is a broken checker, not a clean bill.
            print("cockpit: healthcheck result is empty — the checker may have failed mid-write")
            return EXIT_ALL_UNKNOWN
        if age > ARTIFACT_MAX_AGE_SEC:
            print(f"cockpit: last result is {age / 3600:.0f}h old — healthcheck may not be running")
            return EXIT_ALL_UNKNOWN
        if line != OK:
            print(f"cockpit: {line}")
        return EXIT_OK

    now = time.time()
    results = run_probes(now, args.log, args.window_hours * 3600,
                         args.opa_marker, args.opa_url,
                         tuple(args.service_labels.split(","))
                         if args.service_labels is not None else None)
    try:
        previous = json.loads(ARTIFACT_JSON.read_text())
    except (OSError, ValueError):
        previous = {}
    state = merge_history(results, previous, now)
    code = exit_code(results)

    try:
        ARTIFACT_JSON.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a plain write truncates first, so a failure midway
        # would destroy the previous verdict AND leave an empty file that
        # --brief would read as healthy. Losing evidence while looking fine is
        # the failure mode this tool exists to remove.
        _atomic_write(ARTIFACT_JSON, json.dumps(state, indent=2, sort_keys=True))
        _atomic_write(ARTIFACT_LINE, render_line(state) + "\n")
    except OSError as exc:
        print(f"cockpit: could not write artifact: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED

    print(json.dumps(state, indent=2, sort_keys=True) if args.as_json else render_report(state))
    return code


if __name__ == "__main__":
    sys.exit(main())
