# Multi-model Claude Code example (CLIProxyAPI front end)

Status: **draft/example only.** Not wired into the live Deus `.claude/agents/`
roster — copy the `agents/*.md` files into `.claude/agents/` yourself once
you've burn-in tested this, per the risks below.

## What this demonstrates

- A Claude Code session whose *default* model is GPT 5.6 Luna at max
  reasoning effort, switchable to anything else — regardless of which
  company serves it — using Claude Code's native `/model` command.
- Four subagent roles each pinned to a specific model via frontmatter,
  independent of whatever the session default currently is:
  `plan-writer` → Opus 5, `implementer` → GPT 5.6 Luna (max effort),
  `reviewer-sol` + `reviewer-opus` → GPT 5.6 Sol and Opus 5 (both required).
- A mixed-auth setup: Opus 5 goes through a real, official Anthropic API key
  (safe, ToS-normal); the OpenAI/Codex leg goes through CLIProxyAPI's
  OAuth-login (reuses a Codex/ChatGPT subscription instead of paying
  per-token) — a deliberate, accepted risk tradeoff, not a default
  recommendation. See "Known open risks" below before using it.

This works because Claude Code, once pointed at a custom
`ANTHROPIC_BASE_URL`, skips its own model-name validation and passes
whatever string it's given (via `/model`, `ANTHROPIC_MODEL`, or a
subagent's `model:` frontmatter) straight through to that endpoint.
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) sits behind that
endpoint and routes each alias to whichever real provider/credential you've
mapped it to in `config.yaml`.

## Setup

