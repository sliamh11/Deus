"""
Token estimation for the evolution loop.

Uses a simple heuristic (len(text) // 4) — accurate enough for trend
tracking. Not a replacement for tiktoken; never use for billing.
"""
from typing import Sequence


def estimate_tokens(text: str) -> int:
    """Estimate token count for a single string."""
    return len(text) // 4


def sum_tokens(*parts: str) -> int:
    """Sum estimated tokens across multiple strings."""
    return sum(estimate_tokens(p) for p in parts)
