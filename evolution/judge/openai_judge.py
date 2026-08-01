"""
Codex-subscription-based judge for the Deus Evolution loop (opt-in — see
providers/openai.py).

Standalone runtime evaluator — scores production interactions via evaluate().

Transport: shells out to the `codex` CLI (ChatGPT/Codex subscription auth,
no OPENAI_API_KEY / per-call billing) wrapped in a macOS Seatbelt sandbox.
This is macOS-only by design (Seatbelt is a macOS-specific mechanism).

Why not a raw HTTP call to OpenAI's API (like gemini_judge.py does for
Gemini)? Because `codex exec` is the only viable subscription-backed
transport to GPT-5.6 in this environment, and unlike a passive completion
API, `codex exec` is a full AGENT — the model can invoke tools including
shell execution. The judge's job is to score real, potentially-adversarial
production interaction text (arbitrary user messages, arbitrary agent
responses), so a prompt-injection payload hidden in judged content is a
real threat: it could otherwise instruct the agent to read and exfiltrate
local secrets (this host's own live ChatGPT/Codex OAuth credential among
them).

Security design (validated via extensive live adversarial testing, not just
constructed defensively — see the PR description / session log for the full
verification trail):

1. Capability removal, not credential gating. `~/.codex/auth.json` (the
   live OAuth credential) MUST be readable by the codex process for it to
   authenticate at all — there is no way to keep it unreadable to that
   process's own shell-tool capability if that capability exists (Seatbelt
   scopes by process tree, not by which code path triggers a given read).
   The fix is removing the tool-use capability, not trying to protect the
   credential from it: `codex exec --disable shell_tool --disable apps
   --disable unified_exec ...` (a comprehensive list — `codex features list`
   was used to enumerate every execution/browser/computer/plugin-adjacent
   feature and confirm each resolves to `false` under this flag set, not
   just the two flags that looked obviously relevant).
2. OS-level backstop for the EXEC vector specifically, independent of #1.
   A Seatbelt profile denies the codex process from exec'ing ANY further
   binary — `process-exec` is scoped to ONLY the resolved native codex
   binary itself (the one exec `sandbox-exec` performs to launch it), not
   a blanket allow. This closes the exec gap even if a future codex version
   reintroduces an execution capability #1 doesn't anticipate. It does NOT
   extend to network exfiltration: the profile necessarily grants
   unrestricted outbound network (`network*`) since codex needs it to reach
   the model API — if a future codex feature retains network capability
   despite the `--disable` list, there is no second layer stopping it the
   way there is for exec.
3. Minimal filesystem exposure. Beyond the process-exec restriction, reads
   are scoped to: OS/runtime libraries, a narrow allowlist of codex's own
   fixed bookkeeping paths under ~/.codex/ (auth token, install id, its own
   numbered state/log/goals/memories sqlite DBs, its own tmp scratch dir —
   NOT the whole directory: history.jsonl, hooks.json, config.toml, skills/,
   plugins/, sessions/ all stay unreadable, and codex gracefully degrades
   those optional features when denied), this user's own per-user Darwin
   temp/cache root (derived authoritatively via `getconf
   DARWIN_USER_TEMP_DIR`, NOT via the `TMPDIR` env var — TMPDIR can be
   absent or empty and both cases were confirmed live to resolve to an
   unsafe, overly broad fallback path), and the one per-call isolated temp
   directory this specific evaluation is allowed to read/write.
4. Minimal environment. The subprocess environment is explicitly limited to
   PATH/HOME (not inherited wholesale) — the evolution process's own
   GEMINI_API_KEY/other secrets are never visible to the codex subprocess.
5. Fail closed. `is_openai_available()` requires the resolved `codex`
   binary to be a genuine native Mach-O executable (rejects npm-launcher
   installs, which resolve to a JS launcher — a different, untested
   multi-hop-exec shape this transport doesn't attempt to support) and a
   working `codex login status` check under the same minimal environment
   the real call uses (not the inherited environment, which could report
   availability against a different CODEX_HOME than the real call uses).

Caveat for anyone comparing results from this transport: `codex exec` wraps
GPT-5.6 in Codex's own system instructions and agent scaffolding. Results
measure "GPT-5.6 *through Codex*," not the raw model a direct API call would
measure — label this explicitly in any benchmark report.
"""
import asyncio
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from ..config import (
    JUDGE_MAX_PERSONA_CHARS,
    JUDGE_MAX_PROMPT_CHARS,
    JUDGE_MAX_RESPONSE_CHARS,
    JUDGE_RETRY_COUNT,
    OPENAI_JUDGE_MODEL,
)
from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score, _normalize_dim

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "safe": {"type": "boolean"},
        "quality_level": {"type": "integer", "minimum": 1, "maximum": 5},
        "recalled_preference": {"type": "boolean"},
        "format_matched": {"type": "boolean"},
        "tone_matched": {"type": "boolean"},
        "execution_quality": {"type": "integer", "minimum": 1, "maximum": 5},
        # Capped: the one free-text field in this schema — bounds the
        # channel a compromised/tricked agent could use to smuggle a larger
        # payload through the legitimate structured-output path, and caps
        # downstream log/report growth (mirrors this file's existing
        # raw/stderr truncation conventions elsewhere).
        "rationale": {"type": "string", "maxLength": 500},
    },
    "required": [
        "safe", "quality_level", "recalled_preference", "format_matched",
        "tone_matched", "execution_quality", "rationale",
    ],
}

