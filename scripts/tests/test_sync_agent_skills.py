import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import sync_agent_skills


class TestTransformMarkdown:
    def test_rewrites_paths_and_memory_files(self):
        src = (
            "Read `groups/main/CLAUDE.md`.\n"
            "Install to `~/.claude/skills/`.\n"
            "Archive old items to `$VAULT/CLAUDE-Archive.md`.\n"
        )
        out = sync_agent_skills.transform_markdown(src)
        assert "groups/main/AGENTS.md" in out
        assert "~/.Codex/skills/" in out
        assert "$VAULT/Codex-Archive.md" in out

    def test_rewrites_claude_branding_without_touching_env_var(self):
        src = (
            "Claude Agent SDK and Claude Code both require login.\n"
            "Run `claude --version`.\n"
            "Do not edit `CLAUDE_CODE_OAUTH_TOKEN`.\n"
        )
        out = sync_agent_skills.transform_markdown(src)
        assert "Codex Agent SDK" in out
        assert "Codex both require login" in out
        assert "`Codex --version`" in out
        assert "CLAUDE_CODE_OAUTH_TOKEN" in out


class TestCheckAndSyncAgentsTree:
    def _build_repo(self, tmp_path: Path) -> Path:
        skill_dir = tmp_path / ".claude" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill.\n---\n\n"
            "Read `groups/main/CLAUDE.md` and run `claude --version`.\n"
        )
        (skill_dir / "agent.ts").write_text("export const demo = true;\n")

        agent_dir = tmp_path / ".claude" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "plan-reviewer.md").write_text(
            "You are the plan-reviewer. Read `.claude/wardens/plan-review-rules.md`.\n"
        )

        warden_dir = tmp_path / ".claude" / "wardens"
        warden_dir.mkdir(parents=True)
        (warden_dir / "plan-review-rules.md").write_text("## public-repo-generic\n")

        return tmp_path / ".agents"

    def test_sync_writes_generated_tree(self, tmp_path):
        dest = self._build_repo(tmp_path)
        rc = sync_agent_skills.sync_agents_tree(tmp_path, dest)
        assert rc == 0
        assert (dest / "skills" / "demo" / "agent.ts").read_text() == "export const demo = true;\n"
        skill_text = (dest / "skills" / "demo" / "SKILL.md").read_text()
        assert "groups/main/AGENTS.md" in skill_text
        assert "`Codex --version`" in skill_text

    def test_sync_mirrors_agents_and_wardens(self, tmp_path):
        dest = self._build_repo(tmp_path)
        sync_agent_skills.sync_agents_tree(tmp_path, dest)
        agent_text = (dest / "agents" / "plan-reviewer.md").read_text()
        assert ".Codex/wardens/plan-review-rules.md" in agent_text
        assert (dest / "wardens" / "plan-review-rules.md").exists()

    def test_check_detects_drift(self, tmp_path, capsys):
        dest = self._build_repo(tmp_path)
        sync_agent_skills.sync_agents_tree(tmp_path, dest)
        (dest / "skills" / "demo" / "SKILL.md").write_text("drifted\n")
        rc = sync_agent_skills.check_agents_tree(tmp_path, dest)
        assert rc == 1
        out = capsys.readouterr().out
        assert "AGENT TREE DRIFT" in out
        assert "skills/demo/SKILL.md" in out

    def test_check_skips_when_dest_missing(self, tmp_path, capsys):
        self._build_repo(tmp_path)
        rc = sync_agent_skills.check_agents_tree(tmp_path, tmp_path / ".agents-missing")
        assert rc == 0
        out = capsys.readouterr().out
        assert "SKIP" in out
