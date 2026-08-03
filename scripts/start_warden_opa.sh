#!/usr/bin/env bash
# Generic OPA daemon launcher for scripts/warden_policy -- loopback-only, no --watch (see
# docs/decisions/opa-warden-attestations-v1.md for why: OPA's own docs note file-watching can
# silently drop updates across atomic replace, which is unacceptable here since a stale
# snapshot could still show a superseded SHIP). All mutations activate themselves synchronously
# via attestation_store.py's PUT + generation read-back instead.
#
# Usage: start_warden_opa.sh <policy_dir> <ledger_path>
# Personal paths (which policy dir, which ledger) are passed in by the rendered launchd plist,
# not hardcoded here, so this script itself stays user-agnostic and repo-committed.
set -euo pipefail

POLICY_DIR="${1:?usage: start_warden_opa.sh <policy_dir> <ledger_path>}"
LEDGER_PATH="${2:?usage: start_warden_opa.sh <policy_dir> <ledger_path>}"

exec opa run --server --addr 127.0.0.1:8181 "${POLICY_DIR}/guardrails.rego" "${LEDGER_PATH}"
