"""
Tests for scripts/review_benchmark.py — safety guarantees.

Verifies that the benchmark never modifies the original branch, that all
changes are isolated to a throwaway branch, and that cleanup is complete.

Uses a temporary git repo with realistic source files so we don't touch
the real codebase.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# -- Import target module -------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import review_benchmark as rb


# -- Fixtures -------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=repo, check=True,
    )


def _tree_hash(repo: Path) -> str:
    """Get the tree object hash for HEAD — content fingerprint of the branch."""
    return _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def _commit_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _branch_name(repo: Path) -> str:
    return _git(repo, "branch", "--show-current").stdout.strip()


def _branch_exists(repo: Path, name: str) -> bool:
    result = _git(repo, "branch", "--list", name)
    return bool(result.stdout.strip())


def _is_dirty(repo: Path) -> bool:
    result = _git(repo, "diff", "--name-only")
    return bool(result.stdout.strip())


@pytest.fixture()
def temp_repo(tmp_path):
    """Create a temporary git repo with realistic TypeScript and Python files
    that the injection patterns can target."""
    repo = tmp_path / "test-repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    # Create directory structure matching TARGET_FILES
    (repo / "src").mkdir()
    (repo / "evolution").mkdir()
    (repo / "evolution" / "reflexion").mkdir()
    (repo / "evolution" / "ilog").mkdir()
    (repo / "evolution" / "storage" / "providers").mkdir(parents=True)

    # TypeScript file with async/await, spawn, conditions — injectable
    (repo / "src" / "container-runner.ts").write_text(textwrap.dedent("""\
        import { spawn } from 'child_process';
        import path from 'path';

        export async function runAgent(input: any) {
          const groupFolder = input.group || 'default';
          const containerArgs = ['--rm', '-i'];

          const proc = spawn('docker', containerArgs);

          try {
            await proc.waitForExit();
          } catch (err) {
            console.error('Container failed:', err);
            throw err;
          }

          const isVerbose = process.env.VERBOSE === '1';
          const isError = proc.exitCode !== 0;

          if (isVerbose || isError) {
            console.log('Agent finished with code', proc.exitCode);
          }

          return proc.exitCode === 0;
        }
    """))

    # TypeScript file with path validation — injectable
    (repo / "src" / "group-folder.ts").write_text(textwrap.dedent("""\
        import path from 'path';

        const DATA_DIR = '/data/sessions';

        export function resolveGroupPath(folder: string): string {
          if (folder.includes('..')) {
            throw new Error('Path traversal detected');
          }
          const resolved = path.resolve(DATA_DIR, folder);
          return resolved;
        }

        export function resolveIpcPath(folder: string): string {
          const ipcBase = path.resolve(DATA_DIR, 'ipc');
          const ipcPath = path.resolve(ipcBase, folder);
          return ipcPath;
        }
    """))

    # TypeScript file with === comparisons — injectable
    (repo / "src" / "message-orchestrator.ts").write_text(textwrap.dedent("""\
        import { EventEmitter } from 'events';

        export function createOrchestrator() {
          async function processMessage(msg: any) {
            if (msg.type === 'text' && msg.body !== '') {
              return await handleText(msg);
            }
            return null;
          }

          async function handleText(msg: any) {
            try {
              const result = await runAgent(msg);
              return result;
            } catch (err) {
              console.error('Agent error:', err);
              return 'error';
            }
          }

          return { processMessage };
        }
    """))

    # TypeScript files for other targets
    (repo / "src" / "ipc.ts").write_text(textwrap.dedent("""\
        import fs from 'fs';

        export async function readIpcMessage(path: string) {
          const raw = fs.readFileSync(path, 'utf-8');
          try {
            return JSON.parse(raw);
          } catch {
            return null;
          }
        }

        export async function writeIpcResponse(path: string, data: any) {
          await fs.promises.writeFile(path, JSON.stringify(data));
        }
    """))

    (repo / "src" / "task-scheduler.ts").write_text(textwrap.dedent("""\
        export async function runScheduledTask(task: any) {
          if (task.enabled && task.schedule) {
            await executeTask(task);
          }
        }

        async function executeTask(task: any) {
          return task.run();
        }
    """))

    (repo / "src" / "credential-proxy.ts").write_text("export const proxy = {};\n")
    (repo / "src" / "remote-control.ts").write_text(textwrap.dedent("""\
        import fs from 'fs';

        export function writeState(path: string, state: any) {
          try {
            fs.writeFileSync(path, JSON.stringify(state));
          } catch (err) {
            console.error('Failed to write state:', err);
          }
        }
    """))
    (repo / "src" / "db.ts").write_text(textwrap.dedent("""\
        export function getRow(id: string) {
          if (id === '') return null;
          return { id };
        }
    """))
    (repo / "src" / "router.ts").write_text(textwrap.dedent("""\
        import { EventEmitter } from 'events';

        export function route(msg: any) {
          return msg.to;
        }
    """))

    # Python files
    (repo / "evolution" / "__init__.py").write_text("")
    (repo / "evolution" / "cli.py").write_text(textwrap.dedent("""\
        import json
        import sys

        BATCH_SIZE = 5

        def main():
            try:
                data = json.loads(sys.argv[1])
            except json.JSONDecodeError:
                print("Invalid JSON")
                return
            print(json.dumps({"status": "ok"}))

        if __name__ == "__main__":
            main()
    """))

    (repo / "evolution" / "reflexion" / "__init__.py").write_text("")
    (repo / "evolution" / "reflexion" / "store.py").write_text(textwrap.dedent("""\
        import logging
        from typing import Optional

        log = logging.getLogger(__name__)

        DEDUP_THRESHOLD = 0.4

        def save_reflection(content: str, category: str) -> Optional[str]:
            if not content or not content.strip():
                return None
            return "ref-123"
    """))

    (repo / "evolution" / "reflexion" / "generator.py").write_text(textwrap.dedent("""\
        import json

        def generate_reflection(prompt: str, response: str, score: float):
            return f"Lesson: {prompt[:50]}", "reasoning"
    """))

    (repo / "evolution" / "ilog" / "__init__.py").write_text("")
    (repo / "evolution" / "ilog" / "interaction_log.py").write_text(textwrap.dedent("""\
        import json
        import uuid
        from typing import Optional

        def log_interaction(prompt: str, response: Optional[str]) -> str:
            return str(uuid.uuid4())
    """))

    (repo / "evolution" / "storage" / "__init__.py").write_text("")
    (repo / "evolution" / "storage" / "providers" / "__init__.py").write_text("")
    (repo / "evolution" / "storage" / "providers" / "sqlite.py").write_text(textwrap.dedent("""\
        import sqlite3
        import threading

        LOCK = threading.Lock()

        class SQLiteProvider:
            def __init__(self, path: str):
                self.conn = sqlite3.connect(path)
    """))

    # Commit everything
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")

    # Patch the module to use our temp repo
    original_root = rb.REPO_ROOT
    original_manifest = rb.MANIFEST_PATH
    original_targets = rb.TARGET_FILES

    rb.REPO_ROOT = repo
    rb.MANIFEST_PATH = repo / ".review-benchmark-manifest.json"

    yield repo

    # Restore
    rb.REPO_ROOT = original_root
    rb.MANIFEST_PATH = original_manifest
    rb.TARGET_FILES = original_targets


# == Core safety tests ==========================================================


class TestMainBranchNeverModified:
    """The most critical guarantee: main branch content is never changed."""

    def test_main_tree_hash_unchanged_after_inject_revert(self, temp_repo):
        """Main's tree hash must be identical before inject and after revert."""
        tree_before = _tree_hash(temp_repo)
        sha_before = _commit_sha(temp_repo)

        rb.cmd_inject(count=5, seed=42)
        # We're now on the benchmark branch — main should be untouched
        rb.cmd_revert()

        tree_after = _tree_hash(temp_repo)
        sha_after = _commit_sha(temp_repo)

        assert tree_after == tree_before, "Main branch tree was modified"
        assert sha_after == sha_before, "Main branch commit was modified"

    def test_main_has_no_benchmark_commits(self, temp_repo):
        """Main branch should never receive benchmark commits."""
        main_commits_before = _git(temp_repo, "log", "--oneline").stdout.strip().count("\n")

        rb.cmd_inject(count=5, seed=42)
        rb.cmd_revert()

        main_commits_after = _git(temp_repo, "log", "--oneline").stdout.strip().count("\n")
        assert main_commits_after == main_commits_before, "Benchmark commits leaked to main"

    def test_main_files_untouched_during_injection(self, temp_repo):
        """While bugs are injected on the benchmark branch, main files are clean."""
        # Read a file on main
        main_content = (temp_repo / "src" / "container-runner.ts").read_text()

        rb.cmd_inject(count=5, seed=42)

        # Check main's content via git show (without switching branches)
        result = _git(temp_repo, "show", "main:src/container-runner.ts")
        main_content_during = result.stdout

        assert main_content_during == main_content, "Main branch file was modified during injection"

        rb.cmd_revert()


