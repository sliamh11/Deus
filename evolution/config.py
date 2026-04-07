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

# ── Ollama ────────────────────────────────────────────────────────────────────

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

# ── Gemini ────────────────────────────────────────────────────────────────────

EMBED_DIM = 768
EMBED_MODELS = ["gemini-embedding-2-preview", "gemini-embedding-001"]
GEN_MODELS = [
    "models/gemini-3.1-flash-lite-preview",
    "models/gemini-3-flash-preview",
    "models/gemini-2.5-flash",
    "models/gemini-2.5-flash-lite",
]
GEN_MODEL = os.environ.get("EVOLUTION_GEN_MODEL", GEN_MODELS[0])
JUDGE_MODEL = os.environ.get("EVOLUTION_JUDGE_MODEL", "models/gemini-3.1-flash-lite-preview")

# ── Reflexion ─────────────────────────────────────────────────────────────────

# Interactions scoring below this threshold trigger corrective reflection generation.
REFLECTION_THRESHOLD = float(os.environ.get("EVOLUTION_REFLECTION_THRESHOLD", "0.6"))
# Interactions scoring above this threshold trigger positive pattern extraction.
POSITIVE_THRESHOLD = float(os.environ.get("EVOLUTION_POSITIVE_THRESHOLD", "0.85"))
MAX_REFLECTIONS_PER_QUERY = int(os.environ.get("EVOLUTION_MAX_REFLECTIONS", "3"))
REFLECTION_DEDUP_L2 = float(os.environ.get("EVOLUTION_REFLECTION_DEDUP_L2", "0.4"))

# ── DSPy Optimizer ────────────────────────────────────────────────────────────

# DSPy uses its own env var for independent tuning, but shares the default.
DSPY_OLLAMA_MODEL = os.environ.get("DSPY_OLLAMA_MODEL", OLLAMA_MODEL)

DSPY_MIN_SAMPLES = int(os.environ.get("EVOLUTION_DSPY_MIN_SAMPLES", "20"))
DSPY_MIN_DOMAIN_SAMPLES = int(os.environ.get("EVOLUTION_DSPY_MIN_DOMAIN_SAMPLES", "10"))
DSPY_MAX_BOOTSTRAPPED = int(os.environ.get("EVOLUTION_DSPY_MAX_BOOTSTRAPPED", "4"))
DSPY_MAX_LABELED = int(os.environ.get("EVOLUTION_DSPY_MAX_LABELED", "8"))

# ── Auto-triggers ────────────────────────────────────────────────────────────

# Auto-optimize after this many new scored interactions (0 = disabled).
AUTO_OPTIMIZE_THRESHOLD = int(os.environ.get("EVOLUTION_AUTO_OPTIMIZE_THRESHOLD", "50"))
# Cooldown between principle extractions (hours).
PRINCIPLES_COOLDOWN_HOURS = int(os.environ.get("EVOLUTION_PRINCIPLES_COOLDOWN_HOURS", "24"))
# How many times to retry Gemini judge on JSON parse failure before falling back to neutral score.
JUDGE_RETRY_COUNT = int(os.environ.get("EVOLUTION_JUDGE_RETRY_COUNT", "1"))

# ── Compaction & Batch Judging ───────────────────────────────────────────────

# Compact scored interactions older than N days (replace with summary, NULL response).
COMPACT_AFTER_DAYS = int(os.environ.get("EVOLUTION_COMPACT_AFTER_DAYS", "7"))
# Judge interactions in batches of N to reduce API call frequency.
JUDGE_BATCH_SIZE = int(os.environ.get("EVOLUTION_JUDGE_BATCH_SIZE", "5"))


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
