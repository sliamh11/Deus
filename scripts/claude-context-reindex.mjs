#!/usr/bin/env node
/**
 * claude-context-reindex.mjs — Trigger index_codebase via the claude-context MCP server.
 *
 * Reads server config from ~/.claude.json, spawns the MCP stdio server,
 * sends a tools/call request for index_codebase, and clears the stale flag
 * (~/.deus/index_stale.flag) on success.
 *
 * Usage: node scripts/claude-context-reindex.mjs [directory]
 * Default directory: CWD (git repo root)
 *
 * Designed to run in the background (non-blocking for the caller):
 *   node scripts/claude-context-reindex.mjs /path/to/repo >> ~/.deus/index_reindex.log 2>&1 &
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const STALE_FLAG = path.join(os.homedir(), '.deus', 'index_stale.flag');
const LOG_PREFIX = '[claude-context-reindex]';

function log(msg) {
  const ts = new Date().toISOString();
  process.stderr.write(`${ts} ${LOG_PREFIX} ${msg}\n`);
}

function loadClaudeConfig() {
  // MCP servers live in ~/.claude.json per project convention
  const cfgPath = path.join(os.homedir(), '.claude.json');
  try {
    return JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  } catch (err) {
    throw new Error(`Cannot read ~/.claude.json: ${err.message}`);
  }
}

function getMcpServerConfig(config) {
  const servers = config.mcpServers || {};
  const server = servers['claude-context'];
  if (!server) {
    throw new Error('No "claude-context" entry in ~/.claude.json mcpServers — is claude-context installed?');
  }
  return server;
}

/** Send a JSON-RPC message over MCP's Content-Length framed stdio protocol. */
function sendMessage(stdin, msg) {
  const body = JSON.stringify(msg);
  const header = `Content-Length: ${Buffer.byteLength(body, 'utf8')}\r\n\r\n`;
  stdin.write(header + body);
}

/**
 * Parse LSP/MCP framed messages from a raw Buffer stream.
 * Returns a function `drain()` that yields all fully-received messages so far.
 */
function makeFrameParser() {
  let buf = Buffer.alloc(0);
  const queue = [];

  function onData(chunk) {
    buf = Buffer.concat([buf, chunk]);
    // eslint-disable-next-line no-constant-condition
    while (true) {
      // Find header separator
      const sep = buf.indexOf('\r\n\r\n');
      if (sep === -1) break;

      const header = buf.slice(0, sep).toString('utf8');
      const m = header.match(/Content-Length:\s*(\d+)/i);
      if (!m) {
        // Malformed — skip past separator
        buf = buf.slice(sep + 4);
        continue;
      }
      const len = parseInt(m[1], 10);
      const bodyStart = sep + 4;
      if (buf.length < bodyStart + len) break; // wait for more data

      const body = buf.slice(bodyStart, bodyStart + len).toString('utf8');
      buf = buf.slice(bodyStart + len);
      try {
        queue.push(JSON.parse(body));
      } catch {
        // ignore malformed JSON
      }
    }
  }

  function drain() {
    return queue.splice(0);
  }

  return { onData, drain };
}

/**
 * Wait up to `timeoutMs` for a JSON-RPC response with the given id.
 * Polls `drain()` every 200 ms.
 */
function waitForId(drain, id, timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const tick = setInterval(() => {
      const msgs = drain();
      for (const msg of msgs) {
        if (msg.id === id) {
          clearInterval(tick);
          resolve(msg);
          return;
        }
        // Re-queue anything that isn't ours (simple: just ignore — we only care about our id)
      }
      if (Date.now() > deadline) {
        clearInterval(tick);
        reject(new Error(`Timeout (${timeoutMs}ms) waiting for response id=${id}`));
      }
    }, 200);
  });
}

async function runReindex(directory) {
  const config = loadClaudeConfig();
  const serverCfg = getMcpServerConfig(config);

  const { command, args = [], env: serverEnv = {} } = serverCfg;
  const mergedEnv = { ...process.env, ...serverEnv };

  log(`Spawning MCP server: ${command} ${args.join(' ')}`);
  log(`Target directory: ${directory}`);

  const child = spawn(command, args, {
    env: mergedEnv,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  child.on('error', (err) => {
    throw new Error(`Failed to spawn MCP server: ${err.message}`);
  });

  child.stderr.on('data', (d) => {
    log(`[server] ${d.toString().trimEnd()}`);
  });

  const parser = makeFrameParser();
  child.stdout.on('data', (chunk) => parser.onData(chunk));

  // 1. Initialize handshake
  sendMessage(child.stdin, {
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'deus-reindex', version: '1.0.0' },
    },
  });

  const initResp = await waitForId(parser.drain, 1, 30_000);
  if (initResp.error) {
    throw new Error(`initialize failed: ${JSON.stringify(initResp.error)}`);
  }
  log(`Server ready: ${JSON.stringify(initResp.result?.serverInfo ?? {})}`);

  // 2. Notify server that client is initialized (required by MCP spec)
  sendMessage(child.stdin, {
    jsonrpc: '2.0',
    method: 'notifications/initialized',
    params: {},
  });

  // 3. Call index_codebase — can take several minutes for large repos
  log('Calling index_codebase...');
  sendMessage(child.stdin, {
    jsonrpc: '2.0',
    id: 2,
    method: 'tools/call',
    params: {
      name: 'index_codebase',
      arguments: { directory },
    },
  });

  const callResp = await waitForId(parser.drain, 2, 600_000); // 10-min ceiling
  if (callResp.error) {
    throw new Error(`index_codebase error: ${JSON.stringify(callResp.error)}`);
  }

  const resultText = callResp.result?.content?.[0]?.text ?? JSON.stringify(callResp.result);
  log(`index_codebase done: ${resultText}`);

  child.stdin.end();

  // 4. Clear stale flag — index is now fresh
  try {
    fs.unlinkSync(STALE_FLAG);
    log('Stale flag cleared (~/.deus/index_stale.flag removed).');
  } catch {
    // Flag may have already been removed; that's fine
  }
}

async function main() {
  const directory = process.argv[2] ? path.resolve(process.argv[2]) : process.cwd();

  // Ensure ~/.deus exists (marker directory)
  fs.mkdirSync(path.join(os.homedir(), '.deus'), { recursive: true });

  log(`Starting reindex for ${directory}`);
  try {
    await runReindex(directory);
    log('Reindex complete — index is up to date.');
    process.exit(0);
  } catch (err) {
    log(`FAILED: ${err.message}`);
    process.exit(1);
  }
}

main();