class TestBranchIsolation:
    """All changes must be isolated to the throwaway branch."""

    def test_inject_creates_benchmark_branch(self, temp_repo):
        """Injection must create the benchmark branch."""
        assert not _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)

        rb.cmd_inject(count=3, seed=42)

        assert _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)
        assert _branch_name(temp_repo) == rb.BENCHMARK_BRANCH

        rb.cmd_revert()

    def test_inject_commits_on_benchmark_branch_not_main(self, temp_repo):
        """Injected bugs must be committed on the benchmark branch, not main."""
        main_sha = _commit_sha(temp_repo)

        rb.cmd_inject(count=5, seed=42)

        # Benchmark branch should have a new commit
        benchmark_sha = _commit_sha(temp_repo)
        assert benchmark_sha != main_sha, "No commit created on benchmark branch"

        # Main should still be at original commit
        main_sha_now = _git(temp_repo, "rev-parse", "main").stdout.strip()
        assert main_sha_now == main_sha, "Main branch was advanced"

        rb.cmd_revert()

    def test_revert_deletes_benchmark_branch(self, temp_repo):
        """After revert, the benchmark branch must not exist."""
        rb.cmd_inject(count=3, seed=42)
        assert _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)

        rb.cmd_revert()

        assert not _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)

    def test_revert_returns_to_original_branch(self, temp_repo):
        """After revert, we must be back on the original branch."""
        original = _branch_name(temp_repo)

        rb.cmd_inject(count=3, seed=42)
        assert _branch_name(temp_repo) == rb.BENCHMARK_BRANCH

        rb.cmd_revert()
        assert _branch_name(temp_repo) == original

    def test_working_tree_clean_after_revert(self, temp_repo):
        """After revert, the working tree must have zero modifications."""
        rb.cmd_inject(count=5, seed=42)
        rb.cmd_revert()

        assert not _is_dirty(temp_repo), "Working tree is dirty after revert"