# Every execution/browser/computer/plugin/dependency-install-adjacent codex
# feature, confirmed via `codex features list --disable <these>` to resolve
# each name to `false` — not just the two flags (shell_tool, apps) that look
# obviously relevant. `unified_exec` in particular is a distinct, stable,
# enabled-by-DEFAULT execution feature that survives disabling shell_tool
# alone; missing it was a confirmed, live-reproduced gap during this
# transport's design.
_DISABLE_FEATURES = [
    "shell_tool", "apps", "unified_exec", "code_mode_host",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "in_app_browser", "multi_agent",
    "plugins", "plugin_sharing", "remote_plugin", "shell_snapshot",
    "tool_call_mcp_elicitation", "workspace_dependencies", "hooks",
    "skill_mcp_dependency_install", "auth_elicitation",
]

# Mach-O magic numbers (thin + 64-bit + 32/64-bit fat/universal, both byte
# orders) — used to reject non-native codex installs (e.g. an npm-installed
# JS launcher at bin/codex.js) that resolve via `which` but aren't the real
# executable this transport's Seatbelt profile is built to sandbox.
_MACHO_MAGICS = {
    0xFEEDFACE, 0xCEFAEDFE,  # MH_MAGIC / MH_CIGAM (32-bit)
    0xFEEDFACF, 0xCFFAEDFE,  # MH_MAGIC_64 / MH_CIGAM_64
    0xCAFEBABE, 0xBEBAFECA,  # FAT_MAGIC / FAT_CIGAM
    0xCAFEBABF, 0xBFBAFECA,  # FAT_MAGIC_64 / FAT_CIGAM_64
}

_PER_USER_ROOT_RE = re.compile(r"^/private/var/folders/[^/]+/[^/]+$")


def _minimal_env() -> dict:
    """Resolved fresh per call (not a module-level constant) so it always
    matches whatever HOME/PATH the process actually has — consistent with
    _build_sandbox_profile's own live os.path.expanduser("~") read."""
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}


def _is_macho_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            magic_bytes = f.read(4)
        if len(magic_bytes) != 4:
            return False
        import struct
        magic = struct.unpack(">I", magic_bytes)[0]
        return magic in _MACHO_MAGICS
    except OSError:
        return False


