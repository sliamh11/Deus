import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.command_parser import classify

# A single genuinely-empty, uniquely-allocated directory backs every existing test's
# `-c core.hooksPath=...` value. Round-3 of adversarial plan review added real filesystem
# validation of the hooksPath VALUE (not just the key) -- see command_parser.py's
# _is_valid_empty_hooks_dir -- so tests can no longer point at an arbitrary placeholder path
# that may not exist. A `tempfile.TemporaryDirectory()` (not a fixed literal like `/tmp/empty`,
# even a namespaced one) avoids clobbering any pre-existing unrelated content at a guessed path
# and is safe under concurrent test runs (GPT-backend plan review finding, round 5).
_HOOKS_DIR = None


def setUpModule():
    global _HOOKS_DIR
    _HOOKS_DIR = tempfile.TemporaryDirectory()


def tearDownModule():
    _HOOKS_DIR.cleanup()


def _hooks():
    return _HOOKS_DIR.name


SUPPORTED = lambda: f'git -c core.hooksPath={_hooks()} commit --no-verify -m "reviewed change"'


class TestNotCommitShaped(unittest.TestCase):
    def test_non_git_command(self):
        c = classify("echo hello")
        self.assertFalse(c.is_commit_shaped)

    def test_git_status(self):
        c = classify("git status")
        self.assertFalse(c.is_commit_shaped)

    def test_git_push(self):
        c = classify("git push origin main")
        self.assertFalse(c.is_commit_shaped)

    def test_git_commit_tree_is_a_different_subcommand(self):
        # "commit-tree" contains "commit" as a substring but is a distinct subcommand;
        # tokenized parsing must not mistake it for `commit`.
        c = classify("git commit-tree HEAD^{tree}")
        self.assertFalse(c.is_commit_shaped)

    def test_unrelated_command_mentioning_commit_in_a_string(self):
        c = classify("echo 'please commit your changes'")
        self.assertFalse(c.is_commit_shaped)


