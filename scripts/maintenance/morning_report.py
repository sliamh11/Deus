#!/usr/bin/env python3
"""
Morning memory report (LIA-254) — daily "while you slept" digest.

At 07:00 (launchd/systemd/schtasks), summarize what changed overnight and
deliver it to the CONTROL GROUP chat: memory growth since yesterday + the
04:30 maintenance run's outcomes (any FAILED tasks or WARNs — a credential
expiry, a judge-calibration regression, etc. surface here instead of sitting
silently in a log nobody reads).

Why host-side (not a scheduler agent task): both data sources are unreachable
from a container — `logs/` is shadow-mounted empty for the control-group agent
(container-mounter.ts, LIA-210) and ~/.deus/ is outside the project root. So
this runs host-side (full fs access) and delivers via the same IPC file-drop
mechanism auth-refresh.ts uses: write a schema-valid message file into the
control group's IPC dir and the in-process watcher sends it to the chat.

Read-only on its sources. Reading ~/.deus/memory_health.jsonl is NOT re-running
`--health` — it's the persisted artifact the 04:30 run already wrote.

Exit 0 on a successful delivery OR a benign skip (no control group registered
yet, no data to report) — a benign skip is not a launchd failure. Exit nonzero
only on an unexpected error so the scheduler's error log surfaces real breakage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# A line is a real warning if it carries a WARN/REGRESSION token but is NOT a
# count-summary like "credential_probe: 2 OK, 0 WARN, 0 skipped" or
# "=== Done: 4 OK, 2 failed ===" (those contain "N OK" and a literal "0 WARN"
# substring that must not be mistaken for an actual warning).
_SUMMARY_RE = re.compile(r"\b\d+\s+OK\b")

# __file__-relative so resolution is identical regardless of the scheduler's
# cwd (Windows schtasks passes no working directory). Mirrors credential_probe.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _notify import macos_notify  # noqa: E402  (shared maintenance notifier)

DATA_DIR = _REPO_ROOT / "data"
STORE_DIR = _REPO_ROOT / "store"
# Both default to fixed, cwd-independent paths; env overrides for tests/installs.
DEFAULT_HEALTH = Path(
    os.environ.get(  # LIA-254
        "DEUS_MORNING_REPORT_HEALTH", str(Path("~/.deus/memory_health.jsonl").expanduser())
    )
)
DEFAULT_MAINT_LOG = Path(
    os.environ.get("DEUS_MORNING_REPORT_MAINT_LOG", str(_REPO_ROOT / "logs" / "maintenance.log"))  # LIA-254
)
# The cockpit healthcheck's persisted verdict. Same contract as DEFAULT_HEALTH
# above: this report READS the artifact the 06:45 run already wrote, it never
# re-probes.
DEFAULT_COCKPIT = Path(
    os.environ.get(  # LIA-552
        "DEUS_MORNING_REPORT_COCKPIT", str(Path("~/.deus/cockpit_health.json").expanduser())
    )
)
#: At this cadence (cockpit 06:45 -> report 07:00) the artifact's age here is
#: ~0.25h normally, ~24.25h after one missed run and ~48.25h after two — so
#: every threshold in (24.25h, 48.25h) behaves identically and 30h is simply a
#: midpoint. It therefore flags TWO consecutive missed runs, not one. Catching a
#: single miss would need a threshold below 24.25h, which would false-alarm
#: whenever launchd reschedules a run after a sleeping machine wakes.
COCKPIT_MAX_AGE_SEC = 30 * 3600
#: A verdict is only "missing" if something was supposed to write one. An
#: optional component that was never enabled must read as not-applicable, never
#: as broken (mirrors _launch_agent_installed in cockpit_healthcheck.py).
#:
#: One marker per platform, because setup installs this job on all three
#: (SCHEDULED_JOBS in setup/service.ts): launchd, systemd --user or system, and
#: Task Scheduler. Checking only the macOS plist would silently omit every
#: verdict — failures included — on supported Linux and Windows installs. The
#: systemd names track installScheduledJobLinux's `deus-${id}` unit base and its
#: root-vs-user directory split.
_JOB_ID = "cockpit-healthcheck"
COCKPIT_JOB_MARKERS = (
    Path(f"~/Library/LaunchAgents/com.deus.{_JOB_ID}.plist").expanduser(),
    Path(f"~/.config/systemd/user/deus-{_JOB_ID}.timer").expanduser(),
    Path(f"/etc/systemd/system/deus-{_JOB_ID}.timer"),
)
#: Windows has no install marker on disk; Task Scheduler is queried instead.
COCKPIT_TASK_NAME = "DeusCockpitHealthcheck"


def _read_health(path: Path) -> "tuple[dict | None, dict | None]":
    """Return (latest, previous) health snapshots from the append-only JSONL.

    Each line is one day's structured snapshot (memory_indexer --health). The
    previous line enables overnight-delta reporting. Missing/malformed lines are
    skipped; a missing file yields (None, None).
    """
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None, None
    # Scan from the END collecting the last two VALID snapshots, so a malformed
    # trailing (or interior) line can't hide an otherwise-valid prior snapshot
    # and silently drop the overnight delta.
    snaps: list[dict] = []
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            snaps.append(obj)
            if len(snaps) == 2:
                break
    if not snaps:
        return None, None
    return snaps[0], (snaps[1] if len(snaps) >= 2 else None)


def _cockpit_expected() -> bool:
    """Was a cockpit verdict supposed to exist? True iff its scheduled job is installed.

    Absence is only a signal where something was expected. Without this, every
    install that never enabled the cockpit would receive a "no verdict on
    record" warning every single day.
    """
    if any(m.is_file() for m in COCKPIT_JOB_MARKERS):
        return True
    if sys.platform.startswith("win"):
        try:
            return subprocess.run(
                ["schtasks", "/Query", "/TN", COCKPIT_TASK_NAME],
                capture_output=True, timeout=10,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            # Cannot tell. Say "not expected" rather than emit a daily
            # missing-verdict warning we are not sure is warranted.
            return False
    return False


def _read_cockpit(path: Path) -> "dict | None":
    """The cockpit's last verdict, or None if unavailable for any reason.

    Tolerant in the same way as _read_health: a missing, unreadable, malformed
    or unexpectedly-shaped artifact must never take down the whole morning
    report. None means "no usable verdict"; the caller decides whether that is
    newsworthy.
    """
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    # Validate field shapes, not just the top-level type. This artifact is
    # written by a separate process — a real system boundary — and a string
    # `checked_at` or a non-list `probes` would otherwise raise out of the
    # formatter and take down the whole unattended report.
    # bool is excluded explicitly because it subclasses int, so a JSON `true`
    # would otherwise satisfy a check whose whole job is rejecting wrong shapes
    # and then format an age off 1.0.
    checked_at = obj.get("checked_at")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return None
    probes = obj.get("probes")
    if not isinstance(probes, list):
        return None
    if not all(isinstance(p, dict) for p in probes):
        # A non-dict element would be skipped when collecting failures but still
        # counted in the probe total, manufacturing a false-clean verdict out of
        # garbage. Reject the artifact rather than report a number that is not
        # what it says it is.
        return None
    return obj


def _parse_last_maintenance_run(path: Path) -> "dict | None":
    """Parse the LAST maintenance run block from the log, or None if no run.

    Looks for the final `=== Deus maintenance — ... ===` header and reads from
    there: collects `[name] FAILED/TIMEOUT/ERROR` task lines, any WARN/REGRESSION
    lines, and the closing `=== Done: N OK, M failed ===` counts.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if "=== Deus maintenance" in lines[i]:
            start = i
            break
    if start is None:
        return None
    block = lines[start:]
    header = lines[start].strip().strip("= ").strip()
    failed: list[str] = []
    warns: list[str] = []
    ok = 0
    done = None
    for ln in block:
        s = ln.strip()
        # Task status lines look like "  [name] FAILED (exit 1)" / "[name] OK".
        if s.startswith("[") and "]" in s:
            verdict = s.split("]", 1)[1].strip()
            name = s[1 : s.index("]")]
            if verdict.startswith(("FAILED", "TIMEOUT", "ERROR")):
                failed.append(name)
        if (
            ("WARN" in s or "REGRESSION" in s)
            and not _SUMMARY_RE.search(s)
            and s not in warns
        ):
            warns.append(s)
        if s.startswith("=== Done:"):
            done = s.strip("= ").strip()
            # "Done: N OK, M failed" — regex is robust to format drift; a
            # non-match leaves ok=0 rather than crashing on a split chain.
            m = re.search(r"Done:\s*(\d+)\s*OK", done)
            ok = int(m.group(1)) if m else 0
    return {"ran": True, "header": header, "ok": ok, "failed": failed, "warns": warns, "done": done}


