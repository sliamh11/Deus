"""GPT-via-`codex exec` backend (ChatGPT-subscription OAuth, no API key).

Reuses the Stage-1 engine in ``codex_review.py`` (build_rules_digest / review /
call_codex_exec / CodexReviewConfig) rather than duplicating the codex-exec mechanics —
this module is the thin adapter that maps a ``ReviewRequest`` onto that engine and the
engine's ``{results, meta}`` dict back onto a ``Verdict``.
"""
from __future__ import annotations

from pathlib import Path

import codex_review as cr
from _exit_codes import AUTH_ERROR, RATE_LIMIT

from ..constants import (
    BACKEND_GPT,
    VERDICT_BLOCK,
    VERDICT_COULD_NOT_RUN,
    VERDICT_REVISE,
    VERDICT_SHIP,
)
from .base import ModelReviewerBackend, ReviewRequest, Verdict

# A real review outcome must be exactly one of these. Anything else (absent / null /
# unexpected) is treated as COULD_NOT_RUN — NEVER silently as SHIP (a schema-conformant
# response with no verdict key must not auto-approve a commit).
_REVIEW_VERDICTS = (VERDICT_SHIP, VERDICT_REVISE, VERDICT_BLOCK)

_CODE_FROM_CATEGORY = {RATE_LIMIT: "rate_limit", AUTH_ERROR: "auth"}


class CodexBackend(ModelReviewerBackend):
    """Backend id ``gpt``: drives GPT (default gpt-5.5) through the codex CLI."""

    def id(self) -> str:
        return BACKEND_GPT

    def review(self, request: ReviewRequest) -> Verdict:
        cfg = cr.CodexReviewConfig(
            model=request.model or cr.DEFAULT_MODEL,
            sandbox=cr.DEFAULT_SANDBOX,        # read-only — never elevate for untrusted input
            timeout=request.timeout,
            rules_path=Path(request.rules_path),
            is_diff=request.is_diff,           # False for content-file roles (e.g. plan-reviewer)
        )
        try:
            result = cr.review(request.content, cfg, request.cwd, request.cross_context)
        except cr.ReviewError as exc:
            # Infra failure (rate-limit / timeout / offline / bad model) — fail open, never
            # raise. ABSTAIN (empty content) is handled by the driver before we get here.
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=exc.message,
                category=_CODE_FROM_CATEGORY.get(exc.code, ""),
            )

        meta = result["meta"]
        verdict = meta.get("verdict")
        if verdict not in _REVIEW_VERDICTS:
            # Fail closed: a missing/invalid verdict is an anomaly, not an approval.
            return Verdict(
                VERDICT_COULD_NOT_RUN,
                error=f"backend returned no/invalid verdict ({verdict!r})",
            )
        findings: list[dict] = []
        for r in result["results"]:
            for f in r.get("findings", []):
                findings.append({"file": r.get("file", "<unknown>"), **f})
        return Verdict(
            verdict=verdict,
            findings=findings,
            summary=meta.get("summary", ""),
            raw=result.get("raw", ""),
        )
