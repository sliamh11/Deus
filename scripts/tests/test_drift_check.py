"""
Tests for scripts/drift_check.py — the --paths mode specifically.

The --paths mode walks each pattern's frontmatter `governs:` list and the
backtick-quoted path references in its body, and verifies every referenced
path exists on disk. These tests exercise both sources of references in
isolation using a temporary project tree.
"""
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable so `import drift_check` resolves.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import drift_check


# ── extract_body_paths ─────────────────────────────────────────────────────────

class TestExtractBodyPaths:
    def test_extracts_simple_backtick_path(self):
        text = "See `src/channels/registry.ts` for the implementation."
        assert drift_check.extract_body_paths(text) == {"src/channels/registry.ts"}

    def test_extracts_directory_reference(self):
        text = "All channels live in `src/channels/`."
        assert drift_check.extract_body_paths(text) == {"src/channels"}

    def test_extracts_multiple_paths(self):
        text = "Read `docs/SECURITY.md` and `docs/CONTRIBUTING-AI.md`."
        assert drift_check.extract_body_paths(text) == {
            "docs/SECURITY.md",
            "docs/CONTRIBUTING-AI.md",
        }

    def test_skips_glob_patterns(self):
        text = "Rebuild via `packages/mcp-*/` — one build per package."
        assert drift_check.extract_body_paths(text) == set()

    def test_skips_template_placeholders(self):
        text = "Run `packages/mcp-{channel}/` after editing."
        assert drift_check.extract_body_paths(text) == set()

    def test_skips_angle_placeholders(self):
        text = "Inside `packages/mcp-<name>/` run tsc."
        assert drift_check.extract_body_paths(text) == set()

    def test_ignores_frontmatter(self):
        text = (
            "---\n"
            "governs:\n"
            "  - src/never-scanned-from-body.ts\n"
            "---\n"
            "Body reference: `docs/CONTRIBUTING-AI.md`."
        )
        found = drift_check.extract_body_paths(text)
        assert found == {"docs/CONTRIBUTING-AI.md"}
        assert "src/never-scanned-from-body.ts" not in found

    def test_ignores_non_repo_tokens(self):
        text = "Install with `npm run build` and `node_modules/foo`."
        # Neither starts with a known top-level dir — both skipped.
        assert drift_check.extract_body_paths(text) == set()

    def test_handles_dotted_top_dirs(self):
        text = "Skills live in `.claude/skills/` alongside `.mex/ROUTER.md`."
        assert drift_check.extract_body_paths(text) == {
            ".claude/skills",
            ".mex/ROUTER.md",
        }


# ── check_paths ────────────────────────────────────────────────────────────────

def _build_project(tmp_path: Path, pattern_body: str, governs: list[str]) -> Path:
    """Create a minimal project tree with one pattern file."""
    patterns_dir = tmp_path / "patterns"
    patterns_dir.mkdir()

    governs_yaml = "\n".join(f"  - {p}" for p in governs)
    pattern_text = (
        "---\n"
        f"governs:\n{governs_yaml}\n"
        "---\n"
        f"{pattern_body}\n"
    )
    (patterns_dir / "demo.md").write_text(pattern_text)

    index = patterns_dir / "INDEX.md"
    index.write_text("| task | pattern | source |\n|---|---|---|\n| demo | `patterns/demo.md` | none |\n")
    return tmp_path


