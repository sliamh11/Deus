"""Mock generative provider — returns canned text, for testing and CI."""
import os
from typing import Optional

from ..provider import GenerativeProvider


class MockGenerativeProvider(GenerativeProvider):
    """Returns canned text. Only available when EVOLUTION_GEN_PROVIDER=mock."""

    CANNED_RESPONSE = (
        "- **What went wrong:** Mock response for testing.\n"
        "- **Next time:** This is a mock reflection.\n"
        "- **Category:** reasoning"
    )

    @property
    def name(self) -> str:
        return "mock"

    @property
    def priority(self) -> int:
        return 0

    @property
    def default_model(self) -> str:
        return "mock"

    def is_available(self) -> bool:
        return os.environ.get("EVOLUTION_GEN_PROVIDER", "").lower() == "mock"

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        return self.CANNED_RESPONSE