def _fmt_delta(cur, prev, *, places: int = 0) -> str:
    """Signed delta annotation like ' (+3)' / ' (-0.012)', or '' if no prior."""
    if prev is None or not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return ""
    d = cur - prev
    if abs(d) < 0.0005:
        return ""
    return f" ({d:+.{places}f})"


def _format_cockpit(cockpit: "dict | None", expected: bool, now: float) -> "list[str]":
    """The cockpit section, or nothing at all when no verdict was expected.

    Three states, and the middle one is why this exists: a verdict that should
    be here and is not must be reported, never rendered as silence — a dead
    subsystem looked like a quiet one for five months (LIA-551/LIA-552).
    """
    if not expected:
        return []  # never enabled here: not-applicable, not broken
    # Every line this returns carries the "Cockpit (06:45)" label or sits
    # indented beneath it. An unlabelled warning would render directly under the
    # "Maintenance (04:30)" line, where the existing `  ⚠️ {warn}` convention
    # would make a reader attribute it to maintenance instead.
    if cockpit is None:
        return ["Cockpit (06:45): ⚠️ no verdict on record (healthcheck may never have run)"]

    probes = cockpit.get("probes") or []
    age_sec = now - float(cockpit.get("checked_at") or 0)

    bad = [p for p in probes if p.get("status") != "OK"]
    ok_count = len(probes) - len(bad)
    line = f"Cockpit (06:45): {ok_count} OK"
    if bad:
        line += f", {len(bad)} not OK"
    out: list[str] = [line]
    if age_sec > COCKPIT_MAX_AGE_SEC:
        out.append(
            f"  ⚠️ verdict is {age_sec / 3600:.0f}h old (healthcheck may not be running)"
        )
    for p in bad:
        # The raw status is kept: the cockpit distinguishes DEGRADED, UNKNOWN
        # and FAILED (cockpit_healthcheck.py's _RANK), and collapsing them would
        # throw away a distinction it goes out of its way to preserve.
        tag = "NEW" if p.get("is_regression") else "ONGOING"
        name = p.get("probe") or "<unnamed probe>"
        why = p.get("observed") or "no detail recorded"
        out.append(f"  {p.get('status')} {name} ({tag}) — {why}")
    return out


