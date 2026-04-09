"""
Provider/strategy pattern for storage backends.

Each backend (SQLite, future Postgres/DuckDB) implements StorageProvider.
StorageRegistry resolves the best available backend at runtime.
"""
from abc import ABC, abstractmethod
from typing import Optional


class NoStorageProviderError(RuntimeError):
    """Raised when no storage provider is available."""
    pass


class StorageProvider(ABC):
    """
    A backend that can persist interactions, reflections, artifacts,
    and principle extractions.

    Subclass this for each backend (SQLite, Postgres, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name, e.g. 'sqlite', 'postgres'."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower = preferred during auto-detection."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this backend can serve requests right now."""
        ...

    # ── Interaction operations ───────────────────────────────────────────────

    @abstractmethod
    def log_interaction(
        self,
        *,
        prompt: str,
        response: Optional[str],
        group_folder: str,
        timestamp: str,
        interaction_id: str,
        latency_ms: Optional[float] = None,
        tools_used: Optional[str] = None,
        session_id: Optional[str] = None,
        eval_suite: str = "runtime",
        domain_presets: Optional[str] = None,
        user_signal: Optional[str] = None,
        context_tokens: Optional[int] = None,
    ) -> str:
        """Persist one agent interaction. Returns the interaction ID."""
        ...

    @abstractmethod
    def update_interaction(self, interaction_id: str, **fields) -> None:
        """Update fields on an existing interaction (e.g. judge_score, judge_dims)."""
        ...

    @abstractmethod
    def get_interaction(self, interaction_id: str) -> Optional[dict]:
        """Fetch a single interaction by ID, or None."""
        ...

    @abstractmethod
    def get_recent_interactions(
        self,
        *,
        limit: int = 50,
        group_folder: Optional[str] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        eval_suite: Optional[str] = "runtime",
        domain: Optional[str] = None,
    ) -> list[dict]:
        """Fetch recent interactions, optionally filtered."""
        ...

    @abstractmethod
    def get_previous_in_session(
        self, session_id: str, current_id: str,
    ) -> Optional[dict]:
        """Get the most recent interaction in a session, excluding current_id."""
        ...

    @abstractmethod
    def count_interactions(self, **filters) -> int:
        """Count interactions matching the given filters."""
        ...

    # ── Score trend ──────────────────────────────────────────────────────────

    @abstractmethod
    def score_trend(
        self,
        *,
        group_folder: Optional[str] = None,
        days: int = 30,
        domain: Optional[str] = None,
    ) -> list[dict]:
        """Daily average judge scores for the last N days."""
        ...

    @abstractmethod
    def token_trend(
        self,
        *,
        days: int = 30,
    ) -> list[dict]:
        """Daily average context_tokens for the last N days."""
        ...

    # ── Reflection operations ────────────────────────────────────────────────

    @abstractmethod
    def save_reflection(
        self,
        *,
        reflection_id: str,
        content: str,
        category: str,
        score_at_gen: float,
        timestamp: str,
        embedding: bytes,
        interaction_id: Optional[str] = None,
        group_folder: Optional[str] = None,
    ) -> str:
        """Persist a reflection with its embedding. Returns the reflection ID."""
        ...

    @abstractmethod
    def get_reflections_by_embedding(
        self,
        embedding: bytes,
        top_k: int,
        group_folder: Optional[str] = None,
        min_score: Optional[float] = None,
    ) -> list[dict]:
        """Retrieve reflections by vector similarity search."""
        ...

    @abstractmethod
    def check_reflection_duplicate(
        self,
        embedding: bytes,
        group_folder: Optional[str],
        threshold: float,
    ) -> bool:
        """Check if a semantically similar reflection already exists."""
        ...

    @abstractmethod
    def increment_reflection_retrieved(self, reflection_id: str) -> None:
        """Increment the times_retrieved counter for a reflection."""
        ...

    @abstractmethod
    def increment_reflection_helpful(self, reflection_id: str) -> None:
        """Increment the times_helpful counter for a reflection."""
        ...

    @abstractmethod
    def archive_stale_reflections(self, days: int) -> int:
        """Archive reflections never retrieved and older than N days. Returns count."""
        ...

    @abstractmethod
    def count_stale_reflections(self, days: int) -> int:
        """Count reflections that would be archived (dry-run)."""
        ...

    @abstractmethod
    def count_reflections(self) -> int:
        """Count total reflections."""
        ...

    @abstractmethod
    def count_helpful_reflections(self) -> int:
        """Count reflections that have been marked helpful at least once."""
        ...

    @abstractmethod
    def reflections_by_category(self) -> list[dict]:
        """Return reflection counts grouped by category."""
        ...

    @abstractmethod
    def get_reflections_for_interaction(self, interaction_id: str) -> list[dict]:
        """Get all reflections linked to a specific interaction."""
        ...

    # ── Artifact operations ──────────────────────────────────────────────────

    @abstractmethod
    def save_artifact(
        self,
        *,
        artifact_id: str,
        module: str,
        content: str,
        created_at: str,
        baseline_score: Optional[float] = None,
        optimized_score: Optional[float] = None,
        sample_count: Optional[int] = None,
    ) -> str:
        """Save a prompt artifact, deactivating previous ones for the same module."""
        ...

    @abstractmethod
    def get_active_artifact(self, module: str) -> Optional[dict]:
        """Return the currently active artifact for a module, or None."""
        ...

    @abstractmethod
    def list_artifacts(self, module: Optional[str] = None, limit: int = 10) -> list[dict]:
        """List artifacts, optionally filtered by module."""
        ...

    @abstractmethod
    def get_latest_artifact_timestamp(self) -> Optional[str]:
        """Return the created_at of the most recent artifact, or None."""
        ...

    # ── Principle extraction tracking ────────────────────────────────────────

    @abstractmethod
    def get_last_extraction(self, domain: str) -> Optional[dict]:
        """Get the most recent principle extraction for a domain."""
        ...

    @abstractmethod
    def record_extraction(
        self,
        *,
        extraction_id: str,
        domain: str,
        extracted_at: str,
        interaction_count: int,
        principles_count: int,
    ) -> None:
        """Record that a principle extraction happened."""
        ...

    # ── Status / aggregate queries ───────────────────────────────────────────

    @abstractmethod
    def interaction_stats(self, eval_suite: str) -> dict:
        """Return {total, scored, avg_score} for an eval_suite."""
        ...

    @abstractmethod
    def backfill_reflection_count(self) -> int:
        """Count reflections linked to backfill interactions."""
        ...

    @abstractmethod
    def count_scored_since(self, since_timestamp: str) -> int:
        """Count scored interactions since a timestamp."""
        ...

    @abstractmethod
    def count_new_scored(
        self,
        *,
        since_timestamp: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> int:
        """Count scored interactions since a timestamp, optionally filtered by domain."""
        ...

    @abstractmethod
    def domain_comparison(self, domain: str) -> dict:
        """Compare avg scores with vs without a domain preset. Returns {with_avg, with_n, without_avg, without_n}."""
        ...

    # ── Compaction & batch judging ──────────────────────────────────────────

    @abstractmethod
    def get_compactable_interactions(self, days: int, limit: int = 50) -> list[dict]:
        """Fetch scored interactions older than N days with long prompt text, eligible for compaction."""
        ...

    @abstractmethod
    def compact_interaction(self, interaction_id: str, summary: str) -> None:
        """Replace an interaction's prompt with a summary and NULL out the response."""
        ...

    @abstractmethod
    def get_unjudged_interactions(self, limit: int = 50) -> list[dict]:
        """Fetch interactions that have not been judged yet (judge_score IS NULL)."""
        ...

    @abstractmethod
    def score_by_reflection_count(self) -> list[dict]:
        """
        Return average judge score grouped by the number of reflections an
        interaction has.

        Returns a list of dicts ordered by reflection_count ascending:
            [{"reflection_count": int, "avg_score": float, "interaction_count": int}, ...]

        Only scored interactions with at least one reflection (or zero reflections)
        are included.  Useful for measuring whether generating more reflections per
        interaction correlates with higher or lower base quality.
        """
        ...


class StorageRegistry:
    """
    Central registry of storage providers.

    Usage:
        registry = StorageRegistry.default()
        provider = registry.resolve()              # auto-detect best
        provider = registry.resolve("sqlite")      # explicit choice
    """

    _instance: Optional["StorageRegistry"] = None

    def __init__(self):
        self._providers: dict[str, StorageProvider] = {}

    @classmethod
    def default(cls) -> "StorageRegistry":
        """Return the singleton registry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton -- for testing only."""
        cls._instance = None

    def register(self, provider: StorageProvider) -> None:
        """Register a provider. Last-write-wins for same name."""
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        """Remove a provider by name."""
        self._providers.pop(name, None)

    def get(self, name: str) -> StorageProvider:
        """Get a provider by exact name. Raises KeyError if not found."""
        return self._providers[name]

    def list_providers(self) -> list[str]:
        """Return registered provider names sorted by priority."""
        return [
            p.name for p in sorted(self._providers.values(), key=lambda p: p.priority)
        ]

    def resolve(self, preference: Optional[str] = None) -> StorageProvider:
        """
        Resolve the best available provider.

        Resolution order:
        1. DEUS_STORAGE_PROVIDER env var (if set)
        2. Explicit preference argument
        3. Auto-detect: lowest priority number among available providers

        Raises NoStorageProviderError if nothing works.
        """
        import os

        # 1. Env var override
        env_pref = os.environ.get("DEUS_STORAGE_PROVIDER", "").lower()
        effective = env_pref or (preference.lower() if preference else None)

        # 2. Explicit preference
        if effective:
            if effective not in self._providers:
                raise NoStorageProviderError(
                    f"Provider '{effective}' not registered. "
                    f"Available: {self.list_providers()}"
                )
            provider = self._providers[effective]
            if not provider.is_available():
                raise NoStorageProviderError(
                    f"Provider '{effective}' is registered but not available."
                )
            return provider

        # 3. Auto-detect by priority
        candidates = sorted(self._providers.values(), key=lambda p: p.priority)
        for provider in candidates:
            if provider.is_available():
                return provider

        raise NoStorageProviderError(
            f"No storage provider available. Registered: {self.list_providers()}"
        )
