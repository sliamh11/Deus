#!/usr/bin/env python3
"""
Deus log review and rotation system.

Reads pino-pretty main logs and container session logs, extracts warnings/errors,
analyzes them with a local Ollama model, saves a daily report, and pins critical issues.
Also rotates old logs to prevent disk bloat.

Usage:
    python3 scripts/log_review.py                  # full run (rotate + review)
    python3 scripts/log_review.py --rotate-only    # rotation only
    python3 scripts/log_review.py --review-only    # review without rotation
    python3 scripts/log_review.py --summary        # print last saved report
    python3 scripts/log_review.py --pinned         # print pinned issues

Environment overrides:
    LOG_REVIEW_MODEL              Ollama model (default: gemma4:e4b)
    OLLAMA_HOST                   Ollama base URL (default: http://localhost:11434)
    LOG_CONTAINER_RETENTION_DAYS  Days to keep container logs (default: 14)
    LOG_MAIN_MAX_MB               Rotate main log when it exceeds this size (default: 20)
    LOG_ARCHIVE_RETENTION_DAYS    Days to keep archived logs (default: 30)
"""

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Local helpers — _time.py lives next to this script.
sys.path.insert(0, str(Path(__file__).parent))
from _time import local_now, utc_now  # noqa: E402

# ── Paths ─────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / 'logs'
GROUPS_DIR = PROJECT_ROOT / 'groups'
DEUS_DIR = Path.home() / '.deus'
STATE_FILE = DEUS_DIR / 'log_review_state.json'
REPORTS_DIR = DEUS_DIR / 'reviews'
PINNED_FILE = REPORTS_DIR / 'pinned.md'

# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('LOG_REVIEW_MODEL', 'gemma4:e4b')
CONTAINER_LOG_RETENTION_DAYS = int(os.environ.get('LOG_CONTAINER_RETENTION_DAYS', '14'))
MAIN_LOG_MAX_MB = int(os.environ.get('LOG_MAIN_MAX_MB', '20'))
ARCHIVE_RETENTION_DAYS = int(os.environ.get('LOG_ARCHIVE_RETENTION_DAYS', '30'))
MAX_ENTRIES_PER_REVIEW = 150   # cap to avoid huge Ollama prompts

# ── Evolution logging heartbeat (LIA-195) ──────────────────────────────────────
# Detect a silent evolution-logging outage (like LIA-194, dead ~5 days unnoticed):
# dispatches are happening but no interactions are being logged. Same DEUS_EVOLUTION_DB
# env var evolution/config.py uses (NOT net-new). STALE_H defaults to 48h = the
# activity window: a healthy low-traffic day still logs within the window, so it only
# fires on a sustained >=48h of dispatch activity with zero logged interactions.
EVOLUTION_DB = Path(
    os.environ.get('DEUS_EVOLUTION_DB', '~/.deus/evolution.db')
).expanduser()
HEARTBEAT_ACTIVITY_WINDOW_H = float(
    os.environ.get('EVOLUTION_HEARTBEAT_ACTIVITY_WINDOW_H', '48')
)
HEARTBEAT_STALE_H = float(os.environ.get('EVOLUTION_HEARTBEAT_STALE_H', '48'))

