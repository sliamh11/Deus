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
EVOLUTION_DB_PATH = Path(os.environ.get("DEUS_EVOLUTION_DB", "~/.deus/evolution.db")).expanduser()
CONFIG_ENV = Path(__file__).resolve().parent.parent / ".env"
USER_CONFIG_ENV = Path("~/.config/deus/.env").expanduser()
_ENV_SEARCH_PATHS: list[Path] = [CONFIG_ENV, USER_CONFIG_ENV]

# ── Ollama ────────────────────────────────────────────────────────────────────

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

# ── llama.cpp ────────────────────────────────────────────────────────────────

# Base URL for the local llama-server (OpenAI-compatible /v1 prefix).
# Default localhost so it works OOTB if the /add-llama-cpp skill is installed.
#
# LLAMA_CPP_MODEL is the catch-all model. Per-surface env vars below override
# it. Empty is a valid value:
#   - Single-model llama-server: empty means "use the loaded model"
#   - Router mode (--models-dir + --models-max N): each surface POSTs with its
#     own "model" field; empty means "auto-pick whichever is loaded"
# Phase 3 (post-PR #461) introduces per-surface overrides that fall back to
# LLAMA_CPP_MODEL when unset, preserving back-compat for single-model deploys.
LLAMA_CPP_BASE_URL = os.environ.get("LLAMA_CPP_BASE_URL", "http://localhost:8080/v1")
LLAMA_CPP_MODEL = os.environ.get("LLAMA_CPP_MODEL", "")

# Per-surface model overrides (fall back to LLAMA_CPP_MODEL if unset).
LLAMA_CPP_GEN_MODEL = os.environ.get("LLAMA_CPP_GEN_MODEL", LLAMA_CPP_MODEL)
LLAMA_CPP_JUDGE_MODEL = os.environ.get("LLAMA_CPP_JUDGE_MODEL", LLAMA_CPP_MODEL)
LLAMA_CPP_EMBED_MODEL = os.environ.get("LLAMA_CPP_EMBED_MODEL", LLAMA_CPP_MODEL)

# ── Gemini ────────────────────────────────────────────────────────────────────

EMBED_DIM = 768
EMBED_MODELS = ["gemini-embedding-2-preview", "gemini-embedding-001"]
GEN_MODELS = [
    "models/gemini-3.1-flash-lite",
    "models/gemini-3-flash-preview",
    "models/gemini-2.5-flash",
    "models/gemini-2.5-flash-lite",
]
GEN_MODEL = os.environ.get("EVOLUTION_GEN_MODEL", GEN_MODELS[0])
JUDGE_MODEL = os.environ.get("EVOLUTION_JUDGE_MODEL", "models/gemini-3.1-flash-lite")

# ── Reflexion ─────────────────────────────────────────────────────────────────

# Interactions scoring below this threshold trigger corrective reflection generation.
REFLECTION_THRESHOLD = float(os.environ.get("EVOLUTION_REFLECTION_THRESHOLD", "0.6"))
# Interactions scoring above this threshold trigger positive pattern extraction.
POSITIVE_THRESHOLD = float(os.environ.get("EVOLUTION_POSITIVE_THRESHOLD", "0.85"))
MAX_REFLECTIONS_PER_QUERY = int(os.environ.get("EVOLUTION_MAX_REFLECTIONS", "3"))
REFLECTION_DEDUP_L2 = float(os.environ.get("EVOLUTION_REFLECTION_DEDUP_L2", "0.4"))
# Experiment: how many reflections to generate per interaction (default=1, existing behavior).
# Set to 2-3 to test whether more reflections per interaction improves retrieval quality.
MAX_REFLECTIONS_TO_GENERATE = int(os.environ.get("EVOLUTION_MAX_REFLECTIONS_TO_GENERATE", "1"))

# ── DSPy Optimizer ────────────────────────────────────────────────────────────

# DSPy uses its own env var for independent tuning, but shares the default.
DSPY_OLLAMA_MODEL = os.environ.get("DSPY_OLLAMA_MODEL", OLLAMA_MODEL)

DSPY_MIN_SAMPLES = int(os.environ.get("EVOLUTION_DSPY_MIN_SAMPLES", "20"))
DSPY_MIN_DOMAIN_SAMPLES = int(os.environ.get("EVOLUTION_DSPY_MIN_DOMAIN_SAMPLES", "10"))

# ── Auto-triggers ────────────────────────────────────────────────────────────

# Auto-optimize after this many new scored interactions (0 = disabled).
AUTO_OPTIMIZE_THRESHOLD = int(os.environ.get("EVOLUTION_AUTO_OPTIMIZE_THRESHOLD", "15"))
# Cooldown between principle extractions (hours).
PRINCIPLES_COOLDOWN_HOURS = int(os.environ.get("EVOLUTION_PRINCIPLES_COOLDOWN_HOURS", "24"))
# How many times to retry Gemini judge on JSON parse failure before falling back to neutral score.
JUDGE_RETRY_COUNT = int(os.environ.get("EVOLUTION_JUDGE_RETRY_COUNT", "1"))

# ── Group Opt-Out ────────────────────────────────────────────────────────────

# Comma-separated group folder names that are excluded from evolution tracking.
# Interactions from these groups are skipped in cmd_log_interaction without being stored.
EVOLUTION_SKIP_GROUPS: str = os.environ.get("EVOLUTION_SKIP_GROUPS", "")

# ── Compaction & Batch Judging ───────────────────────────────────────────────

# Compact scored interactions older than N days (replace with summary, NULL response).
COMPACT_AFTER_DAYS = int(os.environ.get("EVOLUTION_COMPACT_AFTER_DAYS", "7"))
# Judge interactions in batches of N to reduce API call frequency.
JUDGE_BATCH_SIZE = int(os.environ.get("EVOLUTION_JUDGE_BATCH_SIZE", "5"))


def load_api_key() -> str:
    """Load GEMINI_API_KEY from .env files (in priority order) or environment."""
    for path in _ENV_SEARCH_PATHS:
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        checked = ", ".join(str(p) for p in _ENV_SEARCH_PATHS)
        raise RuntimeError(
            f"GEMINI_API_KEY not found. Checked: {checked}, and env var."
        )
    return key
