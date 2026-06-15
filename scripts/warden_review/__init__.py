"""Provider-agnostic warden review backends.

A warden ROLE (code-reviewer, ai-eng-warden, …) emits a SHIP/REVISE/BLOCK verdict.
That verdict can be produced by different model PROVIDERS. Two transports exist:

  * ``claude-subagent`` — the in-session Claude subagent dispatched via the Agent tool;
    its verdict is scraped by the ``run_verdict_tracker`` hook in ``codex_warden_hooks.py``
    and stored under the role key. A Python registry CANNOT invoke it (it is driven by
    the Claude Code session), so it is NOT a backend in this package.
  * ``model-reviewer`` (this package) — out-of-band model calls that share one interface:
    gather context + rules → call model → emit a verdict. GPT-via-``codex exec`` today;
    an OpenAI-compatible backend (local / Ollama / OpenRouter / OpenAI) later.

The registry abstracts ONLY the model-reviewer family. The gate
(``run_warden_backends_gate`` in ``codex_warden_hooks.py``) unifies both transports at
the verdict level: a role is satisfied only when every configured backend is SHIP.
"""
