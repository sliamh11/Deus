---
name: add-connector
description: Onboard a `deus connect` connector — a way to route Claude Code sessions to non-Claude models (e.g. GPT via CLIProxyAPI) alongside normal Claude access. Generic orchestrator over whichever connector is selected.
disable-model-invocation: true
---

# Add a `deus connect` Connector

`deus connect` launches the real, unmodified `claude` binary with which
upstream model answers its requests redirected via `ANTHROPIC_BASE_URL` —
identity, vault/memory context, preferences, and portable skills stay
intact, exactly like a normal `deus`/`deus home` session. This is separate
from Deus's own container-agent backend adapters (`deus codex`,
`DEUS_AGENT_BACKEND`) — see
`docs/decisions/backend-neutral-agent-runtime.md`'s note near the Parity
Matrix.

This skill is generic over any registered connector (ships with
`cliproxy-oauth` and `ollama`) — adding another connector in the future
changes `connectors/providers/`, not this skill.

**Four distinct "ollama" surfaces exist in this repo — do not conflate
them when guiding a user:**
1. **`deus connect ollama`** (this skill, this section) — routes a Claude
   Code session directly to a local Ollama instance via its native
   Anthropic-API mode. No proxy, no daemon management.
2. **`deus provider ollama` / `deus fcc`** (`deus-cmd.sh`'s `fcc` proxy
   system) — a *different* mechanism: routes through `fcc-server`'s
   OpenAI-compat translation proxy. Also reaches a local Ollama install,
   but via proxy translation, not a direct native-mode redirect.
3. **`deus backend set ollama`** — a persisted `DEUS_AGENT_BACKEND` value.
   Not yet wired to a working CLI agent; its stub error message points
   here (`deus connect ollama`) and to `deus provider ollama`/`fcc`.
4. **`.claude/skills/add-ollama-tool`** — an unrelated feature: adds an
   Ollama MCP server so a *container agent* (not a `deus connect` session)
   can offload tasks to a local model.

## Phase 1: Pre-flight

```bash
python3 scripts/connectors_cli.py list
```

Shows every registered connector and whether it's already configured. If
the target connector shows `configured` (e.g. a repeat run, or a machine
inherited from another user), still walk through Phase 2 (select) and
Phase 3 (risk acknowledgment) — Phase 3 is required every time, regardless
of configuration state, per its own rule; skipping it here would let an
already-configured install proceed straight to a real connector launch
without ever showing the risks. Only Phases 4-9 (install/authenticate/
write-config) are skippable on an already-configured connector — jump from
Phase 3 straight to Phase 10 (Verify) unless the user explicitly wants to
reconfigure.

