"""OpenAI-compatible ``/v1/chat/completions`` model-reviewer backend.

Reviews a change against a role's rules via ANY OpenAI-compatible endpoint — a local
llama.cpp / Ollama server, OpenRouter, or OpenAI itself. Reuses the codex_review engine's
PURE prompt helpers (``build_rules_digest`` / ``build_prompt``) so the prompt, the
untrusted-diff sentinel boundary, and the findings shape stay identical to the ``gpt``
backend — only the transport differs (an HTTP POST here vs the ``codex`` CLI in codex.py).

Config is env-driven — NO hardcoded hosts, so a fresh clone configures it or abstains:
  WARDEN_OPENAI_COMPAT_BASE_URL  (required)  e.g. http://127.0.0.1:8080/v1, https://openrouter.ai/api/v1
  WARDEN_OPENAI_COMPAT_MODEL     (optional)  model id; ``ReviewRequest.model`` overrides it
  WARDEN_OPENAI_COMPAT_API_KEY   (optional)  sent as ``Authorization: Bearer`` only when set
                                             (a local llama.cpp / Ollama server needs none)

We send ``response_format={"type":"json_object"}`` — the portable common denominator across
all four targets (llama.cpp constrains output to a JSON grammar; Ollama maps it to
``format=json``; OpenRouter passes it through; OpenAI supports it). We deliberately do NOT
use ``json_schema``/strict mode: ``FINDINGS_SCHEMA``'s ``line`` field is a ``["integer",
"null"]`` type-union, which strict structured-outputs reject — so the required shape is
stated in the prompt instead, and the fail-closed verdict check below is the real guarantee.

Security: the diff is UNTRUSTED. It stays inside ``build_prompt``'s per-run 128-bit random
sentinel boundary; ``cross_context`` (trusted system input) is injected OUTSIDE it by
``build_prompt``. This is a read-only HTTP review — no writes, no auto-apply. The API key is
only ever placed in the ``Authorization`` header, never logged.

FAIL-CLOSED (mirrors codex.py): a missing base URL, a transport error, a non-200, an
unparseable body, or a verdict outside {SHIP,REVISE,BLOCK} all become COULD_NOT_RUN — the
gate fails OPEN on that (warn + allow, audit-logged distinctly), but a schema-conformant
response with no/invalid verdict NEVER silently becomes SHIP.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

import codex_review as cr  # PURE prompt helpers (build_rules_digest / build_prompt); no codex CLI here
import httpx               # hard dep of the warden-review stack (codex_review requires it too)

from ..constants import (
    BACKEND_OPENAI_COMPAT,
    VERDICT_BLOCK,
    VERDICT_COULD_NOT_RUN,
    VERDICT_REVISE,
    VERDICT_SHIP,
)
from .base import ModelReviewerBackend, ReviewRequest, Verdict

# A real review outcome must be exactly one of these. Anything else (absent / null /
# unexpected) is COULD_NOT_RUN — NEVER silently SHIP (a valid-JSON response with no verdict
# key must not auto-approve a commit). Mirrors codex.py's contract.
_REVIEW_VERDICTS = (VERDICT_SHIP, VERDICT_REVISE, VERDICT_BLOCK)

# Env-var NAMES are class attributes on OpenAICompatBackend (see its docstring); module-level
# aliases for the old private names are defined after the class for back-compat.

# Single-call guard. Same VALUE as codex_review.WHOLE_DIFF_CHAR_LIMIT (200_000) but a
# different SCOPE: that bounds the DIFF slice alone (minus the rules digest); this bounds the
# WHOLE assembled prompt — i.e. strictly MORE conservative, so do not equate the two constants.
# Oversize -> COULD_NOT_RUN (fail open), NEVER a silent truncate-then-SHIP. Deliberately no
# per-file fan-out: the co-gate reviews working-tree-sized diffs, so the rare oversize case
# (fail-open + audit-logged) is an acceptable trade against the added quota cost and complexity
# of fanning a huge diff across many calls. Add fan-out only if real oversize diffs appear.
_MAX_PROMPT_CHARS = 200_000


def _shape_instruction() -> str:
    """The exact JSON object shape, appended to the prompt for ``json_object`` mode.

    ``response_format={"type":"json_object"}`` guarantees VALID JSON but not a SCHEMA, so the
    required shape (the same fields codex enforces out-of-band via ``--output-schema``) is
    stated here. Defensive parsing + the fail-closed verdict check are the real guarantee;
    this only steers the model to the right shape. Appended AFTER build_prompt's terminal
    instruction so the schema sits in the prompt's terminal position (where models attend best).
    """
    return (
        "\n\nReturn ONLY a single JSON object (no prose, no markdown fence) of EXACTLY this "
        "shape:\n"
        '{"verdict": "SHIP|REVISE|BLOCK", "summary": "<one sentence>", "results": '
        '[{"file": "<path>", "flagged": <bool>, "findings": [{"severity": '
        '"CRITICAL|MAJOR|MINOR", "line": <int|null>, "finding": "<text>", '
        '"confidence": "high|medium|low"}]}]}'
    )


def _post_chat_completion(
    endpoint: str, payload: dict, headers: dict, timeout: float
) -> tuple[int, dict | str]:
    """The ONE network seam (mocked wholesale in tests — zero real HTTP in CI).

    Returns ``(status_code, body)`` where body is the parsed JSON dict on success or the raw
    text when the response is not JSON (e.g. an HTML 502 from a proxy), so ``review`` handles
    status + shape uniformly. Transport failures propagate as ``httpx.HTTPError`` for the
    caller to map to COULD_NOT_RUN.
    """
    resp = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
    try:
        return resp.status_code, resp.json()
    except (ValueError, json.JSONDecodeError):
        return resp.status_code, resp.text


def _parse_findings_json(raw: str) -> dict:
    """Parse the model's content into the findings dict, tolerating a markdown fence or
    leading/trailing prose some models wrap around the object (e.g. a trailing "Note: …").

    Tries the body first (after stripping a leading ```````/`````json`` fence); on
    failure, falls back to the OUTERMOST ``{...}`` span. A still-unparseable result or a
    non-object JSON value raises ValueError/TypeError — which ``review`` maps to COULD_NOT_RUN
    (fail-closed: prose, an array, or junk NEVER becomes a verdict)."""
    body = raw.strip()
    if body.startswith("```"):  # tolerate a fence the server didn't strip
        body = body.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)  # outermost object; surrounding prose tolerated
        if not match:
            raise
        data = json.loads(match.group(0))  # may still raise -> caller fails closed
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    return data


class OpenAICompatBackend(ModelReviewerBackend):
    """Backend id ``openai_compat``: any OpenAI-compatible /v1/chat/completions endpoint.

    Subclass to add a provider: override the ``ENV_*`` / ``DEFAULT_*`` / ``REQUIRE_API_KEY``
    class attributes (and ``id()``) and reuse ``review()`` verbatim — see ``backends/glm.py``.
    """

    # Env-var NAMES this backend reads (no hardcoded host: a fresh clone sets these or abstains).
    ENV_BASE_URL = "WARDEN_OPENAI_COMPAT_BASE_URL"
    ENV_MODEL = "WARDEN_OPENAI_COMPAT_MODEL"
    ENV_API_KEY = "WARDEN_OPENAI_COMPAT_API_KEY"
    # Provider-specific defaults (empty = none; generic openai_compat must be env-configured).
    DEFAULT_BASE_URL = ""
    DEFAULT_MODEL = ""
    # Authenticated endpoints (e.g. Z.ai) set this so a keyless call abstains instead of being
    # sent; generic openai_compat allows keyless (a local llama.cpp / Ollama needs no key).
    REQUIRE_API_KEY = False

    def id(self) -> str:
        return BACKEND_OPENAI_COMPAT

    def review(self, request: ReviewRequest) -> Verdict:
        base_url = (os.environ.get(self.ENV_BASE_URL, "").strip()
                    or self.DEFAULT_BASE_URL).rstrip("/")
        if not base_url:
            # Fail open (never SHIP): the backend is registered but unconfigured here.
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=f"{self.ENV_BASE_URL} is not set — no endpoint to review against. Set it "
                      "to an OpenAI-compatible /v1 base URL (e.g. http://127.0.0.1:8080/v1).",
                category="auth",
            )

        api_key = os.environ.get(self.ENV_API_KEY, "").strip()
        if self.REQUIRE_API_KEY and not api_key:
            # An authenticated endpoint with no key: abstain BEFORE building the prompt or
            # calling out (fail open, never SHIP) — guarantees a no-op when unconfigured.
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=f"{self.ENV_API_KEY} is not set — this backend requires an API key.",
                category="auth",
            )

        model = request.model or os.environ.get(self.ENV_MODEL, "").strip() or self.DEFAULT_MODEL
        rules_digest = cr.build_rules_digest(Path(request.rules_path))
        sentinel = f"<<<UNTRUSTED-DIFF-{secrets.token_hex(16)}>>>"  # 128-bit, infeasible to forge
        prompt = (
            cr.build_prompt(request.content, rules_digest, sentinel, request.cross_context)
            + _shape_instruction()
        )
        if len(prompt) > _MAX_PROMPT_CHARS:
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=f"assembled prompt is {len(prompt)} chars > {_MAX_PROMPT_CHARS} cap "
                      "(single-call backend; per-file fan-out is not implemented).",
            )

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"  # key never logged; header only
        payload: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if model:
            payload["model"] = model

        endpoint = f"{base_url}/chat/completions"
        try:
            status, body = _post_chat_completion(endpoint, payload, headers, request.timeout)
        except (httpx.HTTPError, ValueError) as exc:
            # Transport failure (offline / DNS / timeout) OR a malformed base URL (some httpx
            # versions raise ValueError for a schemeless URL) — honor the backend contract:
            # fail open (COULD_NOT_RUN), NEVER let review() raise out into the gate driver.
            return Verdict(VERDICT_COULD_NOT_RUN, error=f"connection error to {endpoint}: {exc}")

        if status != 200:
            category = ("auth" if status in (401, 403)
                        else "rate_limit" if status == 429 else "")
            snippet = body if isinstance(body, str) else json.dumps(body)
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=f"HTTP {status} from {endpoint}: {snippet[:200]}",
                category=category,
            )

        # Parse the model's JSON content into a Verdict. ANY anomaly -> COULD_NOT_RUN.
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            return Verdict(VERDICT_COULD_NOT_RUN, error=f"unexpected response shape: {exc}")
        raw = (content or "").strip()
        try:
            data = _parse_findings_json(raw)
            verdict = data["verdict"]
        except (ValueError, KeyError, TypeError) as exc:
            return Verdict(VERDICT_COULD_NOT_RUN, raw=raw,
                           error=f"model output was not schema-conforming JSON: {exc}")
        if verdict not in _REVIEW_VERDICTS:
            # Fail closed: a missing/invalid verdict is an anomaly, not an approval.
            return Verdict(VERDICT_COULD_NOT_RUN, raw=raw,
                           error=f"backend returned no/invalid verdict ({verdict!r})")

        findings: list[dict] = []
        results = data.get("results")
        if isinstance(results, list):
            for r in results:
                if not isinstance(r, dict):
                    continue
                file = r.get("file", "<unknown>")
                for f in r.get("findings") or []:
                    if isinstance(f, dict):
                        findings.append({"file": file, **f})
        return Verdict(verdict=verdict, findings=findings,
                       summary=data.get("summary", ""), raw=raw)


# Back-compat module-level aliases for the old private constant names (external importers).
_ENV_BASE_URL = OpenAICompatBackend.ENV_BASE_URL
_ENV_MODEL = OpenAICompatBackend.ENV_MODEL
_ENV_API_KEY = OpenAICompatBackend.ENV_API_KEY
