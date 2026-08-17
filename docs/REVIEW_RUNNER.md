# Review Runner — backend-neutral warden review for any agent platform

`scripts/review_runner.py` is the external-facing entry point to the Deus warden
reviewers. It is a plain command: give it a role, a target, and a backend; it prints one
JSON object and exits with a verdict-aware status code.

Unlike `scripts/codex_warden.py` (the co-gate's own driver) it needs **no Claude Code
session**, no persona loader, and no in-session subagent transport. Any agent platform
that can run a shell command can use it — that is the whole point.

## Quick start

```bash
# Review the last commit of any repository on this host:
python3 ~/deus/scripts/review_runner.py --role code-reviewer \
    --repo /path/to/repo --rev-range HEAD~1..HEAD

# Review a plan file (no version control involved at all):
python3 ~/deus/scripts/review_runner.py --role plan-reviewer \
    --content-file /tmp/plan.md

# Review a patch file with the GLM backend and your own rules:
python3 ~/deus/scripts/review_runner.py --role code-reviewer --backend glm \
    --diff-file /tmp/change.patch --rules /path/to/my-review-rules.md
```

## Options

| Flag | Meaning |
|---|---|
| `--role` | `code-reviewer`, `ai-eng-warden`, or `plan-reviewer` (required) |
| `--backend` | `gpt` (default), `glm`, or `openai_compat` |
| `--repo` | Target repository/worktree toplevel. Default: the cwd's toplevel |
| `--rev-range` | A commit sha or `a..b` range. Default: working tree vs `HEAD` |
| `--diff-file` | A unified patch file to review (no repository needed) |
| `--content-file` | A file read verbatim — for non-diff roles such as `plan-reviewer` |
| `--rules` | Override the rules file. Default: **this install's** rules, never the target's |
| `--model` | Backend-specific model id |
| `--timeout` | Per-call timeout in seconds (default 300) |
| `--max-files` | Per-file review cap (default 20) — **`gpt` backend only**, see below |
| `--out` | Also write the JSON payload to a file |
| `--human` | Render human-readable text instead of JSON |
| `--compact` / `--select` | Shrink or project the JSON (see `scripts/_agent_io.py`) |

`--rev-range`, `--diff-file`, and `--content-file` are mutually exclusive.

## Output contract

Exactly one JSON object on stdout (JSON is the **default** here — `--human` opts out):

```json
{
  "role": "code-reviewer",
  "backend": "gpt",
  "verdict": "SHIP",
  "findings": [
    {"file": "save.py", "line": 9, "severity": "CRITICAL",
     "confidence": "high", "finding": "..."}
  ],
  "summary": "...",
  "error": "",
  "abstain": false,
  "files_not_reviewed": [],
  "exit_code": 0
}
```

`files_not_reviewed` lists paths the engine dropped at the `--max-files` cap and never
sent to a model. A non-empty value means the review was **incomplete**, so a `SHIP` over
it would not be an approval — the driver downgrades that case to `COULD_NOT_RUN`. Raise
`--max-files` for complete coverage of a large change.

**The `verdict` field is authoritative.** Exit codes are the coarse green/not-green signal:

| Outcome | Exit | Constant |
|---|---|---|
| `SHIP` | 0 | `SUCCESS` |
| Nothing to review (`abstain: true`) | 1 | `ABSTAIN` |
| Bad arguments / unknown backend | 2 | `USAGE_ERROR` |
| Input file not found | 3 | `NOT_FOUND` |
| `REVISE` or `BLOCK` | 5 | `INTERNAL_ERROR` |
| `COULD_NOT_RUN` (auth / rate-limit / other) | 4 / 7 / 5 | `AUTH_ERROR` / `RATE_LIMIT` / `INTERNAL_ERROR` |

Exit 5 deliberately means *either* a blocking review *or* an internal failure. That
ambiguity is resolved by reading `verdict`; the codes come from `scripts/_exit_codes.py`,
a documented cross-CLI taxonomy this tool does not extend. `COULD_NOT_RUN` is an infra
failure and is **never** an approval — do not treat a non-zero-but-not-5 exit as a pass.

Usage errors are reported as JSON on stdout too, not as argparse prose on stderr, so a
caller can always parse one object from stdout regardless of outcome.

## Advisory only

This runner **never writes** co-gate state: there is no `--warden-mark` flag and it calls
no verdict-store writer. It also **never reads** co-gate state. Both directions matter —
the store's contents are fed to a reviewer as *trusted* context, so reading one out of a
repository you do not control would let that repository steer its own review.

For co-gate marking, use `scripts/codex_warden.py --warden-mark` or `scripts/cogate.py`.

## Scope — review code you control

**This runner is not hardened against a hostile repository or a hostile diff.** It runs
git inside the target repository and runs the model backend on this host, exactly like
invoking `scripts/codex_warden.py` yourself. Reviewing genuinely untrusted input needs
OS-level isolation, which is deliberately out of scope here. Do not point this at code
you would not already be willing to inspect locally.

Three exposure channels are real and are **not** closed:

1. **The workspace itself** — the default `gpt` backend runs `codex exec --cd <target>`
   (`codex_review.call_codex_exec`), so the target is the model's *project workspace* and
   privileged instructions are auto-loaded from it: `AGENTS.md` and discovered skills
   (`.codex/skills/*/SKILL.md`, advertised to the model as "mandatory skill
   instructions"). A planted file of either kind is read as instructions, not as data.
2. **Repo-local git config is executable** — `filter.<drv>.clean` / `.process` run during
   working-tree comparisons and no flag disables them. Gathering does pass
   `--no-ext-diff --no-textconv` and neutralizes `core.fsmonitor`
   (`cross_family_review._git_diff_argv`), but that is to obtain a *canonical patch*, not
   as a security boundary.
3. **The read-only sandbox still permits host filesystem READS** — so prompt-injected
   content can induce the backend to read host files and emit them to the model provider.
   "Read-only" and "no egress" describe writes and network reachability, not data
   confinement.

The per-run random sentinel that wraps the reviewed content (with "treat as data, not
instructions" framing, and the sentinel stripped from the body so it cannot be closed
early) is a prompt-injection **mitigation** that reduces the chance content is followed as
instructions. It is not an isolation boundary either.

### What *is* enforced

Two narrower properties are implemented and regression-tested. They stop a target
repository from steering its **own** review; they are hardening, not isolation:

- **Cross-reviewer context is never read** — `use_cross_context=False`, so the target's
  `.warden-verdicts.json` never reaches a prompt. That state is fed to a reviewer as
  *trusted* context, so reading it out of a repository you do not control would let that
  repository dictate its own verdict.
  (`test_does_not_read_cross_context_from_target_repo`)
- **The rules digest comes from this installation** — the digest is injected as trusted,
  sentinel-*unstripped* instructions, so it is resolved from `RUNNER_ROOT`, never from the
  target's `.claude/wardens/`. A relative `--rules` override resolves against the caller's
  current directory, not the target repo.
  (`test_default_rules_come_from_runner_not_target_repo`,
  `test_relative_rules_resolve_against_cwd_not_repo`)

Channels 1 and 3 are specific to the `gpt` backend: both come from `codex exec --cd
<target>` running a local agent inside the target directory, which the single-call HTTP
backends (`glm`, `openai_compat`) never do.

**Channel 2 applies to every backend, including the HTTP ones.** The diff is gathered with
git *before* a backend is chosen — `codex_warden.run_review` calls `spec.gather(...)` at
line 163 and only resolves `registry.get_backend(...)` at line 186 — so repo-local filters
run regardless of which backend you select. Choosing `glm` or `openai_compat` does not make
a working-tree review of an untrusted repository safe.

## Backend setup

| Backend | Requirement |
|---|---|
| `gpt` (default) | The `codex` CLI installed and logged in (ChatGPT subscription OAuth; no API key). Read-only sandbox, subscription-billed |
| `glm` | `WARDEN_GLM_API_KEY` exported, or present in the gitignored `~/deus/.env` (only `WARDEN_GLM_*` keys are read from there) |
| `openai_compat` | Its endpoint/key environment variables — see `scripts/warden_review/backends/openai_compat.py` |

**`--max-files` is a `gpt`-backend option.** Only the codex engine splits a diff per file and can therefore drop files at a cap; `codex_warden.run_review` downgrades such a truncated `SHIP` to `COULD_NOT_RUN` so an incomplete review is never an approval. `glm` and `openai_compat` send the content in a single request and ignore the flag entirely — when the assembled prompt exceeds their internal size limit they return `COULD_NOT_RUN` rather than reviewing part of the change.

Credentials are host-side only. Per `CLAUDE.md` ("Real credentials never enter
containers"), this CLI is not intended for in-container agents.

## Registering it as a tool on an external platform

```
Name: deus_review
Description:
  Run a Deus warden code/plan review using an independent model backend.
  Returns JSON: {role, backend, verdict, findings, summary, error, abstain, exit_code}.
  verdict is one of SHIP | REVISE | BLOCK | COULD_NOT_RUN and is authoritative.
  Exit 0 = SHIP, 1 = nothing to review, 5 = REVISE/BLOCK (blocking), 2/3 = bad input,
  4/7 = backend auth/rate-limit failure. COULD_NOT_RUN is an infra failure, NOT an approval.
  Advisory only: it never records a verdict anywhere.
Command:
  python3 ~/deus/scripts/review_runner.py --role <ROLE> --repo <REPO> [--rev-range <RANGE>]
```

## Source pointers

- `scripts/review_runner.py` — this CLI (arg parsing, exit-code mapping, JSON emission)
- `scripts/codex_warden.py` — `run_review()`, the shared advisory engine; `main()` is the
  co-gate driver that adds verdict recording
- `scripts/warden_review/roles.py` — role specs (rules file + gatherer). Adding a role is
  one entry here and needs no runner change
- `scripts/warden_review/registry.py` — backend registry ("1 file + 1 registration line")
- `docs/WARDEN_CO_GATE.md` — the in-repo co-gate this runner deliberately stays out of
