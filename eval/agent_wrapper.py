"""
Black-box interface to the Deus container agent.

Spawns a Docker container, pipes ContainerInput JSON to stdin, and reads the
result from the IPC output directory (mounted volume, not stdout). Stdout is
drained to prevent pipe deadlocks but is not used for result parsing — Docker
buffers the container's stdout until process exit, making it unreliable for
real-time result detection.

Auth mirrors what container-runner.ts does at runtime:
  API-key mode:  set ANTHROPIC_API_KEY in env
  OAuth mode:    set CLAUDE_CODE_OAUTH_TOKEN in env (can be the real token or
                 "placeholder" when the credential proxy is already running)

The credential proxy (localhost:3001) must be running when using OAuth mode.
Containers reach it via host.docker.internal:3001.

Environment:
  ANTHROPIC_API_KEY         Set for API-key auth mode.
  CLAUDE_CODE_OAUTH_TOKEN   Set for OAuth auth mode.
  DEUS_EVAL_IMAGE           Docker image name (default: deus-agent:latest).
  DEUS_EVAL_TIMEOUT         Container timeout in seconds (default: 300).
  CREDENTIAL_PROXY_PORT     Credential proxy port (default: 3001).
"""

import io
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CONTAINER_IMAGE = os.environ.get("DEUS_EVAL_IMAGE", "deus-agent:latest")
CONTAINER_TIMEOUT = int(os.environ.get("DEUS_EVAL_TIMEOUT", "300"))
CREDENTIAL_PROXY_PORT = os.environ.get("CREDENTIAL_PROXY_PORT", "3001")
HOST_GATEWAY = "host.docker.internal"


@dataclass
class AgentResponse:
    status: str          # "success" or "error"
    result: str | None
    error: str | None = None
    new_session_id: str | None = None
    latency_ms: float = 0.0
    stderr_log: str = ""
    ipc_messages: list[dict] = field(default_factory=list)
    ipc_tasks: list[dict] = field(default_factory=list)


def _read_ipc_dir(base: str, subdir: str) -> list[dict]:
    target = Path(base) / subdir
    if not target.exists():
        return []
    results = []
    for f in sorted(target.glob("*.json")):
        try:
            results.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return results


