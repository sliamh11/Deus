"""Minimal OPA REST client for the Hermes adapter: strict timeouts, strict response validation.

Deliberately narrow -- this is not a general OPA SDK. It exists to isolate
the queries the adapters need behind a boundary that can be reasoned about and
tested independent of any one hook contract (the Adapter pattern named in the plan):

- ``query_decision`` -- the Hermes adapter's `deus.wardens.decision` evaluation.
- ``query_backend_verdicts`` / ``query_generation`` -- the Claude-Code-side
  read-only shadow observer's queries (Phase 1, see ``cc_shadow``). Purely
  additive: ``query_decision`` and ``DecisionResult`` are untouched by them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DECISION_PATH = "/v1/data/deus/wardens/decision"
BACKEND_VERDICTS_PATH = "/v1/data/deus/wardens/backend_verdict_map"
GENERATION_PATH = "/v1/data/warden_attestations/generation"
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class DecisionResult:
    ok: bool  # True only if OPA was reachable AND returned a well-formed, valid response
    allow: bool  # only meaningful when ok is True
    reason: str
    error: str | None = None


def query_decision(opa_base_url: str, opa_input: dict, timeout_seconds: float) -> DecisionResult:
    body = json.dumps({"input": opa_input}).encode("utf-8")
    req = urllib.request.Request(
        opa_base_url.rstrip("/") + DECISION_PATH, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            if resp.status != 200:
                return DecisionResult(ok=False, allow=False, reason="", error=f"HTTP {resp.status}")
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                return DecisionResult(ok=False, allow=False, reason="", error="response too large")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return DecisionResult(ok=False, allow=False, reason="", error=f"request failed: {exc}")

    try:
        body_json = json.loads(raw)
    except json.JSONDecodeError as exc:
        return DecisionResult(ok=False, allow=False, reason="", error=f"malformed JSON: {exc}")

    result = body_json.get("result")
    if not isinstance(result, dict) or "allow" not in result:
        return DecisionResult(ok=False, allow=False, reason="", error="missing/malformed result.allow")

    allow = result.get("allow")
    if allow is not True and allow is not False:
        return DecisionResult(ok=False, allow=False, reason="", error="result.allow is not a boolean")

    reason = result.get("reason", "")
    return DecisionResult(ok=True, allow=bool(allow), reason=str(reason))


@dataclass(frozen=True)
class BackendVerdictsResult:
    #: True only if OPA was reachable AND returned a well-formed response.
    ok: bool
    #: backend id -> raw verdict string. Empty when the policy produced no facts --
    #: which is a VALID answer (no attestation, or a `supported` guard failure), not
    #: an error. `ok` distinguishes "OPA said nothing matched" from "OPA said nothing".
    verdicts: dict[str, str]
    error: str | None = None


def _fetch(url: str, timeout_seconds: float, body: bytes | None = None):
    """Return (parsed_json, error_message). Never raises for network/parse failures."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                return None, "response too large"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, f"request failed: {exc}"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON: {exc}"


def query_backend_verdicts(
    opa_base_url: str, opa_input: dict, timeout_seconds: float,
) -> BackendVerdictsResult:
    """Evaluate `deus.wardens.backend_verdict_map` -- raw verdict string per backend.

    Read-only: this only evaluates a Rego rule, it never PUTs data. The strict-AND
    combination across backends is deliberately NOT done here -- it lives in the
    caller, mirroring `_evaluate_backends`, exactly as `guardrails.rego`'s own comment
    block prescribes ("facts in Rego, combination in the shim").

    An absent ``result`` key is OPA's documented shape for a rule that evaluated to
    undefined; for a partial object rule that is indistinguishable in meaning from an
    empty object, so both are reported as ``ok=True`` with an empty ``verdicts`` map.
    Anything else malformed (non-dict result, non-string verdict value) is an error --
    a shadow observer that silently coerced garbage would log fiction.
    """
    body = json.dumps({"input": opa_input}).encode("utf-8")
    body_json, err = _fetch(
        opa_base_url.rstrip("/") + BACKEND_VERDICTS_PATH, timeout_seconds, body,
    )
    if err is not None:
        return BackendVerdictsResult(ok=False, verdicts={}, error=err)
    if not isinstance(body_json, dict):
        return BackendVerdictsResult(ok=False, verdicts={}, error="response is not an object")

    if "result" not in body_json:
        return BackendVerdictsResult(ok=True, verdicts={})
    result = body_json.get("result")
    if not isinstance(result, dict):
        return BackendVerdictsResult(ok=False, verdicts={}, error="result is not an object")
    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return BackendVerdictsResult(
                ok=False, verdicts={}, error="result contains a non-string verdict entry",
            )
    return BackendVerdictsResult(ok=True, verdicts=dict(result))


def query_generation(opa_base_url: str, timeout_seconds: float) -> int | None:
    """Return the generation OPA is currently serving, or None if unavailable.

    Used only to tell a stale-snapshot disagreement apart from a genuine
    no-attestation one when the verdict map comes back empty.
    """
    body_json, err = _fetch(opa_base_url.rstrip("/") + GENERATION_PATH, timeout_seconds)
    if err is not None or not isinstance(body_json, dict):
        return None
    result = body_json.get("result")
    # bool is an int subclass in Python -- exclude it explicitly, a generation is never a bool.
    return result if isinstance(result, int) and not isinstance(result, bool) else None