class TestSafetyGuards:
    """Tests for the safety guards that prevent accidents."""

    def test_refuses_injection_on_dirty_tree(self, temp_repo):
        """Must refuse to inject if the working tree has uncommitted changes."""
        # Dirty the tree
        (temp_repo / "src" / "db.ts").write_text("dirty content")

        with pytest.raises(SystemExit):
            rb.cmd_inject(count=3, seed=42)

        # Verify no benchmark branch was created
        assert not _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)

        # Restore
        _git(temp_repo, "checkout", "--", "src/db.ts")

    def test_refuses_injection_if_benchmark_branch_exists(self, temp_repo):
        """Must refuse if a leftover benchmark branch exists."""
        rb.cmd_inject(count=3, seed=42)

        # Manually go back to main, leaving the benchmark branch behind
        # First remove the manifest so it doesn't interfere
        if rb.MANIFEST_PATH.exists():
            rb.MANIFEST_PATH.unlink()
        _git(temp_repo, "checkout", "main")

        with pytest.raises(SystemExit):
            rb.cmd_inject(count=3, seed=99)

        # Cleanup
        _git(temp_repo, "branch", "-D", rb.BENCHMARK_BRANCH)

    def test_revert_refuses_to_delete_protected_branches(self, temp_repo):
        """Revert must never delete main or master."""
        original_branch_name = rb.BENCHMARK_BRANCH

        # Temporarily change BENCHMARK_BRANCH to "main"
        rb.BENCHMARK_BRANCH = "main"
        try:
            with pytest.raises(SystemExit):
                rb.cmd_revert()
        finally:
            rb.BENCHMARK_BRANCH = original_branch_name

    def test_inject_verifies_branch_checkout_succeeded(self, temp_repo):
        """If branch creation fails, no files should be modified."""
        # Create the branch first so checkout -b fails
        _git(temp_repo, "branch", rb.BENCHMARK_BRANCH)

        tree_before = _tree_hash(temp_repo)

        with pytest.raises(SystemExit):
            rb.cmd_inject(count=3, seed=42)

        tree_after = _tree_hash(temp_repo)
        assert tree_after == tree_before, "Files modified despite failed branch creation"

        # Cleanup
        _git(temp_repo, "branch", "-D", rb.BENCHMARK_BRANCH)


