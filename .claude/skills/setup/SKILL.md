---
name: setup
description: Run initial Deus setup — dependencies, container, credentials, service, and CLI. Channels are added separately after setup completes. Triggers on "setup", "install", "configure deus", or first-time setup requests.
---

# Deus Setup

Run setup steps automatically. Only pause when user action is required (channel authentication, configuration choices). Setup uses `bash setup.sh` for bootstrap, then `npx tsx setup/index.ts --step <name>` for all other steps. Steps emit structured status blocks to stdout. Verbose logs go to `logs/setup.log`.

**Principle:** When something is broken or missing, fix it. Don't tell the user to go fix it themselves unless it genuinely requires their manual action (e.g. authenticating a channel, pasting a secret token). If a dependency is missing, install it. If a service won't start, diagnose and repair. Ask the user for permission when needed, then do the work.

**UX Note:** Use `AskUserQuestion` for all user-facing questions.

**CRITICAL:** Do NOT add git remotes (`git remote add`), fetch from external repos, or install npm packages from the public registry outside of step 0. All channel code and packages are local in the repo. If something seems missing, check `packages/` and `src/channels/` before looking externally.

**Vault structure default:** The memory step seeds `CLAUDE.md` (identity + critical rules + index + session state). Only `CLAUDE.md` auto-loads every turn — everything else loads on demand via `memory_tree`.

## 0. Git & Fork Setup

Check the git remote configuration to ensure the user has a proper setup for receiving updates.

Run:
- `git remote -v`

Determine which case applies based on the origin URL:

**Case A — `origin` points to the Deus source repo (user cloned directly instead of forking):**

The user cloned instead of forking. AskUserQuestion: "You cloned Deus directly. We recommend forking so you can push your customizations. Would you like to set up a fork?"
- Fork now (recommended) — walk them through it
- Continue without fork — they'll only have local changes

If fork: instruct the user to fork the repo on GitHub (they need to do this in their browser), then ask them for their GitHub username. Run:
```bash
git remote rename origin upstream
git remote add origin https://github.com/<their-username>/deus.git
git push --force origin main
```
Verify with `git remote -v`.

If continue without fork: they'll only have local changes.

**Case B — `origin` points to a different repo, no `upstream` remote:**

