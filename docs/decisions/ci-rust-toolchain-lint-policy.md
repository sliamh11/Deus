# ADR: CI Rust Toolchain & Lint Policy — Floating Stable, Fix-Forward

**Status:** Accepted
**Date:** 2026-07-11
**Scope:** `.github/workflows/ci.yml` (`test-tui` job), `tui/` crate lint posture
**Supersedes:** None
**Related:** backend-strategy-trait.md, parallel-agent-orchestration.md (TUI architecture)

## Context

The `test-tui` CI job installs Rust via `dtolnay/rust-toolchain@stable` — a
floating pointer to the latest stable release — and gates on
`cargo clippy -- -D warnings`. When stable moved to 1.97.0, clippy gained the
`useless_borrows_in_formatting` lint, and a pre-existing redundant `&` in
`tui/src/ui.rs` started failing CI on **every open PR**, including Python-only
ones (#23/#24/#25) that never touched Rust. PR #26 fixed the lint site itself;
this ADR records the policy for the failure class.

The structural tradeoff: a floating toolchain means new clippy releases can
break CI on unrelated PRs at any time; a pinned toolchain means lints (and
soundness fixes) silently age until someone remembers to bump it.

## Decision

1. **Keep the toolchain floating on `stable`.** No version pin in
   `.github/workflows/ci.yml`.
2. **Keep `-D warnings` on clippy.** Warnings stay hard errors.
3. **Fix-forward at the source when a new lint fires.** Change the flagged
   code to comply. `#[allow(...)]` is a last resort, allowed only for
   documented false positives, with a comment citing the upstream clippy
   issue.
4. **Unrelated PRs do not absorb the fix.** A new-lint breakage gets its own
   minimal PR (like #26) that lands first; blocked PRs then re-run checks.
   Never merge over the red check, never bundle the lint fix into an
   unrelated diff.

## Alternatives Considered

- **Pin the toolchain (e.g. `toolchain: 1.96.0`).** Deterministic CI, but the
  pin rots: lint coverage freezes, and the eventual bump lands a pile of new
  lints at once instead of one at a time. A solo-maintained repo has no
  process that guarantees timely bumps. Rejected.
- **Drop `-D warnings`.** Warnings accumulate silently until the codebase is
  noise. Rejected — contradicts the repo-wide zero-warning posture (ESLint on
  `src/` behaves the same way).
- **`#[allow]` the new lint crate-wide.** Hides real findings of that lint
  forever to dodge one fix. Rejected as a default; permitted per-site for
  documented false positives only (Decision 3).

## Consequences

- CI can break on unrelated PRs when stable Rust releases (~every 6 weeks).
  The failure is cheap to diagnose: `test-tui` red on a PR that touches no
  Rust ⇒ suspect a toolchain bump; check the clippy version in the job log.
- The fix is always a small, isolated PR — usually minutes of work — and the
  codebase permanently tracks current lint standards.
- Local dev may run an older clippy than CI (local 1.96 vs CI 1.97 during the
  #26 incident), so a local `cargo clippy` pass does not prove CI will pass.
  CI is the arbiter; `rustup update` locally when chasing a CI-only lint.
- Known accepted gap: the CI job lints the binary target only
  (`cargo clippy -- -D warnings`, not `--all-targets`), so test-target lints
  are unchecked (two `field_reassign_with_default` sites exist in tests as of
  this writing). Tightening to `--all-targets` is a separate decision — it
  would fail today's CI and needs its own cleanup PR first.
