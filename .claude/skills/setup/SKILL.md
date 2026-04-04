---
name: setup
description: Run initial Deus setup. Use when user wants to install dependencies, authenticate messaging channels, register their main channel, or start the background services. Triggers on "setup", "install", "configure deus", or first-time setup requests.
---

# Deus Setup

Run setup steps automatically. Only pause when user action is required (channel authentication, configuration choices). Setup uses `bash setup.sh` for bootstrap, then `npx tsx setup/index.ts --step <name>` for all other steps. Steps emit structured status blocks to stdout. Verbose logs go to `logs/setup.log`.

**Principle:** When something is broken or missing, fix it. Don't tell the user to go fix it themselves unless it genuinely requires their manual action (e.g. authenticating a channel, pasting a secret token). If a dependency is missing, install it. If a service won't start, diagnose and repair. Ask the user for permission when needed, then do the work.

**UX Note:** Use `AskUserQuestion` for all user-facing questions.

**CRITICAL:** Do NOT add git remotes (`git remote add`), fetch from external repos, or install npm packages from the public registry outside of step 0. All channel code and packages are local in the repo. If something seems missing, check `packages/` and `src/channels/` before looking externally.

## 0. Git & Fork Setup

Check the git remote configuration to ensure the user has a proper setup for receiving updates.

Run:
- `git remote -v`

Determine which case applies based on the origin URL:

**Case A — `origin` points to `qwibitai/nanoclaw` (user cloned the upstream directly):**

The user cloned instead of forking. AskUserQuestion: "You cloned Deus directly. We recommend forking so you can push your customizations. Would you like to set up a fork?"
- Fork now (recommended) — walk them through it
- Continue without fork — they'll only have local changes

If fork: instruct the user to fork `qwibitai/nanoclaw` on GitHub (they need to do this in their browser), then ask them for their GitHub username. Run:
```bash
git remote rename origin upstream
git remote add origin https://github.com/<their-username>/nanoclaw.git
git push --force origin main
```
Verify with `git remote -v`.

If continue without fork: add upstream so they can still pull updates:
```bash
git remote add upstream https://github.com/qwibitai/nanoclaw.git
```

**Case B — `origin` points to a non-qwibitai repo, no `upstream` remote:**

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

- If HAS_AUTH=true → WhatsApp is already configured, note for step 5
- If HAS_REGISTERED_GROUPS=true → note existing config, offer to skip or reconfigure
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

**Start the container build in the background** — it takes 3-5 minutes (up to 10 on Windows first run) and doesn't need user input. Continue with steps 4-6 while it runs.

Run in background with **10 minute timeout**: `npx tsx setup/index.ts --step container -- --runtime docker`

**Do NOT wait for this to finish.** Immediately continue to step 4. You will check the result before step 7.

**IMPORTANT — if build fails later:** Read the FULL error output before retrying. Common causes:
- TypeScript compilation errors from skill agents → check which skill was staged and if it's compatible
- Timeout → re-run with longer timeout, Docker layers are cached so retry is faster
- Do NOT prune Docker cache unless you're certain the cache itself is the problem

## 4. Claude Authentication (No Script)

If HAS_ENV=true from step 2, read `.env` and check for `ANTHROPIC_API_KEY`. If present, confirm with user: keep or reconfigure?

AskUserQuestion: Claude subscription (Pro/Max) vs Anthropic API key?

**Subscription (OAuth):** The credential proxy reads `~/.claude/.credentials.json` directly — no `.env` entry needed. Just ensure the user is logged in: `claude` (launches Claude Code, which authenticates). Do NOT add `CLAUDE_CODE_OAUTH_TOKEN` to `.env` — writing it there freezes it and causes a login loop when the token auto-rotates.

**API key:** Tell user to add `ANTHROPIC_API_KEY=<key>` to `.env`.

## 5. Set Up Channels

AskUserQuestion (multiSelect): Which messaging channels do you want to enable?
- WhatsApp (authenticates via QR code or pairing code)
- Telegram (authenticates via bot token from @BotFather)
- Slack (authenticates via Slack app with Socket Mode)
- Discord (authenticates via Discord bot token)

**Delegate to each selected channel's own skill.** Each channel skill handles its own code installation, authentication, registration, and JID resolution. This avoids duplicating channel-specific logic and ensures JIDs are always correct.

For each selected channel, invoke its skill:

- **WhatsApp:** Invoke `/add-whatsapp`
- **Telegram:** Invoke `/add-telegram`
- **Slack:** Invoke `/add-slack`
- **Discord:** Invoke `/add-discord`

Each skill will:
1. Install the channel code (via `git merge` of the skill branch)
2. Collect credentials/tokens and write to `.env`
3. Authenticate (WhatsApp QR/pairing, or verify token-based connection)
4. Register the chat with the correct JID format
5. Build and verify

**After all channel skills complete**, install dependencies and rebuild — channel merges may introduce new packages:

```bash
npm install && npm run build
```

