#!/usr/bin/env python3
"""
Installs the weekly launchd job for scripts/hermes_procedure_recheck.py (LIA-511).

Kept as a standalone installer rather than folded into setup/service.ts: the
Hermes-skill-classification recheck is host/dev-tooling for this specific
wayfinder effort, not a general Deus product feature every user's /setup run
needs to install — see setup/service.ts's own setupLogReviewLaunchd() /
setupOAuthRefreshLaunchd() for the pattern this mirrors (plist shape, install
via `launchctl load`, macOS-only).

Usage:
    python3 scripts/hermes_procedure_recheck.py --install-launchd
    python3 scripts/install_hermes_procedure_recheck_launchd.py           # equivalent
    python3 scripts/install_hermes_procedure_recheck_launchd.py --uninstall
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LABEL = "com.deus.hermes-procedure-recheck"


def _python_executable() -> str:
    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    return "python3"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist_contents() -> str:
    python_path = _python_executable()
    home = str(Path.home())
    logs_dir = PROJECT_ROOT / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{PROJECT_ROOT}/scripts/hermes_procedure_recheck.py</string>
        <string>all</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{logs_dir}/hermes-procedure-recheck.log</string>
    <key>StandardErrorPath</key>
    <string>{logs_dir}/hermes-procedure-recheck.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>"""


def install() -> None:
    if sys.platform != "darwin":
        print("hermes-procedure-recheck launchd install: macOS only, skipping.")
        return

    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    plist_path.write_text(_plist_contents())

    try:
        subprocess.run(["launchctl", "load", str(plist_path)], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"hermes-procedure-recheck job scheduled (weekly, Monday 09:00): {plist_path}")
    except subprocess.CalledProcessError:
        print(f"launchctl load failed for {LABEL} (may already be loaded)")


def uninstall() -> None:
    plist_path = _plist_path()
    if not plist_path.is_file():
        print(f"{LABEL}: nothing installed.")
        return
    subprocess.run(["launchctl", "unload", str(plist_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    plist_path.unlink()
    print(f"{LABEL}: unloaded and removed {plist_path}")


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
