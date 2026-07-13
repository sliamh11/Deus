/**
 * Host-side tool proxy for container agents.
 *
 * Containers call POST /tool/:cli-name with { args, compact?, timeout? }.
 * The proxy executes the registered host binary (never arbitrary commands),
 * injects credentials from the host environment, and returns:
 *   { exit: number, stdout: string, stderr: string }
 *
 * Exit codes follow the printing-press-adoption ADR typed exit code convention:
 *   0 = SUCCESS, 2 = USAGE_ERROR, 3 = NOT_FOUND, 4 = AUTH_ERROR, 5 = INTERNAL_ERROR
 *
 * Security:
 *   - Allowlist-only: tool name must be registered in ~/.deus/tool-registry.json
 *   - Arg sanitization: shell metacharacters rejected (no shell is involved, but
 *     defense-in-depth against binaries that parse args unsafely)
 *   - Auth: same x-deus-proxy-token gate as credential-proxy.ts
 *   - Credentials injected via process.env at spawn time, never passed to containers
 *
 * Pattern: mirrors startCredentialProxy in credential-proxy.ts.
 */

import { createServer, Server } from 'http';
import { execFile } from 'child_process';

import { DEUS_PROXY_AUTH_ENABLED } from './config.js';
import { getProjectById, getRegisteredGroupByFolder } from './db.js';
import { validateGroupToken, isToolAllowedForToken } from './group-tokens.js';
import { logger } from './logger.js';
import { loadRegistry, isAllowed, getToolConfig } from './tool-registry.js';

/** Default per-execution timeout in milliseconds. */
const DEFAULT_TOOL_TIMEOUT_MS = 30_000;

/** Regex for valid tool names — lowercase letters, digits, hyphens only. */
const TOOL_NAME_RE = /^[a-z][a-z0-9-]*$/;

/**
 * Shell metacharacters to reject in args.
 * execFile does not invoke a shell, but defense-in-depth for binaries that
 * internally parse args via a shell (e.g., scripts that call eval or system()).
 */