def invoke_agent(
    prompt: str,
    *,
    session_id: str | None = None,
    group_folder: str = "eval_test",
    chat_jid: str = "eval@test",
    is_main: bool = True,
    assistant_name: str = "Andy",
    timeout: int | None = None,
    backend: str = "claude",
) -> AgentResponse:
    """
    Invoke the Deus agent in a Docker container and return the parsed response.
    """
    timeout = timeout or CONTAINER_TIMEOUT

    if backend == "openai":
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            raise RuntimeError("Set OPENAI_API_KEY for OpenAI backend eval runs")
    else:
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not oauth_token and not api_key:
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for eval runs"
            )

    container_input: dict = {
        "prompt": prompt,
        "groupFolder": group_folder,
        "chatJid": chat_jid,
        "isMain": is_main,
        "assistantName": assistant_name,
    }
    if backend == "openai":
        container_input["backend"] = "openai"
    if session_id:
        container_input["sessionId"] = session_id

    # Temp directories for IPC capture and group/session workspaces
    ipc_dir = tempfile.mkdtemp(prefix="deus-eval-ipc-")
    group_dir = tempfile.mkdtemp(prefix="deus-eval-group-")
    claude_dir = tempfile.mkdtemp(prefix="deus-eval-claude-")

    for subdir in ("messages", "tasks", "input", "output"):
        os.makedirs(os.path.join(ipc_dir, subdir), exist_ok=True)

    # Give each eval container a unique name so we can forcefully stop it on timeout.
    container_name = f"deus-eval-{os.getpid()}-{int(time.monotonic() * 1000)}"

    docker_args = ["docker", "run", "-i", "--rm", "--name", container_name, "-e", "TZ=UTC"]

    if backend == "openai":
        # Route through credential proxy — never pass real keys to containers
        docker_args += [
            "-e", f"OPENAI_BASE_URL=http://{HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}/openai",
            "-e", "OPENAI_API_KEY=placeholder",
        ]
        openai_model = os.environ.get("DEUS_OPENAI_MODEL", "")
        if openai_model:
            docker_args += ["-e", f"DEUS_OPENAI_MODEL={openai_model}"]
    else:
        # Route API calls through the credential proxy (same as production)
        docker_args += [
            "-e", f"ANTHROPIC_BASE_URL=http://{HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}",
        ]
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if oauth_token:
            # OAuth mode: send placeholder so proxy can inject the real token
            docker_args += ["-e", "CLAUDE_CODE_OAUTH_TOKEN=placeholder"]
        else:
            docker_args += ["-e", "ANTHROPIC_API_KEY=placeholder"]

    # Allow container to reach host.docker.internal (macOS Docker Desktop handles this)
    docker_args += ["--add-host", f"host.docker.internal:host-gateway"]

    docker_args += [
        "-v", f"{group_dir}:/workspace/group",
        "-v", f"{ipc_dir}:/workspace/ipc",
        "-v", f"{claude_dir}:/home/node/.claude",
        CONTAINER_IMAGE,
    ]

    ipc_close_path = os.path.join(ipc_dir, "input", "_close")

    t0 = time.monotonic()
    proc = subprocess.Popen(
        docker_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Send input and close stdin so the container can start processing.
    proc.stdin.write(json.dumps(container_input).encode())
    proc.stdin.close()

    # Drain stderr in a background thread (doesn't block on result detection).
    stderr_buf = io.BytesIO()

    def _drain_stderr() -> None:
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            stderr_buf.write(chunk)

    # Drain stdout in a background thread so the pipe never fills and deadlocks.
    # We don't parse stdout for results (Docker buffers it until container exit);
    # instead we poll the IPC output directory which is on a shared mount and
    # therefore immediately visible as soon as the container writes a file.
    stdout_buf = io.BytesIO()

    def _drain_stdout() -> None:
        for chunk in iter(lambda: proc.stdout.read(4096), b""):
            stdout_buf.write(chunk)

    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_drain_stdout, daemon=True).start()

    # Poll the IPC output directory for result files written by writeOutput().
    # The container writes /workspace/ipc/output/{seq}.json for each result.
    # We stop when we find a file where "result" is non-null (the real answer,
    # not a session-update with result=null).
    ipc_output_dir = Path(ipc_dir) / "output"
    deadline = t0 + timeout
    ipc_outputs: list[dict] = []

    while time.monotonic() < deadline:
        candidates = sorted(ipc_output_dir.glob("*.json"))
        if candidates:
            ipc_outputs = []
            for f in candidates:
                try:
                    ipc_outputs.append(json.loads(f.read_text()))
                except (json.JSONDecodeError, OSError):
                    pass
            # Stop as soon as we have a file with a real (non-null) result.
            if any(o.get("result") for o in ipc_outputs):
                break
        time.sleep(0.2)
    else:
        # Deadline exceeded — no result file appeared.
        proc.kill()
        subprocess.run(["docker", "stop", "--timeout", "5", container_name],
                       capture_output=True, timeout=10)
        return AgentResponse(
            status="error",
            result=None,
            error=f"Container timed out after {timeout}s",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    # Got a result — write _close so the container exits cleanly.
    try:
        open(ipc_close_path, "w").close()
    except OSError:
        pass

    # Give the container a moment to exit on its own, then force-stop.
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        subprocess.run(["docker", "stop", "--timeout", "2", container_name],
                       capture_output=True, timeout=10)

    stderr_bytes = stderr_buf.getvalue()
    latency_ms = (time.monotonic() - t0) * 1000
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    ipc_messages = _read_ipc_dir(ipc_dir, "messages")
    ipc_tasks = _read_ipc_dir(ipc_dir, "tasks")

    final_result = None
    final_status = "success"
    new_session_id = None
    for out in ipc_outputs:
        if out.get("newSessionId"):
            new_session_id = out["newSessionId"]
        if out.get("result"):
            final_result = out["result"]
            final_status = out.get("status", "success")

    return AgentResponse(
        status=final_status,
        result=final_result,
        new_session_id=new_session_id,
        latency_ms=latency_ms,
        stderr_log=stderr,
        ipc_messages=ipc_messages,
        ipc_tasks=ipc_tasks,
    )
