# CLIProxyAPI Model-ID Cloaking, and Why Gateway-Routed Background Sessions Break

Two independent mechanisms that together make a locally-proxied Claude Code
setup fail in a way that reads like a model-naming bug and is not one. Both were
found while debugging a launcher whose every `--bg` invocation died on
"There's an issue with the selected model (...)".

**Date:** 2026-08-15
**Scope:** Local CLIProxyAPI (https://github.com/router-for-me/CLIProxyAPI)
fronting Claude Code, as in [`examples/multi-model-cliproxyapi/`](../../examples/multi-model-cliproxyapi/).
Findings only -- nothing here changes Deus code, and neither mechanism is
something this repo controls.
**Status:** Recorded. Part 1 is stable upstream behaviour; Part 2 is an
observation about one Claude Code build and is expected to change.

CLIProxyAPI citations below are pinned to commit
`ecc9aa72b32f34b680d03b0724b531a21ae74472`. Claude Code observations are pinned
to CLI version `2.1.233`. Both will drift; see
[Re-deriving this on your own build](#re-deriving-this-on-your-own-build).

---

## Part 1 -- The `claude-fable-5-dd-` prefix is deliberate, and load-bearing

Point Claude Code at a CLIProxyAPI instance and ask it what models are
available, and every non-Anthropic model comes back under a name nobody would
guess: a `claude-fable-5-dd-` prefix followed by the real name with its
characters **reversed**.

| Advertised to Claude Code | Real model |
| -- | -- |
| `claude-fable-5-dd-xam-anul` | `luna-max` |
| `claude-fable-5-dd-los` | `sol` |
| `claude-fable-5-dd-arret-6.5-tpg` | `gpt-5.6-terra` |

That is upstream's own doing, not corruption:

```go
// internal/client/claude/models/models.go:9
const claudeDDModelPrefix = "claude-fable-5-dd-"
```

`EnsureClaudeModelIDPrefix` (`:50`) returns any id already starting with
`claude-` unchanged, and rewrites everything else to the prefix plus
`reverseModelID(id)` (`:94`). `ResolveClaudeModelIDPrefix` (`:59`) reverses the
transform on the way back in, so requests still route to the real model --
applied in `sdk/api/handlers/claude/code_handlers.go:83,115` via
`rewriteClaudeDDModelInBody` (`:136-140`).

### Why upstream does this

Claude Code's gateway model discovery filters the listing it receives with a
`/(claude|anthropic)/i` test against each model's `id`. A model called `sol`
never survives that filter. The prefix is what gets non-Anthropic models through
it; the reversal is cosmetic obfuscation of the real name on top.

So the cloaking is not decoration -- remove it and the gateway's models stop
appearing in the client's picker entirely.

### Only the Anthropic-shaped listing is cloaked

There is one `/v1/models` route
(`internal/api/server_routes.go:65`, `unifiedModelsHandler`), which picks a
response shape from the request itself:

```go
// internal/api/server_routes.go:554-559
func isAnthropicModelsRequest(c *gin.Context) bool {
	if c.GetHeader("Anthropic-Version") != "" {
		return true
	}
	return strings.HasPrefix(c.GetHeader("User-Agent"), "claude-cli")
}
```

Which means a plain `curl` and Claude Code see the same models under different
names -- an easy thing to mistake for the listing disagreeing with itself:

```bash
# OpenAI shape: plain ids (luna-max, sol, gpt-5.6-terra, ...)
curl -s -H "x-api-key: $KEY" http://localhost:8317/v1/models

# Anthropic shape -- what Claude Code actually sees: cloaked ids
curl -s -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
     http://localhost:8317/v1/models
```

### Turning it off, and whether you should

`disable-cloaking-model-list` (`internal/config/sdk_config.go:68-69`, default
`false` per `config.example.yaml:175`) skips the rewrite
(`internal/client/claude/models/models.go:12,16`). Bear in mind that with
cloaking off, the client-side `/(claude|anthropic)/i` filter above drops every
model whose name does not contain "claude" or "anthropic" -- which is most of
the point of running the proxy.

### Both spellings work at request time

Worth knowing before anyone "fixes" a config full of plain ids: **the real name
and the cloaked name both work.** `ANTHROPIC_MODEL=luna-max` and
`ANTHROPIC_MODEL=claude-fable-5-dd-xam-anul` each produced successful
completions through the same gateway. The plain form additionally emits a
cosmetic client-side line:

```
[claude-code:unrecognized_model] {"model":"luna-max","query_source":"sdk"}
```

That is the client's model-family classifier failing to place the name, not a
rejection. The same applies to subagent `model:` pins -- an agent pinned to
`sol` resolves and answers normally, with that one warning.

Prefer the cloaked ids where you want silence, the plain ids where you want a
config a human can read. Neither is more correct.

---

## Part 2 -- `--bg` does not carry your gateway environment

This is the part that actually breaks things, and it has nothing to do with
Part 1.

A `claude --bg` session does not run as a child of the launching shell. It
claims a **pre-forked `claude bg-spare` process** owned by a long-lived daemon,
and the launcher copies across only an **allowlisted subset** of environment
variables. On CLI `2.1.233` that allowlist carries the `ANTHROPIC_*_MODEL`
variables, provider-selection flags, and AWS/GCP region and profile settings --
but **not `ANTHROPIC_BASE_URL` and not `ANTHROPIC_API_KEY`**.

The failure that produces is thoroughly misleading. A launcher that exports

```sh
ANTHROPIC_BASE_URL=http://localhost:8317
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=luna-max
```

hands the background session the model name and nothing else. The session then
resolves a gateway-only model against `api.anthropic.com`, which has never heard
of it:

> There's an issue with the selected model (luna-max). It may not exist or you
> may not have access to it.

Every clue points at the model name. The model name is fine.

### Confirming it, without reading the bundle

Three externally observable checks, in increasing order of effort:

1. **The control experiment.** Run the identical variables in the foreground
   (`ANTHROPIC_BASE_URL=... ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=... claude -p "hi"`).
   If the foreground succeeds and `--bg` fails on the same values, the model
   name is exonerated and the environment is the suspect.
2. **The live process.** `ps eww -o command= -p <pid>` on the background
   session's pid. A `claude bg-spare` process with no `ANTHROPIC_BASE_URL` in
   its environment cannot be talking to your gateway, whatever the launcher
   exported.
3. **The daemon's own record.** The background daemon roster under
   `~/.claude/daemon/` persists a `dispatch.env` per worker. For an affected
   session it contains exactly the model variables and nothing else -- the
   allowlist result, written down.

### The fix: `--settings`, not the environment

Flags survive the hop that the environment does not. `--settings` is forwarded
to the spare, and the spare re-reads the file at startup, so a settings file
holding the gateway config works identically in the foreground and in `--bg`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8317",
    "ANTHROPIC_API_KEY": "...",
    "ANTHROPIC_MODEL": "claude-fable-5-dd-xam-anul",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"
  }
}
```

Keep that file at mode `600` -- it holds a credential.

```sh
claude --settings ~/.claude/gateway.settings.json "$@"
```

Two things to get right:

- **Pass a file path, not inline JSON.** `--settings` accepts either, but the
  argument is persisted verbatim into the daemon roster and is visible in `ps`.
  Inline JSON puts the key in both.
- **Do not put this in `~/.claude/settings.json`.** A global entry applies to
  every session, including ones meant to stay on their normal subscription
  login, and setting `ANTHROPIC_API_KEY` there displaces that login
  account-wide. Scope it to the launcher that wants it.

A launcher doing this should also preflight: fetch `/v1/models` and check the
configured model against what the gateway actually serves, printing the
available list on a mismatch. The failure mode above costs an archaeology
session; a preflight costs one line.

---

## Re-deriving this on your own build

Everything in Part 1 is ordinary Go source -- read it at whatever commit you
have; only the line numbers move.

Part 2 is different and should be treated with suspicion. The Claude Code CLI
ships as a single minified bundle, so its internal identifiers -- the allowlist
array, the function that builds the forwarded environment -- are **build-specific
and change on every release**. Do not cite them. What survives minification is
string literals, which make usable anchors:

| Anchor | Points at |
| -- | -- |
| `stripped non-allowlisted providerEnv key(s)` | the warning logged when env keys are dropped from persisted job state |
| `gatewayDiscovery` | the `/v1/models` fetch, its filter, and its cache |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY not set` | the bootstrap path that skips gateway discovery |

Grep the bundle for one of those, then read outwards. Better still, reach for
the three observable checks above first -- they answer the question without
depending on any build's internals, and they stay true when the bundle changes.

The same caution applies to the `claude-fable-5-dd-` prefix itself. It is an
upstream implementation detail, disabled by one config flag and free to change
in any release. Treat the ids as opaque and read them from the gateway; never
hard-code a translation table.
