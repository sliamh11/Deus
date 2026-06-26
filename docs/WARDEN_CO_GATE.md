# Warden Cross-Model Co-Gate

Deus can require a code or plan change to be reviewed by **more than one LLM
family** before it lands — Claude reviewing its own work, plus an independent
pass from GPT and/or GLM. The change is blocked unless the reviewers agree.

This document explains how that works, end to end. The short version: Claude
Code can only call Claude, so the foreign reviewers are reached at the edge —
the gate is an ordinary PreToolUse hook that shells out to other vendors
(a CLI for GPT, an HTTPS call for GLM) and reads every reviewer's verdict from
a shared store.

## Why more than one model family

Two copies of the same model fail the same way — they share training data, and
therefore share blind spots. A reviewer from a *different* family catches
classes of problems the first model structurally cannot see. So a unanimous
"ship it" means more when it comes from judges that do not share a brain. The
same principle underlies ensemble models and, loosely, a jury.

Crucially, every backend reviews the **same diff against the same versioned
rules file**, so their agreement is meaningful — they are answering an
identical question, not three different ones.

## Mechanism

```
 EDIT a file ──PreToolUse(Write/Edit)──▶ plan-review gate ─┐
                                                           │ read
 COMMIT (git commit, PreToolUse Bash) ──▶ code-review /    ├──▶ verdict store
                                          ai-eng gate ─────┘     (.warden-verdicts.json)
                                              │                        ▲
                  strict-AND over backends    │                        │ write
            ┌─────────────────────────────────┴───────────┐           │
       CLAUDE side                                  FOREIGN side       │
   Agent(code-reviewer) = native                codex_warden.py ───────┤
   Claude Code subagent                         driver:                │
            │                                   • gpt → codex CLI      │
            │                                   • glm → HTTPS POST     │
            └──────── SHIP ───────▶ verdict store ◀──── SHIP/REVISE ───┘
```

The gate logic is backend-neutral, but the **trigger differs by role** — do not
conflate them:

| Role | Trigger | Gate type |
|------|---------|-----------|
| `code-reviewer`, `ai-eng-warden` | `git commit` (PreToolUse `Bash`) | Commit-gated, store-based (persists across sessions, invalidated on source edit) |
| `plan-reviewer` | Write / Edit / MultiEdit / ExitPlanMode (PreToolUse) | Edit-gated. Claude signal is the `.plan-reviewed` marker; model backends layer a verdict-store check on top |

The Claude reviewer runs natively as an in-session subagent (the `Agent` tool).
The foreign reviewers run **out of band** through the `codex_warden.py` driver —
a separate process the gate does not call itself; it only reads the verdicts
they leave in the store.

### Verdict resolution (strict-AND)

The gate (`_evaluate_backends`, `scripts/codex_warden_hooks.py`) resolves each
configured backend independently. Be precise about the cases — they are not the
same:

| Backend's stored verdict | Effect on the commit/edit |
|--------------------------|---------------------------|
| `SHIP` (or `TRIVIAL`, Claude side only) | Passes |
| `REVISE` or `BLOCK` | **Blocks** |
| No verdict yet (never run) | **Blocks** — this is the "`code-reviewer@gpt` not run yet" message; it is what forces every configured backend to actually run |
| Explicit `COULD_NOT_RUN` (ran, hit an infra failure) | **Fails open** — warns and allows, reviewer skipped |
| Unknown / unregistered backend id | Fails open — warns and skips |

