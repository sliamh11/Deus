"""Compact persona digest loader for the Evolution judge.

Without the user's stored preferences in the prompt, the judge's
``personalization`` dimension is ungradable (the model can't know which
preferences exist). This loads a compact, work-style-only digest of the user's
preferences (from ``Persona/INDEX.md``) so the judge can grade it.

Three deliberate choices:
- **Fail-soft** — any failure returns ``None`` → personalization stays ungraded
  exactly as before the fix (no crash, no regression).
- **Primary-user scoped** — ``digest_for_group`` injects only for the configured
  ``DEUS_JUDGE_PERSONA_GROUP``; other groups get ``None`` (no cross-user leakage).
- **PII-scoped** — only the ``work-style`` section is loaded. The taste/life/
  career sections carry names, household, and employer data that are irrelevant
  to grading personalization and must not reach the external Gemini judge.
"""
import logging
import re
from typing import Optional

from .config import JUDGE_MAX_PERSONA_CHARS, JUDGE_PERSONA_GROUP
from .vault import load_vault_path

log = logging.getLogger(__name__)

# Module-level cache. ``_DIGEST_LOADED`` distinguishes "loaded, result was None"
# (vault/persona absent — don't retry every call) from "not yet loaded".
_DIGEST_CACHE: Optional[str] = None
_DIGEST_LOADED: bool = False

_WORKSTYLE_HEADING = re.compile(r"^##\s*work-style/?\s*$", re.MULTILINE)
_NEXT_SECTION = re.compile(r"^##\s", re.MULTILINE)


def _extract_workstyle(text: str) -> Optional[str]:
    """Return only the ``## work-style/`` section of ``Persona/INDEX.md``.

    Excludes taste/life/career — those carry PII (names, household, employer)
    irrelevant to grading personalization and unsafe to send to an external
    judge API. The work-style lines (communication + learning preferences) are
    exactly what the rubric needs. Returns ``None`` if the section is absent.
    """
    m = _WORKSTYLE_HEADING.search(text)
    if not m:
        return None
    rest = text[m.end():]
    nxt = _NEXT_SECTION.search(rest)
    section = (rest[: nxt.start()] if nxt else rest).strip()
    return section or None


def _load_digest() -> Optional[str]:
    """Load the work-style digest from the vault. Fail-soft → ``None``."""
    try:
        index = load_vault_path() / "Persona" / "INDEX.md"
        text = index.read_text(encoding="utf-8")
    except (RuntimeError, ValueError, OSError) as exc:
        log.debug("persona digest unavailable (%s); personalization stays ungraded", exc)
        return None
    section = _extract_workstyle(text)
    if not section:
        return None
    digest = f"Work-style preferences:\n{section}"
    if len(digest) > JUDGE_MAX_PERSONA_CHARS:
        digest = digest[:JUDGE_MAX_PERSONA_CHARS].rstrip()
    return digest


def get_digest() -> Optional[str]:
    """Lazily load + cache the persona digest. ``None`` if unavailable.

    Cached for the process lifetime (persona changes rarely; the judge runs many
    times/day). NOTE: the cache survives vault writes within the same process — a
    long-running maintenance loop won't observe a mid-run persona edit. Tests call
    ``_reset_cache_for_tests()`` to force a reload.
    """
    global _DIGEST_CACHE, _DIGEST_LOADED
    if not _DIGEST_LOADED:
        _DIGEST_CACHE = _load_digest()
        _DIGEST_LOADED = True
    return _DIGEST_CACHE


def digest_for_group(group_folder: Optional[str]) -> Optional[str]:
    """Return the persona digest only for the configured primary host group.

    Every other group (and the unconfigured default) gets ``None`` so the host
    user's preferences never grade a different user's interaction.
    """
    if not JUDGE_PERSONA_GROUP or group_folder != JUDGE_PERSONA_GROUP:
        return None
    return get_digest()


def _reset_cache_for_tests() -> None:
    """Test hook — clears the module cache so monkeypatched env takes effect."""
    global _DIGEST_CACHE, _DIGEST_LOADED
    _DIGEST_CACHE = None
    _DIGEST_LOADED = False