Determine if the user owns the origin repo (it's their fork) or if they cloned someone else's repo:

1. Get the authenticated GitHub user: `gh api user --jq .login`
2. Extract the owner from the origin URL (e.g. `sliamh11` from `sliamh11/Deus`)
3. Compare them.

**If the user OWNS origin** (their GitHub username matches origin owner):
  - Check if origin is a fork: `gh repo view --json parent --jq '.parent.owner.login + "/" + .parent.name'`
  - If it's a fork → add the parent as upstream:
    ```bash
    git remote add upstream https://github.com/<parent-owner>/<parent-name>.git
    ```
  - If it's NOT a fork → this is the source repo. No upstream needed (Case D).

**If the user does NOT own origin** (they cloned someone else's repo):
  - They're using that repo as their source of truth. Do NOT add upstream. Their `origin` is already their update source.

**Case C — both `origin` and `upstream` exist:**

Already configured. Continue.

**Case D — `origin` points to the source repo (no parent):**

This is the maintainer's own repo or a direct clone. No upstream needed. Continue.

## 1. Bootstrap (Node.js + Dependencies)

Run `bash setup.sh` and parse the status block.

- If NODE_OK=false → Node.js is missing or too old. Use `AskUserQuestion: Would you like me to install Node.js 22?` If confirmed:
  - macOS: `brew install node@22` (if brew available) or install nvm then `nvm install 22`
  - Linux: `curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs`, or nvm
  - After installing Node, re-run `bash setup.sh`
- If DEPS_OK=false → Read `logs/setup.log`. Try: delete `node_modules`, re-run `bash setup.sh`. If native module build fails, install build tools (`xcode-select --install` on macOS, `build-essential` on Linux), then retry.
- If NATIVE_OK=false → better-sqlite3 failed to load. Install build tools and re-run.
- Record PLATFORM and IS_WSL for later steps.

## 2. Check Environment

Run `npx tsx setup/index.ts --step environment` and parse the status block.

- If HAS_AUTH=true → WhatsApp is already configured, inform user
- If HAS_REGISTERED_GROUPS=true → note existing config
- Record APPLE_CONTAINER and DOCKER values for step 3

## 3. Container Runtime

### 3a. Install Docker

- DOCKER=running → continue to 3b
- DOCKER=installed_not_running → start Docker:
  - macOS: `open -a Docker`
  - Linux: `sudo systemctl start docker`
  - Windows: launch Docker Desktop from Start menu if not in system tray.
  - After starting, check once with `docker info`. If it fails, **do NOT poll in a loop** — use AskUserQuestion: "Docker is starting up. Let me know when it's ready (you'll see the Docker icon in the system tray turn solid)." Then verify with `docker info`.
- DOCKER=not_found → Use `AskUserQuestion: Docker is required for running agents. Would you like me to install it?` If confirmed:
  - macOS: install via `brew install --cask docker`, then `open -a Docker` and wait for it to start. If brew not available, direct to Docker Desktop download at https://docker.com/products/docker-desktop
  - Linux: install with `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`. Note: user may need to log out/in for group membership.
  - Windows: direct to Docker Desktop download at https://docker.com/products/docker-desktop. Requires WSL 2 (auto-offered by Docker installer). After install, start Docker Desktop from Start menu.

### 3b. Build and test (BACKGROUND)

**Start the container build in the background** — it takes 3-5 minutes (up to 10 on Windows first run) and doesn't need user input. Continue with steps 4-5 while it runs.

Run in background with **10 minute timeout**: `npx tsx setup/index.ts --step container -- --runtime docker`

**Do NOT wait for this to finish.** Immediately continue to step 4. You will check the result before step 6.

**IMPORTANT — if build fails later:** Read the FULL error output before retrying. Common causes:
- TypeScript compilation errors from skill agents → check which skill was staged and if it's compatible
- Timeout → re-run with longer timeout, Docker layers are cached so retry is faster
- Do NOT prune Docker cache unless you're certain the cache itself is the problem

## 4. Claude Authentication (No Script)

If HAS_ENV=true from step 2, read `.env` and check for `ANTHROPIC_API_KEY`. If present, confirm with user: keep or reconfigure?

AskUserQuestion: Claude subscription (Pro/Max) vs Anthropic API key?

**Subscription (OAuth):** The credential proxy reads `~/.claude/.credentials.json` directly — no `.env` entry needed. Just ensure the user is logged in: `claude` (launches Claude Code, which authenticates). Do NOT add `CLAUDE_CODE_OAUTH_TOKEN` to `.env` — writing it there freezes it and causes a login loop when the token auto-rotates.

**API key:** Tell user to add `ANTHROPIC_API_KEY=<key>` to `.env`.

## 5. Mount Allowlist

AskUserQuestion: Agent access to external directories?

**No:** `npx tsx setup/index.ts --step mounts -- --empty`
**Yes:** Collect paths/permissions. `npx tsx setup/index.ts --step mounts -- --json '{"allowedRoots":[...],"blockedPatterns":[],"nonMainReadOnly":true}'`

## 5b. Wait for Container Build

**Before proceeding to step 6, check the container build from step 3b.**

If the background build is still running, wait for it to finish. Parse the status block.

**If BUILD_OK=false:** Read `logs/setup.log` tail for the build error.
- Cache issue (stale layers): `docker builder prune -f`. Retry.
- Dockerfile syntax or missing files: diagnose from the log and fix, then retry.

**If TEST_OK=false but BUILD_OK=true:** The image built but won't run. Check logs — common cause is runtime not fully started. Wait a moment and retry the test.

## 6. Start Service

If service already running: stop first.
- macOS: `launchctl unload ~/Library/LaunchAgents/com.deus.plist`
- Linux: `systemctl --user stop deus` (or `systemctl stop deus` if root)
- Windows (NSSM): `nssm stop deus`
- Windows (Servy): `servy-cli stop --name=deus`

Run `npx tsx setup/index.ts --step service` and parse the status block.

**If FALLBACK=wsl_no_systemd:** WSL without systemd detected. Tell user they can either enable systemd in WSL (`echo -e "[boot]\nsystemd=true" | sudo tee /etc/wsl.conf` then restart WSL) or use the generated `start-deus.sh` wrapper.

**If PLATFORM=windows:** Detect whether NSSM is available for persistent service management:

```bash
where nssm 2>nul && echo "NSSM_AVAILABLE=true" || echo "NSSM_AVAILABLE=false"
```

**If NSSM_AVAILABLE=true:** Install and configure the Windows service with NSSM:

```bash
nssm install deus node <project-root>\dist\index.js
nssm set deus AppDirectory <project-root>
nssm set deus AppRestartDelay 5000
nssm start deus
```

Replace `<project-root>` with the absolute path to the Deus project directory (from `cd` or `%CD%`).

**If NSSM_AVAILABLE=false:** AskUserQuestion: NSSM is not installed. It's needed for running Deus as a persistent Windows service. How would you like to proceed?
- **Install NSSM via winget** (Recommended) — run `winget install nssm`, then re-run this step
- **Download NSSM manually** — download from https://nssm.cc/download and add it to your PATH, then re-run this step
- **Use Windows Task Scheduler instead** — create a task that runs `node <project-root>\dist\index.js` at login (less reliable than NSSM for restarts)
- **Use the batch launcher** — skip persistent service, use `.\start-deus.bat` manually

If the user chose Task Scheduler: guide them to create a scheduled task:
1. Open Task Scheduler (`taskschd.msc`)
2. Create a Basic Task named "Deus"
3. Trigger: "When I log on"
4. Action: Start a Program — `node`, arguments: `<project-root>\dist\index.js`, start in: `<project-root>`
5. Check "Run with highest privileges" if Docker requires it

**If PLATFORM=windows and FALLBACK=batch (and user skipped NSSM/Task Scheduler):** A `start-deus.bat` launcher was generated for the background service. Tell user: the service can be started with `.\start-deus.bat` or by double-clicking it. For auto-start on login, add a shortcut to `shell:startup`. The `deus` CLI command will be set up in step 6b.

**If DOCKER_GROUP_STALE=true:** The user was added to the docker group after their session started — the systemd service can't reach the Docker socket. Ask user to run these two commands:

1. Immediate fix: `sudo setfacl -m u:$(whoami):rw /var/run/docker.sock`
2. Persistent fix (re-applies after every Docker restart):
```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/socket-acl.conf << 'EOF'
[Service]
ExecStartPost=/usr/bin/setfacl -m u:USERNAME:rw /var/run/docker.sock
EOF
sudo systemctl daemon-reload
```
Replace `USERNAME` with the actual username (from `whoami`). Run the two `sudo` commands separately — the `tee` heredoc first, then `daemon-reload`. After user confirms setfacl ran, re-run the service step.

**If SERVICE_LOADED=false:**
- Read `logs/setup.log` for the error.
- macOS: check `launchctl list | grep deus`. If PID=`-` and status non-zero, read `logs/deus.error.log`.
- Linux: check `systemctl --user status deus`.
- Re-run the service step after fixing.

## 6b. Register CLI Command

Run `npx tsx setup/index.ts --step cli` and parse the status block.

This creates a global `deus` command so the user can type `deus` from any terminal.

- macOS/Linux: symlinks `deus-cmd.sh` → `~/.local/bin/deus`
- Windows: creates `deus.cmd` shim → `%USERPROFILE%\.local\bin\` and adds it to user PATH

**If STATUS=conflict and EXISTING=foreign:** A non-Deus binary named `deus` already exists at the CLI path. Ask the user: "A file already exists at `<LINK_PATH>` that doesn't appear to be a Deus installation. Should I replace it with the Deus CLI? (The existing file will be deleted.)" If they confirm, delete the file and re-run `npx tsx setup/index.ts --step cli`. If they decline, skip CLI registration and tell them they can run Deus directly with `./deus-cmd.sh` from the repo directory.

**If IN_PATH=false:** The setup step auto-appends `~/.local/bin` to the user's shell config. If it couldn't (permissions, etc.), tell user to add it manually:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc  # or ~/.bashrc
```

**After CLI registration:** Tell user they can type `deus` from any terminal after reopening their shell (or running `source ~/.zshrc` / `source ~/.bashrc` to apply immediately).

## 6c. Install Core Skills

Install Deus's 6 core memory skills to `~/.claude/skills/` so they work in any directory (home mode AND external project mode).

Run using Python (cross-platform — macOS, Linux, Windows):
```bash
python3 -c "
import os, shutil, sys
from pathlib import Path

repo = Path.cwd()
src_base = repo / '.claude' / 'skills'
dest_base = Path.home() / '.claude' / 'skills'
skills = ['compress', 'resume', 'checkpoint', 'preserve', 'preferences', 'project-settings']
failed = []

for skill in skills:
    src = src_base / skill / 'skill.md'
    dest_dir = dest_base / skill
    dest = dest_dir / 'skill.md'
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        dest.symlink_to(src.resolve())
        print(f'  ✓ {skill} (symlink)')
    except OSError:
        # Windows without Developer Mode — fall back to copy
        shutil.copy2(src, dest)
        print(f'  ✓ {skill} (copied — re-run setup after repo updates)')

if failed:
    print(f'  ✗ failed: {failed}', file=sys.stderr)
    sys.exit(1)
"
```

- **macOS/Linux:** creates symlinks — repo updates propagate automatically
- **Windows (Developer Mode on):** creates symlinks
- **Windows (Developer Mode off):** falls back to file copy — user must re-run setup after updating the repo
- Idempotent — safe to re-run anytime

**If any skill fails:** warn the user and continue — the other skills still install. The commands at `.claude/commands/` (home-mode-only) remain as fallback.

**After installing:** Tell the user that `/compress`, `/resume`, `/checkpoint`, `/preserve`, `/preferences`, and `/project-settings` are now available in any project directory.

## 7. Verify

Run `npx tsx setup/index.ts --step verify` and parse the status block.

**If STATUS=failed, fix each:**
- SERVICE=stopped → `npm run build`, then restart:
  - macOS: `launchctl kickstart -k gui/$(id -u)/com.deus`
  - Linux: `systemctl --user restart deus`
  - Windows (NSSM): `nssm restart deus`
  - Windows (Servy): `servy-cli restart --name=deus`
  - WSL nohup fallback: `bash start-deus.sh`
- SERVICE=not_found → re-run step 6
- CREDENTIALS=missing → re-run step 4
- MOUNT_ALLOWLIST=missing → `npx tsx setup/index.ts --step mounts -- --empty`

**Note:** CONFIGURED_CHANNELS=0 and REGISTERED_GROUPS=0 are expected at this point — channels are added after setup completes. These are informational, not failures.

## 7b. Ollama Model Advisor (Optional)

Run `npx tsx setup/index.ts --step ollama` and parse the status block.

This step is **non-fatal** — Ollama is optional. If Ollama is not installed, the step exits gracefully.

**If STATUS=skipped and REASON=ollama_not_installed:**
Tell the user: "Ollama is not installed. If you want to use local AI models for self-improvement scoring, install Ollama from https://ollama.ai, then re-run this step with `npx tsx setup/index.ts --step ollama`."

**If STATUS=skipped and REASON=hardware_detection_unavailable:**
Python evolution package is not set up. This is fine — Ollama can be configured manually later by setting `OLLAMA_MODEL` in `~/.config/deus/.env`.

**If STATUS=success and PULLED=true:**
Tell the user: "Pulled `{MODEL}` ({MODEL_SIZE_GB} GB) — configured as your local judge model."

**If STATUS=success and ALREADY_PRESENT=true:**
Tell the user: "Model `{MODEL}` is already pulled — no action needed."

**If STATUS=failed:**
Tell the user: "Model pull failed (see ERROR field). You can retry with `ollama pull {MODEL}` and set `OLLAMA_MODEL={MODEL}` in `~/.config/deus/.env` manually."


## 7c. Code Intelligence (Optional)

Run `npx tsx setup/index.ts --step codeintel` and parse the status block.

This registers the two code-intelligence MCP servers Deus uses during
development — **codegraph** (third-party npm global) and **code-search**
(first-party semantic search) — via the official `claude mcp add --scope user`
CLI. It is **optional and non-fatal**: each server is best-effort and skips
cleanly if its prerequisites are missing. Run it **after** step 7b — code-search
builds its index using Ollama embeddings.

> **Note:** codegraph installs the third-party package `@colbymchenry/codegraph`
> globally via npm (version-pinned). This is the standard Deus code-intelligence
> tool; if you prefer not to auto-install it, skip this step and install it
> manually later.

Per-server fields are `CODEGRAPH` / `CODE_SEARCH` (`success` | `skipped` with a
`*_REASON`), `*_MCP` (`registered` | `failed` | `skipped`), and `*_INDEX`
(`started` | `failed` | `skipped`). The build logs live in `LOG_DIR`.

**If STATUS=success or partial:**
Tell the user which servers registered. For any `*_INDEX=started`, note the
index is building in the background (logs in `LOG_DIR`) and will be ready
shortly. They take effect in new Claude Code sessions.

**Common skip reasons (all non-fatal — report and continue):**
- `CODEGRAPH_REASON=npm_not_installed` → "codegraph needs Node/npm. Install Node, then re-run `npx tsx setup/index.ts --step codeintel`."
- `CODEGRAPH_REASON=install_failed` → "codegraph install failed (often a global-npm permissions issue). Run manually: `npm install -g @colbymchenry/codegraph` and re-run this step."
- `CODEGRAPH_REASON=binary_unverified` → "codegraph installed but didn't run. Often the npm global bin dir isn't on PATH — check `npm bin -g` and add it to your shell PATH, then re-run this step. (Also covers unsupported platforms.) Skipped cleanly — no broken MCP entry left behind."
- `CODE_SEARCH_REASON=windows_unsupported` → "code-search is macOS/Linux only (needs sqlite_vec + Ollama). Skipped on Windows."
- `CODE_SEARCH_REASON=sqlite_vec_missing` → "code-search needs the `sqlite_vec` Python package. Run the `memory` step first (`npx tsx setup/index.ts --step memory`), then re-run this step."
- `CODE_SEARCH_REASON=python_not_found` → "No Python found. Install Python 3, then re-run this step."
- `CODE_SEARCH_INDEX=skipped` with `CODE_SEARCH=success` → "code-search registered but its index isn't built (Ollama absent). After installing Ollama: `python3 scripts/code_search.py reindex .`"

**Verify (optional):** `claude mcp list` should show `codegraph` and/or
`code-search` once their background indexes finish.


## 8. Personality Kickstarter (Optional)

AskUserQuestion: "Want to load curated behavioral defaults into your Deus config?" Options:
- "Apply recommended defaults (fastest)"
- "Let me choose"
- "Skip for now"

**If "Skip for now":** Continue to step 9.

**If "Apply recommended defaults":** Run Path A below.

**If "Let me choose":** Run Path B below.

---

### Path A — Recommended Defaults

**Step 1 — Role question:**

AskUserQuestion: "What best describes how you'll use Deus?" Options:
- "Software development"
- "Research & writing"
- "Learning & studying"
- "General assistant / mixed"

Map the answer to an overlay:
- Software development → Developer overlay
- Research & writing → Researcher overlay
- Learning & studying → Student overlay
- General assistant → no overlay (base only)

**Step 2 — Preview and consent:**

Assemble the full rule list: Base Layer rules + the selected overlay's rules (see rule content below). Display all rules as a numbered list.

AskUserQuestion: "Here are your defaults. Say 'looks good' to apply, or describe any changes."

Apply any edits the user requests. The user is the final author — only write what they approve.

**Step 3 — Write rules:**

Read `groups/main/CLAUDE.md`. If the file does not exist, create it from the template. If a `## Behavioral Defaults` heading already exists, ask the user: "Behavioral defaults already configured. Overwrite / Keep existing / Skip?" — then act accordingly.

Append the approved rules under `## Behavioral Defaults`.

**Step 4 — Import seeds:**

Check if the evolution package is available:
```bash
python3 -c "from evolution.reflexion.store import save_reflection; print('ok')" 2>/dev/null
```
If unavailable, tell the user "Evolution package not set up — skipping seed import." and continue to step 9.

If available, import the role-matched seeds silently (see seed mapping table below):
```bash
python3 scripts/import_seeds.py --seeds '<json_array_of_role_matched_seeds>'
```

Report: "Imported N seed reflections."

Tell the user: "Defaults saved. You can edit `groups/main/CLAUDE.md` anytime to add, remove, or rephrase any rule."

Continue to step 9.

---

### Path B — Let Me Choose

**Step 8a — Overlay selection:**

AskUserQuestion (multiSelect): "Which overlays would you like to add? Pick any combination." Options:
- "Developer — git workflow, CI gates, security-first"
- "Researcher — vault saving, explore-first, measure-before-designing"
- "Student — retrieval practice, spaced review, example-first teaching"
- "Autonomous Operator — parallel-everything, background-first, memory-search"

**Step 8b — Review pass:**

Assemble the full rule list: Base Layer rules + all selected overlays' rules (see rule content below). Display all rules as a single numbered list.

If the Student overlay is selected, also present optional add-ons after the core rules:
- "Visual-first explanations: diagrams/flowcharts over text walls; Feynman-first approach (analogy → why it matters → how it connects)"
- "Route long study sessions to NotebookLM"
- "Ground theory in code: connect concepts to the code being written, use edge-cases to build intuition"

If all 4 overlays are selected, show a note: "All overlays selected — this adds ~25 rules to your config (loaded every turn). Consider removing overlays that don't match your primary workflow."

AskUserQuestion: "Here are your defaults. Say 'looks good' to apply, or describe any changes."

Apply any edits the user requests.

**Step 8c — À la carte:**

Build the à la carte list dynamically — skip items already covered by a selected overlay:
- "Research saving — save significant research to vault with tags frontmatter" (skip if Researcher overlay selected)
- "Code hygiene — only modify code you were asked to modify; no cleanup of surrounding functions"
- "Memory-first — search vault for prior decisions before implementing any feature" (skip if Autonomous Operator overlay selected)

If all items would be skipped (both Researcher and Autonomous selected), skip this step entirely.

AskUserQuestion (multiSelect): "Any of these cross-cutting behaviors to add?"

Append any selected à la carte rules under `## Behavioral Defaults` alongside the overlay rules.

**Step 8d — Seeds:**

Check evolution package availability (same as Path A Step 4). If unavailable, skip with message.

If available, read `seeds/reflections.json`. Build a pre-selected list based on the role mapping table below. Display the full seed list with `summary` and `category`, with role-matched seeds pre-selected.

AskUserQuestion (multiSelect): "These seed reflections are pre-selected based on your overlays. Deselect any that don't apply."

Import the final set:
```bash
python3 scripts/import_seeds.py --seeds '<json_array_of_selected_seeds>'
```

Report: "Imported N reflections (M skipped as near-duplicates)."

**Step 8e — Write rules:**

Read `groups/main/CLAUDE.md`. If the file does not exist, create it from the template. If a `## Behavioral Defaults` heading already exists, ask the user: "Behavioral defaults already configured. Overwrite / Keep existing / Skip?" — then act accordingly.

Append all approved rules (overlay + à la carte) under `## Behavioral Defaults`.

Tell the user: "Defaults saved. You can edit `groups/main/CLAUDE.md` anytime to add, remove, or rephrase any rule."

---

### Rule Content

**Base Layer (always included, ~150 tokens):**
- Never execute after asking a confirmation question — stop and wait for explicit response.
- Run all independent work in parallel: tool calls, agents, research branches. Don't serialize without a hard data dependency.
- Diagnosis before treatment: identify what changed since it last worked before attempting a fix.
- Evaluate alternatives before committing: compare at least two approaches before writing code.

**Developer Overlay (~200 tokens):**
- `git status` + clean tree + feature branch before every task. Never start work on main.
- Plan → Branch → Implement → Verify → Propose commit → Approval → Commit. Never commit without explicit approval.
- Use `git worktree add` for branch isolation — never `git checkout` between branches mid-task.
- One concern per branch — never bundle unrelated changes.
- Debugging: read full pipeline end-to-end, follow data flow, grep all consumers before modifying signatures.
- Never merge when CI is failing — hard gate, no exceptions.
- Security-first: audit diff for secrets, injection, and auth bypasses before every commit.

**Researcher Overlay (~120 tokens):**
- Save significant research to memory vault with `tags:` frontmatter for future retrieval.
- Full exploration before system-level changes — read-everything pass, structured findings, get alignment first.
- Measure before designing — quantify the actual problem before proposing architecture.
- Pushback-first — when a suggestion has engineering risks, lead with the tradeoff before presenting it as an option.

**Student Overlay (~120 tokens, plus optional add-ons):**
- 3-minute rule: if stuck for 3 min with no path forward — look at the solution, understand every step, close it, rewrite from scratch.
- Retrieval practice over re-reading: quiz first, explain after. Every act of retrieval is the learning.
- Spaced review: next day → 3 days → 1 week → 2 weeks.
- Interleave problem types — don't block. Demand the reason for every step.
- Explain with specific example first, then generalize. Never just state the formula.

**Autonomous Operator Overlay (~150 tokens, extends Base parallelism):**
- Full-pipeline parallelism: when parallel branches have no dependency, each pipeline (implement → review → PR → CI → merge) runs independently end-to-end. Never batch-wait for siblings.
- Start long-running tasks in background immediately without asking. Say "started in background" and return control.
- Deep research: fan out parallel research agents from the start, synthesize after all return.
- Cache-first: before producing anything new (bench runs, research, analysis), check what already exists and is still valid. Only regenerate what changed.
- Memory-search before implementing: query vault for prior decisions and research before writing code.

### Seed Mapping

Seeds in `seeds/reflections.json` are pre-selected based on the user's role. The base seeds are always included.

| Role | Pre-selected seed IDs |
|------|----------------------|
| Base (always) | seed_wait_confirm, seed_parallel_everything, seed_diagnosis_first, seed_evaluate_alternatives |
| Developer | + seed_dirty_branch, seed_commit_approval, seed_explore_first, seed_minimal_impl, seed_no_scope_creep, seed_worktree_discipline, seed_one_concern_per_branch, seed_never_merge_failing_ci, seed_security_audit |
| Researcher | + seed_explore_first, seed_tradeoff_flag, seed_memory_search |
| Student | + (base seeds only — no student-specific seeds yet) |
| Autonomous | + seed_background_tasks, seed_memory_search |

## 9. First Steps

Tell the user: "Deus is ready. Here are your next steps:"

### Add a messaging channel

Tell the user: "Deus needs at least one messaging channel to communicate through. Add one now:"

Present these options:
- `/add-whatsapp` — WhatsApp (QR code or pairing code authentication)
- `/add-telegram` — Telegram (bot token from @BotFather)
- `/add-slack` — Slack (Socket Mode, no public URL needed)
- `/add-discord` — Discord (bot token)

Tell the user: "Run one of these commands to add your first channel. Each channel skill handles authentication, registration, and smoke testing. You can add more channels later."

### Quick wins

**Quick Win 1 — Import knowledge from your previous AI tools**

Tell the user: "If you've been using ChatGPT, Gemini, or Claude.ai, your history there is a goldmine. Paste this prompt to any of them and send the result to Deus:"

Present this prompt in a code block for the user to copy:

```
I'm setting up a new AI assistant. Please write a detailed personal profile of me based on our conversations. Include: who I am (profession, role, location if known), my current projects and ongoing work, my technical background and expertise areas, my communication style and preferences, topics I bring up regularly, how I like problems approached and explained, any personal context that's relevant, and anything else that would help a new assistant skip the "getting to know you" phase. Be thorough — this will be used to onboard my new assistant. Format it as a first-person profile I can paste directly.
```

Tell the user: "Send that profile here in a message and I'll remember it."

**Quick Win 2 — Tell Deus about your current project**

Tell the user: "Send a message like: 'I'm working on [project name]. It's [brief description]. The main challenge right now is [X].' Deus will remember this and you won't have to re-explain context every session."

**Quick Win 3 — Start with something real**

Tell the user: "Don't start with test messages. Give Deus a real task from your actual work — a bug to fix, a question you've been sitting on, a document to draft. That's how the memory and evolution loop start building useful patterns."

## Troubleshooting

**Service not starting:** Check `logs/deus.error.log`. Common: wrong Node path (re-run step 6), missing `.env` (step 4).

**Container agent fails ("Claude Code process exited with code 1"):** Ensure Docker is running — `open -a Docker` (macOS) or `sudo systemctl start docker` (Linux). Check container logs in `groups/main/logs/container-*.log`.

**No response to messages:** Check trigger pattern. Main channel doesn't need prefix. Check DB: `npx tsx setup/index.ts --step verify`. Check `logs/deus.log`.

**Channel not connecting:** Verify the channel's credentials are set in `.env`. Channels auto-enable when their credentials are present. For WhatsApp: check `store/auth/creds.json` exists. For token-based channels: check token values in `.env`. Restart the service after any `.env` change.

**Unload service:** macOS: `launchctl unload ~/Library/LaunchAgents/com.deus.plist` | Linux: `systemctl --user stop deus`

**KB maintenance:** Setup also installs a daily maintenance job (04:30) that runs prune, decay, health, memory_gc, and weekly digest/compile tasks. macOS: `com.deus.maintenance` launchd agent | Linux: `deus-maintenance.timer` systemd timer | Windows: `DeusMaintenance` scheduled task. Logs at `logs/maintenance.log`. Unload: macOS `launchctl unload ~/Library/LaunchAgents/com.deus.maintenance.plist` | Linux `systemctl --user stop deus-maintenance.timer`
