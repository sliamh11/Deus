---
name: setup
description: Run initial Deus setup. Use when user wants to install dependencies, authenticate messaging channels, register their main channel, or start the background services. Triggers on "setup", "install", "configure deus", or first-time setup requests.
---

# Deus Setup

Run setup steps automatically. Only pause when user action is required (channel authentication, configuration choices). Setup uses `bash setup.sh` for bootstrap, then `npx tsx setup/index.ts --step <name>` for all other steps. Steps emit structured status blocks to stdout. Verbose logs go to `logs/setup.log`.

**Principle:** When something is broken or missing, fix it. Don't tell the user to go fix it themselves unless it genuinely requires their manual action (e.g. authenticating a channel, pasting a secret token). If a dependency is missing, install it. If a service won't start, diagnose and repair. Ask the user for permission when needed, then do the work.

**UX Note:** Use `AskUserQuestion` for all user-facing questions.

## 0. Git & Fork Setup

Check the git remote configuration to ensure the user has a fork and upstream is configured.

Run:
- `git remote -v`

**Case A — `origin` points to `qwibitai/nanoclaw` (user cloned directly):**

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

**Case B — `origin` points to user's fork, no `upstream` remote:**

Add upstream:
```bash
git remote add upstream https://github.com/qwibitai/nanoclaw.git
```

**Case C — both `origin` (user's fork) and `upstream` (qwibitai) exist:**

Already configured. Continue.

**Verify:** `git remote -v` should show `origin` → user's repo, `upstream` → `qwibitai/nanoclaw.git`.

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
  - Windows: Docker Desktop auto-starts. Check the system tray icon — if it's not there, launch "Docker Desktop" from Start. Wait 15s then re-check with `docker info`.
  - Wait 15s, re-check with `docker info`.
- DOCKER=not_found → Use `AskUserQuestion: Docker is required for running agents. Would you like me to install it?` If confirmed:
  - macOS: install via `brew install --cask docker`, then `open -a Docker` and wait for it to start. If brew not available, direct to Docker Desktop download at https://docker.com/products/docker-desktop
  - Linux: install with `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`. Note: user may need to log out/in for group membership.
  - Windows: direct to Docker Desktop download at https://docker.com/products/docker-desktop. Requires WSL 2 (auto-offered by Docker installer). After install, start Docker Desktop from Start menu.

### 3b. Build and test

Run `npx tsx setup/index.ts --step container -- --runtime docker` and parse the status block.

**If BUILD_OK=false:** Read `logs/setup.log` tail for the build error.
- Cache issue (stale layers): `docker builder prune -f`. Retry.
- Dockerfile syntax or missing files: diagnose from the log and fix, then retry.

**If TEST_OK=false but BUILD_OK=true:** The image built but won't run. Check logs — common cause is runtime not fully started. Wait a moment and retry the test.

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

## 7. Start Service

If service already running: stop first.
- macOS: `launchctl unload ~/Library/LaunchAgents/com.deus.plist`
- Linux: `systemctl --user stop deus` (or `systemctl stop deus` if root)
- Windows (NSSM): `nssm stop deus`
- Windows (Servy): `servy-cli stop --name=deus`

Run `npx tsx setup/index.ts --step service` and parse the status block.

**If FALLBACK=wsl_no_systemd:** WSL without systemd detected. Tell user they can either enable systemd in WSL (`echo -e "[boot]\nsystemd=true" | sudo tee /etc/wsl.conf` then restart WSL) or use the generated `start-deus.sh` wrapper.

**If PLATFORM=windows and FALLBACK=batch:** Windows without NSSM/Servy — a `start-deus.bat` launcher was generated. Tell user: run it from PowerShell as `.\start-deus.bat` or double-click it. For auto-start on login, add a shortcut to `shell:startup`.

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

**If Yes, show me:** Present the following three bundles as a multi-select AskUserQuestion. The user can pick any combination.

AskUserQuestion (multiSelect): "Which default bundles would you like to enable?" Options:
- "Bundle A — Universal Defaults (recommended for everyone)"
- "Bundle B — Developer Workflow (for users who code with Deus)"
- "Bundle C — Student/Learner Mode (for users who study with Deus)"

Read `groups/main/CLAUDE.md` first to see the current contents. If the file does not exist, create it. Append selected bundle content under a `## Behavioral Defaults` heading — create the heading at the end of the file if it is not already present.

**Bundle A — Universal Defaults** content to append under `## Behavioral Defaults`:
- Never execute after asking a confirmation question — stop and wait for explicit response. No exceptions for destructive/irreversible actions.
- Long-running tasks (>30s) start in the background immediately. Say "started in background" and return control. Don't ask first.
- Default to the simplest solution. Don't add features, abstraction, or complexity beyond what was asked.
- Push back and verify before implementing. If something has a non-obvious tradeoff, flag it and discuss before acting.
- Session start: give a 2-bullet catch-up ("Previous session: ..." + "Pending: ...") then wait.

**Bundle B — Developer Workflow** content to append under `## Behavioral Defaults`:
- Before implementing any planned change: verify git working tree is clean, create a feature branch, then implement.
- Every code change cycle: Plan (brief) → Branch → Implement → Verify/test → Propose commit message → Wait for approval → Commit.
- When debugging: read the full pipeline end-to-end before touching anything. Follow data flow across file/language boundaries. Grep all consumers before modifying a function signature.
- For system exploration: do a full read-everything pass first, synthesize into structured findings, get agreement on priorities before writing code.

**Bundle C — Student/Learner Mode** content to append under `## Behavioral Defaults`:
- 3-minute rule: if stuck for 3 min with no path forward — look at the solution, understand every step, close it, rewrite from scratch.
- Retrieval practice over re-reading: quiz first, explain after. Every act of retrieval is the learning.
- Spaced review schedule: next day → 3 days → 1 week → 2 weeks.
- Interleave problem types — don't block. Demand the reason for every step.
- Explain with specific example first, then generalize. Never just state the formula.

**How to append:** If `## Behavioral Defaults` heading already exists in the file, append the bullet points after the last item under that heading. If the heading does not exist, append it and the selected bullets at the end of the file.

After updating `groups/main/CLAUDE.md`, tell the user: "Defaults saved to your main agent. You can edit groups/main/CLAUDE.md anytime to customize."

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
