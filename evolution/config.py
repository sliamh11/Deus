"""
Shared configuration for the Deus Evolution loop.
All values can be overridden via environment variables.
"""
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

EVOLUTION_DIR = Path(__file__).parent
ARTIFACTS_DIR = EVOLUTION_DIR / "artifacts"
DB_PATH = Path(os.environ.get("DEUS_DB", "~/.deus/memory.db")).expanduser()
CONFIG_ENV = Path(__file__).resolve().parent.parent / ".env"

# ── Gemini ────────────────────────────────────────────────────────────────────

EMBED_DIM = 768
EMBED_MODELS = ["gemini-embedding-2-preview", "gemini-embedding-001"]
GEN_MODELS = [
    "models/gemini-3.1-flash-lite-preview",
    "models/gemini-3-flash-preview",
    "models/gemini-2.5-flash",
    "models/gemini-2.5-flash-lite",
]
JUDGE_MODEL = os.environ.get("EVOLUTION_JUDGE_MODEL", "models/gemini-3.1-flash-lite-preview")

# ── Reflexion ─────────────────────────────────────────────────────────────────

# Interactions scoring below this threshold trigger reflection generation.
REFLECTION_THRESHOLD = float(os.environ.get("EVOLUTION_REFLECTION_THRESHOLD", "0.6"))
MAX_REFLECTIONS_PER_QUERY = int(os.environ.get("EVOLUTION_MAX_REFLECTIONS", "3"))
REFLECTION_DEDUP_L2 = float(os.environ.get("EVOLUTION_REFLECTION_DEDUP_L2", "0.4"))

# ── DSPy Optimizer ────────────────────────────────────────────────────────────

DSPY_MIN_SAMPLES = int(os.environ.get("EVOLUTION_DSPY_MIN_SAMPLES", "20"))
DSPY_MAX_BOOTSTRAPPED = int(os.environ.get("EVOLUTION_DSPY_MAX_BOOTSTRAPPED", "4"))
DSPY_MAX_LABELED = int(os.environ.get("EVOLUTION_DSPY_MAX_LABELED", "8"))
DSPY_NUM_CANDIDATES = int(os.environ.get("EVOLUTION_DSPY_NUM_CANDIDATES", "4"))


def load_api_key() -> str:
    """Load GEMINI_API_KEY from project .env or environment."""
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY not found in .env or environment"
        )
    return key