# ── Parsing ───────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07')
_BOX_RE = re.compile(r'[\u2500-\u257f\u2580-\u259f\u2800-\u28ff]')
# pino-pretty: [HH:MM:SS.mmm] LEVEL (pid): message
_PINO_RE = re.compile(
    r'^\[(\d{2}:\d{2}:\d{2}\.\d{3})\]\s+(WARN|ERROR|FATAL)\s+\(\d+\):\s+(.+)$'
)
# Container log header fields
_CONTAINER_META_RE = re.compile(r'^(Timestamp|Exit Code|Duration|Group):\s+(.+)$')
_CONTAINER_ERROR_RE = re.compile(
    r'(error|warn|fail|exception|traceback|crash|killed|sigterm|sigsegv)',
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Strip ANSI codes, box-drawing chars, and control characters."""
    text = _ANSI_RE.sub('', text)
    text = _BOX_RE.sub('', text)
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text).strip()


def parse_pino_log(path: Path, from_byte: int = 0) -> tuple[list[dict], int]:
    """
    Read pino-pretty log from byte offset.
    Returns (warn/error entries, new byte offset).
    """
    entries: list[dict] = []
    try:
        size = path.stat().st_size
        if from_byte > size:
            from_byte = 0  # file was truncated/rotated
        with open(path, 'rb') as f:
            f.seek(from_byte)
            raw = f.read()
        new_offset = from_byte + len(raw)
        for line in raw.decode('utf-8', errors='replace').splitlines():
            clean = _clean(line)
            if not clean:
                continue
            m = _PINO_RE.match(clean)
            if m:
                entries.append({
                    'time': m.group(1),
                    'level': m.group(2),
                    'message': m.group(3).strip(),
                    'source': path.name,
                })
    except OSError:
        new_offset = from_byte
    return entries, new_offset


def parse_container_log(path: Path) -> dict:
    """
    Parse a container session log.
    Returns metadata + any error lines found.
    """
    meta: dict = {'path': str(path), 'errors': []}
    try:
        content = path.read_text(errors='replace')
        for line in content.splitlines():
            m = _CONTAINER_META_RE.match(line.strip())
            if m:
                meta[m.group(1).lower().replace(' ', '_')] = m.group(2).strip()
            elif _CONTAINER_ERROR_RE.search(line):
                clean = _clean(line)
                if clean and len(clean) > 10:
                    meta['errors'].append(clean[:200])
        # Deduplicate errors
        seen: set = set()
        unique = []
        for e in meta['errors']:
            k = e[:60]
            if k not in seen:
                seen.add(k)
                unique.append(e)
        meta['errors'] = unique[:10]
    except OSError:
        pass
    return meta


# ── Rotation ──────────────────────────────────────────────────────────────────

def rotate_container_logs() -> int:
    """Delete container logs older than retention period. Returns count deleted."""
    cutoff = utc_now() - timedelta(days=CONTAINER_LOG_RETENTION_DAYS)
    deleted = 0
    for log_file in GROUPS_DIR.glob('*/logs/container-*.log'):
        try:
            mtime = datetime.fromtimestamp(
                log_file.stat().st_mtime, tz=timezone.utc
            )
            if mtime < cutoff:
                log_file.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


def rotate_main_logs() -> list[str]:
    """
    Archive main logs that exceed MAIN_LOG_MAX_MB by gzip-compressing them
    to logs/archives/ and truncating the original.
    Truncation is safe with launchd (O_APPEND): the running process will
    continue writing at offset 0 of the now-empty file.
    Also deletes archives older than ARCHIVE_RETENTION_DAYS.
    Returns list of human-readable action strings.
    """
    actions: list[str] = []
    archive_dir = LOGS_DIR / 'archives'
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale archives
    cutoff = utc_now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    for f in archive_dir.glob('*.gz'):
        try:
            if (
                datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                < cutoff
            ):
                f.unlink()
                actions.append(f'deleted old archive: {f.name}')
        except OSError:
            pass

    for log_file in [LOGS_DIR / 'deus.log', LOGS_DIR / 'deus.error.log']:
        if not log_file.exists():
            continue
        size_mb = log_file.stat().st_size / (1024 * 1024)
        if size_mb <= MAIN_LOG_MAX_MB:
            continue
        stamp = local_now().strftime('%Y-%m-%d-%H%M%S')
        archive = archive_dir / f'{log_file.stem}.{stamp}.log.gz'
        try:
            with open(log_file, 'rb') as f_in, gzip.open(archive, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            # Truncate in place after successful archive
            with open(log_file, 'w'):
                pass
            actions.append(f'archived {size_mb:.1f} MB → {archive.name}')
        except OSError as exc:
            actions.append(f'failed to archive {log_file.name}: {exc}')

    return actions


# ── Ollama ────────────────────────────────────────────────────────────────────

def _ollama_available() -> bool:
    try:
        urllib.request.urlopen(
            f'{OLLAMA_HOST.rstrip("/")}/api/tags', timeout=3
        )
        return True
    except Exception:
        return False


def _call_ollama(prompt: str) -> str:
    url = f'{OLLAMA_HOST.rstrip("/")}/api/generate'
    body = json.dumps({
        'model': OLLAMA_MODEL,
        'prompt': prompt,
        'stream': False,
        'options': {'temperature': 0.1},
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())['response'].strip()
    except urllib.error.HTTPError as exc:
        return f'[Ollama error {exc.code}: {exc.reason}]'
    except Exception as exc:
        return f'[Ollama unavailable: {type(exc).__name__}: {exc}]'


# ── Review ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    DEUS_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Evolution logging heartbeat (LIA-195) ──────────────────────────────────────

def _heartbeat_verdict(
    last_dispatch_ts: Optional[float],
    last_log_ts: Optional[float],
    now_ts: float,
    activity_window_h: float,
    stale_h: float,
) -> tuple[bool, Optional[str]]:
    """Pure decision: is evolution logging stale despite active dispatches?

    STALE when a dispatch ran within ``activity_window_h`` but the most recent
    logged interaction is older than ``stale_h`` (or there are none). An idle
    system (no recent dispatch) never alarms. No IO — unit-tested exhaustively.
    """
    if last_dispatch_ts is None:
        return False, None  # no activity proxy — can't judge
    dispatch_age_h = (now_ts - last_dispatch_ts) / 3600.0
    if dispatch_age_h > activity_window_h:
        return False, None  # system idle — not an outage
    if last_log_ts is None:
        return True, (
            f'dispatch ran {dispatch_age_h:.0f}h ago but evolution.db has no '
            'logged interactions at all'
        )
    log_age_h = (now_ts - last_log_ts) / 3600.0
    if log_age_h > stale_h:
        return True, (
            f'last interaction {log_age_h:.0f}h ago but a dispatch ran '
            f'{dispatch_age_h:.0f}h ago (stale threshold {stale_h:.0f}h)'
        )
    return False, None


def _skip_groups() -> set:
    raw = os.environ.get('EVOLUTION_SKIP_GROUPS', '')
    return {g.strip() for g in raw.split(',') if g.strip()}


def _last_dispatch_activity() -> Optional[float]:
    """Most-recent mtime (epoch) of per-turn dispatch artifacts, EXCLUDING
    EVOLUTION_SKIP_GROUPS (those never log to evolution.db, so their activity
    must not count). Counts ALL dispatch activity — success, timeout AND error —
    because a LIA-194-class outage reaps successful dispatches as TIMEOUTs, so a
    success-only proxy would miss the exact thing this detects."""
    if not GROUPS_DIR.exists():
        return None
    skip = _skip_groups()
    latest = 0.0
    for group_dir in GROUPS_DIR.iterdir():
        if not group_dir.is_dir() or group_dir.name in skip:
            continue
        logs = group_dir / 'logs'
        candidates = list(logs.glob('container-*.log'))
        usage = logs / 'usage.jsonl'
        if usage.exists():
            candidates.append(usage)
        for p in candidates:
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
    return latest or None


def _last_interaction_ts() -> Optional[float]:
    """Most-recent ``interactions.timestamp`` as epoch, or None (missing DB / no
    rows / unparseable). Read-only; never throws."""
    if not EVOLUTION_DB.exists():
        return None
    try:
        db = sqlite3.connect(f'file:{EVOLUTION_DB}?mode=ro', uri=True)
        try:
            row = db.execute(
                'SELECT MAX(timestamp) FROM interactions'
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        ts = datetime.fromisoformat(row[0])
        # evolution writes tz-aware UTC (ilog/interaction_log.py:34,
        # datetime.now(timezone.utc).isoformat()); defensively treat any future
        # naive value as UTC so .timestamp() can't apply a silent local-time skew.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except (ValueError, TypeError):
        return None


def check_evolution_heartbeat() -> tuple[bool, Optional[str]]:
    """Live heartbeat: ``(is_stale, reason)``. Wraps the IO + thresholds around
    the pure verdict. Fail-safe — any unexpected error returns ``(False, None)``
    (the heartbeat must never break the log review)."""
    try:
        return _heartbeat_verdict(
            _last_dispatch_activity(),
            _last_interaction_ts(),
            utc_now().timestamp(),
            HEARTBEAT_ACTIVITY_WINDOW_H,
            HEARTBEAT_STALE_H,
        )
    except Exception:
        return False, None


def run_review() -> Optional[Path]:
    """
    Collect new warnings/errors from all log sources since last run,
    analyze with Ollama, save daily report, pin critical issues.
    Returns the report path, or None if nothing to review.
    """
    state = _load_state()
    offsets: dict = state.get('offsets', {})
    last_ts: float = state.get('last_review_ts', 0.0)

    # ── Collect from main logs ─────────────────────────────────────────────
    all_entries: list[dict] = []
    new_offsets: dict = {}

    for log_file in [LOGS_DIR / 'deus.log', LOGS_DIR / 'deus.error.log']:
        key = str(log_file)
        entries, new_off = parse_pino_log(log_file, offsets.get(key, 0))
        all_entries.extend(entries)
        new_offsets[key] = new_off

    # ── Collect from new container logs ───────────────────────────────────
    container_errors: list[dict] = []
    for log_file in sorted(GROUPS_DIR.glob('*/logs/container-*.log')):
        try:
            if log_file.stat().st_mtime <= last_ts:
                continue
        except OSError:
            continue
        meta = parse_container_log(log_file)
        exit_code = meta.get('exit_code', '0')
        if meta['errors'] or (exit_code not in ('0', '1')):
            container_errors.append(meta)

    # ── Save state ────────────────────────────────────────────────────────
    state['offsets'] = {**offsets, **new_offsets}
    state['last_review_ts'] = utc_now().timestamp()
    _save_state(state)

    # ── Evolution logging heartbeat (LIA-195) ──────────────────────────────
    # A SILENT outage (like LIA-194) produces no log errors, so this runs
    # independent of the entry count and gates the "healthy" early-return below.
    hb_stale, hb_reason = check_evolution_heartbeat()

    total = len(all_entries) + sum(len(c['errors']) for c in container_errors)
    if total == 0 and not container_errors and not hb_stale:
        print('No warnings or errors since last review — system healthy.')
        return None

    # ── Deduplicate and cap ───────────────────────────────────────────────
    seen: set = set()
    unique_entries: list[dict] = []
    for e in all_entries:
        k = e['message'][:80]
        if k not in seen:
            seen.add(k)
            unique_entries.append(e)
    unique_entries = unique_entries[:MAX_ENTRIES_PER_REVIEW]

    # ── Build analysis ────────────────────────────────────────────────────
    if unique_entries or container_errors:
        sections: list[str] = []

        if unique_entries:
            lines = '\n'.join(
                f"[{e['level']}] {e['message']}" for e in unique_entries
            )
            sections.append(f'## Main service logs\n{lines}')

        if container_errors:
            c_lines: list[str] = []
            for c in container_errors[:20]:
                meta_str = (
                    f"group={c.get('group', '?')} "
                    f"exit={c.get('exit_code', '?')} "
                    f"duration={c.get('duration', '?')}"
                )
                c_lines.append(f'  [{meta_str}]')
                for err in c['errors'][:5]:
                    c_lines.append(f'    {err}')
            sections.append(f'## Container session logs\n' + '\n'.join(c_lines))

        log_block = '\n\n'.join(sections)

        prompt = f"""You are a system health analyst for Deus — a personal AI assistant running locally.

Analyze these log entries and write a concise health report. Deus runs on Node.js (pino logger) with a Python evolution pipeline. Container sessions are isolated Docker runs for each user message.

{log_block}

Respond in EXACTLY this format (no extra text before or after):

## Health: [OK | DEGRADED | CRITICAL]

### Issues Found
- <each distinct issue, max 20 words, or "None">

### Root Causes
- <probable cause per issue, max 20 words, or "None">

### Action Required
- <concrete fix step per issue, or "None — system healthy">

### Pinned
<YES if health is DEGRADED or CRITICAL, NO otherwise>"""

        if _ollama_available():
            analysis = _call_ollama(prompt)
        else:
            # Fallback: plain summary without Ollama
            issue_list = '\n'.join(f'- {e["message"][:100]}' for e in unique_entries[:10])
            health = 'CRITICAL' if any(e['level'] == 'FATAL' for e in unique_entries) else 'DEGRADED'
            analysis = (
                f'## Health: {health}\n\n'
                f'### Issues Found\n{issue_list or "- (see raw entries)"}\n\n'
                f'### Root Causes\n- Ollama unavailable — automated analysis skipped\n\n'
                f'### Action Required\n- Review log entries manually\n\n'
                f'### Pinned\nYES'
            )
    else:
        # Pure-silence heartbeat alarm — no log entries to analyze, so synthesize
        # a DEGRADED report in the exact shape _pin_issue's regexes expect.
        analysis = (
            '## Health: DEGRADED\n\n'
            f'### Issues Found\n- Evolution logging heartbeat STALE: {hb_reason}\n\n'
            '### Root Causes\n- Dispatches active but the interactions table is not '
            'advancing (LIA-194 class: host->python logging path or output-marker parse)\n\n'
            '### Action Required\n- Run `python3 scripts/log_review.py --heartbeat`; check '
            'logInteraction -> evolution/cli.py and recent ipc-protocol/container-runner changes\n\n'
            '### Pinned\nYES'
        )

    # ── Heartbeat overlay (LIA-195): a hard signal, independent of Ollama ───
    heartbeat_banner = ''
    if hb_stale:
        heartbeat_banner = (
            f'> \U0001f534 **Evolution logging heartbeat: STALE** — {hb_reason}\n'
            '> Dispatches are happening but no interactions are being logged.\n\n'
        )
        if '## Health: OK' in analysis:
            analysis = analysis.replace('## Health: OK', '## Health: DEGRADED', 1)
        # Keep the report artifact self-consistent: it IS pinned (should_pin below
        # forces it on hb_stale), so flip the Ollama "Pinned: NO" marker too.
        analysis = analysis.replace('### Pinned\nNO', '### Pinned\nYES', 1)
        analysis = analysis.replace('Pinned: NO', 'Pinned: YES', 1)
        marker = '### Issues Found\n'
        if marker in analysis and 'heartbeat' not in analysis.lower():
            analysis = analysis.replace(
                marker,
                marker + f'- Evolution logging heartbeat STALE: {hb_reason}\n',
                1,
            )

    # ── Save report ───────────────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = local_now().strftime('%Y-%m-%d')
    report_path = REPORTS_DIR / f'{today}.md'
    report_content = (
        f'---\ndate: {today}\n'
        f'entries: {len(all_entries)}\n'
        f'unique: {len(unique_entries)}\n'
        f'container_sessions_with_errors: {len(container_errors)}\n'
        f'model: {OLLAMA_MODEL}\n---\n\n'
        f'{heartbeat_banner}'
        f'{analysis}\n\n'
        f'---\n*{len(all_entries)} log entries · '
        f'{len(container_errors)} container sessions with errors*\n'
    )
    report_path.write_text(report_content)

    # ── Pin if critical (single pin/notify — heartbeat folds into this one) ─
    should_pin = hb_stale or 'Pinned\nYES' in analysis or 'Pinned: YES' in analysis
    if should_pin:
        _pin_issue(today, analysis, report_path)

    print(report_content)
    print(f'[saved → {report_path}]')
    return report_path


def _pin_issue(date: str, analysis: str, report_path: Path) -> None:
    """Append to pinned issues file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    health_match = re.search(r'## Health: (\w+)', analysis)
    health = health_match.group(1) if health_match else 'UNKNOWN'
    issues_match = re.search(
        r'### Issues Found\n(.*?)(?=###|\Z)', analysis, re.DOTALL
    )
    issues = issues_match.group(1).strip() if issues_match else '(see report)'

    entry = (
        f'\n## [{date}] {health}\n'
        f'{issues}\n'
        f'→ {report_path}\n'
    )
    mode = 'a' if PINNED_FILE.exists() else 'w'
    with open(PINNED_FILE, mode) as f:
        if mode == 'w':
            f.write('# Pinned Issues\n\nReview these and mark as resolved by deleting the entry.\n')
        f.write(entry)
    print(f'[pinned → {PINNED_FILE}]')

    # macOS notification
    try:
        import subprocess
        subprocess.run([
            'osascript', '-e',
            f'display notification "Health: {health}" with title "Deus Log Review" '
            f'subtitle "Issues pinned — check ~/.deus/reviews/pinned.md"'
        ], timeout=5, capture_output=True)
    except Exception:
        pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='Deus log review and rotation')
    parser.add_argument('--rotate-only', action='store_true',
                        help='Only rotate logs, skip review')
    parser.add_argument('--review-only', action='store_true',
                        help='Only review logs, skip rotation')
    parser.add_argument('--summary', action='store_true',
                        help='Print the last saved daily report')
    parser.add_argument('--pinned', action='store_true',
                        help='Print all pinned issues')
    parser.add_argument('--heartbeat', action='store_true',
                        help='Check evolution-logging staleness only; exit 1 if stale '
                             '(LIA-195 — use as a fast post-deploy smoke)')
    args = parser.parse_args()

    if args.heartbeat:
        stale, reason = check_evolution_heartbeat()
        if stale:
            print(f'EVOLUTION LOGGING STALE: {reason}')
            sys.exit(1)
        print('Evolution logging heartbeat OK (or system idle).')
        return

    if args.summary:
        reports = sorted(REPORTS_DIR.glob('[0-9]*.md')) if REPORTS_DIR.exists() else []
        print(reports[-1].read_text() if reports else 'No reports yet.')
        return

    if args.pinned:
        print(PINNED_FILE.read_text() if PINNED_FILE.exists() else 'No pinned issues.')
        return

    if not args.review_only:
        deleted = rotate_container_logs()
        actions = rotate_main_logs()
        if deleted:
            print(f'Rotated {deleted} old container log(s).')
        for action in actions:
            print(f'Rotation: {action}')

    if not args.rotate_only:
        run_review()


if __name__ == '__main__':
    main()