const SHELL_META_RE = /[;|&$`\n\r\0]/;

/** Validate a single argument string. Returns error message or null if safe. */
function validateArg(arg: string): string | null {
  if (typeof arg !== 'string') return 'all args must be strings';
  if (SHELL_META_RE.test(arg))
    return `arg contains forbidden character: ${arg}`;
  return null;
}

/**
 * `gh` global flags that consume the following token as their value. `-X` /
 * `--method` are included so a method verb (e.g. the `PUT` in `-X PUT`) is not
 * mistaken for a positional subcommand when reading the subcommand path.
 */
const GH_FLAGS_WITH_VALUE = new Set([
  '-R',
  '--repo',
  '--hostname',
  '-X',
  '--method',
]);

/**
 * `gh` subcommand paths that are known-safe READS: a first-position subcommand
 * mapped to the exact set of allowed second-position subcommands. Anything not
 * listed here is treated as gated — an allowlist, not a denylist, so a new or
 * unknown `gh` subcommand (or an unknown `gh search` target) fails closed.
 */
const GH_READ_SUBCOMMANDS: Record<string, ReadonlySet<string>> = {
  pr: new Set(['view', 'list', 'diff', 'status', 'checks']),
  issue: new Set(['view', 'list', 'status']),
  repo: new Set(['view', 'list']),
  release: new Set(['view', 'list']),
  run: new Set(['view', 'list']),
  workflow: new Set(['view', 'list']),
  search: new Set(['code', 'commits', 'issues', 'prs', 'repos']),
};

/**
 * `gh api` shorthand flags that consume a value — either glued into the same
 * token or taken from the following token. Needed to walk POSIX shorthand
 * clusters (pflag): `-if k=v` means `-i -f k=v`, so a value flag hidden behind
 * a boolean shorthand like `-i`/`--include` must not be missed. `X`=method,
 * `f`/`F`=body param, `H`=header (mutation-relevant); `q`/`p`/`t`=read-only
 * filters (they consume a value but do not mutate).
 */
const GH_API_VALUE_SHORTHANDS = new Set(['X', 'f', 'F', 'H', 'q', 'p', 't']);

/**
 * True if a `gh api` invocation can mutate GitHub state (so it must be gated).
 * `gh api` defaults to GET (a read) and switches to POST when body params are
 * added; an explicit method flag can select any verb. Fail-closed: anything not
 * provably a plain GET is treated as mutating. A real shorthand-cluster walk
 * (not string-prefix sniffing) handles glued (`-XPUT`), `=`-form
 * (`--method=PUT`), dangling, AND POSIX-clustered (`-if k=v`, `-iX PUT`) forms,
 * and scans every `-H`/`--header` value for an `X-HTTP-Method-Override`
 * (defense-in-depth; GitHub REST v3 does not honor it, but the check is cheap).
 */
function ghApiIsMutating(args: string[]): boolean {
  const isMutatingMethod = (v: string | undefined): boolean =>
    v === undefined || v.trim().toUpperCase() !== 'GET';

  for (let i = 0; i < args.length; i++) {
    const a = args[i];

    // --- long forms: --method / --field / --raw-field / --input / --header ---
    if (a === '--method' || a.startsWith('--method=')) {
      const v = a === '--method' ? args[++i] : a.slice('--method='.length);
      if (isMutatingMethod(v)) return true;
      continue;
    }
    if (
      a === '--field' ||
      a.startsWith('--field=') ||
      a === '--raw-field' ||
      a.startsWith('--raw-field=') ||
      a === '--input' ||
      a.startsWith('--input=')
    ) {
      return true; // request body present
    }
    if (a === '--header' || a.startsWith('--header=')) {
      const v = a === '--header' ? args[++i] : a.slice('--header='.length);
      if (v !== undefined && /method-override/i.test(v)) return true;
      continue;
    }

    // --- POSIX shorthand cluster: -i, -X, -f, -if, -iX, -ifk=v, ... ---
    if (a.length >= 2 && a[0] === '-' && a[1] !== '-') {
      for (let j = 1; j < a.length; j++) {
        const ch = a[j];
        if (!GH_API_VALUE_SHORTHANDS.has(ch)) continue; // boolean flag (e.g. i)
        // Value = rest of this token after ch, else the next token (consumed).
        const glued = a.slice(j + 1);
        const val = glued.length > 0 ? glued : args[++i];
        if (ch === 'X') {
          if (isMutatingMethod(val)) return true;
        } else if (ch === 'f' || ch === 'F') {
          return true; // request body present
        } else if (ch === 'H') {
          if (val !== undefined && /method-override/i.test(val)) return true;
        }
        // q/p/t are read-only filters; their value is consumed above. Either
        // way the token's remainder was this flag's value — stop walking it.
        break;
      }
    }
  }
  return false;
}

/**
 * True if this tool invocation must be gated as publish/mutate/execute-capable
 * for an untrusted external project — i.e. it could push, merge, publish, mutate
 * GitHub state, or execute code using the host's GitHub credentials. Detection
 * is by SUBCOMMAND POSITION, not substring, so a branch literally named "push"
 * does not false-trip. Platform-neutral (tool names + arg tokens only).
 *
 * Fail-closed allowlist for `gh`: only the read subcommands in
 * `GH_READ_SUBCOMMANDS` (and a non-mutating `gh api` GET) are allowed through;
 * every other subcommand — including `alias`/`extension` (which can persist an
 * alias or install code that later runs with host creds), `secret`, `release`,
 * `repo`, `pr merge|create|close`, and anything unknown — is gated (LIA-361).
 */
export function isPushOrMergeTool(toolName: string, args: string[]): boolean {
  // The dedicated push tool is always a push.
  if (toolName === 'deus-git-push') return true;
  if (toolName === 'gh') {
    // Collect the first two positional (non-flag) tokens, skipping global flags
    // and their values, to read the subcommand path.
    const positional: string[] = [];
    for (let i = 0; i < args.length && positional.length < 2; i++) {
      const a = args[i];
      if (a.startsWith('-')) {
        if (GH_FLAGS_WITH_VALUE.has(a)) i++; // skip the flag's value token
        continue;
      }
      positional.push(a);
    }
    const [c0, c1] = positional;
    if (c0 === 'api') return ghApiIsMutating(args); // read GET allowed
    const reads = c0 ? GH_READ_SUBCOMMANDS[c0] : undefined;
    if (reads && c1 !== undefined && reads.has(c1)) {
      return false; // known safe read
    }
    return true; // fail closed: unknown / mutating / exec-capable subcommand
  }
  return false;
}

/**
 * Returns a denial message if a push/merge from *groupFolder* must be blocked,
 * or null if allowed. Allowed = the home/control project (no projectId) or an
 * external project with `allow_external_push`. Fail-closed: an unresolvable
 * group or unregistered folder is denied (we cannot prove it is internal).
 */
export function externalPushDenialReason(
  groupFolder: string | null,
): string | null {
  if (!groupFolder) {
    return 'External-project git push/merge is blocked: caller group could not be resolved (no proxy token).';
  }
  const group = getRegisteredGroupByFolder(groupFolder);
  if (!group) {
    return `External-project git push/merge is blocked: group "${groupFolder}" is not registered.`;
  }
  if (!group.projectId) return null; // home/control project — allowed
  const project = getProjectById(group.projectId);
  if (project?.allow_external_push === true) return null; // allowlisted external
  return `External-project git push/merge is blocked for project "${group.projectId}". Allowlist this project to enable push/merge (operator action — see LIA-180).`;
}

export function startToolProxy(
  port: number,
  host = '127.0.0.1',
): Promise<Server> {
  // Warm the registry cache on startup so the first request doesn't cold-load.
  loadRegistry();

  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        // ── Resolve caller's group folder ─────────────────────────────────
        // Resolved ALWAYS (independent of auth enforcement) because the
        // push/merge gate below needs it even when DEUS_PROXY_AUTH is disabled
        // (dev). Auth ENFORCEMENT (401) stays gated on DEUS_PROXY_AUTH_ENABLED.
        const token = req.headers['x-deus-proxy-token'] as string | undefined;
        const groupFolder = token ? validateGroupToken(token) : null;
        if (DEUS_PROXY_AUTH_ENABLED && !groupFolder) {
          logger.warn(
            { url: req.url, hasToken: !!token },
            'Tool proxy rejected unauthenticated request',
          );
          res.writeHead(401);
          res.end('Unauthorized');
          return;
        }

        // ── Route: POST /tool/:name ────────────────────────────────────────
        if (req.method !== 'POST') {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Method not allowed' }));
          return;
        }

        // Parse and validate tool name from URL
        const urlMatch = (req.url ?? '').match(/^\/tool\/([^/?#]+)$/);
        if (!urlMatch) {
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end(
            JSON.stringify({ error: 'Not found — use POST /tool/:name' }),
          );
          return;
        }

        const rawName = urlMatch[1];
        if (!TOOL_NAME_RE.test(rawName)) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: `Invalid tool name: ${rawName}` }));
          return;
        }

        // Allowlist check
        if (!isAllowed(rawName)) {
          logger.warn(
            { tool: rawName },
            'Tool proxy rejected unregistered tool',
          );
          res.writeHead(403, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: `Tool not allowed: ${rawName}` }));
          return;
        }

        // Per-token scope check (LIA-315 Phase 2, R2). A scoped (publicIngress)
        // token may invoke ONLY its curated tool set; everything else 403s here
        // even though it passed the global allowlist. Unscoped (normal-group)
        // tokens are unaffected — isToolAllowedForToken returns true for them.
        // The `token &&` guard only no-ops when auth is disabled (dev, no token);
        // publicIngress containers always receive an injected DEUS_PROXY_TOKEN, so
        // the scope check always fires for them.
        if (token && !isToolAllowedForToken(token, rawName)) {
          logger.warn(
            { tool: rawName },
            'Tool proxy rejected tool outside token scope',
          );
          res.writeHead(403, { 'Content-Type': 'application/json' });
          res.end(
            JSON.stringify({ error: `Tool not in token scope: ${rawName}` }),
          );
          return;
        }

        // Parse body
        let body: { args?: unknown; compact?: unknown; timeout?: unknown };
        try {
          body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
        } catch {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Invalid JSON body' }));
          return;
        }

        // Validate args
        if (!Array.isArray(body.args)) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: '"args" must be an array' }));
          return;
        }

        const args = body.args as unknown[];
        for (const arg of args) {
          const argErr = validateArg(arg as string);
          if (argErr) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: argErr }));
            return;
          }
        }

        const safeArgs = args as string[];

        // ── Push/merge gate ────────────────────────────────────────────────
        // External projects cannot git push/merge by default. Allowed only for
        // the home/control project or an allowlisted external project (and
        // fail-closed when the caller group is unresolvable). Default-block
        // applies to everyone, independent of the auth-enforcement flag.
        if (isPushOrMergeTool(rawName, safeArgs)) {
          const denial = externalPushDenialReason(groupFolder);
          if (denial) {
            logger.warn(
              { tool: rawName, groupFolder },
              'Tool proxy blocked external push/merge',
            );
            res.writeHead(403, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: denial }));
            return;
          }
        }

        // Resolve tool config (binary path + injected env)
        const toolConfig = getToolConfig(rawName);
        if (!toolConfig) {
          // Should not happen (isAllowed passed), but guard anyway
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(
            JSON.stringify({
              error: 'Tool config disappeared after allowlist check',
            }),
          );
          return;
        }

        // Determine timeout: body > tool config > default
        const timeoutMs =
          typeof body.timeout === 'number' && body.timeout > 0
            ? body.timeout
            : (toolConfig.timeout ?? DEFAULT_TOOL_TIMEOUT_MS);

        // Append --compact if requested
        const execArgs = [...safeArgs];
        if (body.compact === true) {
          execArgs.push('--compact');
        }

        // Inject tool credentials from process.env (never from container env)
        const execEnv = { ...process.env, ...toolConfig.env };

        logger.debug(
          { tool: rawName, args: execArgs, timeout: timeoutMs },
          'Tool proxy executing',
        );

        execFile(
          toolConfig.binary,
          execArgs,
          { timeout: timeoutMs, env: execEnv },
          (err, stdout, stderr) => {
            const errAny = err as
              | (NodeJS.ErrnoException & { killed?: boolean; code?: unknown })
              | null;

            // Timeout
            if (errAny?.killed || errAny?.code === 'ETIMEDOUT') {
              logger.warn(
                { tool: rawName, timeout: timeoutMs },
                'Tool proxy execution timed out',
              );
              res.writeHead(504, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ error: 'Execution timed out' }));
              return;
            }

            // Binary not found or permission error (non-printing-press errors)
            if (errAny && typeof errAny.code === 'string') {
              logger.error(
                { err: errAny, tool: rawName },
                'Tool proxy spawn error',
              );
              res.writeHead(500, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ error: `Spawn error: ${errAny.code}` }));
              return;
            }

            // Normal execution — return exit code + output (even on non-zero exit).
            // The printing-press typed exit codes (2/3/4/5) are meaningful to callers
            // and must not be converted to HTTP errors.
            const exitCode = errAny?.code != null ? Number(errAny.code) : 0;
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ exit: exitCode, stdout, stderr }));
          },
        );
      });
    });

    // Port-retry loop — matches credential-proxy.ts pattern
    let retries = 0;
    const maxRetries = 10;
    const retryDelay = 2000;

    const tryListen = () => {
      server.listen(port, host, () => {
        logger.info({ port, host }, 'Tool proxy started');
        resolve(server);
      });
    };

    server.on('error', (err: NodeJS.ErrnoException) => {
      if (err.code === 'EADDRINUSE' && retries < maxRetries) {
        retries++;
        logger.warn(
          { port, attempt: retries, maxRetries },
          'Tool proxy port in use, retrying...',
        );
        server.close();
        setTimeout(tryListen, retryDelay);
      } else {
        reject(err);
      }
    });

    tryListen();
  });
}