def _format_digest(latest: "dict | None", prev: "dict | None", maint: "dict | None", today: str,
                   cockpit: "dict | None" = None, cockpit_expected: bool = False,
                   now: "float | None" = None) -> str:
    """Build the concise, skimmable 'while you slept' digest. Pure function."""
    out: list[str] = [f"🌙 While you slept — {today}"]

    if latest:
        atoms = latest.get("atoms")
        conf = latest.get("avg_confidence")
        d_atoms = _fmt_delta(atoms, (prev or {}).get("atoms"), places=0)
        d_conf = _fmt_delta(conf, (prev or {}).get("avg_confidence"), places=3)
        out.append(
            f"Memory: {atoms} atoms{d_atoms} · avg confidence "
            f"{conf:.3f}{d_conf}" if isinstance(conf, (int, float)) else f"Memory: {atoms} atoms{d_atoms}"
        )
        bits = []
        for label, key in (("sessions", "sessions"), ("entities", "entities"), ("articles", "articles")):
            v = latest.get(key)
            if isinstance(v, int):
                bits.append(f"{v} {label}")
        stale = latest.get("articles_stale")
        if isinstance(stale, int) and stale:
            bits.append(f"{stale} stale")
        if bits:
            out.append("  " + " · ".join(bits))
        snap_date = latest.get("date")
        if snap_date and snap_date != today:
            out.append(f"  ⚠️ health snapshot is from {snap_date} (no fresh 04:30 run?)")
    else:
        out.append("Memory: no health snapshot yet.")

    if maint and maint.get("ran"):
        failed = maint.get("failed") or []
        line = f"Maintenance (04:30): {maint.get('ok', 0)} OK"
        if failed:
            line += f", {len(failed)} failed: {', '.join(failed)}"
        out.append(line)
        for w in maint.get("warns") or []:
            out.append(f"  ⚠️ {w}")
    else:
        out.append("Maintenance: no overnight run found.")

    out.extend(_format_cockpit(cockpit, cockpit_expected,
                               now if now is not None else time.time()))

    return "\n".join(out)


