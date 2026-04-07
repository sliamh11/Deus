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
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
    cutoff = datetime.now() - timedelta(days=CONTAINER_LOG_RETENTION_DAYS)
    deleted = 0
    for log_file in GROUPS_DIR.glob('*/logs/container-*.log'):
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
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
    cutoff = datetime.now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    for f in archive_dir.glob('*.gz'):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
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
        stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
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
    state['last_review_ts'] = datetime.now().timestamp()
    _save_state(state)

    total = len(all_entries) + sum(len(c['errors']) for c in container_errors)
    if total == 0 and not container_errors:
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

    # ── Build Ollama prompt ───────────────────────────────────────────────
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

    # ── Save report ───────────────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    report_path = REPORTS_DIR / f'{today}.md'
    report_content = (
        f'---\ndate: {today}\n'
        f'entries: {len(all_entries)}\n'
        f'unique: {len(unique_entries)}\n'
        f'container_sessions_with_errors: {len(container_errors)}\n'
        f'model: {OLLAMA_MODEL}\n---\n\n'
        f'{analysis}\n\n'
        f'---\n*{len(all_entries)} log entries · '
        f'{len(container_errors)} container sessions with errors*\n'
    )
    report_path.write_text(report_content)

    # ── Pin if critical ───────────────────────────────────────────────────
    if 'Pinned\nYES' in analysis or 'Pinned: YES' in analysis:
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
    args = parser.parse_args()

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