1. Build or download CLIProxyAPI (see the upstream repo's releases/instructions).
2. Copy the config so real secrets never land in a tracked file:
   ```
   cp config.yaml config.local.yaml
   ```
   `config.local.yaml` is already listed in this directory's `.gitignore` —
   `config.yaml` itself stays placeholder-only and committed.
3. In `config.local.yaml`, fill in:
   - `api-keys`: a value of your own choosing (Claude Code will authenticate
     to the proxy with this).
   - `claude-api-key[0].api-key`: your real, official Anthropic API key.
   - Confirm the real upstream model ID strings under `oauth-model-alias.codex`
     for your account/plan — `gpt-5.6-luna` / `gpt-5.6-sol` are illustrative
     names from the design conversation this example came from, **not**
     verified real Codex/OpenAI model IDs.
4. Authenticate the Codex leg once (opens a browser for OAuth):
   ```
   ./cli-proxy-api --codex-login --config config.local.yaml
   ```
   Running headless/remote instead of on a local desktop? Use `--no-browser`
   (a real CLIProxyAPI flag) and follow the printed URL manually.
5. Confirm `echo $CLAUDE_CODE_SUBAGENT_MODEL` prints **empty** in the shell
   you'll launch Claude Code from. Per Claude Code's docs, subagent model
   resolution order is: `CLAUDE_CODE_SUBAGENT_MODEL` env var → per-invocation
   `model` param → subagent frontmatter `model:` → session default. If that
   env var is set anywhere (shell profile, another tool), it silently
   overrides all four pinned roles above with a single model — no error,
   just quietly wrong behavior.
6. Start the proxy:
   ```
   ./cli-proxy-api --config config.local.yaml
   ```
7. In the Claude Code session:
   ```
   export ANTHROPIC_BASE_URL=http://localhost:8317
   export ANTHROPIC_API_KEY=<the api-keys value from config.local.yaml>
   export ANTHROPIC_MODEL=luna-max                 # default model for this example
   claude
   ```
8. Mid-session, switch the default freely, regardless of creator company:
   ```
   /model opus-planner
   /model sol
   /model luna-max
   ```
9. Copy `agents/*.md` into `.claude/agents/` to make
   `plan-writer` / `implementer` / `reviewer-sol` / `reviewer-opus` available
   as pinned-model subagent types via the `Agent` tool.

## Verify the files themselves before relying on them

```
python3 -c "import yaml; yaml.safe_load(open('config.yaml'))"
```

and a manual read-through of each `agents/*.md` file confirming `name` and
`description` frontmatter fields are present (Claude Code's required
subagent frontmatter fields).

## Known open risks — verify before trusting this on real work

- **Anthropic explicitly disclaims this use case, independent of whether
  it's configured correctly.** From
  https://code.claude.com/docs/en/llm-gateway : *"Anthropic doesn't
  endorse, maintain, or audit third-party gateway products, and doesn't
  support routing Claude Code to non-Claude models through any gateway."*
  This is a support-scope statement, not a configuration detail — even a
  perfectly-working proxy setup is running outside what Anthropic supports.
- **The Codex OAuth leg is a real, irreversible-if-it-fires risk, not a
  compatibility gap like the other risks on this list.** CLIProxyAPI's
  `--codex-login` obtains a Codex/ChatGPT-subscription token and reuses it
  for a separate HTTP client. This repo's own prior research,
  `docs/decisions/hermes-cli-subprocess-provider-design.md` (LIA-525),
  found this exact shape — extracting a CLI's OAuth token and feeding it
  to a separate HTTP client — risky because Anthropic's OAuth policy
  restricts these tokens to native apps, versus spawning the genuine CLI
  binary as a subprocess (safe, because it authenticates as itself with
  no re-signed traffic). That safe alternative doesn't transfer here:
  Claude Code's only extension point for this is an HTTP
  `ANTHROPIC_BASE_URL`, not a subprocess-spawn hook, so there is no safe
  path available for this leg specifically — only the accepted-risk one.
  (The ADR's finding is grounded in Anthropic's own policy; whether OpenAI
  enforces an equivalent restriction on Codex/ChatGPT tokens was not
  independently checked.)
  On top of the token-reuse shape itself, CLIProxyAPI's default-on header
  forcing (`disable-codex-cloaking: false`, forcing the official Codex
  User-Agent/Originator headers — verified in CLIProxyAPI's own
  `config.example.yaml`) is a separate, more active layer the ADR doesn't
  cover: it disguises the re-served traffic to look like the genuine
  Codex CLI, not just reuses the token. Real, dated account-ban reports
  exist in CLIProxyAPI's own issue tracker — but note the specific
  providers: #2211 is a Claude subscription ban, and #1814, #1803, and
  discussion #1882 are Google-login, Gemini-CLI, and Antigravity
  suspensions, respectively. No Codex-specific ban report was found;
  these are cited as evidence the same OAuth-re-serving pattern gets
  enforced across multiple providers, not as direct Codex precedent.
  Unlike every other risk in this list, there's no rollback if it fires —
  it's an account suspension, not a config fix.
- **Placeholder model IDs**: `gpt-5.6-luna` / `gpt-5.6-sol` are illustrative
  names from the design conversation this example came from, not confirmed
  real Codex/OpenAI model IDs — confirm before real use. (CLIProxyAPI's
  config surface itself — the keys and flags used in this example — was
  independently verified directly against its source during design, unlike
  the earlier LiteLLM draft's config surface, which was flagged unverified.)
- **Undocumented mid-session behavior**: whether a subagent set to
  `model: inherit` picks up a `/model` change made *after* it was already
  dispatched is not documented one way or the other. Test empirically.
- **Unverified tool-calling fidelity**: whether non-Claude models behave
  reliably inside Claude Code's tool-use harness (Edit/Bash/apply_patch
  schemas, multi-step agentic loops) is unverified for the specific models
  named here. Burn-in test on a low-stakes task — including whether gate
  compliance survives (does the driving model actually invoke
  `plan-reviewer` at the right moment, accept a REVISE, etc.) — before
  relying on this for real gate-compliant workflows.
- **Never commit a filled-in config.** `config.yaml` in this directory is
  placeholder-only and safe to commit. Real secrets go in `config.local.yaml`
  (gitignored, see Setup step 2) — never edit `config.yaml` in place with
  real values.

## Note

This is entirely about Claude Code's own native model-routing feature. It
is unrelated to Deus's own backend-neutral agent runtime (separate
container-agent backends like Claude Code vs. OpenAI/Codex) — easy to
conflate by name, but a genuinely separate mechanism.
