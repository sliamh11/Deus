"""Minimal OPA REST client for the Hermes adapter: strict timeouts, strict response validation.

Deliberately narrow -- this is not a general OPA SDK. It exists to isolate
the one query the Hermes adapter needs (evaluate `deus.wardens.decision`)
behind a boundary that can be reasoned about and tested independent of
Hermes's own hook contract (the Adapter pattern named in the plan).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DECISION_PATH = "/v1/data/deus/wardens/decision"
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
