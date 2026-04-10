# ADR: IPC via shared-volume file output (not stdout)

**Status:** Accepted
**Date:** 2026-03-29
**Scope:** `eval/`, `container/`

## Context

The eval layer needs to read the agent's response after it runs inside a Docker container. The natural approach is to read stdout — the container's entrypoint already writes a JSON result there.

However, Docker buffers a container's stdout in a kernel pipe and only flushes it to the host when the container process exits. This creates a deadlock: the host is waiting for output to parse before sending `_close`, but the container is waiting for `_close` before it exits.

## Decision

Results are written to a shared-volume directory (`/workspace/ipc/output/{seq}.json`) instead of stdout. The host polls this directory (200ms interval) and stops as soon as a file with a non-null `result` appears. Stdout is still drained in a background thread to prevent the pipe from filling and blocking the container, but its content is ignored.

## Consequences

- **No deadlock.** Shared-volume writes are immediately visible on the host; no kernel pipe buffering involved.
- **Slightly more filesystem I/O** per eval run (temp dirs created and cleaned up per container).
- **Do not revert** to stdout-based result parsing — Docker pipe buffering is a permanent constraint of the Docker runtime, not a fixable bug.