class TestSupportedForms(unittest.TestCase):
    def test_plain_supported_form(self):
        c = classify(SUPPORTED())
        self.assertTrue(c.is_commit_shaped)
        self.assertTrue(c.supported)

    def test_amend_supported(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --amend --no-edit')
        self.assertTrue(c.supported)

    def test_dash_c_path_form_supported(self):
        c = classify(f'git -C /path/to/repo -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertTrue(c.supported)
        # LIA-524 Design section D (round 13-18): the corrected fix resolves identity FROM a
        # safe -C target rather than rejecting -C outright -- this original assertion stays
        # correct and must NOT be flipped. Added assertions only.
        self.assertEqual(c.dash_c_target, "/path/to/repo")  # @oracle LIA-524: a safe -C value is captured verbatim on Classification
        self.assertFalse(c.dash_c_rejected)  # @oracle LIA-524: a SAFE -C is never flagged rejected

    def test_message_equals_form(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --message=hello')
        self.assertTrue(c.supported)

    def test_quoted_message_with_shell_metacharacters_is_just_a_message(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "fix: a > b && c"')
        self.assertTrue(c.supported)

    def test_bare_double_dash_separator_is_a_safe_noop(self):
        # found live: a real Hermes agent produced this form unprompted -- it's a standard
        # git idiom ("end of options"), always safe since it consumes/mutates nothing.
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x --')
        self.assertTrue(c.supported)

    def test_pathspec_after_double_dash_still_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x -- file.txt')
        self.assertFalse(c.supported)

    def test_signoff_allow_empty_quiet_verbose(self):
        c = classify(
            f'git -c core.hooksPath={_hooks()} commit --no-verify --signoff '
            '--allow-empty --quiet --verbose -m x'
        )
        self.assertTrue(c.supported)


class TestBlockedForms(unittest.TestCase):
    def test_missing_no_verify(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)
        self.assertIn("--no-verify", c.reason)

    def test_missing_hooks_path_override(self):
        c = classify('git commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)
        self.assertIn("core.hooksPath", c.reason)

    def test_hooks_path_after_commit_is_wrong_global_option_position(self):
        # -c must come BEFORE `commit` (git global option syntax); after `commit` it's
        # git-commit's own -c/--reedit-message shorthand, an entirely different, unsupported flag.
        c = classify(f'git commit --no-verify -c core.hooksPath={_hooks()} -m x')
        self.assertFalse(c.supported)

    def test_dash_a_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -a -m x')
        self.assertFalse(c.supported)

    def test_all_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --all -m x')
        self.assertFalse(c.supported)

    def test_combined_am_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -am x')
        self.assertFalse(c.supported)

    def test_include_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --include -m x')
        self.assertFalse(c.supported)

    def test_only_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --only -m x')
        self.assertFalse(c.supported)

    def test_interactive_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --interactive')
        self.assertFalse(c.supported)

    def test_patch_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --patch')
        self.assertFalse(c.supported)

    def test_pathspec_from_file_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --pathspec-from-file=x')
        self.assertFalse(c.supported)

    def test_bare_pathspec_operand_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x -- file.txt')
        self.assertFalse(c.supported)

    def test_compound_and_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x && rm -rf /')
        self.assertFalse(c.supported)

    def test_compound_semicolon_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x; echo done')
        self.assertFalse(c.supported)

    def test_pipe_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x | cat')
        self.assertFalse(c.supported)

    def test_unbalanced_quotes_mentioning_commit_blocked(self):
        c = classify('git commit -m "unterminated')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)


class TestCommandSubstitutionInjection(unittest.TestCase):
    """Regression tests for a real, live-verified vulnerability found during deep testing of
    the shipped v1: a "supported" commit whose message/author/global-option values contain a
    shell command-substitution marker executes arbitrary code as a side effect when Hermes's
    terminal tool runs it through a real persistent shell -- independent of git or the
    tree-attestation logic entirely. See the module docstring's "Defense-in-depth" paragraph.
    """

    def test_backtick_in_message_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "safe `whoami`"')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_dollar_paren_in_message_space_form_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "safe $(touch /tmp/x)"')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_author_space_form_blocked(self):
        c = classify(
            f'git -c core.hooksPath={_hooks()} commit --no-verify --author "$(touch /tmp/x) <a@b.c>" -m x'
        )
        self.assertFalse(c.supported)

    def test_dollar_paren_in_message_equals_form_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --message=$(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_author_equals_form_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify --author=$(touch /tmp/x) -m x')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_dash_capital_c_value_blocked(self):
        # GPT-5.6-sol's specific finding: -C's value is consumed with zero content validation.
        c = classify(f'git -C "$(touch /tmp/x)" -c core.hooksPath={_hooks()} commit --no-verify -m safe')
        self.assertFalse(c.supported)

    def test_process_substitution_input_form_blocked(self):
        # Code-review's finding: <(...) is the same vulnerability class as $(...) -- bash forks
        # and runs the enclosed command to set up the substitution as a side effect of
        # word-splitting alone, regardless of whether the resulting path is ever read.
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m <(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_process_substitution_output_form_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m >(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_non_hookspath_dash_c_value_blocked(self):
        c = classify(
            f'git -c core.hooksPath={_hooks()} -c user.name="$(touch /tmp/x)" commit --no-verify -m safe'
        )
        self.assertFalse(c.supported)

    def test_line_continuation_split_marker_quoted_form_blocked(self):
        # Round 4: shlex.split() preserves a literal backslash-newline inside a quoted value
        # intact -- a per-token check could catch this form alone, but the actual fix
        # normalizes the raw string, which also covers it.
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "safe $\\\n(touch /tmp/x)"')
        self.assertFalse(c.supported)

    def test_line_continuation_split_marker_unquoted_form_blocked(self):
        # Round 5: the case that broke the per-token normalization approach -- shlex's own
        # escape handling consumes the backslash for an UNQUOTED value, leaving only a bare
        # newline by the time a token exists, which a post-tokenization regex can't recover.
        # The fix normalizes the raw command string before shlex.split() ever runs, so this
        # form is caught the same way as the quoted one.
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m $\\\n(touch>/tmp/x)')
        self.assertFalse(c.supported)

    def test_literal_dollar_sign_alone_not_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "fix: cost is \\$5"')
        self.assertTrue(c.supported)

    def test_literal_braces_not_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m "fix: use {{x}} syntax"')
        self.assertTrue(c.supported)

    def test_unrelated_non_git_command_with_marker_and_commit_substring_not_commit_shaped(self):
        # Round 3's finding: the check must never fire before commit-shape is confirmed, or an
        # entirely unrelated command gets misclassified just for mentioning "commit" and a
        # marker in unrelated positions (e.g. a filename).
        c = classify("rg -F '$(' docs/commit-notes.md")
        self.assertFalse(c.is_commit_shaped)


class TestDashCKeyAllowlist(unittest.TestCase):
    """Regression tests for a real, live-verified vulnerability found during continued deep
    testing: the classifier only checked WHETHER one of possibly several `-c` flags equaled
    `core.hooksPath` -- it never rejected other `-c` keys, and never rejected a duplicate
    `-c core.hooksPath=<different-value>`. `-c core.fsmonitor=<script>` is a known git-config
    code-execution vector, live-verified via a real Hermes agent's real terminal tool against
    the deployed (pre-fix) daemon: the configured script executed as a side effect of an
    otherwise "supported" commit. A duplicate `core.hooksPath` with a different value is a
    second bypass -- git's own config precedence takes the LAST value for a single-valued key,
    so a second override could silently redirect back to a real (dangerous) hooks directory.
    """

    def test_non_hookspath_c_key_alone_blocked(self):
        c = classify(f'git -c core.fsmonitor={_hooks()}/evil.sh commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_dangerous_key_after_hookspath_blocked(self):
        c = classify(
            f'git -c core.hooksPath={_hooks()} -c core.fsmonitor=/tmp/evil.sh commit --no-verify -m x'
        )
        self.assertFalse(c.supported)

    def test_dangerous_key_before_hookspath_blocked(self):
        c = classify(
            f'git -c core.fsmonitor=/tmp/evil.sh -c core.hooksPath={_hooks()} commit --no-verify -m x'
        )
        self.assertFalse(c.supported)

    def test_duplicate_hookspath_different_value_blocked(self):
        # The bypass the round-1 fix (key-only allowlist) missed: both `-c` occurrences have the
        # RIGHT key, so a check that only asks "was core.hooksPath seen at least once" would pass
        # this -- but git resolves the LAST value for a single-valued key, so the second,
        # non-empty value would actually govern the real commit.
        c = classify(
            f'git -c core.hooksPath={_hooks()} -c core.hooksPath=/tmp/real-hooks commit --no-verify -m x'
        )
        self.assertFalse(c.supported)

    def test_second_distinct_dangerous_key_blocked(self):
        # Confirms this is allowlist-by-key, not a `core.fsmonitor`-only blocklist entry.
        c = classify(f'git -c credential.helper=/tmp/evil.sh commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_single_hookspath_still_supported(self):
        # Regression: the original, single-`-c` supported form must still work.
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertTrue(c.supported)


class TestHooksPathValueValidation(unittest.TestCase):
    """Regression tests for a second, more severe pre-existing gap found by adversarial
    (GPT-backend) plan review during the same round: the classifier never validated that the
    `-c core.hooksPath=<value>` VALUE actually points to a real, empty, trusted directory --
    only that the KEY was right. `--no-verify` does NOT suppress `prepare-commit-msg`
    (live-verified directly against real git), so an attacker- or agent-supplied
    non-empty/attacker-controlled hooksPath directory can still execute a hook as a side effect
    of an otherwise "supported" commit -- defeating the entire reason this override exists.
    """

    def test_nonexistent_hookspath_blocked(self):
        c = classify(f'git -c core.hooksPath={_hooks()}/does-not-exist commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_relative_hookspath_blocked(self):
        c = classify('git -c core.hooksPath=relative/dir commit --no-verify -m x')
        self.assertFalse(c.supported)

    def test_hookspath_pointing_to_a_file_blocked(self):
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(dir=_hooks()) as tmp_file:
            c = classify(f'git -c core.hooksPath={tmp_file.name} commit --no-verify -m x')
            self.assertFalse(c.supported)

    def test_hookspath_pointing_to_nonempty_dir_blocked(self):
        # The exact attack case: a directory that exists and IS a directory, but contains a
        # real (or attacker-supplied) file -- e.g. a malicious prepare-commit-msg hook.
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as nonempty_dir:
            (Path(nonempty_dir) / "prepare-commit-msg").write_text("#!/bin/sh\necho pwned\n")
            c = classify(f'git -c core.hooksPath={nonempty_dir} commit --no-verify -m x')
            self.assertFalse(c.supported)

    def test_real_empty_hookspath_still_supported(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertTrue(c.supported)

    def test_bare_hookspath_no_equals_blocked(self):
        # `-c core.hooksPath` with no `=value` at all -- hooks_path_value ends up "" (not None),
        # which _is_valid_empty_hooks_dir correctly rejects: the character-allowlist
        # `fullmatch` requires at least one character (the regex uses `+`), so an empty
        # string is rejected there, before is_absolute() is ever reached.
        c = classify('git -c core.hooksPath commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)


class TestHooksPathShellExpansionDivergence(unittest.TestCase):
    """Regression tests for a real, live-verified vulnerability found by adversarial GPT
    code-review while reviewing the round-3 hooksPath-value-emptiness fix above: `classify()`
    validates the LITERAL string from `shlex.split()`, but Hermes's terminal tool later runs
    the SAME raw command through a real shell, which performs parameter expansion (`$USER`,
    `${VAR}`) and globbing (`*`) that `shlex` does not. An attacker can create a literal, empty
    decoy directory matching an unexpanded string, while the real shell expands it to a
    completely different, attacker-prepopulated path -- confirmed live via direct bash
    reproduction (both bare and braced parameter-expansion forms, and globbing).
    """

    def test_bare_dollar_variable_blocked(self):
        # Live-verified: `bash -c 'git -c core.hooksPath=/tmp/$USER commit ...'` expands to a
        # real, different directory than the literal "/tmp/$USER" path this would validate.
        c = classify(f'git -c core.hooksPath={_hooks()}/$USER commit --no-verify -m x')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_braced_dollar_variable_blocked(self):
        # Live-verified this round: braced (`${VAR}`) and bare (`$VAR`) parameter expansion
        # behave identically in this argument position -- same confidence as the bare form.
        c = classify(f'git -c core.hooksPath={_hooks()}/${{HOME}} commit --no-verify -m x')
        self.assertFalse(c.supported)

    def test_glob_star_blocked(self):
        # Live-verified: globbing expands when a matching filesystem entry exists.
        c = classify(f'git -c core.hooksPath={_hooks()}/* commit --no-verify -m x')
        self.assertFalse(c.supported)

    def test_tilde_blocked_as_allowlist_conservatism(self):
        # NOT independently demonstrated to expand in this argument position (confirmed via
        # direct bash reproduction: `core.hooksPath=~/dir` is NOT tilde-expanded, since tilde
        # expansion only applies at the start of a word or in a genuine shell-variable
        # assignment). Blocked anyway per this module's "new/obscure -- default to BLOCKED"
        # allowlist philosophy, not because a live bypass was shown.
        c = classify('git -c core.hooksPath=~/somedir commit --no-verify -m x')
        self.assertFalse(c.supported)

    def test_trailing_newline_blocked(self):
        # Regression test for the fullmatch-vs-match anchor fix specifically: Python's `$`
        # anchor allows a single trailing newline, `fullmatch` correctly rejects it. The value
        # must be QUOTED for shlex to preserve the embedded newline as part of one token
        # (an unquoted bare newline would just be whitespace, splitting into separate tokens).
        c = classify(f'git -c core.hooksPath="{_hooks()}\n" commit --no-verify -m x')
        self.assertFalse(c.supported)

    def test_safe_characters_only_still_supported(self):
        c = classify(f'git -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertTrue(c.supported)


class TestDashCTargetAndRejectedFields(unittest.TestCase):
    """Independent oracle for LIA-524 Design section D (`Classification.dash_c_target` /
    `.dash_c_rejected`) -- authored from the plan's own final, round-18 design, blind to any
    implementation: `command_parser.py` has neither field yet (confirmed by reading the file --
    `Classification` currently declares only `is_commit_shaped`, `supported`, `reason`).

    The round-18 design requires an ORDER-INDEPENDENT PRE-SCAN over the pre-`commit` token
    prefix (not a check placed at any point WITHIN the existing sequential parsing loop --
    three prior placements, rounds 15/16/17, were each tried and found bypassable by
    attacker-controlled token order). `test_dash_c_target_survives_malformed_dash_c_appearing_
    first_order_independence` below is the single test that discriminates a genuine
    order-independent pre-scan from any of those three prior (broken) placements -- the most
    important test in this whole ticket, per the dispatch brief.
    """

    def test_absent_dash_c_leaves_target_none_and_rejected_false(self):
        c = classify(SUPPORTED())
        self.assertIsNone(c.dash_c_target)  # @oracle LIA-524: no -C at all -> dash_c_target stays None
        self.assertFalse(c.dash_c_rejected)  # @oracle LIA-524: no -C at all -> dash_c_rejected stays False

    def test_unsafe_dash_c_value_rejected(self):
        c = classify(f'git -C "$(touch /tmp/x)" -c core.hooksPath={_hooks()} commit --no-verify -m safe')
        self.assertTrue(c.dash_c_rejected)  # @oracle LIA-524: command-substitution in the -C value is rejected
        self.assertIsNone(c.dash_c_target)  # @oracle LIA-524: a rejected -C never leaves a usable target
        self.assertFalse(c.supported)

    def test_dash_c_safety_check_deferred_until_commit_confirmed(self):
        # Round-14 Claude's false-block regression guard: a genuinely non-commit command merely
        # containing a -C flag (and the substring "commit" elsewhere) must never be misclassified
        # as commit-shaped just because the -C safety check ran too early.
        c = classify("git -C /tmp log --grep=commit")
        self.assertFalse(c.is_commit_shaped)  # @oracle LIA-524: -C safety check deferred until `commit` is confirmed as the subcommand

    def test_dash_c_no_value_at_end_of_prefix_rejected(self):
        # Round-17 addition: -C as the LAST token of the pre-`commit` prefix, with no value
        # following it at all within that prefix -- mirrors the existing -c-with-no-value
        # handling.
        c = classify(f'git -c core.hooksPath={_hooks()} -C commit --no-verify -m x')
        self.assertTrue(c.dash_c_rejected)  # @oracle LIA-524: -C with no value in the prefix must be rejected
        self.assertIsNone(c.dash_c_target)
        self.assertFalse(c.supported)

    def test_safe_dash_c_survives_loop2_unrelated_defect(self):
        # Round-16 addition: dash_c_target must survive a _blocked() return that fires from the
        # SECOND parsing loop (missing --no-verify) -- not just the final success/hooksPath-value
        # return. Proves the safety-check point is not deferred all the way to the end.
        c = classify(f'git -C /path/to/repo -c core.hooksPath={_hooks()} commit -m x')  # missing --no-verify
        self.assertEqual(c.dash_c_target, "/path/to/repo")  # @oracle LIA-524: round-16 -- safe -C survives an unrelated loop-2 (missing --no-verify) block
        self.assertFalse(c.supported)
        self.assertFalse(c.dash_c_rejected)

    def test_safe_dash_c_survives_loop1_internal_defect(self):
        # Round-17 addition: dash_c_target must survive a _blocked() return that fires from
        # WITHIN the FIRST parsing loop (a malformed -c), before that loop's own break -- not
        # just loop 2's returns.
        c = classify(f'git -C /path/to/repo -c core.fsmonitor=x commit --no-verify -m x')
        self.assertEqual(c.dash_c_target, "/path/to/repo")  # @oracle LIA-524: round-17 -- safe -C survives a loop-1-INTERNAL malformed -c block
        self.assertFalse(c.supported)
        self.assertFalse(c.dash_c_rejected)

    def test_dash_c_target_survives_malformed_dash_c_appearing_first_order_independence(self):
        # THE discriminating test for the whole -C mechanism (round 18): a malformed -c
        # appearing BEFORE the safe -C in token order -- the exact reverse of the round-17 test
        # above, which is precisely why round 17's "validate inline, the instant -C is parsed"
        # fix passed its OWN test while remaining broken. command_parser's loop 1 is a single
        # left-to-right pass that returns on the FIRST _blocked()-triggering token (already
        # proven by the EXISTING, shipped test_dangerous_key_before_hookspath_blocked) -- a
        # check placed at any point WITHIN that loop, however early, never reaches -C here,
        # because the -c malformation short-circuits first. Only a dedicated, order-independent
        # PRE-SCAN over the whole pre-`commit` prefix -- run once, before loop 1 even starts --
        # can see -C regardless of what else surrounds it.
        c = classify(f'git -c core.fsmonitor=x -C /path/to/repo commit --no-verify -m x')
        # @oracle LIA-524: order-independence -- dash_c_target must be populated even though a
        # malformed -c appears BEFORE -C in token order. Falsifies every "earliest point in the
        # sequential loop" placement (rounds 15, 16, AND 17 all fail this specific test).
        self.assertEqual(c.dash_c_target, "/path/to/repo")
        self.assertFalse(c.supported)  # blocked on the -c malformation, not on -C
        self.assertFalse(c.dash_c_rejected)  # dash_c_rejected means an UNSAFE -C value; this -C is safe

    def test_second_dash_c_rejected(self):
        # Round-18 addition: mirrors the file's EXISTING "reject a second -c" precedent
        # (test_duplicate_hookspath_different_value_blocked) for -C.
        c = classify(f'git -C /path/a -C /path/b -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertTrue(c.dash_c_rejected)  # @oracle LIA-524: a second -C is rejected outright
        self.assertIsNone(c.dash_c_target)
        self.assertFalse(c.supported)

    def test_dash_c_value_equal_to_literal_commit_token_fails_closed(self):
        # Round-18-review informational addition: -C's own value literally equal to the token
        # "commit" -- confirmed by fresh review to fail closed in every construction tried (the
        # `tokens.index("commit", 1)` prefix-boundary computation always ends up truncating the
        # pre-scan's prefix such that the triggering -C is left value-less or flagged as a
        # second -C, both already-handled rejection paths). Only the robust invariant
        # (never resolves to `.supported`) is asserted -- which specific rejection path fires is
        # explicitly construction-dependent per the plan, not a fixed contract.
        c = classify(f'git -C commit -c core.hooksPath={_hooks()} commit --no-verify -m x')
        self.assertFalse(c.supported)  # @oracle LIA-524: -C value literally "commit" must fail closed, never silently mis-resolve


if __name__ == "__main__":
    unittest.main()
