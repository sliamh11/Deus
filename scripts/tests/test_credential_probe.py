"""Tests for the credential expiry health probe (scripts/maintenance/credential_probe.py).

Hermetic: every test builds throwaway token files under tmp_path with an injected
`now_ms`, so nothing touches real credentials and nothing shells out (the macOS
notifier is replaced by a recorder). SECURITY-relevant: a test asserts no token
value ever appears in the probe's output. Claude is intentionally not probed (its
macOS keychain dual-source resolution can't be read from the file alone), so there
are no Claude cases here.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "credential_probe.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("credential_probe", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["credential_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load()

NOW = 1_800_000_000_000  # fixed "now" in ms
MIN = 60_000


def _jwt(exp_seconds: int) -> str:
    """A minimal unsigned (alg:none) JWT carrying only an `exp` claim.

    Intentionally unsigned: the probe reads expiry, it does NOT verify the
    signature, so the test needs no crypto dependency.
    """
    def b64(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp_seconds})}.sig"


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj))


# ── Codex ───────────────────────────────────────────────────────────────────

def test_codex_healthy_waiting_for_tick_is_ok(tmp_path: Path):
    # 30min and 16min from expiry: inside the 45min gate but ABOVE the 10min
    # grace — healthy, refresher will renew on its next tick. NOT a warning.
    for mins in (30, 16):
        p = tmp_path / f"codex_{mins}.json"
        _write(p, {"auth_mode": "chatgpt", "tokens": {"access_token": _jwt((NOW + mins * MIN) // 1000)}})
        assert cp.check_codex(p, 10 * MIN, NOW)[0] == "OK", f"{mins}min should be OK"


def test_codex_within_grace_and_expired_warn(tmp_path: Path):
    for secs_from_now, expect_detail in ((5 * MIN, "expiry"), (-5 * MIN, "expired")):
        p = tmp_path / "codex.json"
        _write(p, {"auth_mode": "chatgpt", "tokens": {"access_token": _jwt((NOW + secs_from_now) // 1000)}})
        status, detail = cp.check_codex(p, 10 * MIN, NOW)
        assert status == "WARN" and expect_detail in detail


def test_codex_apikey_mode_with_key_is_ok(tmp_path: Path):
    p = tmp_path / "apikey.json"
    _write(p, {"auth_mode": "apikey", "OPENAI_API_KEY": "x", "tokens": {}})
    status, detail = cp.check_codex(p, 10 * MIN, NOW)
    assert status == "OK" and "api-key" in detail


def test_codex_apikey_mode_without_key_warns(tmp_path: Path):
    p = tmp_path / "nokey.json"
    _write(p, {"auth_mode": "apikey", "tokens": {}})  # no OPENAI_API_KEY
    assert cp.check_codex(p, 10 * MIN, NOW)[0] == "WARN"


def test_codex_oauth_mode_no_token_warns(tmp_path: Path):
    # chatgpt mode but no access token = logged out/corrupted = WARN. Both
    # shapes: tokens dict present-but-empty, and tokens key absent entirely.
    for body in ({"auth_mode": "chatgpt", "tokens": {}}, {"auth_mode": "chatgpt"}):
        p = tmp_path / "partial.json"
        _write(p, body)
        status, detail = cp.check_codex(p, 10 * MIN, NOW)
        assert status == "WARN" and "logged out" in detail


def test_codex_malformed_jwt_warns(tmp_path: Path):
    p = tmp_path / "bad.json"
    _write(p, {"auth_mode": "chatgpt", "tokens": {"access_token": "not-a-jwt"}})
    assert cp.check_codex(p, 10 * MIN, NOW)[0] == "WARN"


def test_codex_missing_skips_malformed_warns(tmp_path: Path):
    # Missing file = not configured = SKIP. Present-but-broken = WARN.
    assert cp.check_codex(tmp_path / "nope.json", 10 * MIN, NOW)[0] == "SKIP"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cp.check_codex(bad, 10 * MIN, NOW)[0] == "WARN"


# ── gcal (durable-credential model — access-token expiry IGNORED) ────────────

def test_gcal_ok_with_refresh_token_even_if_access_expired(tmp_path: Path):
    p = tmp_path / "tokens.json"
    _write(p, {"refresh_token": "present", "expiry_date": NOW - 10 * MIN})  # access expired
    status, detail = cp.check_gcal(p)
    assert status == "OK" and "refresh_token present" in detail


def test_gcal_missing_skips_no_refresh_token_warns(tmp_path: Path):
    assert cp.check_gcal(tmp_path / "absent.json")[0] == "SKIP"  # not configured
    p = tmp_path / "tokens.json"
    _write(p, {"access_token": "x", "expiry_date": NOW + 99 * MIN})  # no refresh_token
    assert cp.check_gcal(p)[0] == "WARN"


# ── shared robustness ────────────────────────────────────────────────────────

def test_non_dict_json_warns_not_crashes(tmp_path: Path):
    # Valid JSON that isn't an object (e.g. `[]`) must WARN, not raise.
    p = tmp_path / "list.json"
    p.write_text("[]")
    assert cp.check_codex(p, 10 * MIN, NOW)[0] == "WARN"
    assert cp.check_gcal(p)[0] == "WARN"


# ── main() exit codes + notifier + secret hygiene ────────────────────────────

def _all_ok(tmp_path: Path) -> list[str]:
    codex = tmp_path / "x.json"; _write(codex, {"auth_mode": "apikey", "OPENAI_API_KEY": "x", "tokens": {}})
    gcal = tmp_path / "g.json"; _write(gcal, {"refresh_token": "r"})
    return ["--codex-auth", str(codex), "--gcal-tokens", str(gcal)]


def test_main_all_ok_exit_zero_no_notify(tmp_path: Path, capsys):
    fired = []
    rc = cp.main(_all_ok(tmp_path), notifier=lambda *a: fired.append(a), now_ms=NOW)
    assert rc == 0 and fired == []
    assert "2 OK, 0 WARN, 0 skipped" in capsys.readouterr().out


def test_unconfigured_surfaces_skip_not_fail(tmp_path: Path, capsys):
    # Both opt-in surfaces absent (minimal install) -> SKIP -> exit 0, no alert.
    fired = []
    rc = cp.main(["--codex-auth", str(tmp_path / "absent-codex.json"),
                  "--gcal-tokens", str(tmp_path / "absent-gcal.json")],
                 notifier=lambda *a: fired.append(a), now_ms=NOW)
    assert rc == 0 and fired == []
    assert "0 OK, 0 WARN, 2 skipped" in capsys.readouterr().out


def test_main_any_warn_exits_nonzero_and_notifies(tmp_path: Path):
    args = _all_ok(tmp_path)
    gcal = tmp_path / "g.json"; _write(gcal, {"access_token": "x"})  # break gcal: no refresh_token
    fired = []
    rc = cp.main(args, notifier=lambda *a: fired.append(a), now_ms=NOW)
    assert rc == 1 and len(fired) == 1


def test_grace_min_at_or_above_floor_is_rejected(tmp_path: Path):
    rc = cp.main(_all_ok(tmp_path) + ["--grace-min", "15"], notifier=lambda *a: None, now_ms=NOW)
    assert rc == 2


def test_output_never_leaks_token_values(tmp_path: Path, capsys):
    codex = tmp_path / "x.json"
    _write(codex, {"auth_mode": "chatgpt", "tokens": {"access_token": _jwt((NOW + 99 * MIN) // 1000)}})
    gcal = tmp_path / "g.json"; _write(gcal, {"refresh_token": "SECRET-REFRESH"})
    fired = []
    cp.main(["--codex-auth", str(codex), "--gcal-tokens", str(gcal)],
            notifier=lambda *a: fired.append(a), now_ms=NOW)
    out = capsys.readouterr().out
    # Neither stdout nor any notifier argument carries a raw token value.
    assert "SECRET-REFRESH" not in out and "SECRET-REFRESH" not in str(fired)
