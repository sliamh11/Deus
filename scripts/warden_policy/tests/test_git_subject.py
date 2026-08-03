import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.git_subject import GitSubjectError, resolve_repo_id, resolve_subject_key


def _run(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path):
    _run("init", "-q", cwd=path)
    _run("config", "user.email", "test@example.com", cwd=path)
    _run("config", "user.name", "Test", cwd=path)


class TestRepoId(unittest.TestCase):
    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            a = resolve_repo_id(Path(d))
            b = resolve_repo_id(Path(d))
            self.assertEqual(a, b)
            self.assertTrue(a.startswith("git-common-dir-sha256:"))

    def test_two_unrelated_repos_differ(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            _init_repo(d1)
            _init_repo(d2)
            self.assertNotEqual(resolve_repo_id(Path(d1)), resolve_repo_id(Path(d2)))

    def test_linked_worktree_shares_repo_id(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            _run("commit", "-q", "-m", "init", cwd=d)
            with tempfile.TemporaryDirectory() as wt_parent:
                wt = Path(wt_parent) / "wt"
                _run("worktree", "add", str(wt), "-b", "sidebranch", cwd=d)
                self.assertEqual(resolve_repo_id(Path(d)), resolve_repo_id(wt))

    def test_not_a_repo_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(GitSubjectError):
                resolve_repo_id(Path(d))


class TestSubjectKey(unittest.TestCase):
    def test_staged_change_changes_subject(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            _run("commit", "-q", "-m", "init", cwd=d)
            before = resolve_subject_key(Path(d))
            (Path(d) / "f.txt").write_text("hello world\n")
            _run("add", "f.txt", cwd=d)
            after = resolve_subject_key(Path(d))
            self.assertNotEqual(before, after)

    def test_unstaged_change_does_not_change_subject(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            _run("commit", "-q", "-m", "init", cwd=d)
            before = resolve_subject_key(Path(d))
            (Path(d) / "f.txt").write_text("hello, unstaged edit\n")  # NOT git add'ed
            after = resolve_subject_key(Path(d))
            self.assertEqual(before, after)

    def test_untracked_file_does_not_change_subject(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            _run("commit", "-q", "-m", "init", cwd=d)
            before = resolve_subject_key(Path(d))
            (Path(d) / "untracked.txt").write_text("new file, never staged\n")
            after = resolve_subject_key(Path(d))
            self.assertEqual(before, after)

    def test_matches_actual_commit_tree(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            subject = resolve_subject_key(Path(d))
            _run("commit", "-q", "-m", "init", cwd=d)
            actual_tree = subprocess.run(
                ["git", "-C", d, "rev-parse", "HEAD^{tree}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(subject, f"git-tree:sha1:{actual_tree}")

    def test_key_format(self):
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            (Path(d) / "f.txt").write_text("hello\n")
            _run("add", "f.txt", cwd=d)
            key = resolve_subject_key(Path(d))
            self.assertRegex(key, r"^git-tree:(sha1|sha256):[0-9a-f]+$")


if __name__ == "__main__":
    unittest.main()
