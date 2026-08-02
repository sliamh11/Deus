"""Unit tests for evolution/judge/openai_judge.py — codex-exec runtime judge wrapper."""
import asyncio
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from evolution.judge.base import JudgeResult
from evolution.judge.openai_judge import (
    OpenAIRuntimeJudge,
    _build_sandbox_profile,
    _call_openai,
    _cap_context_and_profile,
    _darwin_per_user_root,
    _is_macho_binary,
    _parse_result,
    is_openai_available,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


_FAKE_CODEX_PATH = "/opt/homebrew/bin/codex"


@pytest.fixture(autouse=True)
def _fake_env():
    """codex/sandbox-exec present, EVOLUTION_OPENAI_JUDGE_ENABLED set, per-user
    root resolvable, Darwin platform — the baseline every test starts from.
    platform.system is pinned to "Darwin" here (not just left to the real OS)
    because this module is Darwin-only by design and CI runs these tests on
    Linux runners: without this, is_openai_available()'s own Darwin gate
    short-circuits to False before ever reaching the mocks individual tests
    set up (_is_macho_binary, subprocess.run), so tests asserting the
    all-mocks-succeed path silently fail on Linux CI while passing locally
    on macOS. test_false_on_non_darwin overrides this back to "Linux" within
    its own `with` block to test that gate specifically. Deliberately does
    NOT patch os.path.realpath globally (that leaked into pytest's own
    tmp_path fixture machinery, which also calls realpath internally) —
    shutil.which is patched to directly return the fake codex path, and
    os.path.realpath is left real (harmless on a nonexistent path: returns
    the input unchanged)."""
    with patch("evolution.judge.openai_judge.shutil.which", return_value=_FAKE_CODEX_PATH), \
         patch("evolution.judge.openai_judge._darwin_per_user_root", return_value="/private/var/folders/1z/xxxxxxxxxxxxxxxxxxxxxxxxxxxx"), \
         patch("evolution.judge.openai_judge.platform.system", return_value="Darwin"), \
         patch.dict("os.environ", {"EVOLUTION_OPENAI_JUDGE_ENABLED": "1"}):
        yield


# ── _is_macho_binary ─────────────────────────────────────────────────────────


class TestIsMachoBinary:
    def test_thin_macho_magic_is_binary(self, tmp_path):
        p = tmp_path / "fake_binary"
        p.write_bytes(bytes.fromhex("feedfacf") + b"\x00" * 100)
        assert _is_macho_binary(str(p)) is True

    def test_fat_64_magic_is_binary(self, tmp_path):
        p = tmp_path / "fake_universal"
        p.write_bytes(bytes.fromhex("cafebabf") + b"\x00" * 100)
        assert _is_macho_binary(str(p)) is True

    def test_shebang_script_is_not_binary(self, tmp_path):
        p = tmp_path / "launcher.js"
        p.write_bytes(b"#!/usr/bin/env node\nconsole.log('hi')\n")
        assert _is_macho_binary(str(p)) is False

    def test_missing_file_is_not_binary(self, tmp_path):
        assert _is_macho_binary(str(tmp_path / "nonexistent")) is False

    def test_empty_file_is_not_binary(self, tmp_path):
        p = tmp_path / "empty"
        p.write_bytes(b"")
        assert _is_macho_binary(str(p)) is False


# ── _darwin_per_user_root ────────────────────────────────────────────────────


class TestDarwinPerUserRoot:
    def test_resolves_valid_root(self):
        with patch("evolution.judge.openai_judge.subprocess.run") as mock_run, \
             patch("evolution.judge.openai_judge.os.path.realpath",
                   return_value="/private/var/folders/1z/hashhashhash/T"):
            mock_run.return_value = MagicMock(stdout="/var/folders/1z/hashhashhash/T/\n")
            root = _darwin_per_user_root()
        assert root == "/private/var/folders/1z/hashhashhash"

    def test_fails_closed_on_unexpected_shape(self):
        """The TMPDIR-absent (-> /private) and TMPDIR="" (-> cwd) failure
        modes this function exists to close both produce a path with the
        wrong segment count -- confirm the regex guard rejects them rather
        than silently returning a broader-than-intended grant."""
        with patch("evolution.judge.openai_judge.subprocess.run") as mock_run, \
             patch("evolution.judge.openai_judge.os.path.realpath", return_value="/private"):
            mock_run.return_value = MagicMock(stdout="/tmp\n")
            with pytest.raises(RuntimeError, match="Unexpected Darwin per-user temp root shape"):
                _darwin_per_user_root()

    def test_fails_closed_on_cwd_derived_path(self):
        with patch("evolution.judge.openai_judge.subprocess.run") as mock_run, \
             patch("evolution.judge.openai_judge.os.path.realpath",
                   return_value="/Users/someone/deus/.claude/worktrees/foo"):
            mock_run.return_value = MagicMock(stdout="\n")
            with pytest.raises(RuntimeError, match="Unexpected Darwin per-user temp root shape"):
                _darwin_per_user_root()


# ── _build_sandbox_profile ───────────────────────────────────────────────────


class TestBuildSandboxProfile:
    def test_process_exec_scoped_to_exact_codex_binary(self):
        profile = _build_sandbox_profile("/real/codex/binary", "/per/user/root", "/iso/dir")
        assert '(allow process-exec (literal "/real/codex/binary"))' in profile
        # No blanket process-exec allow anywhere in the profile.
        assert "(allow process-exec)" not in profile

    def test_grants_are_scoped_not_blanket(self):
        profile = _build_sandbox_profile("/real/codex/binary", "/per/user/root", "/iso/dir")
        assert '(subpath "/per/user/root")' in profile
        assert '(subpath "/iso/dir")' in profile
        # The whole-tree grant this design deliberately avoids.
        assert '(subpath "/private/var/folders")' not in profile

    def test_codex_home_narrow_allowlist_present(self):
        profile = _build_sandbox_profile("/real/codex/binary", "/per/user/root", "/iso/dir")
        assert "auth.json" in profile
        assert "installation_id" in profile
        # history.jsonl / hooks.json / config.toml must NOT be individually
        # granted -- only the directory-listing + narrow fixed-path allowlist.
        assert "history.jsonl" not in profile
        assert "hooks.json" not in profile


# ── is_openai_available ──────────────────────────────────────────────────────


class TestIsOpenAIAvailable:
    def test_false_when_not_opted_in(self):
        with patch.dict("os.environ", {"EVOLUTION_OPENAI_JUDGE_ENABLED": "0"}):
            assert is_openai_available() is False

    def test_false_on_non_darwin(self):
        with patch("evolution.judge.openai_judge.platform.system", return_value="Linux"):
            assert is_openai_available() is False

    def test_false_when_codex_not_on_path(self):
        with patch("evolution.judge.openai_judge.shutil.which", return_value=None):
            assert is_openai_available() is False

    def test_false_when_sandbox_exec_not_on_path(self):
        def which(name):
            return None if name == "sandbox-exec" else "/opt/homebrew/bin/codex"
        with patch("evolution.judge.openai_judge.shutil.which", side_effect=which):
            assert is_openai_available() is False

    def test_false_for_non_macho_launcher(self):
        """The npm-install case: `codex` resolves to a JS launcher, not a
        native binary -- must report unavailable, not attempt a call that
        would fail confusingly against a Seatbelt profile built for a
        binary that isn't the one actually running."""
        with patch("evolution.judge.openai_judge._is_macho_binary", return_value=False):
            assert is_openai_available() is False

    def test_true_when_native_binary_and_logged_in(self):
        with patch("evolution.judge.openai_judge._is_macho_binary", return_value=True), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)):
            assert is_openai_available() is True

    def test_false_when_native_binary_but_logged_out(self):
        with patch("evolution.judge.openai_judge._is_macho_binary", return_value=True), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=1)):
            assert is_openai_available() is False

    def test_false_on_login_status_timeout(self):
        with patch("evolution.judge.openai_judge._is_macho_binary", return_value=True), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10)):
            assert is_openai_available() is False

    def test_login_status_uses_minimal_env_not_inherited(self):
        """Round-6 GPT-backend finding: availability check must use the same
        minimal env as the real call, not the inherited environment (which
        could report availability against a different CODEX_HOME)."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _fake_completed_process(returncode=0)

        with patch("evolution.judge.openai_judge._is_macho_binary", return_value=True), \
             patch("evolution.judge.openai_judge.subprocess.run", side_effect=fake_run), \
             patch.dict("os.environ", {"CODEX_HOME": "/some/other/codex/home", "SECRET": "leak-me"}):
            is_openai_available()

        assert "CODEX_HOME" not in captured["env"]
        assert "SECRET" not in captured["env"]
        assert set(captured["env"].keys()) <= {"PATH", "HOME"}


# ── _call_openai ─────────────────────────────────────────────────────────────


class TestCallOpenAI:
    def test_returns_output_file_contents_on_success(self, tmp_path):
        out_file = tmp_path / "out.json"
        out_file.write_text('{"quality_level": 5}')

        def fake_mkdtemp(dir=None):
            return str(tmp_path)

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", side_effect=fake_mkdtemp), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)) as mock_run:
            # subprocess "writes" out.json as a side effect of running codex;
            # simulate that by ensuring the file exists before _call_openai reads it.
            result = _call_openai("hello", model="gpt-5.6-luna")
        assert result == '{"quality_level": 5}'
        assert mock_run.call_count == 1

    def test_sandbox_exec_argv_shape(self, tmp_path):
        out_file = tmp_path / "out.json"
        out_file.write_text("ok")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _fake_completed_process(returncode=0)

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run", side_effect=fake_run):
            _call_openai("hello", model="gpt-5.6-luna")

        cmd = captured["cmd"]
        assert cmd[0] == "sandbox-exec"
        assert cmd[1] == "-f"
        assert "codex" in cmd
        assert "exec" in cmd
        # Every disabled feature must be present as --disable <name>.
        # Exact count, not a loose lower bound: this list is the
        # security-critical output of `codex features list` ground-truth
        # verification (round 4-6 of the plan review) — a future edit that
        # silently drops one feature must fail this test, not slide by.
        from evolution.judge.openai_judge import _DISABLE_FEATURES
        assert cmd.count("--disable") == len(_DISABLE_FEATURES) == 20
        assert "shell_tool" in cmd
        assert "unified_exec" in cmd
        assert "auth_elicitation" in cmd
        assert "-m" in cmd and "gpt-5.6-luna" in cmd
        assert "--ignore-user-config" in cmd
        assert "--ignore-rules" in cmd
        assert "--sandbox" in cmd and "read-only" in cmd

    def test_env_is_minimal_allowlist(self, tmp_path):
        out_file = tmp_path / "out.json"
        out_file.write_text("ok")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _fake_completed_process(returncode=0)

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run", side_effect=fake_run), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "leak-me", "OPENAI_API_KEY": "also-leak-me"}):
            _call_openai("hello", model="gpt-5.6-luna")

        assert "GEMINI_API_KEY" not in captured["env"]
        assert "OPENAI_API_KEY" not in captured["env"]
        assert set(captured["env"].keys()) <= {"PATH", "HOME"}

    def test_codex_not_found_raises_clear_error(self):
        with patch("evolution.judge.openai_judge.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found on PATH"):
                _call_openai("hello", model="gpt-5.6-luna")

    def test_nonzero_exit_fatal_marker_raises_immediately(self, tmp_path):
        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=1, stderr="401 unauthorized")):
            with pytest.raises(RuntimeError, match="401"):
                _call_openai("hello", model="gpt-5.6-luna")

    def _fresh_subdir_mkdtemp(self, tmp_path, out_content=None):
        """Real `mkdtemp()` creates a genuinely fresh directory each call;
        the retry-exercising tests below need the same, since production
        code's `finally` block removes `iso_dir` after every attempt
        (including a retried one) — a fixed return_value would have the
        second attempt try to write into a directory the first attempt's
        cleanup already deleted."""
        counter = {"n": 0}

        def _mkdtemp(dir=None):
            counter["n"] += 1
            sub = tmp_path / f"attempt-{counter['n']}"
            sub.mkdir()
            if out_content is not None:
                (sub / "out.json").write_text(out_content)
            return str(sub)
        return _mkdtemp

    def test_nonzero_exit_retryable_marker_retries_then_succeeds(self, tmp_path):
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _fake_completed_process(returncode=1, stderr="429 rate limit exceeded")
            return _fake_completed_process(returncode=0)

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp",
                   side_effect=self._fresh_subdir_mkdtemp(tmp_path, out_content="ok")), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run", side_effect=fake_run):
            result = _call_openai("hello", model="gpt-5.6-luna")
        assert result == "ok"
        assert calls["n"] == 2

    def test_timeout_raises_clear_error(self, tmp_path):
        with patch("evolution.judge.openai_judge.tempfile.mkdtemp",
                   side_effect=self._fresh_subdir_mkdtemp(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=300)):
            with pytest.raises(RuntimeError, match="timed out"):
                _call_openai("hello", model="gpt-5.6-luna")

    def test_empty_output_raises_clear_error(self, tmp_path):
        with patch("evolution.judge.openai_judge.tempfile.mkdtemp",
                   side_effect=self._fresh_subdir_mkdtemp(tmp_path, out_content="")), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)):
            with pytest.raises(RuntimeError, match="EMPTY final message"):
                _call_openai("hello", model="gpt-5.6-luna")

    def test_cleans_up_iso_dir_on_success(self, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        out_file = work_dir / "out.json"
        out_file.write_text("ok")

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(work_dir)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)):
            _call_openai("hello", model="gpt-5.6-luna")
        assert not work_dir.exists()

    def test_cleans_up_iso_dir_on_failure(self, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(work_dir)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=1, stderr="401 unauthorized")):
            with pytest.raises(RuntimeError):
                _call_openai("hello", model="gpt-5.6-luna")
        assert not work_dir.exists()

    def test_writes_raw_schema_not_wrapped_envelope(self, tmp_path):
        """Regression test: --output-schema expects a raw JSON Schema, not an
        OpenAI response_format envelope -- wrapping it was a confirmed live
        bug ('json_schema is not valid under any of the given schemas')."""
        out_file = tmp_path / "out.json"
        out_file.write_text("ok")
        written_schema = {}

        real_open = open

        def spy_open(path, *args, **kwargs):
            f = real_open(path, *args, **kwargs)
            if str(path).endswith("schema.json"):
                orig_write = f.write
                def spy_write(s):
                    written_schema["text"] = written_schema.get("text", "") + s
                    return orig_write(s)
                f.write = spy_write
            return f

        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)), \
             patch("builtins.open", side_effect=spy_open):
            _call_openai("hello", model="gpt-5.6-luna")

        schema = json.loads(written_schema["text"])
        assert schema["type"] == "object"
        assert "json_schema" not in schema
        assert "response_format" not in schema


# ── _parse_result ────────────────────────────────────────────────────────────


class TestParseResult:
    def test_well_formed_json_returns_judge_result(self):
        raw = json.dumps({
            "safe": True,
            "quality_level": 4,
            "recalled_preference": True,
            "format_matched": False,
            "tone_matched": True,
            "execution_quality": 5,
            "rationale": "Looks good",
        })
        result = _parse_result(raw)
        assert isinstance(result, JudgeResult)
        assert not result.is_parse_error
        assert 0.0 <= result.score <= 1.0
        assert "Looks good" in (result.rationale or "")

    def test_strips_markdown_fences(self):
        raw = (
            '```json\n{"safe": true, "quality_level": 3, "recalled_preference": false, '
            '"format_matched": false, "tone_matched": false, "execution_quality": 3, '
            '"rationale": "ok"}\n```'
        )
        result = _parse_result(raw)
        assert not result.is_parse_error

    def test_invalid_json_returns_neutral_fallback(self):
        result = _parse_result("not json at all")
        assert result.is_parse_error
        assert result.score == 0.5
        assert "Parse error" in (result.rationale or "")


# ── OpenAIRuntimeJudge ───────────────────────────────────────────────────────


class TestOpenAIRuntimeJudge:
    def test_evaluate_round_trip(self, tmp_path):
        canned = json.dumps({
            "safe": True, "quality_level": 5, "recalled_preference": True,
            "format_matched": True, "tone_matched": True, "execution_quality": 5,
            "rationale": "Clear and correct",
        })
        out_file = tmp_path / "out.json"
        out_file.write_text(canned)
        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)):
            judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
            result = judge.evaluate(prompt="What's 2+2?", response="4", tools_used=["calculator"])
        assert isinstance(result, JudgeResult)
        assert not result.is_parse_error

    def test_init_skips_preflight_check(self):
        judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
        assert judge.model == "gpt-5.6-luna"

    def test_a_evaluate_runs_in_executor(self, tmp_path):
        canned = json.dumps({
            "safe": True, "quality_level": 3, "recalled_preference": False,
            "format_matched": False, "tone_matched": False, "execution_quality": 3,
            "rationale": "ok",
        })
        out_file = tmp_path / "out.json"
        out_file.write_text(canned)
        with patch("evolution.judge.openai_judge.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge._darwin_per_user_root", return_value=str(tmp_path)), \
             patch("evolution.judge.openai_judge.subprocess.run",
                   return_value=_fake_completed_process(returncode=0)):
            judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
            result = asyncio.run(judge.a_evaluate(prompt="hi", response="hi"))
        assert not result.is_parse_error


# ── _cap_context_and_profile ─────────────────────────────────────────────────


class TestCapContextAndProfile:
    def test_none_values_pass_through(self):
        assert _cap_context_and_profile(None, None) == (None, None)

    def test_context_truncated_to_judge_max_prompt_chars(self):
        from evolution.config import JUDGE_MAX_PROMPT_CHARS
        huge = "x" * (JUDGE_MAX_PROMPT_CHARS + 500)
        capped, _ = _cap_context_and_profile(huge, None)
        assert len(capped) == JUDGE_MAX_PROMPT_CHARS

    def test_user_profile_truncated_to_judge_max_persona_chars(self):
        from evolution.config import JUDGE_MAX_PERSONA_CHARS
        huge = "y" * (JUDGE_MAX_PERSONA_CHARS + 500)
        _, capped = _cap_context_and_profile(None, huge)
        assert len(capped) == JUDGE_MAX_PERSONA_CHARS

    def test_short_values_unaffected(self):
        assert _cap_context_and_profile("short ctx", "short profile") == ("short ctx", "short profile")
