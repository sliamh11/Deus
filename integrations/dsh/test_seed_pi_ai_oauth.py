#!/usr/bin/env python3
"""Unit tests for seed_pi_ai_oauth.py.

Run: python3 integrations/dsh/test_seed_pi_ai_oauth.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_pi_ai_oauth import SeedError, anthropic_grant, codex_grant, main  # noqa: E402


ACCESS = "secret-access-token"
REFRESH = "secret-refresh-token"
ACCOUNT = "secret-account-id"


def jwt(payload: dict) -> str:
    def part(value: dict) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return encoded

    return f"{part({'alg': 'none'})}.{part(payload)}.signature"


class SeedPiAiOauthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.claude = self.root / "claude.json"
        self.codex = self.root / "codex.json"
        self.store = self.root / ".dsh" / ".credentials.yaml"
        self.claude.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": ACCESS,
                "refreshToken": REFRESH,
                "expiresAt": 1_786_000_000_000,
            }
        }))
        self.codex.write_text(json.dumps({
            "tokens": {
                "access_token": jwt({
                    "exp": 1_786_000_000,
                    "https://api.openai.com/auth": {"chatgpt_account_id": ACCOUNT},
                }),
                "refresh_token": REFRESH,
            }
        }))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "--store", str(self.store),
            "--claude-credentials", str(self.claude),
            "--codex-credentials", str(self.codex),
            *extra,
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_translates_both_source_formats(self) -> None:
        self.assertEqual(anthropic_grant(self.claude), {
            "type": "oauth", "access": ACCESS, "refresh": REFRESH, "expires": 1_786_000_000_000,
        })
        grant = codex_grant(self.codex)
        self.assertEqual(grant["type"], "oauth")
        self.assertEqual(grant["refresh"], REFRESH)
        self.assertEqual(grant["expires"], 1_786_000_000_000)
        self.assertEqual(grant["accountId"], ACCOUNT)

    def test_seeds_both_records_without_printing_secrets(self) -> None:
        result, stdout, stderr = self.invoke()
        self.assertEqual((result, stderr), (0, ""))
        document = yaml.safe_load(self.store.read_text())
        self.assertEqual(set(document["records"]), {
            "llm-pi-ai/anthropic", "llm-pi-ai/openai-codex",
        })
        self.assertEqual(document["records"]["llm-pi-ai/anthropic"]["payload"]["access"], ACCESS)
        for secret in (ACCESS, REFRESH, ACCOUNT):
            self.assertNotIn(secret, stdout)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    def test_preserves_refs_and_unrelated_records_and_backs_up(self) -> None:
        self.store.parent.mkdir(parents=True)
        original = {
            "version": 1,
            "refs": {"DEEPSEEK_API_KEY": "existing-secret"},
            "records": {"llm-pi-ai/amazon-bedrock": {"kind": "api-key", "env": {"AWS_PROFILE": "prod"}}},
        }
        self.store.write_text(yaml.safe_dump(original, sort_keys=False))

        result, _, _ = self.invoke("--provider", "anthropic")
        self.assertEqual(result, 0)
        document = yaml.safe_load(self.store.read_text())
        self.assertEqual(document["refs"], original["refs"])
        self.assertEqual(
            document["records"]["llm-pi-ai/amazon-bedrock"],
            original["records"]["llm-pi-ai/amazon-bedrock"],
        )
        backups = list(self.store.parent.glob(".credentials.yaml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(yaml.safe_load(backups[0].read_text()), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_replaces_a_record_instead_of_duplicating_it(self) -> None:
        self.assertEqual(self.invoke("--provider", "anthropic")[0], 0)
        self.claude.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "replacement-access",
                "refreshToken": "replacement-refresh",
                "expiresAt": 1_800_000_000_000,
            }
        }))
        self.assertEqual(self.invoke("--provider", "anthropic")[0], 0)
        text = self.store.read_text()
        self.assertEqual(text.count("llm-pi-ai/anthropic:"), 1)
        self.assertEqual(
            yaml.safe_load(text)["records"]["llm-pi-ai/anthropic"]["payload"]["access"],
            "replacement-access",
        )

    def test_dry_run_does_not_create_or_modify_the_store(self) -> None:
        result, stdout, _ = self.invoke("--provider", "anthropic", "--dry-run")
        self.assertEqual(result, 0)
        self.assertIn("would seed: llm-pi-ai/anthropic", stdout)
        self.assertFalse(self.store.exists())

    def test_invalid_destination_fails_before_backup_or_write(self) -> None:
        self.store.parent.mkdir(parents=True)
        self.store.write_text("version: 1\nrecords: nope\n")
        before = self.store.read_bytes()
        result, _, stderr = self.invoke("--provider", "anthropic")
        self.assertEqual(result, 1)
        self.assertIn("records", stderr)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(list(self.store.parent.glob("*.bak-*")), [])

    def test_malformed_codex_jwt_is_rejected(self) -> None:
        self.codex.write_text(json.dumps({
            "tokens": {"access_token": "not-a-jwt", "refresh_token": REFRESH}
        }))
        with self.assertRaisesRegex(SeedError, "three-part JWT"):
            codex_grant(self.codex)


if __name__ == "__main__":
    unittest.main()