def _find_control_group(db_path: Path) -> "tuple[str, str] | None":
    """(folder, jid) of the control group (registered_groups.is_main=1), or None."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT folder, jid FROM registered_groups WHERE is_main = 1 LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1])


def _deliver(data_dir: Path, folder: str, jid: str, text: str, ts: int) -> bool:
    """Drop a schema-valid IPC message file for the control group; the in-process
    watcher picks it up and sends `text` to `jid`. Returns True on write success.

    A control-group-FOLDER-sourced drop may target any jid (ipc.ts authorize),
    and the watcher validates against IpcMessageFileSchema {type, chatJid?, text?}.
    """
    # Defense-in-depth: `folder` is DB-sourced (validated at insert), but this is
    # a standalone host script building a filesystem path from it — reject any
    # value that could escape data/ipc/ before writing.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", folder):
        return False
    messages_dir = data_dir / "ipc" / folder / "messages"
    try:
        messages_dir.mkdir(parents=True, exist_ok=True)
        payload = {"type": "message", "chatJid": jid, "text": text, "source": "morning-report"}
        (messages_dir / f"morning-report-{ts}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except OSError:
        return False


def main(argv: "list[str] | None" = None, deliverer=_deliver, notifier=macos_notify,
         now: "float | None" = None, cockpit_expected=_cockpit_expected) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--health", type=Path, default=DEFAULT_HEALTH)
    parser.add_argument("--maint-log", type=Path, default=DEFAULT_MAINT_LOG)
    parser.add_argument("--db", type=Path, default=STORE_DIR / "messages.db")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cockpit", type=Path, default=DEFAULT_COCKPIT)
    args = parser.parse_args(argv)

    now_s = now if now is not None else time.time()
    ts = int(now_s * 1000)
    latest, prev = _read_health(args.health)
    maint = _parse_last_maintenance_run(args.maint_log)
    cockpit = _read_cockpit(args.cockpit)
    expected = cockpit_expected()

    # Nothing to report at all (fresh install, or the cockpit job uninstalled
    # after having run): benign skip. Keyed on whether a cockpit verdict was
    # EXPECTED, never on whether one is present — presence cuts both ways and
    # neither direction is a reason to report. Requiring the artifact to be
    # ABSENT would keep the skip from firing once the scheduled job is
    # uninstalled but its last JSON artifact is still on disk, and since
    # _format_cockpit deliberately renders nothing when `expected` is False,
    # that stale file would buy a header-and-fallbacks-only digest every single
    # day. Requiring it to be PRESENT would exit precisely when an expected
    # verdict is missing — the one case that most needs reporting — which
    # `not expected` already rules out on its own.
    if latest is None and maint is None and not expected:
        print("morning_report: no health snapshot or maintenance log yet — nothing to report")
        return 0

    today = time.strftime("%Y-%m-%d", time.localtime(now_s))
    digest = _format_digest(latest, prev, maint, today, cockpit, expected, now_s)

    control = _find_control_group(args.db)
    if control is None:
        # No control group registered (fresh install): fall back to a desktop
        # banner so the digest isn't silently lost, and skip cleanly.
        print("morning_report: no control group registered — skipping chat delivery")
        print(digest)
        notifier("Deus morning report", digest)
        return 0

    folder, jid = control
    if deliverer(args.data_dir, folder, jid, digest, ts):
        print(f"morning_report: delivered to control group ({folder})")
        return 0
    print("morning_report: IPC delivery failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
