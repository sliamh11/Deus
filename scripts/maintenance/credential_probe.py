#!/usr/bin/env python3
"""
Credential expiry health probe (read-only).

8 `fix(auth)` commits over two months were reactive fixes to overnight 401s.
`auth-refresh.ts` already refreshes credentials every 30min and alerts when a
refresh ATTEMPT fails, but if the refresher silently stops keeping a token fresh
(daemon down, file stale), nothing proactively warns before the next day's work.

This standalone tool fills that gap: a nightly read-only check that each probed
surface's durable auth mechanism is intact, alerting if not. It is wired into
scripts/maintenance.py's daily block (launchd 04:30).

SECURITY: this reads ONLY expiry metadata. It NEVER reads, logs, or prints a
token, refresh-token, or API-key VALUE — every message contains a status and a
minute count, nothing more.

Surfaces probed (both are SINGLE-SOURCE files, so a file read is the whole
truth):
- Codex (~/.codex/auth.json): a dedicated refresher renews the OAuth token once
  it drops inside the 45min REFRESH_GATE_MS, on a 30min cadence. So the gate
  value is NOT a safe threshold: a HEALTHY token legitimately sits 15-45min from
  expiry while waiting for the next tick (worst case: missed a tick at 46min ->
  next tick ~16min -> refreshed). The healthy FLOOR is 45-30 = 15min, so we warn
  only on "expired OR within GRACE" (default 10min, below the floor) — a healthy
  token never warns, yet a token the refresher failed to renew is caught ~10min
  out. (api-key mode does not expire.)
- gcal (integrations/gcal/tokens.json): keepalive runs DAILY and Google access
  tokens are ~1h-lived, refreshed on demand, so a stale gcal ACCESS token is
  normal. The real signal is the DURABLE credential: tokens.json present, valid
  JSON, non-empty refresh_token. (Network revocation check is out of scope.)

Claude is deliberately NOT probed: on macOS the runtime resolves the FRESHEST of
~/.claude/.credentials.json AND the OS keychain (anthropic.ts resolveFreshest-
Credentials) — the file is a Deus-managed cache that can lag the keychain, so a
file-only probe would false-positive exactly as the stale-file 401 bug it guards
against. Probing it correctly means duplicating that keychain resolution; and
Claude is already the best-covered surface (auth-refresh.ts serves-freshest and
alerts on its own refresh failure). So it is left to that path.

A surface whose credential file is absent is treated as "not configured" and
SKIPPED (Codex and gcal are opt-in; a minimal install must not fail daily
maintenance). Exit 0 if no surface WARNs (OK or SKIP); non-zero if any surface
WARNs (so maintenance.py marks the task failed and the launchd error log
surfaces it). A failed refresher leaves the file PRESENT-but-expired, which is a
WARN, so SKIP-on-missing does not weaken the core refresher-failure detection.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CODEX = Path(os.path.expanduser("~/.codex/auth.json"))
DEFAULT_GCAL = _REPO_ROOT / "integrations" / "gcal" / "tokens.json"

# Healthy floor = REFRESH_GATE_MS(45) - refresh cadence(30) = 15min. GRACE must
# stay below it so a healthy token (always >=15min when the refresher is alive)
# never warns. See auth-refresh.ts:44,390 and com.deus.oauth-refresh StartInterval.
DEFAULT_GRACE_MIN = int(os.environ.get("DEUS_CRED_PROBE_GRACE_MIN", "10"))  # #920


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_jwt_exp_ms(token: str) -> int | None:
    """Return the `exp` claim of a JWT as epoch-ms, or None if unreadable.

    Expiry read only — the signature is NOT verified (we check freshness, not
    authenticity). urlsafe base64 with the standard right-pad to a multiple of 4.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = data.get("exp")
    # Fail closed: a non-numeric/absent exp returns None -> caller WARNs.
    return int(exp) * 1000 if isinstance(exp, (int, float)) else None


