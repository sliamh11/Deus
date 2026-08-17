# Surfaces — how to reach one and what to keep

Loaded on demand from `SKILL.md` §3. Find your row, drive it, keep the artefact
named under "Capture". Everything here assumes you already have the software
running (see `SKILL.md` §4 — that is `/run`'s job, not this file's).

## Terminal — CLI

Run the real entrypoint as a user would, not `node ./src/thing.js`.

```bash
<entrypoint> <the subcommand the diff touches> ; echo "exit=$?"
```

**Capture:** the full stdout/stderr *and* the exit status. A status alone hides
the message; a message alone hides whether the shell saw failure. Both, always —
"exits non-zero and says why" is two observables.

Note stderr and stdout separately when the claim concerns one of them
(`2>/dev/null` to prove a message went to stderr rather than stdout).

## Terminal — TUI / interactive

Drive it inside a private tmux server so you never touch the host's sessions.

```bash
sk="verify-$$"                                    # socket + session unique to this run
tmux -L "$sk" new-session -d -s "$sk" -x 120 -y 40 '<entrypoint>'

ready=""
for _ in $(seq 60); do                            # ~30s ceiling
  tmux -L "$sk" has-session -t "$sk" 2>/dev/null || break
  tmux -L "$sk" capture-pane -p -t "$sk" | grep -q '<the ready marker>' && { ready=1; break; }
  sleep 0.5
done

tmux -L "$sk" capture-pane -p -t "$sk" 2>/dev/null   # capture either way
[ -n "$ready" ] && tmux -L "$sk" send-keys -t "$sk" '<the navigation>' && sleep 0.5 \
  && tmux -L "$sk" capture-pane -p -t "$sk"
tmux -L "$sk" kill-session -t "$sk" 2>/dev/null      # this run's session only
```

Four requirements. Adapt the shape freely; these are what make it trustworthy:

- **A socket and session name unique per run.** A fixed pair collides with a
  concurrent verification or a stale session from an interrupted one, and then
  `new-session` fails while the following commands happily capture *someone else's*
  session — evidence for the wrong software.
- **`kill-session`, never `kill-server`.** On a shared socket, `kill-server` ends
  every session on it, including another verification's.
- **Bound the wait, and check the process is alive.** A program that dies at
  startup otherwise leaves you looping instead of reporting BLOCKED. Both are real
  outcomes; only one is a hang.
- **Capture the pane on the failure path too.** What it printed before dying is
  the most useful evidence available, and never becoming ready is a BLOCKED verdict
  that still needs evidence attached.

Poll for a ready marker rather than sleeping a fixed interval, and say which string
means ready. Fix the geometry with `-x`/`-y`; some interfaces hide
content at narrow widths, and a capture that differs between control and
treatment only because the pane was a different size is a confound, not a
finding.

**Capture:** the pane dump. `-e` keeps escape sequences when colour is the
claim; `-J` rejoins wrapped lines when the wrap is getting in the way.

## Socket — server / API

Launch in the background, wait for the port rather than for a duration, then hit
the specific route the diff touches.

```bash
<launch> &
pid=$!

up=""
for _ in $(seq 60); do                                   # ~30s ceiling
  kill -0 "$pid" 2>/dev/null || { echo "server exited before listening"; break; }
  curl -sf -o /dev/null "http://127.0.0.1:<port>/<health>" && { up=1; break; }
  sleep 0.5
done

if [ -z "$up" ]; then
  # Kill BEFORE waiting: a server that is alive but never healthy would make a
  # bare `wait` block forever, defeating the ceiling above.
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  echo "never came up — BLOCKED; the launch output is the evidence"
else
  curl -sS -D - -o body.txt "http://127.0.0.1:<port>/<the changed route>"
  kill "$pid" 2>/dev/null
fi
```

Same requirements as the TUI loop, plus one specific to a backgrounded process:
**kill before `wait`.** An unbounded `until curl ...` never returns against a server
that crashed at startup, and a bare `wait` never returns against one that is alive
but permanently unhealthy — both convert a legitimate BLOCKED into a stalled
session. Bind to a per-run port too (or port 0 and read back what was assigned):
a fixed port collides with a concurrent run, and you may end up measuring its
server rather than yours.

Bind port 0 (or a per-run port) when anything else on the host might be
listening; two runs racing for one port is a confound that looks like flakiness.

**Capture:** status line, the headers that carry the claim, and the body. A
claim about `Retry-After` is not evidenced by a status code, and "returns 429"
is not evidenced by a body that happens to mention rate limits.

## Pixels — GUI / web

Drive headless, then **open the screenshot and look at it.** An all-white frame
is a failed launch that a passing script will happily report as success, and no
amount of exit-status checking catches it.

Read the rendered text too, not only the image — an accessibility snapshot or a
text extraction proves the content is present rather than merely that pixels
were drawn.

**Capture:** the image, plus whatever text you extracted. Both control and
treatment at the same viewport.

## Package boundary — library / SDK

Consume the built, packaged artefact the way an outside caller would: import the
public entry, never a deep path into the source tree. Where the packaging step
itself can drop an export, install the packed tarball into a scratch directory
and import from there — that catches an export missing from the manifest, which
importing from the working tree cannot.

**Capture:** the sample program and its output, verbatim.

## Compiler — build-time API

A public type with no runtime emission still has a real surface: the compiler,
as experienced by a consumer outside the package. Write the smallest external
consumer that exercises the changed declaration, and compile it against the
*shipped* declarations in both states.

**Capture:** the compiler's own diagnostics. Control failing to compile with an
error naming the missing member, and treatment compiling clean, is a proper
control/treatment pair.

## The agent — prompt, role spec, gate spec

The surface is the agent's behaviour, not the text you edited. Run it on an
input that reaches the changed instruction and record what it did.

Where the change is meant to *stop* something, the observable is the refusal
under the condition that should trigger it — and the corresponding
non-interference when it should not. Both halves: a gate that blocks everything
satisfies its own claim while being useless.

**Capture:** the transcript, or the artefact the agent produced. Where a
decision is logged, the log line.

## The runner — CI workflow

Trigger the event the change actually concerns. A manual dispatch does not
reproduce push, pull-request, or scheduled semantics, nor their permissions and
payloads, and by default it targets the repository's default branch — so a
dispatch can silently exercise a different version of the workflow than the one
you changed.

Pass the ref explicitly, then confirm the run's head SHA before you believe it.
Where the real event cannot be reproduced before merge, that is BLOCKED or
INCONCLUSIVE — not an excuse to grade the default branch's CI and call it
verified.

**Capture:** the run's identity (SHA, event, workflow file version) plus the log
region that shows the changed behaviour.
