"""Tests for persona-digest injection into the Evolution judge prompt (B).

Deterministic — no model calls. Covers the 3 prompt builders (block present iff a
profile is given; no-profile path byte-identical to legacy; gemini strict_json
retry carries the block) and the persona loader (trim, fail-soft, group-scoping).
"""
import pytest

from evolution.judge.ollama_judge import _build_eval_prompt as ollama_build
from evolution.judge.gemini_judge import _build_eval_prompt as gemini_build
from evolution.judge.llama_cpp_judge import _build_eval_prompt as llama_build
from evolution import persona

PROFILE = "- communication: concise + direct\n- learning: visuals + analogies"
BLOCK = "**Known user preferences (stored profile):**"

ALL_BUILDERS = [ollama_build, gemini_build, llama_build]


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_profile_block_present_when_profile_given(build):
    out = build("p", "r", None, None, PROFILE)
    assert BLOCK in out
    assert "concise + direct" in out


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_no_block_when_profile_absent(build):
    assert BLOCK not in build("p", "r", None, None)


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_absent_profile_byte_identical_to_legacy(build):
    """Passing no user_profile must reproduce the pre-change prompt exactly."""
    legacy = build("p", "r", ["Bash"], "some context")
    explicit_none = build("p", "r", ["Bash"], "some context", None)
    assert legacy == explicit_none
    assert BLOCK not in legacy


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_profile_block_after_context_before_user_prompt(build):
    out = build("p", "r", None, "ctx", PROFILE)
    assert out.index("**Context:**") < out.index(BLOCK) < out.index("**User prompt:**")


def test_gemini_strict_json_retry_keeps_block():
    out = gemini_build("p", "r", None, None, PROFILE, strict_json=True)
    assert BLOCK in out
    assert "valid JSON object" in out  # strict_json instruction still appended


# ── persona loader ────────────────────────────────────────────────────────────

def test_extract_workstyle_excludes_pii_sections():
    # Fixture uses fictional placeholders only (no real personal data in the public repo).
    raw = (
        "---\nid: x\ntype: persona-index\n---\n# Persona Index\n\n"
        "## taste/\n- movies: crime; roommate Alice likes romance\n\n"
        "## life/\n- household: Bob, Carol replacing Bob Aug 2099\n\n"
        "## work-style/\n- communication: concise+direct\n- learning: visuals+analogies\n\n"
        "## career/\n- employer: ExampleCorp interview salary notes\n"
    )
    out = persona._extract_workstyle(raw)
    assert out is not None
    assert "concise+direct" in out and "visuals+analogies" in out
    # PII-bearing sections must be excluded from anything sent to an external judge
    for pii in ("Alice", "Bob", "Carol", "ExampleCorp"):
        assert pii not in out, f"PII '{pii}' leaked into the digest"


def test_extract_workstyle_none_when_section_absent():
    assert persona._extract_workstyle("# Persona\n## taste/\n- movies\n") is None


def test_digest_respects_char_cap(monkeypatch, tmp_path):
    (tmp_path / "Persona").mkdir()
    (tmp_path / "Persona" / "INDEX.md").write_text(
        "## work-style/\n- communication: " + ("x" * 2000) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(persona, "load_vault_path", lambda: tmp_path)
    monkeypatch.setattr(persona, "JUDGE_MAX_PERSONA_CHARS", 120)
    persona._reset_cache_for_tests()
    assert len(persona.get_digest()) <= 120


def test_get_digest_failsoft_on_resolver_error(monkeypatch):
    def boom():
        raise RuntimeError("vault not configured")
    monkeypatch.setattr(persona, "load_vault_path", boom)
    persona._reset_cache_for_tests()
    assert persona.get_digest() is None


def test_digest_for_group_scopes_to_primary(monkeypatch, tmp_path):
    (tmp_path / "Persona").mkdir()
    (tmp_path / "Persona" / "INDEX.md").write_text(
        "---\nid: x\n---\n# Persona\n\n## work-style/\n- communication: concise\n", encoding="utf-8"
    )
    monkeypatch.setattr(persona, "load_vault_path", lambda: tmp_path)
    monkeypatch.setattr(persona, "JUDGE_PERSONA_GROUP", "primary")
    persona._reset_cache_for_tests()

    digest = persona.digest_for_group("primary")
    assert digest is not None and "concise" in digest
    assert persona.digest_for_group("other") is None   # cross-user → no leakage
    assert persona.digest_for_group(None) is None


def test_digest_for_group_off_by_default(monkeypatch):
    monkeypatch.setattr(persona, "JUDGE_PERSONA_GROUP", "")
    persona._reset_cache_for_tests()
    assert persona.digest_for_group("anything") is None
