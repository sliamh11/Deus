---
name: review-logs
description: Review Deus system health logs, show daily reports, surface pinned issues, and rotate old log files. Uses a local Ollama model to analyze errors and warnings.
---

# /review-logs

Runs the Deus log review and rotation system at `scripts/log_review.py`.

## Subcommands

Run the appropriate command based on what the user asks for:

| User intent | Command |
|-------------|---------|
| Full run / daily review / "what's wrong" | `python3 scripts/log_review.py` |
| See last report / summary | `python3 scripts/log_review.py --summary` |
| See pinned / flagged issues | `python3 scripts/log_review.py --pinned` |
| Clean up old logs / rotate | `python3 scripts/log_review.py --rotate-only` |
| Re-analyze without rotating | `python3 scripts/log_review.py --review-only` |

If invoked with no further context, run the full review (`python3 scripts/log_review.py`).

## What each mode does

**Full run (default):**
1. Deletes container logs older than `LOG_CONTAINER_RETENTION_DAYS` (default: 14 days)
2. Archives `logs/deus.log` / `logs/deus.error.log` to `logs/archives/` if they exceed `LOG_MAIN_MAX_MB` (default: 20 MB) and truncates them
3. Reads new log entries since the last review (byte-offset tracked — never re-reads old entries)
4. Scans new container session logs for errors
5. Sends collected warnings/errors to a local Ollama model (`LOG_REVIEW_MODEL`, default: `gemma4:e4b`) for structured health analysis
6. Saves daily report to `~/.deus/reviews/YYYY-MM-DD.md`
7. Pins DEGRADED/CRITICAL reports to `~/.deus/reviews/pinned.md` and fires a macOS notification

**Health levels returned by Ollama:**
- `OK` — no actionable issues
- `DEGRADED` — issues present, not urgent
- `CRITICAL` — needs immediate attention, gets pinned

## Output interpretation

After running, explain the health report to the user:
- Summarize the **Issues Found** section in plain language
- For each **Action Required** item, explain what it means and how to fix it
- If health is OK, confirm the system is running cleanly
- If issues are pinned, tell the user they can run `deus logs pinned` anytime to review them

## Environment overrides

| Variable | Default | Effect |
|----------|---------|--------|
| `LOG_REVIEW_MODEL` | `gemma4:e4b` | Ollama model for analysis |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `LOG_CONTAINER_RETENTION_DAYS` | `14` | Days before container logs are deleted |
| `LOG_MAIN_MAX_MB` | `20` | Main log size threshold for archiving |
| `LOG_ARCHIVE_RETENTION_DAYS` | `30` | Days before archived logs are deleted |

## Scheduling

The review runs automatically every day at 08:00 via a launchd job (`com.deus.log-review`).
To check if it's registered: `launchctl list | grep log-review`
To install it manually (macOS): `launchctl load ~/Library/LaunchAgents/com.deus.log-review.plist`
