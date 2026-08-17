#!/usr/bin/env python3
"""
Installs the periodic launchd job that self-heals OPA/disk ledger drift (LIA-533).

Root cause this exists to fix: `AttestationStore.sync()`/`reconcile_if_drifted()` are the only
reconciliation mechanisms between the on-disk attestation ledger and OPA's live in-memory copy,
and nothing ever calls them automatically -- once OPA's copy diverges from disk (a transient PUT
failure inside a real write, or an out-of-band PUT from elsewhere), the divergence is permanent
until a human manually runs `warden_attest.py sync`. See docs/decisions/opa-warden-attestations-v1.md
and docs/HERMES_WARDEN_OPA.md for the full design; this script mirrors the pattern established by
scripts/install_hermes_procedure_recheck_launchd.py (LIA-511) -- a standalone, macOS-only
installer rather than folding into setup/service.ts, since com.deus.warden-opa.plist itself (the
daemon this job repairs) is not installed via /setup either -- it's manual, documented Hermes/dev
infra, and this job belongs at the same install layer.

Usage:
    python3 scripts/install_warden_opa_sync_launchd.py
    python3 scripts/install_warden_opa_sync_launchd.py --uninstall
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LABEL = "com.deus.warden-opa-sync"


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
        <string>{PROJECT_ROOT}/scripts/warden_attest.py</string>
        <string>--json</string>
        <string>reconcile</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{logs_dir}/warden-opa-sync.log</string>
    <key>StandardErrorPath</key>
    <string>{logs_dir}/warden-opa-sync.log</string>
</dict>
</plist>"""


def install() -> None:
    if sys.platform != "darwin":
        print("warden-opa-sync launchd install: macOS only, skipping.")
        return

    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    plist_path.write_text(_plist_contents())

    try:
        subprocess.run(["launchctl", "load", str(plist_path)], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"warden-opa-sync job scheduled (every 5 min, RunAtLoad): {plist_path}")
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
