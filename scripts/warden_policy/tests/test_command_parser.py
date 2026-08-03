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


if __name__ == "__main__":
    unittest.main()