class TestManifestIntegrity:
    """Tests for the manifest (ground truth) tracking."""

    def test_manifest_records_original_branch(self, temp_repo):
        """Manifest must record which branch we came from."""
        original = _branch_name(temp_repo)

        rb.cmd_inject(count=3, seed=42)

        manifest = json.loads(rb.MANIFEST_PATH.read_text())
        assert manifest["original_branch"] == original

        rb.cmd_revert()

    def test_manifest_deleted_after_revert(self, temp_repo):
        """Manifest file must be cleaned up after revert."""
        rb.cmd_inject(count=3, seed=42)
        assert rb.MANIFEST_PATH.exists()

        rb.cmd_revert()
        assert not rb.MANIFEST_PATH.exists()

    def test_manifest_injection_count_matches(self, temp_repo):
        """Manifest count must match actual injections."""
        results = rb.cmd_inject(count=5, seed=42)

        manifest = json.loads(rb.MANIFEST_PATH.read_text())
        assert manifest["count"] == len(results)
        assert len(manifest["injections"]) == len(results)

        rb.cmd_revert()

    def test_all_injected_files_are_on_benchmark_branch(self, temp_repo):
        """Every file in the manifest must have changes on the benchmark branch."""
        rb.cmd_inject(count=5, seed=42)

        manifest = json.loads(rb.MANIFEST_PATH.read_text())
        changed = _git(temp_repo, "diff", "--name-only", "main...HEAD").stdout.strip().split("\n")

        for inj in manifest["injections"]:
            assert inj["file"] in changed, f"{inj['file']} not in benchmark diff"

        rb.cmd_revert()


class TestFullCycle:
    """End-to-end tests for the complete inject → diff → revert cycle."""

    def test_full_run_leaves_repo_clean(self, temp_repo):
        """The `run` command must leave the repo in the exact pre-run state."""
        tree_before = _tree_hash(temp_repo)
        branch_before = _branch_name(temp_repo)

        rb.cmd_run(count=5, seed=42)

        tree_after = _tree_hash(temp_repo)
        branch_after = _branch_name(temp_repo)

        assert tree_after == tree_before
        assert branch_after == branch_before
        assert not _branch_exists(temp_repo, rb.BENCHMARK_BRANCH)
        assert not _is_dirty(temp_repo)

    def test_reproducible_with_same_seed(self, temp_repo):
        """Same seed must produce same injections."""
        results1 = rb.cmd_inject(count=5, seed=999)
        files1 = [(r.file, r.bug_type) for r in results1]
        rb.cmd_revert()

        results2 = rb.cmd_inject(count=5, seed=999)
        files2 = [(r.file, r.bug_type) for r in results2]
        rb.cmd_revert()

        assert files1 == files2

    def test_different_seeds_produce_different_results(self, temp_repo):
        """Different seeds should produce different injection sets."""
        results1 = rb.cmd_inject(count=5, seed=111)
        files1 = set((r.file, r.bug_type) for r in results1)
        rb.cmd_revert()

        results2 = rb.cmd_inject(count=5, seed=222)
        files2 = set((r.file, r.bug_type) for r in results2)
        rb.cmd_revert()

        assert files1 != files2, "Different seeds produced identical results"


class TestHardcodedSecretInject:
    """HardcodedSecret.inject previously referenced an undefined `filepath`
    (only defined in the sibling `find_targets`). Regression: ensure it now
    reads `filepath` from the `context` dict the caller passes."""

    def test_inject_does_not_raise_nameerror_on_py(self):
        pattern = rb.HardcodedSecret()
        content = "API_TIMEOUT = 30\n"
        line_no, match = 1, "API_TIMEOUT = 30"
        context = {"filepath": "src/example.py", "rng": __import__("random").Random(0)}

        modified, desc, disguise = pattern.inject(content, line_no, match, context)
        assert "API_TIMEOUT" in modified
        assert "fallback for CI environments" in modified
        # Python path → should emit `NAME = "..."`, not `const NAME = "..."`
        assert "const " not in modified

    def test_inject_does_not_raise_nameerror_on_ts(self):
        pattern = rb.HardcodedSecret()
        content = "const API_TIMEOUT = 30;\n"
        line_no, match = 1, "const API_TIMEOUT = 30;"
        context = {"filepath": "src/example.ts", "rng": __import__("random").Random(0)}

        modified, _, _ = pattern.inject(content, line_no, match, context)
        assert "const " in modified
        assert "fallback for CI environments" in modified
