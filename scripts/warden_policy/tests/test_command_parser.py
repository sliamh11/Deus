import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.command_parser import classify

SUPPORTED = 'git -c core.hooksPath=/tmp/empty commit --no-verify -m "reviewed change"'


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
        c = classify(SUPPORTED)
        self.assertTrue(c.is_commit_shaped)
        self.assertTrue(c.supported)

    def test_amend_supported(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --amend --no-edit')
        self.assertTrue(c.supported)

    def test_dash_c_path_form_supported(self):
        c = classify('git -C /path/to/repo -c core.hooksPath=/tmp/empty commit --no-verify -m x')
        self.assertTrue(c.supported)

    def test_message_equals_form(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --message=hello')
        self.assertTrue(c.supported)

    def test_quoted_message_with_shell_metacharacters_is_just_a_message(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m "fix: a > b && c"')
        self.assertTrue(c.supported)

    def test_bare_double_dash_separator_is_a_safe_noop(self):
        # found live: a real Hermes agent produced this form unprompted -- it's a standard
        # git idiom ("end of options"), always safe since it consumes/mutates nothing.
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x --')
        self.assertTrue(c.supported)

    def test_pathspec_after_double_dash_still_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x -- file.txt')
        self.assertFalse(c.supported)

    def test_signoff_allow_empty_quiet_verbose(self):
        c = classify(
            'git -c core.hooksPath=/tmp/empty commit --no-verify --signoff '
            '--allow-empty --quiet --verbose -m x'
        )
        self.assertTrue(c.supported)


class TestBlockedForms(unittest.TestCase):
    def test_missing_no_verify(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit -m x')
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
        c = classify('git commit --no-verify -c core.hooksPath=/tmp/empty -m x')
        self.assertFalse(c.supported)

    def test_dash_a_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -a -m x')
        self.assertFalse(c.supported)

    def test_all_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --all -m x')
        self.assertFalse(c.supported)

    def test_combined_am_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -am x')
        self.assertFalse(c.supported)

    def test_include_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --include -m x')
        self.assertFalse(c.supported)

    def test_only_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --only -m x')
        self.assertFalse(c.supported)

    def test_interactive_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --interactive')
        self.assertFalse(c.supported)

    def test_patch_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --patch')
        self.assertFalse(c.supported)

    def test_pathspec_from_file_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify --pathspec-from-file=x')
        self.assertFalse(c.supported)

    def test_bare_pathspec_operand_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x -- file.txt')
        self.assertFalse(c.supported)

    def test_compound_and_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x && rm -rf /')
        self.assertFalse(c.supported)

    def test_compound_semicolon_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x; echo done')
        self.assertFalse(c.supported)

    def test_pipe_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/empty commit --no-verify -m x | cat')
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
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m "safe `whoami`"')
        self.assertTrue(c.is_commit_shaped)
        self.assertFalse(c.supported)

    def test_dollar_paren_in_message_space_form_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m "safe $(touch /tmp/x)"')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_author_space_form_blocked(self):
        c = classify(
            'git -c core.hooksPath=/tmp/e commit --no-verify --author "$(touch /tmp/x) <a@b.c>" -m x'
        )
        self.assertFalse(c.supported)

    def test_dollar_paren_in_message_equals_form_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify --message=$(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_author_equals_form_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify --author=$(touch /tmp/x) -m x')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_dash_capital_c_value_blocked(self):
        # GPT-5.6-sol's specific finding: -C's value is consumed with zero content validation.
        c = classify('git -C "$(touch /tmp/x)" -c core.hooksPath=/tmp/e commit --no-verify -m safe')
        self.assertFalse(c.supported)

    def test_process_substitution_input_form_blocked(self):
        # Code-review's finding: <(...) is the same vulnerability class as $(...) -- bash forks
        # and runs the enclosed command to set up the substitution as a side effect of
        # word-splitting alone, regardless of whether the resulting path is ever read.
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m <(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_process_substitution_output_form_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m >(touch /tmp/x)')
        self.assertFalse(c.supported)

    def test_dollar_paren_in_non_hookspath_dash_c_value_blocked(self):
        c = classify(
            'git -c core.hooksPath=/tmp/e -c user.name="$(touch /tmp/x)" commit --no-verify -m safe'
        )
        self.assertFalse(c.supported)

    def test_line_continuation_split_marker_quoted_form_blocked(self):
        # Round 4: shlex.split() preserves a literal backslash-newline inside a quoted value
        # intact -- a per-token check could catch this form alone, but the actual fix
        # normalizes the raw string, which also covers it.
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m "safe $\\\n(touch /tmp/x)"')
        self.assertFalse(c.supported)

    def test_line_continuation_split_marker_unquoted_form_blocked(self):
        # Round 5: the case that broke the per-token normalization approach -- shlex's own
        # escape handling consumes the backslash for an UNQUOTED value, leaving only a bare
        # newline by the time a token exists, which a post-tokenization regex can't recover.
        # The fix normalizes the raw command string before shlex.split() ever runs, so this
        # form is caught the same way as the quoted one.
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m $\\\n(touch>/tmp/x)')
        self.assertFalse(c.supported)

    def test_literal_dollar_sign_alone_not_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m "fix: cost is \\$5"')
        self.assertTrue(c.supported)

    def test_literal_braces_not_blocked(self):
        c = classify('git -c core.hooksPath=/tmp/e commit --no-verify -m "fix: use {x} syntax"')
        self.assertTrue(c.supported)

    def test_unrelated_non_git_command_with_marker_and_commit_substring_not_commit_shaped(self):
        # Round 3's finding: the check must never fire before commit-shape is confirmed, or an
        # entirely unrelated command gets misclassified just for mentioning "commit" and a
        # marker in unrelated positions (e.g. a filename).
        c = classify("rg -F '$(' docs/commit-notes.md")
        self.assertFalse(c.is_commit_shaped)


if __name__ == "__main__":
    unittest.main()