class TestCheckPaths:
    def test_passes_when_all_paths_exist(self, tmp_path, monkeypatch):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.ts").write_text("// real")
        _build_project(tmp_path, "See `src/real.ts` for details.", ["src/real.ts"])

        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_paths(tmp_path) == 0

    def test_fails_on_missing_governs_path(self, tmp_path, monkeypatch):
        _build_project(tmp_path, "No body refs.", ["src/gone.ts"])

        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_paths(tmp_path) == 1

    def test_fails_on_missing_body_path(self, tmp_path, monkeypatch):
        _build_project(
            tmp_path,
            "Check `src/ghost.ts` for the fix.",
            [],  # empty governs
        )

        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_paths(tmp_path) == 1

    def test_reports_source_in_output(self, tmp_path, monkeypatch, capsys):
        _build_project(tmp_path, "Ref `src/ghost.ts`.", ["src/also-gone.ts"])

        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        drift_check.check_paths(tmp_path)
        out = capsys.readouterr().out
        assert "[governs]" in out
        assert "[body]" in out
        assert "src/ghost.ts" in out
        assert "src/also-gone.ts" in out

    def test_ignores_glob_in_body(self, tmp_path, monkeypatch):
        (tmp_path / "packages").mkdir()  # wildcard expansion not attempted
        _build_project(
            tmp_path,
            "Run tsc inside `packages/mcp-*/`.",
            [],
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        # Glob is skipped, not treated as missing — exit 0.
        assert drift_check.check_paths(tmp_path) == 0


# ── parse_adr / helpers ───────────────────────────────────────────────────────

class TestPathsOverlap:
    def test_exact_match(self):
        assert drift_check._paths_overlap("eval/", "eval/")

    def test_dir_contains_file(self):
        assert drift_check._paths_overlap("src/", "src/startup-gate.ts")

    def test_file_under_dir(self):
        assert drift_check._paths_overlap("src/startup-gate.ts", "src/")

    def test_no_overlap_similar_names(self):
        assert not drift_check._paths_overlap("eval/", "evolution/")

    def test_strips_backticks_and_slashes(self):
        assert drift_check._paths_overlap("`eval/`", "eval")


class TestParseAdr:
    def test_parses_date_and_scope(self, tmp_path):
        adr = tmp_path / "a.md"
        adr.write_text(
            "# ADR: Example\n\n"
            "**Date:** 2026-04-08\n"
            "**Scope:** `evolution/`, `scripts/memory_indexer.py`\n"
        )
        parsed = drift_check.parse_adr(adr)
        assert parsed is not None
        assert parsed["date"] == "2026-04-08"
        assert parsed["scopes"] == ["evolution", "scripts/memory_indexer.py"]

    def test_returns_none_without_date(self, tmp_path):
        adr = tmp_path / "b.md"
        adr.write_text("# ADR: No date\n\n**Status:** Accepted\n")
        assert drift_check.parse_adr(adr) is None

    def test_empty_scopes_when_missing(self, tmp_path):
        adr = tmp_path / "c.md"
        adr.write_text("# ADR\n\n**Date:** 2026-04-01\n**Status:** Accepted\n")
        parsed = drift_check.parse_adr(adr)
        assert parsed is not None
        assert parsed["scopes"] == []


# ── check_adr ─────────────────────────────────────────────────────────────────

def _build_adr_project(
    tmp_path: Path,
    pattern_governs: list[str],
    pattern_last_verified: str,
    adr_date: str,
    adr_scope: str,
) -> Path:
    """Create a project with one pattern and one ADR."""
    (tmp_path / "patterns").mkdir()
    (tmp_path / "docs" / "decisions").mkdir(parents=True)

    governs_yaml = "\n".join(f"  - {p}" for p in pattern_governs)
    pattern_text = (
        "---\n"
        f"governs:\n{governs_yaml}\n"
        f'last_verified: "{pattern_last_verified}"\n'
        "---\n"
        "Demo pattern.\n"
    )
    (tmp_path / "patterns" / "demo.md").write_text(pattern_text)
    (tmp_path / "patterns" / "INDEX.md").write_text(
        "| task | pattern | source |\n|---|---|---|\n| demo | `patterns/demo.md` | none |\n"
    )

    adr_text = (
        "# ADR: Demo\n\n"
        f"**Date:** {adr_date}\n"
        f"**Scope:** {adr_scope}\n"
        "**Status:** Accepted\n"
    )
    (tmp_path / "docs" / "decisions" / "demo.md").write_text(adr_text)
    return tmp_path


class TestCheckAdr:
    def test_passes_when_adr_older_than_pattern(self, tmp_path, monkeypatch):
        _build_adr_project(
            tmp_path,
            pattern_governs=["evolution/"],
            pattern_last_verified="2026-04-09",
            adr_date="2026-03-01",
            adr_scope="`evolution/`",
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_adr(tmp_path) == 0

    def test_fails_when_adr_newer_with_overlap(self, tmp_path, monkeypatch):
        _build_adr_project(
            tmp_path,
            pattern_governs=["evolution/"],
            pattern_last_verified="2026-04-01",
            adr_date="2026-04-08",
            adr_scope="`evolution/`, `scripts/memory_indexer.py`",
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_adr(tmp_path) == 1

    def test_passes_when_adr_newer_but_no_overlap(self, tmp_path, monkeypatch):
        _build_adr_project(
            tmp_path,
            pattern_governs=["packages/"],  # unrelated
            pattern_last_verified="2026-04-01",
            adr_date="2026-04-08",
            adr_scope="`evolution/`",
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_adr(tmp_path) == 0

    def test_dir_file_overlap_triggers(self, tmp_path, monkeypatch):
        """A pattern governing `src/` should flag on an ADR scoped to `src/startup-gate.ts`."""
        _build_adr_project(
            tmp_path,
            pattern_governs=["src/"],
            pattern_last_verified="2026-04-01",
            adr_date="2026-04-09",
            adr_scope="`src/startup-gate.ts`",
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_adr(tmp_path) == 1

    def test_warns_on_missing_scope(self, tmp_path, monkeypatch, capsys):
        _build_adr_project(
            tmp_path,
            pattern_governs=["src/"],
            pattern_last_verified="2026-04-09",
            adr_date="2026-04-08",
            adr_scope="",  # will write an empty Scope line
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        # Missing scope is a fatal warning (exit 1) so CI forces the fix.
        rc = drift_check.check_adr(tmp_path)
        out = capsys.readouterr().out
        assert "missing **Scope:**" in out
        assert rc == 1


# ── parse_test_tasks / check_test_tasks ──────────────────────────────────────

def _build_tt_project(tmp_path: Path, test_tasks: list[str]) -> Path:
    """Create a project with one pattern file containing test_tasks."""
    (tmp_path / "patterns").mkdir()
    tasks_yaml = "\n".join(f'  - "{t}"' for t in test_tasks) if test_tasks else ""
    tasks_block = f"test_tasks:\n{tasks_yaml}\n" if test_tasks else ""
    pattern_text = (
        "---\n"
        "governs:\n  - src/\n"
        "last_verified: \"2026-04-09\"\n"
        f"{tasks_block}"
        "---\n"
        "Body.\n"
    )
    (tmp_path / "patterns" / "demo.md").write_text(pattern_text)
    (tmp_path / "patterns" / "INDEX.md").write_text(
        "| task | pattern | source |\n|---|---|---|\n| demo | `patterns/demo.md` | none |\n"
    )
    return tmp_path


class TestParseTestTasks:
    def test_parses_quoted_list(self, tmp_path):
        _build_tt_project(tmp_path, ["Add a Discord channel", "Fix silent drop"])
        tasks = drift_check.parse_test_tasks(tmp_path / "patterns" / "demo.md")
        assert tasks == ["Add a Discord channel", "Fix silent drop"]

    def test_empty_when_missing(self, tmp_path):
        _build_tt_project(tmp_path, [])  # no test_tasks key
        tasks = drift_check.parse_test_tasks(tmp_path / "patterns" / "demo.md")
        assert tasks == []

    def test_stops_at_next_key(self, tmp_path):
        pattern = tmp_path / "demo.md"
        pattern.write_text(
            "---\n"
            "test_tasks:\n"
            '  - "one"\n'
            '  - "two"\n'
            "governs:\n"
            "  - src/\n"
            "---\n"
            "body\n"
        )
        tasks = drift_check.parse_test_tasks(pattern)
        assert tasks == ["one", "two"]


class TestCheckTestTasks:
    def test_passes_with_three_tasks(self, tmp_path, monkeypatch):
        _build_tt_project(tmp_path, ["one", "two", "three"])
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_test_tasks(tmp_path) == 0

    def test_fails_below_minimum(self, tmp_path, monkeypatch):
        _build_tt_project(tmp_path, ["one", "two"])  # below minimum of 3
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_test_tasks(tmp_path) == 1

    def test_custom_minimum(self, tmp_path, monkeypatch):
        _build_tt_project(tmp_path, ["one", "two"])
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_test_tasks(tmp_path, minimum=2) == 0

    def test_fails_on_missing_test_tasks(self, tmp_path, monkeypatch):
        _build_tt_project(tmp_path, [])
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_test_tasks(tmp_path) == 1


# ── _load_source_docs ────────────────────────────────────────────────────────

class TestLoadSourceDocs:
    def test_loads_known_docs(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CONTRIBUTING-AI.md").write_text("# Contributing")
        (tmp_path / "docs" / "DEVELOPMENT.md").write_text("# Dev")
        (tmp_path / "docs" / "SECURITY.md").write_text("# Security")
        docs = drift_check._load_source_docs(tmp_path)
        assert "docs/CONTRIBUTING-AI.md" in docs
        assert "docs/DEVELOPMENT.md" in docs
        assert "docs/SECURITY.md" in docs
        assert docs["docs/CONTRIBUTING-AI.md"] == "# Contributing"

    def test_loads_all_adrs(self, tmp_path):
        (tmp_path / "docs" / "decisions").mkdir(parents=True)
        (tmp_path / "docs" / "decisions" / "INDEX.md").write_text("index")
        (tmp_path / "docs" / "decisions" / "one.md").write_text("adr one")
        (tmp_path / "docs" / "decisions" / "two.md").write_text("adr two")
        docs = drift_check._load_source_docs(tmp_path)
        assert "docs/decisions/INDEX.md" in docs
        assert "docs/decisions/one.md" in docs
        assert "docs/decisions/two.md" in docs

    def test_missing_files_skipped_silently(self, tmp_path):
        # No docs/ directory at all
        docs = drift_check._load_source_docs(tmp_path)
        assert docs == {}

    def test_partial_docs_directory(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CONTRIBUTING-AI.md").write_text("only this one")
        docs = drift_check._load_source_docs(tmp_path)
        assert list(docs.keys()) == ["docs/CONTRIBUTING-AI.md"]

    def test_discovers_new_docs_dynamically(self, tmp_path):
        """New docs are picked up without editing any hardcoded list."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "ONBOARDING.md").write_text("# Onboarding")
        (tmp_path / "docs" / "CONTRIBUTING-AI.md").write_text("# Contributing")
        docs = drift_check._load_source_docs(tmp_path)
        assert "docs/ONBOARDING.md" in docs
        assert "docs/CONTRIBUTING-AI.md" in docs


# ── _normalize_router_response ───────────────────────────────────────────────

class TestNormalizeRouterResponse:
    VALID = [
        "channel-add.md",
        "cross-platform.md",
        "debugging.md",
        "deployment.md",
        "eval-change.md",
        "general-code.md",
        "security-review.md",
        "skill-add.md",
    ]

    def test_exact_match(self):
        assert drift_check._normalize_router_response("deployment.md", self.VALID) == "deployment.md"

    def test_with_backticks(self):
        assert drift_check._normalize_router_response("`deployment.md`", self.VALID) == "deployment.md"

    def test_case_insensitive(self):
        assert drift_check._normalize_router_response("Deployment.MD", self.VALID) == "deployment.md"

    def test_missing_md_suffix(self):
        assert drift_check._normalize_router_response("general-code", self.VALID) == "general-code.md"

    def test_leading_path(self):
        assert drift_check._normalize_router_response("patterns/debugging.md", self.VALID) == "debugging.md"

    def test_truncated_unique_prefix(self):
        # `cross-` should match `cross-platform.md` uniquely.
        assert drift_check._normalize_router_response("cross-", self.VALID) == "cross-platform.md"

    def test_truncated_with_trailing_dot(self):
        # `deployment.` is a truncation of `deployment.md`.
        assert drift_check._normalize_router_response("deployment.", self.VALID) == "deployment.md"

    def test_empty_response(self):
        assert drift_check._normalize_router_response("", self.VALID) == ""

    def test_whitespace_only(self):
        assert drift_check._normalize_router_response("   \n\t  ", self.VALID) == ""

    def test_first_token_from_prose(self):
        # Model prepends prose despite instructions.
        assert drift_check._normalize_router_response("deployment.md would be best", self.VALID) == "deployment.md"

    def test_ambiguous_prefix_returns_cleaned_token(self):
        # `d` matches both `debugging.md` and `deployment.md` — not a unique prefix.
        result = drift_check._normalize_router_response("d", self.VALID)
        assert result == "d"  # no unique match, returned as-is for mismatch reporting

    def test_unknown_filename_passed_through(self):
        result = drift_check._normalize_router_response("phantom.md", self.VALID)
        assert result == "phantom.md"  # preserved so caller reports it as mismatch


# ── check_validate_router (skip paths — LLM-free) ───────────────────────────

class TestCheckValidateRouterSkip:
    """Unit tests for --validate-router's graceful-skip paths.

    The full LLM flow is smoke-tested separately. These tests verify the
    function exits cleanly in the three environments where it is expected
    to no-op without failing CI:
      1. No ROUTER.md present
      2. pattern_filter matches nothing
      3. (Gemini / API-key skip paths are exercised by the same code path
          in check_validate and covered there by the shared helpers.)
    """

    def test_skip_when_no_router(self, tmp_path, monkeypatch):
        (tmp_path / "patterns").mkdir()
        (tmp_path / "patterns" / "INDEX.md").write_text(
            "| task | pattern | source |\n|---|---|---|\n"
            "| demo | `patterns/demo.md` | none |\n"
        )
        (tmp_path / "patterns" / "demo.md").write_text(
            "---\ngovers:\n  - src/\nlast_verified: \"2026-04-09\"\n"
            "test_tasks:\n  - \"do a thing\"\n---\nbody\n"
        )
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        # No .mex/ROUTER.md anywhere. Should skip, not crash.
        # The google.genai import happens first — skip if unavailable too.
        rc = drift_check.check_validate_router(tmp_path)
        assert rc == 0

    def test_skip_when_pattern_filter_matches_nothing(self, tmp_path, monkeypatch):
        (tmp_path / "patterns").mkdir()
        (tmp_path / "patterns" / "INDEX.md").write_text(
            "| task | pattern | source |\n|---|---|---|\n"
            "| demo | `patterns/demo.md` | none |\n"
        )
        (tmp_path / "patterns" / "demo.md").write_text(
            "---\nlast_verified: \"2026-04-09\"\n---\nbody\n"
        )
        (tmp_path / ".mex").mkdir()
        (tmp_path / ".mex" / "ROUTER.md").write_text("# router")
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        # Must exit cleanly regardless of whether Gemini is configured.
        # On CI without GEMINI_API_KEY, skip happens earlier via the
        # API key check (stderr). Either path returns 0.
        rc = drift_check.check_validate_router(tmp_path, pattern_filter="nonexistent")
        assert rc == 0


# ── check_contradictions ──────────────────────────────────────────────────────

def _make_pattern_tree(tmp_path, patterns: dict[str, str]):
    """Helper: create patterns/INDEX.md + pattern files from a dict."""
    pdir = tmp_path / "patterns"
    pdir.mkdir(exist_ok=True)
    rows = []
    for name, body in patterns.items():
        (pdir / name).write_text(body)
        rows.append(f"| test | `patterns/{name}` | none |")
    table = "| task | pattern | source |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    (pdir / "INDEX.md").write_text(table)


class TestCheckContradictionsSkip:
    """Skip-path tests — no LLM needed."""

    def test_skip_without_api_key(self, tmp_path, monkeypatch):
        """Without google-genai or API key, should return 0 (skip)."""
        _make_pattern_tree(tmp_path, {"a.md": "---\n---\nrule A"})
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        rc = drift_check.check_contradictions(tmp_path)
        assert rc == 0

    def test_skip_when_filter_matches_nothing(self, tmp_path, monkeypatch):
        _make_pattern_tree(tmp_path, {"a.md": "---\n---\nrule A"})
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        rc = drift_check.check_contradictions(tmp_path, pattern_filter="nonexistent")
        assert rc == 0

    def test_not_in_check_all(self, tmp_path, monkeypatch, capsys):
        """check_all must NOT invoke check_contradictions (opt-in only)."""
        _make_pattern_tree(tmp_path, {
            "demo.md": "---\ngoverns:\n  - src/\nlast_verified: \"2026-04-09\"\n"
                       "test_tasks:\n  - \"do a thing\"\n  - \"do b\"\n  - \"do c\"\n---\nbody\n"
        })
        (tmp_path / "src").mkdir()
        (tmp_path / "docs").mkdir()
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        drift_check.check_all(tmp_path)
        captured = capsys.readouterr()
        assert "contradictions" not in captured.out.lower()


class TestCheckContradictionsMocked:
    """Mocked Gemini tests for the contradiction detection logic."""

    def _setup_and_mock(self, tmp_path, monkeypatch, patterns, llm_response):
        """Set up pattern tree and mock the Gemini imports + client."""
        _make_pattern_tree(tmp_path, patterns)
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)

        # Build a fake genai module tree
        class FakeResponse:
            text = llm_response

        class FakeModels:
            def generate_content(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self, **kwargs):
                self.models = FakeModels()

        class FakeConfig:
            def __init__(self, **kwargs):
                pass

        fake_genai = type(sys)("genai")
        fake_genai.Client = FakeClient
        fake_types = type(sys)("types")
        fake_types.GenerateContentConfig = FakeConfig
        fake_genai.types = fake_types

        fake_google = type(sys)("google")
        fake_google.genai = fake_genai

        # Inject into sys.modules so the lazy import inside check_contradictions works
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        # Mock evolution.config
        fake_config = type(sys)("config")
        fake_config.GEN_MODELS = ["test-model"]
        fake_config.load_api_key = lambda: "fake-key"
        fake_evolution = type(sys)("evolution")
        fake_evolution.config = fake_config
        monkeypatch.setitem(sys.modules, "evolution", fake_evolution)
        monkeypatch.setitem(sys.modules, "evolution.config", fake_config)

    def test_detects_conflict(self, tmp_path, monkeypatch):
        patterns = {
            "a.md": "---\n---\nAlways use tabs for indentation.",
            "b.md": "---\n---\nNever use tabs — spaces only.",
        }
        self._setup_and_mock(
            tmp_path, monkeypatch, patterns,
            'CONTRADICTION: a.md says "Always use tabs" vs b.md says "Never use tabs"',
        )
        rc = drift_check.check_contradictions(tmp_path)
        assert rc == 1

    def test_clean_no_conflict(self, tmp_path, monkeypatch):
        patterns = {
            "a.md": "---\n---\nAlways run tests before committing.",
            "b.md": "---\n---\nAlways lint before committing.",
        }
        self._setup_and_mock(tmp_path, monkeypatch, patterns, "NO_CONTRADICTIONS")
        rc = drift_check.check_contradictions(tmp_path)
        assert rc == 0


# ── _file_in_changed_set ─────────────────────────────────────────────────────


class TestFileInChangedSet:
    def test_exact_file_match(self, tmp_path):
        changed = {"scripts/memory_indexer.py", "src/types.ts"}
        assert drift_check._file_in_changed_set("scripts/memory_indexer.py", changed, tmp_path)

    def test_directory_governs_file_inside(self, tmp_path):
        changed = {"src/container-runner.ts", "src/types.ts"}
        assert drift_check._file_in_changed_set("src/", changed, tmp_path)

    def test_no_match_different_dir(self, tmp_path):
        changed = {"src/types.ts"}
        assert not drift_check._file_in_changed_set("evolution/", changed, tmp_path)

    def test_no_false_positive_on_prefix(self, tmp_path):
        """evolution/ should not match eval/ even though both start with 'e'."""
        changed = {"evolution/judge/provider.py"}
        assert not drift_check._file_in_changed_set("eval/", changed, tmp_path)

    def test_empty_changed_set(self, tmp_path):
        assert not drift_check._file_in_changed_set("src/", set(), tmp_path)


# ── check_shadow ─────────────────────────────────────────────────────────────


def _build_shadow_project(
    tmp_path: Path,
    public_files: list[str],
    private_files: list[str],
) -> Path:
    """Create a project tree with public and private script files."""
    for f in public_files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# public {f}")
    for f in private_files:
        p = tmp_path / "src" / "private" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# private {f}")
    return tmp_path


class TestCheckShadow:
    def test_no_private_dir(self, tmp_path, monkeypatch):
        """No src/private/ at all — should pass cleanly."""
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_shadow(tmp_path) == 0

    def test_private_only_file_no_warning(self, tmp_path, monkeypatch):
        """Private file with no public equivalent — no shadow, no warning."""
        _build_shadow_project(tmp_path, [], ["scripts/secret.py"])
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_shadow(tmp_path) == 0

    def test_shadow_with_correct_symlink(self, tmp_path, monkeypatch):
        """Private shadows public and /tmp/ symlink points to private — OK."""
        _build_shadow_project(
            tmp_path,
            ["scripts/tool.py"],
            ["scripts/tool.py"],
        )
        private_path = tmp_path / "src" / "private" / "scripts" / "tool.py"
        tmp_link = Path("/tmp") / "tool.py"
        # Clean up any pre-existing symlink
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(private_path)
        try:
            monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
            assert drift_check.check_shadow(tmp_path) == 0
        finally:
            tmp_link.unlink()

    def test_shadow_with_missing_symlink(self, tmp_path, monkeypatch, capsys):
        """Private shadows public but no /tmp/ symlink — should warn."""
        # Use a unique filename to avoid collisions with real /tmp/ files
        fname = "_drift_test_shadow_missing.py"
        _build_shadow_project(
            tmp_path,
            [f"scripts/{fname}"],
            [f"scripts/{fname}"],
        )
        tmp_link = Path("/tmp") / fname
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_shadow(tmp_path) == 1
        out = capsys.readouterr().out
        assert "WARN" in out
        assert fname in out

    def test_shadow_with_wrong_symlink(self, tmp_path, monkeypatch, capsys):
        """Private shadows public but /tmp/ symlink points to public — should warn."""
        fname = "_drift_test_shadow_wrong.py"
        _build_shadow_project(
            tmp_path,
            [f"scripts/{fname}"],
            [f"scripts/{fname}"],
        )
        public_path = tmp_path / "scripts" / fname
        tmp_link = Path("/tmp") / fname
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(public_path)
        try:
            monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
            assert drift_check.check_shadow(tmp_path) == 1
            out = capsys.readouterr().out
            assert "WARN" in out
            assert "should point to" in out
        finally:
            tmp_link.unlink()

    def test_skips_dotfiles(self, tmp_path, monkeypatch):
        """Hidden files in src/private/ should be ignored."""
        _build_shadow_project(tmp_path, ["scripts/.hidden"], ["scripts/.hidden"])
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        assert drift_check.check_shadow(tmp_path) == 0

    def test_included_in_check_all(self, tmp_path, monkeypatch, capsys):
        """check_all should include the shadow check."""
        (tmp_path / "patterns").mkdir()
        (tmp_path / "patterns" / "INDEX.md").write_text(
            "| task | pattern | source |\n|---|---|---|\n"
        )
        (tmp_path / "docs").mkdir()
        (tmp_path / "src" / "private").mkdir(parents=True)
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        drift_check.check_all(tmp_path)
        out = capsys.readouterr().out
        assert "shadow" in out.lower()


# ── check_index_completeness ─────────────────────────────────────────────

class TestCheckIndexCompleteness:
    """Bidirectional check: every leaf referenced, every reference resolves.

    Guards against the common mistake of adding a pattern (or ADR) file but
    forgetting to wire it into the index — which would leave the agent unable
    to route to it.
    """

    def _build(self, tmp_path, pattern_files, index_refs, decisions_files=None, decisions_refs=None):
        """Seed tmp_path with patterns/ and docs/decisions/ at fabricated contents.

        pattern_files / decisions_files: filenames to create under the dir.
        index_refs / decisions_refs: filenames to reference from INDEX.md.
        """
        (tmp_path / "patterns").mkdir()
        for name in pattern_files:
            (tmp_path / "patterns" / name).write_text("# stub\n")
        refs = "\n".join(f"- [x](patterns/{n})" for n in index_refs)
        (tmp_path / "patterns" / "INDEX.md").write_text(f"# Patterns\n{refs}\n")

        if decisions_files is not None:
            (tmp_path / "docs" / "decisions").mkdir(parents=True)
            for name in decisions_files:
                (tmp_path / "docs" / "decisions" / name).write_text("# stub\n")
            drefs = "\n".join(f"- `{n}`" for n in (decisions_refs or []))
            (tmp_path / "docs" / "decisions" / "INDEX.md").write_text(f"# Decisions\n{drefs}\n")

    def test_all_synced_returns_zero(self, tmp_path, capsys):
        self._build(tmp_path, ["a.md", "b.md"], ["a.md", "b.md"])
        assert drift_check.check_index_completeness(tmp_path) == 0
        out = capsys.readouterr().out
        assert "in sync" in out

    def test_orphan_leaf_is_flagged(self, tmp_path, capsys):
        # b.md exists on disk but INDEX.md only references a.md.
        self._build(tmp_path, ["a.md", "b.md"], ["a.md"])
        rc = drift_check.check_index_completeness(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "orphan" in out.lower()
        assert "b.md" in out

    def test_dangling_reference_is_flagged(self, tmp_path, capsys):
        # INDEX.md references c.md, but it doesn't exist on disk.
        self._build(tmp_path, ["a.md"], ["a.md", "c.md"])
        rc = drift_check.check_index_completeness(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "dangling" in out.lower()
        assert "c.md" in out

    def test_orphan_and_dangling_both_reported(self, tmp_path, capsys):
        self._build(tmp_path, ["a.md", "b.md"], ["a.md", "c.md"])
        rc = drift_check.check_index_completeness(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "b.md" in out  # orphan
        assert "c.md" in out  # dangling

    def test_index_md_itself_is_not_an_orphan(self, tmp_path):
        # INDEX.md is always excluded from the on-disk set even though it
        # matches the *.md glob — otherwise every index would self-flag.
        self._build(tmp_path, ["a.md"], ["a.md"])
        assert drift_check.check_index_completeness(tmp_path) == 0

    def test_decisions_index_also_checked(self, tmp_path, capsys):
        # Orphan in docs/decisions/ is caught just like patterns/.
        self._build(
            tmp_path,
            pattern_files=["a.md"], index_refs=["a.md"],
            decisions_files=["0001.md", "0002-orphan.md"], decisions_refs=["0001.md"],
        )
        rc = drift_check.check_index_completeness(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "0002-orphan.md" in out

    def test_missing_index_file_is_flagged(self, tmp_path, capsys):
        # patterns dir exists with a leaf, but no INDEX.md at all.
        (tmp_path / "patterns").mkdir()
        (tmp_path / "patterns" / "a.md").write_text("# stub\n")
        rc = drift_check.check_index_completeness(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "index file is missing" in out.lower()

    def test_included_in_check_all(self, tmp_path, monkeypatch, capsys):
        # --all must run the new check; look for the section header.
        self._build(tmp_path, ["a.md"], ["a.md"])
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "src" / "private").mkdir(parents=True)
        monkeypatch.setattr(drift_check, "PROJECT_ROOT", tmp_path)
        drift_check.check_all(tmp_path)
        out = capsys.readouterr().out
        assert "index completeness" in out.lower()