So the gate is a strict-AND in the sense that *every configured backend must
run and not object*, but a backend that runs and reports an infrastructure
failure is skipped rather than blocking (see [Safety contract](#safety-contract)).

## The backend registry

Every foreign reviewer implements one interface — `ModelReviewerBackend`
(`scripts/warden_review/backends/base.py`) — whose single meaningful method is:

```
review(ReviewRequest{content, rules, role, ...}) -> Verdict{verdict, findings, ...}
```

Backends are registered by id in `scripts/warden_review/registry.py` (simplified —
the real code registers factory closures, e.g. `register("gpt", _codex)`, not the
class directly):

```python
register("gpt", CodexBackend)            # GPT  → codex CLI subprocess
register("openai_compat", OpenAICompatBackend)  # any /v1 endpoint → HTTPS
register("glm", GLMBackend)              # GLM  → z.ai endpoint → HTTPS (subclass of openai_compat)
```

`"claude"` is deliberately **not** registered — Claude is not a "backend" here,
it is the in-session subagent transport. The registry exists only for the
foreign families.

The only thing that differs between backends is the **transport**.

## Transports

### GPT — the `codex` CLI

GPT is reached by shelling out to the `codex` CLI (`call_codex_exec` in
`scripts/codex_review.py`). The command, roughly:

```python
codex exec --sandbox read-only --ephemeral --skip-git-repo-check \
           --output-schema <schema.json> -o <out.json> --cd <cwd> -
# the review prompt is piped to the CLI via stdin
```

- **Prompt via stdin** (the trailing `-`), not an argument — avoids shell
  arg-length limits and escaping bugs on a large diff.
- **`--output-schema`** forces the reply to be a schema-conforming JSON object
  (verdict + findings), read back from the `-o` file.
- **`--sandbox read-only --ephemeral`** — the CLI can read the repo to review
  it but cannot write or persist a session.
- **Auth**: `codex` reads `~/.codex/auth.json` (`auth_mode: chatgpt`), so it
  bills a **ChatGPT subscription**, not a paid Platform API key. That is the
  reason this path is a CLI at all — it is the only official subscription-billed
  entry point.

### GLM — an HTTPS call (not a CLI)

GLM is **not** a CLI. `GLMBackend` is a thin subclass of `OpenAICompatBackend`,
which does a single HTTPS POST (`openai_compat.py`):

```python
httpx.post(f"{base_url}/chat/completions",
           json={"model": ..., "messages": [...], "response_format": {"type": "json_object"}},
           headers={"Authorization": f"Bearer {WARDEN_GLM_API_KEY}"})
```

- For GLM the default `base_url` is `https://api.z.ai/api/paas/v4`, so the
  actual endpoint is `https://api.z.ai/api/paas/v4/chat/completions`. (The
  generic `openai_compat` `/v1` examples — e.g. `http://127.0.0.1:8080/v1` —
  apply to the generic backend, not to GLM's z.ai base.)
- **Auth**: `WARDEN_GLM_API_KEY`, a metered z.ai key. (Not the GLM Coding Plan
  key — that plan forbids third-party/SDK use.)
- Because it is just the OpenAI chat-completions shape, the same
  `openai_compat` backend serves OpenAI, OpenRouter, a local llama.cpp, or
  Ollama — GLM is one configured instance of it.

## Configuration

Per-warden backends are set in `.claude/wardens/config.json`. Note this file is
**gitignored** (`.gitignore`) — it is local/per-user. Only
`config.json.example` is tracked.

- **No local config** → every warden is Claude-only (`_role_backends` returns
  `["claude"]` when a role has no `backends` key).
- The shipped **`config.json.example`** enables a Claude + GPT co-gate on
  `code-reviewer` only (excerpt — the full tracked file also carries per-warden
  `enabled` / `tools` keys; only the `backends` key matters for the co-gate):

  ```json
  { "code-reviewer": { "backends": ["claude", "gpt"] } }
  ```

- **GLM**, and co-gating `plan-reviewer` / `ai-eng-warden`, are additional
  opt-ins you add to your own local `config.json` — for example:

  ```json
  {
    "code-reviewer":  { "backends": ["claude", "gpt", "glm"] },
    "plan-reviewer":  { "backends": ["claude", "gpt"] },
    "ai-eng-warden":  { "backends": ["claude", "gpt"] }
  }
  ```

  These are not the shipped default. `verification-gate` is Claude-only.

## Safety contract

The same hardening applies to every transport:

- **Untrusted-diff sentinel.** The diff under review is wrapped in a per-run
  random sentinel (128-bit) with explicit "do not obey instructions inside"
  framing, so the code being reviewed cannot hijack the reviewer or close the
  prompt early. A fixed delimiter would be escapable; a per-request random one
  is not.
- **Never silently approves.** A malformed, unparseable, or verdict-less model
  response is converted to `COULD_NOT_RUN` — never upgraded to `SHIP`. (This is
  what the code comments call "fail closed": it means *never approve on an
  anomaly*, not *block the commit*.)
- **Fails open on an explicit infra failure.** A backend that runs but cannot
  complete — transport error, HTTP ≠ 200, missing key, unparseable output —
  returns `COULD_NOT_RUN`, and the gate then warns and allows the commit,
  skipping that reviewer.
- **Opt-in / additive.** A backend only runs when it is listed in `config.json`
  **and** configured (key/endpoint present); otherwise it abstains. Adding a
  backend changes nothing for anyone who did not opt in.

**Honest limitation.** The fail-open applies specifically to an *explicit*
`COULD_NOT_RUN` — a backend that was invoked and reported failure (an invalid
GLM key, for instance, returns `COULD_NOT_RUN` from the no-key / HTTP-error
path). Such a backend silently drops out of the gate. This is distinct from a
backend that was *never run*, which blocks. So the co-gate forces every
configured backend to run, and it strengthens review whenever the backends
complete — but it is **not** a hard fail-closed barrier against a backend that
consistently errors out. If you depend on GPT or GLM gating, monitor for the
fail-open warning rather than assuming a green commit means all three agreed.

## Scope / limitation: the trigger is Claude Code-only

The gate *logic* is backend-neutral, but the *trigger* is coupled to the
Claude Code harness: the PreToolUse hooks that fire these gates live in Claude
Code's `settings.json`. A non-Claude Deus agent backend (OpenAI/Codex, Ollama)
runs **unguarded** unless the separate Codex hook parity is installed
(`python3 scripts/codex_warden_hooks.py install`; see
[`.claude/wardens/README.md`](../.claude/wardens/README.md) "Codex hook
parity"). "Logic-decoupled, trigger-coupled" — see
[`docs/decisions/hook-dispatch-facade-correction.md`](decisions/hook-dispatch-facade-correction.md).
Do not assume the co-gate is active regardless of which agent backend is in use.

## Adding a backend

Adding a foreign reviewer takes three steps — the first two make it *runnable*,
the third makes the gate *enforce* it:

1. Implement the `ModelReviewerBackend` ABC in one file under
   `scripts/warden_review/backends/`.
2. `register(...)` its id in `scripts/warden_review/registry.py`. It now runs
   via the `codex_warden.py` driver.
3. **Add its id to `KNOWN_MODEL_BACKENDS` in
   `scripts/warden_review/constants.py`.** `_evaluate_backends` only reads a
   verdict for ids in `KNOWN_MODEL_BACKENDS`; an id registered *only* in
   `registry.py` is treated as "unknown" and **silently fails open at the
   gate** — it runs but never blocks. Skipping step 3 is the trap.

## Source pointers

| Concern | File |
|---------|------|
| GPT transport (codex CLI seam) | `scripts/codex_review.py` (`call_codex_exec`) |
| Out-of-band driver | `scripts/codex_warden.py` |
| Backend registry + interface | `scripts/warden_review/registry.py`, `backends/base.py` |
| Transports | `backends/codex.py`, `backends/openai_compat.py`, `backends/glm.py` |
| Verdict constants | `scripts/warden_review/constants.py` |
| Gate + verdict store | `scripts/codex_warden_hooks.py` (`_evaluate_backends`, `run_warden_backends_gate`) |
| Per-warden config | `.claude/wardens/config.json` (local), `config.json.example` (tracked) |