If the build fails, read the error output and fix it (usually a missing dependency). Then continue to step 6.

## 6. Mount Allowlist

AskUserQuestion: Agent access to external directories?

**No:** `npx tsx setup/index.ts --step mounts -- --empty`
**Yes:** Collect paths/permissions. `npx tsx setup/index.ts --step mounts -- --json '{"allowedRoots":[...],"blockedPatterns":[],"nonMainReadOnly":true}'`

## 6b. Wait for Container Build

**Before proceeding to step 7, check the container build from step 3b.**

If the background build is still running, wait for it to finish. Parse the status block.

**If BUILD_OK=false:** Read `logs/setup.log` tail for the build error.
- Cache issue (stale layers): `docker builder prune -f`. Retry.
- Dockerfile syntax or missing files: diagnose from the log and fix, then retry.

**If TEST_OK=false but BUILD_OK=true:** The image built but won't run. Check logs — common cause is runtime not fully started. Wait a moment and retry the test.

## 7. Start Service

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

**If PLATFORM=windows and FALLBACK=batch (and user skipped NSSM/Task Scheduler):** A `start-deus.bat` launcher was generated for the background service. Tell user: the service can be started with `.\start-deus.bat` or by double-clicking it. For auto-start on login, add a shortcut to `shell:startup`. The `deus` CLI command will be set up in step 7b.

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

## 7b. Register CLI Command

Run `npx tsx setup/index.ts --step cli` and parse the status block.

This creates a global `deus` command so the user can type `deus` from any terminal.

- macOS/Linux: symlinks `deus-cmd.sh` → `~/.local/bin/deus`
- Windows: creates `deus.cmd` shim → `%USERPROFILE%\.local\bin\` and adds it to user PATH

**If IN_PATH=false:** The setup step auto-appends `~/.local/bin` to the user's shell config. If it couldn't (permissions, etc.), tell user to add it manually:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc  # or ~/.bashrc
```

**After CLI registration:** Tell user they can type `deus` from any terminal after reopening their shell (or running `source ~/.zshrc` / `source ~/.bashrc` to apply immediately).

## 8. Verify

Run `npx tsx setup/index.ts --step verify` and parse the status block.

**If STATUS=failed, fix each:**
- SERVICE=stopped → `npm run build`, then restart:
  - macOS: `launchctl kickstart -k gui/$(id -u)/com.deus`
  - Linux: `systemctl --user restart deus`
  - Windows (NSSM): `nssm restart deus`
  - Windows (Servy): `servy-cli restart --name=deus`
  - WSL nohup fallback: `bash start-deus.sh`
- SERVICE=not_found → re-run step 7
- CREDENTIALS=missing → re-run step 4
- CHANNEL_AUTH shows `not_found` for any channel → re-invoke that channel's skill (e.g. `/add-telegram`)
- REGISTERED_GROUPS=0 → re-invoke the channel skills from step 5
- MOUNT_ALLOWLIST=missing → `npx tsx setup/index.ts --step mounts -- --empty`

Tell user to test: send a message in their registered chat. Show: `tail -f logs/deus.log`

## 9. Personality Kickstarter (Optional)

AskUserQuestion: "Deus works best when it knows your preferences. Want to load battle-tested defaults from real usage?" Options: "Yes, show me" / "Skip"

**If Skip:** Continue to step 10.

**If Yes, show me:** Run steps 9a → 9b → 9c in order.

---

### 9a. Bundles

AskUserQuestion (multiSelect): "Which default bundles would you like to enable? Pick any combination." Options:
- "Bundle A — Universal Defaults (recommended for everyone)"
- "Bundle B — Developer Workflow (for users who code with Deus)"
- "Bundle C — Student/Learner Mode (for users who study with Deus)"
- "None — skip to individual behaviors"

For each selected bundle (A, B, or C), display its bullet list to the user, then ask:

AskUserQuestion: "Here are the items in [Bundle Name]. Anything to add, remove, or rephrase? Describe changes or say 'looks good'."

Apply any edits the user requests to the bullet list before writing. The user is the final author — only write what they approve.

Read `groups/main/CLAUDE.md`. If the file does not exist, create it. Append each selected (and edited) bundle under a `## Behavioral Defaults` heading. If the heading already exists, append after the last item under it; otherwise add it at the end of the file.

**Bundle A — Universal Defaults:**
- Never execute after asking a confirmation question — stop and wait for explicit response. No exceptions for destructive or irreversible actions.
- Long-running tasks (>30s) start in the background immediately. Say "started in background" and return control. Don't ask first.
- Default to the simplest solution. Don't add features, abstraction, or complexity beyond what was asked. Don't add docstrings, comments, or type annotations to code you didn't change.
- Push back and verify before implementing. If something has a non-obvious tradeoff, flag it and discuss before acting.
- Session start: give a 2-bullet catch-up ("Previous session: ..." + "Pending: ...") then wait.

