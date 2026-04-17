from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseResult:
    case_id: str
    score: float              # 0.0–1.0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    passed: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    suite: str
    score: float              # suite-level summary in 0.0–1.0
    cases: list[CaseResult] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