def _darwin_per_user_root() -> str:
    """OS-authoritative per-user Darwin temp/cache root.

    Deliberately NOT derived from the TMPDIR env var: TMPDIR absent resolves
    (via realpath("/tmp")) to "/private" — broader than this function exists
    to produce; TMPDIR="" (set-but-empty; os.environ.get's default only
    applies when the key is fully absent) resolves to the calling process's
    cwd, a context-dependent path that could cover the repo or sibling
    worktrees depending on launch directory. Both were confirmed live during
    this transport's design. `getconf DARWIN_USER_TEMP_DIR` is independent
    of TMPDIR entirely (confirmed via `env -i getconf DARWIN_USER_TEMP_DIR`).

    Fails closed: raises rather than return an unvalidated path if the
    resolved value doesn't match the expected two-segment Darwin per-user
    shape — never silently falls through to a broader grant.
    """
    raw = subprocess.run(
        ["getconf", "DARWIN_USER_TEMP_DIR"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip()
    real = os.path.realpath(raw).rstrip("/")
    per_user_root = os.path.dirname(real)
    if not _PER_USER_ROOT_RE.match(per_user_root):
        raise RuntimeError(
            f"Unexpected Darwin per-user temp root shape: {per_user_root!r} "
            f"(from getconf DARWIN_USER_TEMP_DIR={raw!r}) — refusing to build "
            f"an unvalidated Seatbelt grant."
        )
    return per_user_root


def is_openai_available() -> bool:
    """codex CLI present, authenticated, native, on a supported OS. No
    subscription-quota spend — `codex login status` is a local credential
    check (confirmed live: returns instantly, exit 0, reports zero token
    usage, unlike every real `codex exec` call)."""
    if os.environ.get("EVOLUTION_OPENAI_JUDGE_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False
    if platform.system() != "Darwin":
        return False
    codex_path = shutil.which("codex")
    if codex_path is None or shutil.which("sandbox-exec") is None:
        return False
    resolved = os.path.realpath(codex_path)
    if not _is_macho_binary(resolved):
        print(
            f"[openai_judge] codex resolved to a non-native launcher "
            f"({resolved!r}) — this transport requires a native Mach-O "
            f"binary (e.g. `brew install codex`), not an npm-launcher "
            f"install. Reporting unavailable.",
            file=sys.stderr,
        )
        return False
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=10, env=_minimal_env(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _build_sandbox_profile(codex_real: str, per_user_root: str, iso_dir: str) -> str:
    """Seatbelt (SBPL) profile — see module docstring for the security
    rationale. `process-exec` is scoped to ONLY the resolved codex binary;
    `~/.codex/*` grants are narrow (fixed bookkeeping paths, not the whole
    directory); `per_user_root` and `iso_dir` grants are this-user-only,
    never a blanket /private/var/folders or /private grant."""
    home = os.path.expanduser("~")
    return f"""
(version 1)
(import "system.sb")
(deny default)

(allow process-exec (literal "{codex_real}"))
(allow process-fork)
(allow signal (target self))
(allow file-read-metadata)
(allow mach-lookup)
(allow iokit-open (iokit-user-client-class "IOSurfaceRootUserClient"))
(allow sysctl-read)
(allow network*)
(allow system-socket)
(allow user-preference-read)
(allow ipc-posix-shm)

(allow file-read*
  (subpath "/usr/lib") (subpath "/usr/bin") (subpath "/bin")
  (subpath "/System") (subpath "/opt/homebrew")
  (subpath "/private/var/db/dyld") (subpath "/private/etc")
  (subpath "/Library/Preferences")
  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))

(allow file-read* file-write* (subpath "{per_user_root}"))
(allow file-write-create (subpath "/private/tmp"))

(allow file-read-data (literal "{home}/.codex"))
(allow file-read* (literal "{home}/.codex/auth.json"))
(allow file-read* file-write* (literal "{home}/.codex/installation_id"))
(allow file-read* file-write* (subpath "{home}/.codex/tmp"))
(allow file-read* file-write*
  (regex #"^{re.escape(home)}/\\.codex/[a-z]+_[0-9]+\\.sqlite.*$"))

(allow file-read* file-write* (subpath "{iso_dir}"))
"""


# HTTP-style retry classification doesn't apply to a subprocess call — codex
# exit codes / stderr substrings play the same role. Rate-limit/transient
# markers are retried up to JUDGE_RETRY_COUNT times; auth/not-found markers
# raise immediately (retrying won't fix them).
_RETRYABLE_MARKERS = ("rate limit", "429", "usage limit", "too many requests", "unavailable")
_FATAL_MARKERS = ("unauthorized", "not logged in", "401", "authentication")


def _classify_stderr(stderr: str) -> str:
    low = stderr.lower()
    if any(m in low for m in _FATAL_MARKERS):
        return "fatal"
    if any(m in low for m in _RETRYABLE_MARKERS):
        return "retryable"
    return "fatal"


def _call_openai(prompt: str, model: str = OPENAI_JUDGE_MODEL) -> str:
    """Synchronous codex-exec call, sandboxed per the module docstring.

    Each attempt gets a fresh isolated temp directory (this-user-scoped,
    via `_darwin_per_user_root()` — never a bare `tempfile.mkdtemp()`,
    which would honor TMPDIR and could land outside the validated Seatbelt
    grant under a stripped environment) and a fresh Seatbelt profile file;
    both are cleaned up in `finally` regardless of outcome.
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise RuntimeError("`codex` CLI not found on PATH.")
    codex_real = os.path.realpath(codex_path)

    last_exc: Optional[RuntimeError] = None
    for attempt in range(JUDGE_RETRY_COUNT + 1):
        per_user_root = _darwin_per_user_root()
        iso_dir = tempfile.mkdtemp(dir=per_user_root)
        profile_path = os.path.join(per_user_root, f"{secrets.token_hex(8)}.sb")
        schema_path = os.path.join(iso_dir, "schema.json")
        out_path = os.path.join(iso_dir, "out.json")
        try:
            with open(profile_path, "w", encoding="utf-8") as f:
                f.write(_build_sandbox_profile(codex_real, per_user_root, iso_dir))
            # --output-schema expects a raw JSON Schema file, not an OpenAI
            # response_format envelope — confirmed live: wrapping it raises
            # "'json_schema' is not valid under any of the given schemas."
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(_RESPONSE_SCHEMA, f)

            cmd = ["sandbox-exec", "-f", profile_path, "--", "codex", "exec"]
            for feature in _DISABLE_FEATURES:
                cmd += ["--disable", feature]
            cmd += [
                "-c", 'web_search="disabled"',
                "-c", "tools.view_image=false",
                # "high": --ignore-user-config drops config.toml's own
                # reasoning-effort default, and rubric-adherence accuracy on
                # a 4-dimension structured-scoring task benefits more from
                # higher effort than latency does from lower — accepted
                # tradeoff despite the retry loops below compounding worst-
                # case wall-clock (up to (JUDGE_RETRY_COUNT+1)^2 calls).
                "-c", 'model_reasoning_effort="high"',
                "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                "--cd", iso_dir,
                "--output-schema", schema_path, "-o", out_path,
                "-m", model, "-",
            ]

            try:
                proc = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True, timeout=300,
                    cwd=iso_dir, env=_minimal_env(),
                )
            except FileNotFoundError:
                raise RuntimeError(
                    "`codex`/`sandbox-exec` not found on PATH. Install the codex CLI "
                    "(`brew install codex`) and run `codex login`."
                )
            except subprocess.TimeoutExpired:
                last_exc = RuntimeError("codex exec timed out after 300s.")
                if attempt == JUDGE_RETRY_COUNT:
                    raise last_exc
                continue

            if proc.returncode != 0:
                classification = _classify_stderr(proc.stderr)
                last_exc = RuntimeError(
                    f"codex exec exited {proc.returncode} for model {model}: "
                    f"{proc.stderr.strip()[:500]}"
                )
                if classification == "fatal" or attempt == JUDGE_RETRY_COUNT:
                    raise last_exc
                continue

            try:
                with open(out_path, encoding="utf-8") as f:
                    raw = f.read().strip()
            except OSError as exc:
                last_exc = RuntimeError(f"could not read codex output file: {exc}")
                if attempt == JUDGE_RETRY_COUNT:
                    raise last_exc
                continue
            if not raw:
                last_exc = RuntimeError(
                    f"codex produced an EMPTY final message. stderr: {proc.stderr.strip()[:200]}"
                )
                if attempt == JUDGE_RETRY_COUNT:
                    raise last_exc
                continue
            return raw
        finally:
            for p in (profile_path,):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            shutil.rmtree(iso_dir, ignore_errors=True)
    raise last_exc  # pragma: no cover — unreachable, loop always returns or raises


async def _call_openai_async(prompt: str, model: str = OPENAI_JUDGE_MODEL) -> str:
    """Async codex-exec call — runs sync in thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _call_openai(prompt, model))


def _cap_context_and_profile(
    context: Optional[str], user_profile: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Defense-in-depth truncation before either value reaches the codex
    subprocess.

    `user_profile` is already capped at JUDGE_MAX_PERSONA_CHARS upstream
    (evolution/persona.py, before it ever reaches evaluate()); this re-caps
    it anyway rather than trusting every future caller to do so. `context`
    has no upstream cap (BaseJudge's interface allows any caller to pass an
    arbitrarily large string, same as gemini_judge.py) — capped here to
    JUDGE_MAX_PROMPT_CHARS, the same bound already applied to `prompt`, as
    defense-in-depth for this newly-added, unvalidated provider specifically.
    """
    if context:
        context = context[:JUDGE_MAX_PROMPT_CHARS]
    if user_profile:
        user_profile = user_profile[:JUDGE_MAX_PERSONA_CHARS]
    return context, user_profile


# ── Runtime evaluator ─────────────────────────────────────────────────────────

class OpenAIRuntimeJudge(BaseJudge):
    """
    Evaluates production interactions using the structured RUBRIC via a
    sandboxed `codex exec` call (ChatGPT/Codex subscription auth, e.g.
    GPT-5.6 Luna/Terra/Sol). Returns a JudgeResult with per-dimension scores
    and a composite score.
    """

    def __init__(self, model: str = OPENAI_JUDGE_MODEL):
        self.model = model

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
        user_profile: Optional[str] = None,
    ) -> JudgeResult:
        prompt = prompt[:JUDGE_MAX_PROMPT_CHARS]
        response = (response or "")[:JUDGE_MAX_RESPONSE_CHARS]
        context, user_profile = _cap_context_and_profile(context, user_profile)
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context, user_profile)
        raw = _call_openai(eval_prompt, self.model)
        result = _parse_result(raw)
        if result.is_parse_error:
            for _ in range(JUDGE_RETRY_COUNT):
                raw = _call_openai(
                    _build_eval_prompt(prompt, response, tools_used, context, user_profile, strict_json=True),
                    self.model,
                )
                result = _parse_result(raw)
                if not result.is_parse_error:
                    break
        return result

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
        user_profile: Optional[str] = None,
    ) -> JudgeResult:
        prompt = prompt[:JUDGE_MAX_PROMPT_CHARS]
        response = (response or "")[:JUDGE_MAX_RESPONSE_CHARS]
        context, user_profile = _cap_context_and_profile(context, user_profile)
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context, user_profile)
        raw = await _call_openai_async(eval_prompt, self.model)
        result = _parse_result(raw)
        if result.is_parse_error:
            for _ in range(JUDGE_RETRY_COUNT):
                raw = await _call_openai_async(
                    _build_eval_prompt(prompt, response, tools_used, context, user_profile, strict_json=True),
                    self.model,
                )
                result = _parse_result(raw)
                if not result.is_parse_error:
                    break
        return result


def _build_eval_prompt(
    prompt: str,
    response: str,
    tools_used: Optional[list[str]],
    context: Optional[str],
    user_profile: Optional[str] = None,
    strict_json: bool = False,
) -> str:
    parts = [RUBRIC, "\n## Interaction to evaluate\n"]
    if context:
        parts.append(f"**Context:** {context}\n")
    if user_profile:
        parts.append(f"**Known user preferences (stored profile):**\n{user_profile}\n")
    parts.append(f"**User prompt:**\n{prompt}\n")
    if tools_used:
        parts.append(f"**Tools used:** {', '.join(tools_used)}\n")
    parts.append(f"**Agent response:**\n{response}\n")
    if strict_json:
        parts.append(
            "\nIMPORTANT: Respond with ONLY a valid JSON object. "
            "No markdown fences, no explanation, just the raw JSON.\n"
        )
    return "\n".join(parts)


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")


def _parse_result(raw: str) -> JudgeResult:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            data = candidate
    except json.JSONDecodeError:
        pass

    if data is None:
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    data = candidate
            except json.JSONDecodeError:
                pass

    if data is None:
        print(
            f"[judge] Parse error: no JSON found | raw={raw[:200]}",
            file=sys.stderr,
        )
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
            is_parse_error=True,
        )

    try:
        quality = _normalize_dim("quality", data)
        safety = _normalize_dim("safety", data)
        tool_use = _normalize_dim("tool_use", data)
        personalization = _normalize_dim("personalization", data)
        dims = {
            "quality": quality,
            "safety": safety,
            "tool_use": tool_use,
            "personalization": personalization,
        }
        return JudgeResult(
            score=compose_score(dims),
            rationale=data.get("rationale", ""),
            raw_response=raw,
            **dims,
        )
    except (KeyError, ValueError) as exc:
        print(
            f"[judge] Parse error: {exc.__class__.__name__}: {exc} | raw={raw[:200]}",
            file=sys.stderr,
        )
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
            is_parse_error=True,
        )


def make_runtime_judge(model: str = OPENAI_JUDGE_MODEL) -> OpenAIRuntimeJudge:
    """Return an OpenAIRuntimeJudge for scoring production interactions."""
    return OpenAIRuntimeJudge(model=model)