**Bundle B — Developer Workflow:**
- At the start of every new feature or task: run `git status`, confirm the working tree is clean, then create a dedicated feature branch. Never start work on main directly.
- Every code change cycle: Plan (brief) → Branch → Implement → Verify/test → Propose commit message → Wait for approval → Commit. Never commit without explicit approval.
- When debugging: read the full pipeline end-to-end before touching anything. Follow data flow across file/language boundaries. Grep all consumers before modifying a function signature.
- For system exploration: do a full read-everything pass first, synthesize into structured findings, get agreement on priorities before writing code.

**Bundle C — Student/Learner Mode:**
- 3-minute rule: if stuck for 3 min with no path forward — look at the solution, understand every step, close it, rewrite from scratch.
- Retrieval practice over re-reading: quiz first, explain after. Every act of retrieval is the learning.
- Spaced review schedule: next day → 3 days → 1 week → 2 weeks.
- Interleave problem types — don't block. Demand the reason for every step.
- Explain with specific example first, then generalize. Never just state the formula.

---

### 9b. À la carte behaviors

Present these as a multi-select — independent of bundle selection. Each is a single rule the user can add on top of whatever bundles they chose (or instead of any bundle).

AskUserQuestion (multiSelect): "Any of these individual behaviors to add? Pick any that apply." Options:
- "Image analysis — always route images to a vision model (Gemini) first, never analyze inline"
- "Research saving — save significant research results to vault with searchable tags frontmatter"
- "Deploy integrity — rebuild dist/ before restarting; never write rotating credentials (OAuth tokens) to .env"
- "Deep research workflow — full codebase exploration pass + structured findings before implementing any system-level change"
- "Code hygiene — only touch code you were asked to change; no docstrings, comments, or annotations added to surrounding functions"
- "None"

For each selected behavior, append its rule under `## Behavioral Defaults` in `groups/main/CLAUDE.md`:

- **Image analysis:** Always route image/screenshot analysis to a vision model (e.g. Gemini) first. Do not analyze images inline.
- **Research saving:** Any significant research result (architecture comparisons, platform decisions, tool evaluations) must be saved to the memory vault with `tags:` frontmatter for future retrieval.
- **Deploy integrity:** Always rebuild `dist/` before restarting any service. Never write auto-rotating credentials (OAuth tokens, session tokens) to `.env` — doing so freezes the token and causes login loops on auto-refresh.
- **Deep research workflow:** Before implementing any system-level change, run a full codebase exploration (Explore agent) and synthesize structured findings. Get alignment before writing code.
- **Code hygiene:** Only modify code you were asked to modify. Don't add docstrings, comments, or type annotations to surrounding functions. Don't clean up adjacent code.

---

### 9c. Evolution seed reflections

Seeds pre-warm the self-improvement loop so it isn't starting cold. Each seed is a past-learned lesson (corrective or positive) that will be retrieved and applied in relevant future conversations.

First, check if the evolution package is available:
```bash
python3 -c "from evolution.reflexion.store import save_reflection; print('ok')" 2>/dev/null
```
If this fails, tell the user "Evolution package not set up — skipping seed import." and continue to step 10.

If available, read `seeds/reflections.json` and display a numbered list to the user: show each seed's `summary` and `category` (not the full content, to keep it scannable).

AskUserQuestion (multiSelect): "Which of these seed reflections would you like to import? Deselect any that don't apply to your workflow."

After selection, ask:

AskUserQuestion: "Any seed you'd like to edit before importing? Enter its number(s) (comma-separated), or say 'none'."

For each seed the user wants to edit, show its full `content` and ask for the replacement text. Use their text verbatim.

Then import the final set:
```bash
python3 scripts/import_seeds.py --seeds '<json_array_of_final_seeds>'
```

Report the result: "Imported N reflections (M skipped as near-duplicates)."

---

After all three sub-steps, tell the user: "Defaults saved. You can edit `groups/main/CLAUDE.md` anytime to add, remove, or rephrase any rule."

## 10. First Steps

Tell the user: "Deus is ready. Here are three quick wins to get the most out of it fast:"

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

**Service not starting:** Check `logs/deus.error.log`. Common: wrong Node path (re-run step 7), missing `.env` (step 4), missing channel credentials (re-invoke channel skill).

**Container agent fails ("Claude Code process exited with code 1"):** Ensure Docker is running — `open -a Docker` (macOS) or `sudo systemctl start docker` (Linux). Check container logs in `groups/main/logs/container-*.log`.

**No response to messages:** Check trigger pattern. Main channel doesn't need prefix. Check DB: `npx tsx setup/index.ts --step verify`. Check `logs/deus.log`.

**Channel not connecting:** Verify the channel's credentials are set in `.env`. Channels auto-enable when their credentials are present. For WhatsApp: check `store/auth/creds.json` exists. For token-based channels: check token values in `.env`. Restart the service after any `.env` change.

**Unload service:** macOS: `launchctl unload ~/Library/LaunchAgents/com.deus.plist` | Linux: `systemctl --user stop deus`
