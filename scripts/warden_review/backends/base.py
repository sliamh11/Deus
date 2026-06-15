"""The model-reviewer backend interface.

A backend turns (role rules + context to review) into a structured Verdict. Adding a
provider = implement this ABC in one file + register it in ``registry.py`` ("1 file +
1 reg"), mirroring the evolution provider registries (evolution/judge/provider.py).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..constants import VERDICT_COULD_NOT_RUN, VERDICT_SHIP

# The verdicts a model reviewer can emit live in ``warden_review.constants`` (MODEL_VERDICTS):
# SHIP/REVISE/BLOCK are review outcomes; COULD_NOT_RUN is an INFRA failure (rate-limit /
# timeout / offline / bad config), explicitly NOT a SHIP — the gate fails open on it
# (warn + allow) but audit-logs it distinctly so it can never be mistaken for a pass.


@dataclass
class Verdict:
    """A backend's structured review outcome for one role on one change."""

    verdict: str                                   # one of VERDICTS
    findings: list[dict] = field(default_factory=list)  # [{file,severity,line,finding,confidence}]
    summary: str = ""
    raw: str = ""                                  # verbatim model output, for diagnostics
    error: str = ""                                # populated when verdict == COULD_NOT_RUN
    category: str = ""                             # "" | "rate_limit" | "auth" — steers messaging

    @property
    def is_ship(self) -> bool:
        return self.verdict == VERDICT_SHIP

    @property
    def could_not_run(self) -> bool:
        return self.verdict == VERDICT_COULD_NOT_RUN


@dataclass
class ReviewRequest:
    """Everything a backend needs to review one role's change. Assembled by the driver."""

    role: str                  # e.g. "code-reviewer"
    rules_path: str            # absolute path to the role's rules file (may not exist)
    content: str               # the thing to review (a unified diff, a plan, …)
    cwd: str                   # repo/worktree root the model runs in (read-only sandbox)
    cross_context: str = ""    # other backends' findings on this change (trusted; injected
                               # OUTSIDE the untrusted-content boundary)
    model: str | None = None   # backend-specific model id; None = backend/config default
    timeout: float = 300.0


class ModelReviewerBackend(ABC):
    """Out-of-band model reviewer. One instance per backend id."""

    @abstractmethod
    def id(self) -> str:
        """Stable backend id used in config (``backends: [...]``) and verdict keys."""

    @abstractmethod
    def review(self, request: ReviewRequest) -> Verdict:
        """Run the review. MUST NOT raise for infra failures — return
        ``Verdict("COULD_NOT_RUN", error=…, category=…)`` instead, so the gate can fail
        open. May only raise for genuine programmer error (e.g. a malformed request)."""
