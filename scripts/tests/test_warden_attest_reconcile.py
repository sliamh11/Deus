"""LIA-533: tests for warden_attest.py's `reconcile` subcommand -- the CLI entry point the
periodic launchd job (scripts/install_warden_opa_sync_launchd.py) invokes. The underlying
reconciliation logic itself is tested in scripts/warden_policy/tests/test_attestation_store.py
(TestReconcileIfDrifted, TestLockedNonBlocking); this file only proves the CLI correctly wires
`reconcile` to `AttestationStore.reconcile_if_drifted()` and emits the documented JSON shape with
the right exit code, consistent with `sync`'s existing convention."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import warden_attest
from warden_policy.attestation_store import WriteResult


def test_reconcile_calls_reconcile_if_drifted_not_sync(capsys):
    fake_store = mock.MagicMock()
    fake_store.reconcile_if_drifted.return_value = WriteResult(
        ok=True, generation=5, activated=True, error=None,
    )
    with mock.patch.object(warden_attest, "_store", return_value=fake_store):
        exit_code = warden_attest.main(["--json", "reconcile"])
    fake_store.reconcile_if_drifted.assert_called_once()
    fake_store.sync.assert_not_called()
    assert exit_code == warden_attest.EXIT_OK


def test_reconcile_emits_documented_json_shape(capsys):
    fake_store = mock.MagicMock()
    fake_store.reconcile_if_drifted.return_value = WriteResult(
        ok=True, generation=7, activated=True, error=None,
    )
    with mock.patch.object(warden_attest, "_store", return_value=fake_store):
        warden_attest.main(["--json", "reconcile"])
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"activated": true' in out
    assert '"generation": 7' in out


def test_reconcile_not_activated_returns_exit_not_activated(capsys):
    fake_store = mock.MagicMock()
    fake_store.reconcile_if_drifted.return_value = WriteResult(
        ok=False, generation=5, activated=False, error="lock busy (reconciliation skipped, retries next tick)",
    )
    with mock.patch.object(warden_attest, "_store", return_value=fake_store):
        exit_code = warden_attest.main(["--json", "reconcile"])
    assert exit_code == warden_attest.EXIT_NOT_ACTIVATED


def test_sync_still_calls_sync_not_reconcile_if_drifted(capsys):
    # Regression guard: `sync` (the existing documented manual-recovery command) must remain
    # byte-identical -- it must never be silently rerouted to the new reconciliation logic.
    fake_store = mock.MagicMock()
    fake_store.sync.return_value = WriteResult(ok=True, generation=3, activated=True, error=None)
    with mock.patch.object(warden_attest, "_store", return_value=fake_store):
        warden_attest.main(["--json", "sync"])
    fake_store.sync.assert_called_once()
    fake_store.reconcile_if_drifted.assert_not_called()