def _expiry_status(remaining_ms: int, grace_ms: int) -> tuple[str, str]:
    """OK/WARN verdict for an expiry-window surface."""
    mins = remaining_ms // 60000
    if remaining_ms <= grace_ms:
        return "WARN", (
            "expired" if remaining_ms <= 0 else f"only {mins}min to expiry (refresher stalled?)"
        )
    return "OK", f"fresh ({mins}min to expiry)"


def check_codex(path: Path, grace_ms: int, now_ms: int) -> tuple[str, str]:
    if not path.exists():
        return "SKIP", "not configured (no auth file)"  # Codex is opt-in
    try:
        d = json.loads(path.read_text())
    except Exception:
        return "WARN", "auth file malformed"
    if not isinstance(d, dict):
        return "WARN", "auth file malformed"
    # API-key mode does not expire — but the key must actually be present.
    if d.get("auth_mode") == "apikey":
        if d.get("OPENAI_API_KEY"):
            return "OK", "api-key mode (no expiry)"
        return "WARN", "api-key mode but no OPENAI_API_KEY"
    # OAuth (chatgpt) mode: a usable access-token JWT is required. A missing
    # token here is a logged-out/corrupted state, NOT a healthy no-expiry case.
    tokens = d.get("tokens")
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(access, str):
        return "WARN", "no OAuth access token (logged out?)"
    exp_ms = _decode_jwt_exp_ms(access)
    if exp_ms is None:
        return "WARN", "access token not a readable JWT"
    return _expiry_status(exp_ms - now_ms, grace_ms)


def check_gcal(path: Path) -> tuple[str, str]:
    # Durable-credential model: gcal access tokens are short-lived and refreshed
    # on demand, so access-token staleness is NORMAL. Only the refresh_token
    # (the durable credential) being gone is a real failure.
    if not path.exists():
        return "SKIP", "not configured (no tokens file)"  # gcal is opt-in
    try:
        d = json.loads(path.read_text())
    except Exception:
        return "WARN", "tokens file malformed"
    if not isinstance(d, dict):
        return "WARN", "tokens file malformed"
    if not d.get("refresh_token"):
        return "WARN", "no refresh_token (re-auth needed)"
    return "OK", "refresh_token present"


def _macos_notify(title: str, message: str) -> None:
    """Best-effort macOS banner. Never raises; no-op off macOS."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def run_probe(codex_path: Path, gcal_path: Path, grace_ms: int, now_ms: int) -> list[tuple[str, str, str]]:
    """Return [(surface, status, detail)] for all probed surfaces."""
    return [
        ("codex", *check_codex(codex_path, grace_ms, now_ms)),
        ("gcal", *check_gcal(gcal_path)),
    ]


def main(argv: list[str] | None = None, notifier=_macos_notify, now_ms: int | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--codex-auth", type=Path, default=DEFAULT_CODEX)
    parser.add_argument("--gcal-tokens", type=Path, default=DEFAULT_GCAL)
    parser.add_argument(
        "--grace-min", type=int, default=DEFAULT_GRACE_MIN,
        help="WARN if a Codex token is within this many minutes of expiry "
             "(default 10; must stay below the 15min healthy floor).",
    )
    args = parser.parse_args(argv)

    if args.grace_min >= 15:
        # The 15min healthy floor (45min gate - 30min cadence) is the hard
        # ceiling; a grace at/above it would WARN on healthy tokens.
        print("credential_probe: --grace-min must be < 15 (the healthy floor)", file=sys.stderr)
        return 2

    now = now_ms if now_ms is not None else _now_ms()
    grace_ms = args.grace_min * 60000

    results = run_probe(args.codex_auth, args.gcal_tokens, grace_ms, now)

    warns = [(s, d) for s, st, d in results if st == "WARN"]
    n_ok = sum(1 for _, st, _ in results if st == "OK")
    n_skip = sum(1 for _, st, _ in results if st == "SKIP")
    for surface, status, detail in results:
        print(f"  [{surface}] {status} — {detail}")
    print(f"credential_probe: {n_ok} OK, {len(warns)} WARN, {n_skip} skipped")

    if warns:
        summary = "; ".join(f"{s}: {d}" for s, d in warns)
        notifier("Deus credential probe", summary)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
