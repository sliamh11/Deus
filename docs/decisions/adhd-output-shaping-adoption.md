# ADR: ADHD-Style Output Shaping for Claude Code CLI Responses

**Status:** Accepted
**Date:** 2026-08-13
**Scope:** `.claude/rules/response-shape-rules.md`
**Related:** [backend-neutral-agent-runtime.md](backend-neutral-agent-runtime.md) (why this doesn't reach the Codex backend)

## Context

The user asked for `github.com/ayghri/i-have-adhd`'s output-shaping ruleset —
lead with the next action, number multi-step tasks, no preamble/recap/closers,
cap lists at 5, restate state every turn, and similar — integrated as a
hard-baked rule governing Claude Code CLI responses in this repo, explicitly
framed as "the first rule of my output."

Upstream ships this as an invokable Claude Code plugin: a `SKILL.md` (10
rules, essay-style writeup with worked before/after examples on most rules)
plus an opt-in always-on mechanism (`$CLAUDE_CONFIG_DIR/.i-have-adhd-always` flag file
+ a SessionStart hook that injects the ruleset, toggleable at runtime via
"stop adhd mode"/"normal mode").

## Decision

Adopted as a new sibling file, `.claude/rules/response-shape-rules.md` — not
an edit to the existing `.claude/rules/core-behavioral-rules.md`.
`.claude/rules/*.md` was confirmed (empirically, via direct observation
across review rounds) to load every file in that directory unconditionally,
every turn, regardless of filename — so a new sibling file gets identical
guaranteed activation to an edit, without growing a file whose own header
explicitly says "Always loaded — keep it lean." This activation is
resolved from the main checkout (`~/deus`), not from any worktree — so it
takes effect once this PR merges *and* the main checkout is updated
(`git -C ~/deus pull`), the same staleness caveat `orchestration-rules.md`
already documents for this repo generally.

What differs from upstream, and why:

- **(a) Condensed, not byte-identical.** Upstream is a long essay-style
  `SKILL.md` with worked before/after examples. This repo's rules files are
  terse "what + why" one-liners, matching the density of
  `core-behavioral-rules.md`'s existing sections.
- **(b) CLI-only scope.** Upstream has no concept of "bot output to end
  users," so it ships no such restriction. This repo explicitly excludes
  Deus's own product responses to end users on WhatsApp/Telegram/Slack/
  Discord/Gmail — those stay governed by `copy-writer`/`ux-reviewer`
  conventions, unchanged.
- **(c) Hard-baked, no runtime toggle.** Upstream ships a "stop adhd
  mode"/"normal mode" escape phrase plus an opt-in flag-file + SessionStart-
  hook mechanism. Deliberately not used here, per explicit user choice —
  folded directly into the always-loaded `.claude/rules/` directory instead,
  since that's unconditional per-file injection rather than a hook that can
  silently no-op on failure (upstream's own hook comment: "Never blocks
  session start: any failure exits 0").
- **(d) Provenance kept out of the always-loaded rules file.** A pure
  license/adaptation-notes doc has zero runtime behavioral value and never
  needs to be re-read by a live session — putting it in `.claude/rules/`
  would be permanent per-turn token cost for nothing. This ADR carries that
  content instead; `response-shape-rules.md` keeps only a one-line pointer.
- **(e) Claude-Code-backend-only, by conscious decision.** This repo is
  backend-neutral by design (Claude Code is the default adapter, OpenAI/
  Codex is opt-in — see [backend-neutral-agent-runtime.md](backend-neutral-agent-runtime.md)),
  but `.claude/rules/*.md` has no Codex-backend equivalent today — Codex
  sessions read their own `hooks.json`/config, and the only existing
  Claude↔Codex bridge (`codex_warden_hooks.py`, per `AGENTS.md`'s Codex-bridge
  section) mirrors Warden **gates**, not arbitrary rule *content*. Codex
  parity is a real, named, deliberately out-of-scope gap — not a silent
  oversight — revisitable later if a Codex-backend Deus session should get
  matching output shaping.

Source: `github.com/ayghri/i-have-adhd`, MIT license.