**`cliproxy-oauth`-specific one-time check, added for the model-picker-
visibility follow-up — this is a deliberate, documented exception to this
skill's normal connector-generic design, not a pattern to copy for a
future connector.** An already-configured `cliproxy-oauth` install
predating this feature has no `claude-gpt-*` discovery aliases in its real
config, and the normal skip straight to Phase 10 would never surface the
migration step needed to add them. Before jumping to Phase 10 on an
already-configured `cliproxy-oauth`, run:
```bash
if [ -f ~/.config/deus/connectors/cliproxy/config.local.yaml ]; then
  grep -c 'claude-gpt-' ~/.config/deus/connectors/cliproxy/config.local.yaml
else
  echo 0
fi
```
If the count is below 3 (a fresh install has 0; a partially-applied manual
edit could have 1 or 2 — `-c` counts matches rather than a bare presence
check specifically so a partial edit isn't mistaken for a complete one),
walk through Phase 7's "Already configured? Add discovery aliases
manually" subsection before continuing to Phase 10 — every other Phase
4-9 step still stays skipped.

## Phase 2: Which connector

AskUserQuestion: Which connector do you want to set up?
- `cliproxy-oauth` — CLIProxyAPI, OAuth-login, reuses your ChatGPT/Codex
  subscription for GPT models alongside Claude.
- `ollama` — routes to a locally-pulled Ollama model via its native
  Anthropic-API mode. No OAuth, no proxy daemon (Ollama runs its own
  service already).

## Phase 3: Required risk acknowledgment

**Not a footnote — block progression on explicit confirmation, every time,
for every future user.** Risks 2-5 below are structural to `deus connect`
itself and apply to every connector; risk 1 is `cliproxy-oauth`-specific
(OAuth-subscription reuse — Ollama has no OAuth token extraction, this risk
class does not apply to it), risk 6 is `cliproxy-oauth`-specific, and
risk 7 is `ollama`-specific. Show the set that matches the connector
selected in Phase 2:

> **`deus connect` risk disclosure:**
>
> 1. **(cliproxy-oauth only) OAuth-subscription reuse is a real, non-zero,
>    documented account-ban risk class.** This connector extracts an OAuth
>    token from your ChatGPT/Codex subscription and reuses it for a
>    separate HTTP client — see `examples/multi-model-cliproxyapi/README.md`'s
>    "Known open risks" for the full account, including real dated ban
>    reports in CLIProxyAPI's own issue tracker for other providers. Unlike
>    a config mistake, there is no rollback if this fires.
> 2. **Anthropic explicitly disclaims support** for routing Claude Code to
>    non-Claude models through any gateway
>    (https://code.claude.com/docs/en/llm-gateway) — a support-scope
>    statement independent of whether this is configured correctly.
> 3. **Connector sessions are discoverable via a bare `claude`'s resume
>    picker.** `--name` tags the session (`connect:<id> (non-Claude)`) so
>    it's identifiable, not invisible or inaccessible — native Claude Code
>    session-resume is not isolated between a connector session and a bare
>    one.
> 4. **A nested `claude`/`deus claude` launched from inside a connector
>    session inherits that session's model redirection.** Once
>    `ANTHROPIC_*` env vars are set for the launched session, any subprocess
>    it spawns (e.g. a `Bash` tool call running `claude` again) inherits
>    them — a plain Unix process-inheritance property, no mechanism exists
>    to prevent it. Narrow in practice (requires deliberately launching
>    another interactive session mid-connector-session).
> 5. **A connector session gets the same no-prompt tool execution as a
>    trusted Claude session, driven by a model outside Anthropic's support
>    scope.** `deus connect` launches through the same `launch_claude`
>    every other session uses; if `bypass_permissions` is `true` in your
>    Deus preferences (the default), a connector session runs with
>    `--dangerously-skip-permissions` like any other Deus session — full,
>    unprompted tool execution, just with a non-Claude, unsupported model
>    driving it instead of Claude. Each connector's inline subagents are
>    scoped to `Read`/`Grep`/`Glob`/`Bash`/`WebSearch`/`WebFetch` (not the
>    session's full tool set), but the top-level connector session itself
>    is not additionally restricted beyond your normal Deus bypass setting.
> 6. **(cliproxy-oauth only) `/model` surfaces GPT model names directly in
>    Claude Code's native picker**, labeled "From gateway", rather than
>    only being reachable by typing an exact alias. Applies to every fresh
>    setup (the tracked template always includes the picker-discovery
>    aliases now — not an opt-in); an already-configured install only
>    gains this once its real config is migrated per Phase 7's "Already
>    configured" step. Not a new exposure either way — the same models
>    were already reachable via `ANTHROPIC_MODEL`/typed `/model` — this
>    just makes it easier to notice at a glance which model is actually
>    selected, which is a mitigation of confusion, not a new risk in its
>    own right.
> 7. **(ollama only) A small/weak local model can silently produce
>    lower-quality or subtly wrong tool-use sequences** — misread
>    instructions, bad tool-call arguments, incomplete reasoning — while
>    running under the exact same full bypass-permission trust as Claude
>    (risk 5). This is a different risk profile than `cliproxy-oauth`'s,
>    not a lesser version of it: it's about the model's own capability
>    silently degrading task correctness, not about where the model comes
>    from, and it carries no distinct warning signal beyond noticing the
>    output itself looks off.

AskUserQuestion: Confirm you understand and accept the risks above (risk 6
applies only to `cliproxy-oauth`; risk 7 applies only to `ollama`) before
continuing?
- Yes, continue
- No, cancel setup

Stop here if the user declines.

## Phase 4: Install the engine

**cliproxy-oauth:**
```bash
python3 scripts/connectors_cli.py install-check cliproxy-oauth
```

If `not installed`, tell the user:

> The `cli-proxy-api` binary isn't on your PATH. Build or download it from
> https://github.com/router-for-me/CLIProxyAPI (see the upstream repo's
> releases/instructions).
>
> After installation, verify: `cli-proxy-api --version`

**Require explicit confirmation before proceeding once installed — never
silently auto-fetch a binary that's about to hold real OAuth tokens.**

Re-run the install-check after the user confirms installation is done.

**ollama:**
```bash
python3 scripts/connectors_cli.py install-check ollama
```

Checks only that the `ollama` binary is on PATH — deliberately does NOT
probe whether the background service is actually running, since at this
point Phase 5 hasn't collected the user's real host yet and a live check
here could only ever reach the default `localhost:11434` (silently wrong
for a non-default-host setup, with no way to recover except repeating the
same failing check). Service liveness — against the real configured host —
is checked in Phase 10's `verify-setup`, after Phase 7 writes it. If
`not installed`, tell the user to install Ollama
(https://ollama.com/download); if it's installed but the service isn't
running (menu-bar app on macOS / systemd unit on Linux), Phase 10 will
catch that instead, with the real host. This connector never manages the
Ollama service itself — no daemon to start on the user's behalf, unlike
`cliproxy-oauth`'s launchd plist.

**ollama-only: context window prerequisite.** Ollama defaults its context
window by available VRAM — under 24 GiB VRAM defaults to 4k, well below
what a real Deus session needs (Ollama's own Claude Code guidance
recommends 64k+). Tell the user to raise `OLLAMA_CONTEXT_LENGTH` to at
least 64000 before continuing:
- **macOS menu-bar app**: Settings → context-length slider.
- **CLI/systemd-managed install**: `export OLLAMA_CONTEXT_LENGTH=65536`
  in the service's environment, then restart the service.

Phase 10's `verify-setup` checks this automatically (via `/api/ps`'s
`context_length`) and fails if it's still too small — but flag it now so
the user isn't surprised by a failed verify later.

## Phase 5: Collect config values

**cliproxy-oauth:**

- **Inbound key** — generate one rather than asking the user to invent it:
  `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. This is
  what Claude Code authenticates to the local proxy with.
- **Route Opus/Claude through the same proxy too?** Optional. If yes,
  collect a real, official Anthropic API key (not OAuth) for the
  `claude-api-key` leg.
- **Confirm the real upstream Codex/GPT model id strings.** The tracked
  placeholder (`connectors/cliproxy/config.yaml`) ships with illustrative
  names (`gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna`) — **not** confirmed
  real IDs for the user's account/plan. These get hand-edited into
  `oauth-model-alias.codex[].name` in the written config (Phase 7) — the
  `alias` values (`sol`/`terra`/`luna-max`, and their `claude-gpt-*` picker-
  discovery twins) stay fixed; only the `name` values (real upstream ids)
  vary per account. **Each `claude-gpt-*` entry's `name` must be set to the
  exact same value as its plain-alias twin** (e.g. `claude-gpt-sol`'s
  `name` = `sol`'s `name`) — a mismatch wouldn't error, CLIProxyAPI would
  just silently treat them as two different upstream models.
- **Default model** for the session itself when no `/model` switch has been
  made — recommend `luna-max` (max reasoning effort) unless the user
  prefers otherwise. This is separate from picker visibility: the plain
  aliases (not the `claude-gpt-*` twins) are what `ANTHROPIC_MODEL` uses.
- **Picker visibility for the 3 GPT models** — included automatically,
  not a choice to make here. The tracked template (Phase 7) already bakes
  in a `claude-`-prefixed alias + `display-name` per GPT model, and
  `write-config` always writes the full template verbatim — there's no
  partial-write mechanism to selectively drop these three entries, so
  don't present skipping them as an option. This is low-risk by design
  (see Phase 3 risk 6): purely additive, no real alias sacrificed, no new
  credential exposure. If a user genuinely wants `/model` to show nothing
  extra, they can remove the 3 `claude-gpt-*` entries from their real
  `config.local.yaml` by hand after setup — but that's a post-setup
  edit, not a Phase 5 choice.

**ollama:**

- **Which locally-pulled model backs `deus-ollama-local`?** Run
  `ollama list` and show the user the real, currently-pulled models —
  never invent a plausible-looking tag the way `cliproxy-oauth`'s
  placeholder does for GPT ids (that's necessary there because a remote
  account's real upstream id can't be locally enumerated; here it can, so
  there's no reason to guess). If nothing is pulled yet, help the user
  `ollama pull <model>` first.
- **Host** — default `http://localhost:11434`, only ask if the user runs
  Ollama on a non-default host/port.
- No inbound key, no OAuth leg, no launchd config — this connector doesn't
  need them.

## Phase 6: Authenticate

**cliproxy-oauth:**
```bash
python3 scripts/connectors_cli.py authenticate cliproxy-oauth
```

Runs `cli-proxy-api --codex-login --config <local config>` — opens a
browser for OAuth. Running headless/remote instead of a local desktop? The
underlying engine supports `--no-browser` with a printed URL; if that's
needed, tell the user to run the login step manually with that flag instead
of through this script, then continue to Phase 7.

**ollama:**
```bash
python3 scripts/connectors_cli.py authenticate ollama
```

No-op — always returns success. No OAuth/login concept for locally-pulled
models. (Ollama's optional hosted "cloud" model aliases require `ollama
signin`, out of scope for this connector's current scope.)

## Phase 7: Write config

**cliproxy-oauth (+ launchd daemon):**
```bash
echo '<json values>' | python3 scripts/connectors_cli.py write-config cliproxy-oauth
```

Where `<json values>` is a JSON object:
```json
{
  "inbound_key": "<generated key from Phase 5>",
  "anthropic_api_key": "<optional, only if the user opted in>",
  "model_map": {"deus-gpt-sol": "sol", "deus-gpt-terra": "terra", "deus-gpt-luna": "luna-max"},
  "default_model_alias": "luna-max",
  "binary_path": "<absolute path from: command -v cli-proxy-api>"
}
```

This writes the real config to
`~/.config/deus/connectors/cliproxy/config.local.yaml` — **outside any
project root a container agent could ever have mounted** (confirmed
against `src/project-registry.ts:155-174` +
`src/container-mounter.ts:76-102`: the tracked placeholder at
`connectors/cliproxy/config.yaml` stays in-repo and safe to commit; the
real values never do) — and the launchd plist
(`~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist`,
`RunAtLoad`+`KeepAlive`, macOS-only for now) with a fully home-expanded
absolute `--config` path (launchd execs directly with literal argv
strings, never shell-expanding `~`).

**If `write-config` errors with "A launchd job already exists at ... with
different settings"**: something else — possibly the user's own,
unrelated launchd job — already occupies that exact path. This is a
refusal-to-overwrite safety check (`_write_launchd_plist`), not a bug.
Show the user the error's own `ProgramArguments` detail and ask them to
move or remove that file themselves before retrying — never delete or
overwrite it on their behalf.

**Then hand-edit the real upstream model id strings** collected in Phase 5
into `~/.config/deus/connectors/cliproxy/config.local.yaml`'s
`oauth-model-alias.codex[].name` fields (the `write-config` call above does
not touch these — only `deus-model-map`, `api-keys`, `claude-api-key`, and
`default-model-alias`).

Load the daemon:

```bash
launchctl load ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
launchctl kickstart -k gui/$(id -u)/com.deus.connectors.cliproxy-oauth
```

### Already configured (cliproxy-oauth)? Add discovery aliases manually

**For an existing `config.local.yaml` predating the model-picker-
visibility feature** (reached via Phase 1's check on an already-configured
`cliproxy-oauth`) — do NOT re-run `write-config` above: it always rebuilds
the file from the tracked template, which would discard the real upstream
`name` values you already hand-edited in. Instead, hand-edit
`~/.config/deus/connectors/cliproxy/config.local.yaml` directly, adding 3
new entries under `oauth-model-alias.codex[]` — reuse the SAME real
upstream `name` value already present for each existing plain alias
(`sol`/`terra`/`luna-max`); only `alias` (add a `claude-` prefix) and
`display-name` are new:

```yaml
oauth-model-alias:
  codex:
    # ... existing sol/terra/luna-max entries -- do not remove or modify ...
    - name: "<same real name as the existing sol entry, verbatim>"
      alias: "claude-gpt-sol"
      display-name: "GPT Sol"
    - name: "<same real name as the existing terra entry, verbatim>"
      alias: "claude-gpt-terra"
      display-name: "GPT Terra"
    - name: "<same real name as the existing luna entry, verbatim>"
      alias: "claude-gpt-luna"
      display-name: "GPT Luna (max)"
```

No daemon restart needed — CLIProxyAPI watches its config file for
changes and reloads automatically on write (confirmed:
`internal/watcher/events.go`'s fsnotify-based watcher), and Claude Code's
own gateway discovery re-queries `/v1/models` on each new session start,
so the very next `deus connect cliproxy-oauth` launch picks this up.

**ollama (no daemon):**
```bash
echo '<json values>' | python3 scripts/connectors_cli.py write-config ollama
```

Where `<json values>` is a JSON object:
```json
{
  "host": "http://localhost:11434",
  "model_map": {"deus-ollama-local": "<real pulled model tag from Phase 5>"},
  "default_model_alias": "<same real pulled model tag>"
}
```

Writes only `~/.config/deus/connectors/ollama/config.local.yaml` — no
launchd plist, no daemon to load or kickstart. Ollama's own service is
already running (confirmed in Phase 4) and this connector never manages it.

## Phase 8: `~/.claude.json` pre-approval

**cliproxy-oauth only.** Read-merge the connector's inbound key into
`~/.claude.json`'s `customApiKeyResponses.approved` array — back up the
file first (`cp ~/.claude.json ~/.claude.json.bak-<date>`), then merge
(never overwrite the whole array — other entries may already exist).

**ollama: not applicable, based on docs — not yet confirmed by a live run.**
`env_for_launch()` sets `ANTHROPIC_API_KEY=""` (empty) and authenticates via
`ANTHROPIC_AUTH_TOKEN=ollama` instead, which Claude Code's own auth
precedence resolves before `ANTHROPIC_API_KEY` — the custom-API-key
approval prompt this phase exists to pre-answer is keyed off
`ANTHROPIC_API_KEY` being set, so it's expected not to trigger. This is
reasoned from `code.claude.com/docs/en/authentication`'s precedence order,
not yet observed in a real run (no live Ollama setup existed at
implementation time to test against). Treat Phase 10's live check as the
first real confirmation of this — if a prompt does appear there, that's a
genuine finding to fix, not an expected step to route around.

## Phase 9: Validate subagent definitions

```bash
python3 scripts/connectors_cli.py agents-json <id>
```

(`<id>` = `cliproxy-oauth` or `ollama`, whichever was set up.) Confirm it
prints valid, non-empty JSON — no file templates to install, `deus
connect`'s launch mechanism passes these inline via `claude --agents`.

## Phase 10: Verify

```bash
python3 scripts/connectors_cli.py verify-setup <id>
```

Engine health + a real functional probe (for `ollama`: liveness, a
tool-schema-bearing `/v1/messages` probe, and the context-length check from
Phase 4 — all three must pass). Then run one real launch and confirm, end
to end:

```bash
deus connect <id>
```

- Model resolution: check `/model` shows the redirected model.
- **Picker visibility** (new, model-picker-visibility follow-up): if
  `claude-gpt-*` aliases are configured, run `/model` and confirm all 3
  GPT entries appear labeled "From gateway" with clean names ("GPT Sol"/
  "GPT Terra"/"GPT Luna (max)"), not raw ids — and that switching between
  them takes effect on the next turn without relaunching the session.
- Inline subagents resolve: dispatch the `Agent` tool with
  `deus-gpt-sol`/`deus-gpt-terra`/`deus-gpt-luna` (`cliproxy-oauth`) or
  `deus-ollama-local` (`ollama`).
- Normal Deus context is present: ask it to recall a recent checkpoint —
  the round 11/12 fix's whole point was that a connector session gets the
  same identity/vault/memory/preferences pipeline as a bare `deus` launch,
  not a redirected-but-otherwise-bare Claude Code process.
- A bare `claude` in a separate terminal never sees the connector's
  subagents — confirms subagent isolation (the one fully structural
  guarantee this feature makes; env-var inheritance by nested processes
  and session-resume visibility are both disclosed tradeoffs, not
  guarantees — see Phase 3).

## Phase 11: Set as default (optional)

By default, every future session still needs `deus connect <id>` typed
explicitly — a bare `deus`/`deus home` is never affected. If you want this
connector to be your default everywhere instead (every bare `deus`/`deus
home` session, in any project, routes through it automatically), ask:

AskUserQuestion: Set `<id>` as your default connector for every session?
- Yes — run `deus connect default <id>` interactively and walk through its
  own confirmation prompt (a materially bigger blast radius than one-off
  `deus connect <id>` use, so it carries its own disclosure, not just
  Phase 3's).
- No — skip; `deus connect <id>` remains available on demand.

If yes, `deus connect default <id>` requires an interactive terminal (fails
closed, not silently, when piped/non-interactive) and prints its own
Phase-3-adjacent disclosure before writing anything — most importantly:
`deus claude` (typed explicitly) is the only guaranteed way to force plain
Claude once a default is set; a persistent `deus backend set claude` choice
or `DEUS_CLI_AGENT`/`DEUS_AGENT_BACKEND` env vars are NOT enough on their
own to block it. Reversible any time with `deus connect default off`
(`clear` is an accepted synonym).

## Troubleshooting

### `deus connect cliproxy-oauth` says "connector is unknown or not configured"

Config didn't write correctly, or the daemon isn't running:

```bash
python3 scripts/connectors_cli.py status cliproxy-oauth
launchctl list | grep com.deus.connectors.cliproxy-oauth
```

### `deus connect ollama` says "connector is unknown or not configured"

Config didn't write correctly, or the Ollama service isn't running (this
connector never starts/manages it):

```bash
python3 scripts/connectors_cli.py status ollama
curl -s http://localhost:11434/api/version
```

### Model doesn't resolve / 401s from the proxy (cliproxy-oauth)

```bash
curl -s http://localhost:8317/healthz
cat ~/.config/deus/connectors/cliproxy/config.local.yaml   # confirm api-keys, oauth-model-alias
```

### `deus connect ollama` fails verify / real session fails on tool use or truncates

```bash
curl -s http://localhost:11434/api/ps   # check context_length for the loaded model
cat ~/.config/deus/connectors/ollama/config.local.yaml   # confirm host, deus-model-map
```

If `context_length` is below 64000, raise `OLLAMA_CONTEXT_LENGTH` per Phase
4 and restart the Ollama service. If the model itself doesn't support
tool-calling, pick a different pulled model in Phase 5/7.

### Subagents don't appear via the `Agent` tool

Confirm you launched through `deus connect <id>`, not a bare `claude` —
subagent definitions are injected inline on that exact launch command and
never written to disk.

## Removal

**cliproxy-oauth:**
1. Unload and remove the launchd daemon:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
   rm ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
   ```
2. Remove the real local config:
   ```bash
   rm ~/.config/deus/connectors/cliproxy/config.local.yaml
   ```
3. Revert the `~/.claude.json` `customApiKeyResponses.approved` entry added
   in Phase 8 (remove that one key, keep the rest of the array intact).
4. Confirm: `python3 scripts/connectors_cli.py status cliproxy-oauth` now
   reports "not configured".

**ollama:**
1. Remove the real local config (no launchd daemon to unload — this
   connector never created one):
   ```bash
   rm ~/.config/deus/connectors/ollama/config.local.yaml
   ```
2. Confirm: `python3 scripts/connectors_cli.py status ollama` now reports
   "not configured". Ollama's own service is untouched — it wasn't started
   or managed by this connector, so there's nothing else to stop.
