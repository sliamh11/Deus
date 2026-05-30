"""Tests for the LIA-143 Layer 2 net-new DEUS_* flag-gate lint (scripts/ci/flag_lint.py).

Each test builds a fully isolated temp git repo with its own base commit and HEAD
commit, then runs the lint against that repo's real diff + base ref. Isolation
matters: the net-new check shells out to `git grep <flag> <base>`, so the harness
must not leak into the real repo's history.

The load-bearing assertions: a net-new uncited DEUS_* gate FAILS (exit 1); a
re-read of a flag that already exists in the base ref stays SILENT (exit 0) —
that pair is the whole point of the "net-new only" design.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_MOD_PATH = _ROOT / "scripts" / "ci" / "flag_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("flag_lint", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flag_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


flag_lint = _load()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_repo(tmp_path: Path, base_files: dict[str, str], head_files: dict[str, str]) -> tuple[Path, str]:
    """Init a repo, commit base_files, then commit head_files on a branch.

    Returns (repo_dir, base_sha). head_files are written on top of base.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    for path, content in base_files.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "-b", "feat")
    for path, content in head_files.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat")
    return repo, base_sha


def _run(repo: Path, base_sha: str, allowlist=None):
    diff = flag_lint._get_diff(base_sha, str(repo))
    kwargs = {} if allowlist is None else {"allowlist": allowlist}
    return flag_lint.scan(flag_lint.parse_diff(diff), base_sha, str(repo), **kwargs)


# --- core red/green pair --------------------------------------------------


def test_net_new_uncited_flag_fires(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "export const x = 1;\nif (process.env.DEUS_TEST_UNUSED_GATE) doThing();\n"},
    )
    violations = _run(repo, base)
    assert len(violations) == 1
    assert violations[0].flag == "DEUS_TEST_UNUSED_GATE"


def test_existing_flag_reread_is_silent(tmp_path):
    # Flag already present in base → reading it again in a new location must not fire.
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "const a = process.env.DEUS_EXISTING_FLAG;\n"},
        {
            "src/a.ts": "const a = process.env.DEUS_EXISTING_FLAG;\n",
            "src/b.ts": "const b = process.env.DEUS_EXISTING_FLAG ?? 'x';\n",
        },
    )
    assert _run(repo, base) == []


# --- citation + escape paths ---------------------------------------------


def test_net_new_with_linear_citation_passes(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "// LIA-999: gated until orchestrator lands\nif (process.env.DEUS_TEST_NEW_GATE) go();\n"},
    )
    assert _run(repo, base) == []


def test_net_new_with_pr_citation_passes(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "if (process.env.DEUS_TEST_NEW_GATE) go(); // tracked in #1234\n"},
    )
    assert _run(repo, base) == []


def test_net_new_with_dev_only_escape_passes(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "if (process.env.DEUS_TEST_NEW_GATE) go(); // dev-only\n"},
    )
    assert _run(repo, base) == []


# --- comment syntax across languages -------------------------------------


def test_python_hash_comment_citation_passes(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"scripts/x.py": "x = 1\n"},
        {"scripts/x.py": "# LIA-777: experimental path\nif os.environ.get('DEUS_TEST_NEW_GATE'):\n    go()\n"},
    )
    assert _run(repo, base) == []


def test_python_net_new_uncited_fires(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"scripts/x.py": "x = 1\n"},
        {"scripts/x.py": "if os.getenv('DEUS_TEST_UNUSED_GATE'):\n    go()\n"},
    )
    violations = _run(repo, base)
    assert len(violations) == 1
    assert violations[0].flag == "DEUS_TEST_UNUSED_GATE"


def test_shell_net_new_uncited_fires(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"deus.sh": "echo hi\n"},
        {"deus.sh": 'if [ -n "$DEUS_TEST_UNUSED_GATE" ]; then run; fi\n'},
    )
    violations = _run(repo, base)
    assert len(violations) == 1
    assert violations[0].flag == "DEUS_TEST_UNUSED_GATE"


# --- self-exclusion + allowlist ------------------------------------------


def test_flag_in_excluded_path_is_ignored(tmp_path):
    # The lint's own dir / test dirs must not trip it on example flag strings.
    repo, base = _make_repo(
        tmp_path,
        {"scripts/ci/other.py": "x = 1\n"},
        {
            "scripts/ci/other.py": "if os.getenv('DEUS_TEST_UNUSED_GATE'): go()\n",
            "scripts/tests/test_z.py": "v = process.env.DEUS_TEST_UNUSED_GATE\n",
        },
    )
    assert _run(repo, base) == []


def test_allowlisted_flag_passes(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "const lvl = process.env.DEUS_OPS_ONLY;\n"},
    )
    assert _run(repo, base, allowlist=frozenset({"DEUS_OPS_ONLY"})) == []
    # Without the allowlist it should fire (proves the allowlist is what passed it).
    assert len(_run(repo, base)) == 1


# --- non-code files + end-to-end exit code -------------------------------


def test_flag_in_markdown_is_ignored(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"docs/x.md": "hello\n"},
        {"docs/x.md": "Set `process.env.DEUS_TEST_UNUSED_GATE` to enable.\n"},
    )
    assert _run(repo, base) == []


def test_main_exit_code_on_violation(tmp_path, capsys):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "export const x = 1;\n"},
        {"src/a.ts": "if (process.env.DEUS_TEST_UNUSED_GATE) go();\n"},
    )
    rc = flag_lint.main(["--base", base, "--repo", str(repo)])
    assert rc == 1
    assert "DEUS_TEST_UNUSED_GATE" in capsys.readouterr().err


def test_citation_window_boundary(tmp_path):
    # Citation exactly 3 lines above the gate (== _CITE_WINDOW) passes;
    # 4 lines above (just outside the window) fires. Guards the constant.
    within = "// LIA-1: tracked\nconst p = 0;\nconst q = 0;\nif (process.env.DEUS_TEST_NEW_GATE) go();\n"
    outside = "// LIA-1: tracked\nconst p = 0;\nconst q = 0;\nconst r = 0;\nif (process.env.DEUS_TEST_NEW_GATE) go();\n"
    repo_in, base_in = _make_repo(tmp_path / "a", {"src/a.ts": "x;\n"}, {"src/a.ts": within})
    assert _run(repo_in, base_in) == []
    repo_out, base_out = _make_repo(tmp_path / "b", {"src/a.ts": "x;\n"}, {"src/a.ts": outside})
    assert len(_run(repo_out, base_out)) == 1


def test_parse_diff_tracks_new_line_numbers(tmp_path):
    repo, base = _make_repo(
        tmp_path,
        {"src/a.ts": "line1\nline2\nline3\n"},
        {"src/a.ts": "line1\nline2\nINSERTED\nline3\n"},
    )
    files = flag_lint.parse_diff(flag_lint._get_diff(base, str(repo)))
    added = [dl for dl in files["src/a.ts"] if dl.added]
    assert len(added) == 1
    assert added[0].text == "INSERTED"
    assert added[0].lineno == 3
