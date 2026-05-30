"""
Shared configuration for the Deus Evolution loop.
All values can be overridden via environment variables.

Data-sharing notice
-------------------
When EVOLUTION_ENABLED is not '0', the following user data may reach external
services:

  - Gemini API (via GEMINI_API_KEY): full interaction prompts + responses for
    scoring (judge), corrective lesson generation (reflexion), and optionally
    domain classification. Truncated to JUDGE_MAX_PROMPT/RESPONSE_CHARS each.
  - Gemini API: vault/memory file chunks for semantic embeddings, UNLESS
    Ollama is running and EMBEDDING_PROVIDER != 'gemini'.

To keep all processing local:
  - Set EVOLUTION_ENABLED=0  (disables judge + reflexion + domain LLM fallback)
  - Keep Ollama running with an embedding model (handles vault embeddings)
  - Or set EMBEDDING_PROVIDER=ollama  (forces local embeddings)

See docs/security/data-flows.md for the full audit.
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

# Maximum characters of user prompt / agent response sent to the Gemini judge.
# Bounds API payloads and limits inadvertent PII exposure.
# The judge only needs enough context to assess quality — truncation at 2000
# chars preserves scoring signal while capping outbound payload size.
JUDGE_MAX_PROMPT_CHARS = int(os.environ.get("EVOLUTION_JUDGE_MAX_PROMPT_CHARS", "2000"))
JUDGE_MAX_RESPONSE_CHARS = int(os.environ.get("EVOLUTION_JUDGE_MAX_RESPONSE_CHARS", "2000"))

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

# Minimum judge-score improvement a new artifact must clear (over the better of
# the un-optimized baseline and the current active artifact) before it is
# activated. Below this margin the artifact is persisted for audit but NOT
# activated — the ship-if-better gate that makes the optimizer loop monotonic.
DSPY_SHIP_MARGIN = float(os.environ.get("EVOLUTION_DSPY_SHIP_MARGIN", "0.02"))

# Hard cap (chars) on an optimized-prompt instruction before it is injected into
# the agent prompt (LIA-152). An artifact is untrusted LLM output, so its
# extracted instruction is length-capped in addition to being boundary-tagged.
OPTIMIZED_PROMPT_MAX_CHARS = int(os.environ.get("EVOLUTION_OPTIMIZED_PROMPT_MAX_CHARS", "2000"))

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


# ── Correction Mining ───────────────────────────────────────────────────────

CORRECTION_VOCAB = [
    "without the", "not like that", "can you redo", "shorter", "longer",
    "different format", "try again", "no,", "wrong",
    "that's not", "thats not", "don't include", "remove the", "change the",
    "actually,", "i meant", "i said",
]
CORRECTION_MAX_PROMPT_LEN = int(os.environ.get("EVOLUTION_CORRECTION_MAX_LEN", "120"))


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
