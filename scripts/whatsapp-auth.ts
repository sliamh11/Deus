#!/usr/bin/env npx tsx
/**
 * Standalone WhatsApp authentication script.
 * Uses baileys from the mcp-whatsapp workspace package.
 * Shows QR in terminal + writes to store/qr-data.txt for external rendering.
 */
import {
  makeWASocket,
  Browsers,
  DisconnectReason,
  fetchLatestWaWebVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Derive project root from script location (immune to MSYS2 cwd mangling)
const __filename = fileURLToPath(import.meta.url);
const PROJECT_ROOT = path.resolve(path.dirname(__filename), '..');
const STORE_DIR = path.join(PROJECT_ROOT, 'store');

const AUTH_DIR = path.join(STORE_DIR, 'auth');
const QR_DATA_PATH = path.join(STORE_DIR, 'qr-data.txt');
const logger = pino({ level: 'silent' });

// Parse CLI args
const args = process.argv.slice(2);
const usePairingCode = args.includes('--pairing-code');
const phoneIdx = args.indexOf('--phone');
const phone = phoneIdx !== -1 ? args[phoneIdx + 1] : undefined;

if (usePairingCode && !phone) {
  console.error('--pairing-code requires --phone <number>');
  process.exit(1);
}

const MAX_RETRIES = 3;

/** Delay before a reconnect attempt. Short and fixed, not exponential: a human is
 *  waiting to enter a pairing code within its validity window. The daemon's
 *  ReconnectController (packages/mcp-whatsapp) is deliberately NOT reused here —
 *  its 60s cap and stability timer are tuned for a long-lived socket, not a
 *  bounded interactive tool. */
const RECONNECT_DELAY_MS = 2000;

/** Reason 515 is "restart required" and ALWAYS follows a successful pairing, so
 *  this is the one disconnect that must reconnect rather than terminate. */
const RESTART_REQUIRED = 515;

type ConnectionUpdateLike = {
  connection?: string;
  lastDisconnect?: { error?: unknown } | undefined;
  qr?: string;
};

/** Retry bookkeeping shared across reconnect attempts.
 *
 *  Must live OUTSIDE the handler: each reconnect builds a fresh socket and a
 *  fresh handler, so a per-handler counter would reset every attempt and the
 *  MAX_RETRIES bound would never be reached. Injectable so tests get isolation
 *  instead of sharing one module-global counter across cases. */
export interface RetryState {
  count: number;
}

const sharedRetryState: RetryState = { count: 0 };

export interface ConnectionHandlerDeps {
  /** Re-run the connect flow. The defect this fixes was that nothing called it. */
  reconnect: () => void;
  /** Drop the dead socket's listeners before reconnecting (avoids the
   *  MaxListenersExceeded accumulation documented in #305). */
  teardown: () => void;
  /** Terminal failure. Production exits 1. */
  fail: (message: string) => void;
  /** Terminal success. */
  succeed: (userId: string | undefined) => void;
  /** Handle a QR frame (render, or request a pairing code). */
  onQr?: (qr: string) => void;
  maxRetries?: number;
  /** Defaults to the process-wide counter; tests pass their own. */
  retryState?: RetryState;
}

/**
 * Build the `connection.update` handler with its side effects injected.
 *
 * Extracted so the reconnect behaviour is assertable without a live socket:
 * the production wiring passes real effects, tests pass fakes and check that a
 * retriable close actually tears down and reconnects, and that it stays bounded.
 */
export function createConnectionUpdateHandler(deps: ConnectionHandlerDeps) {
  const maxRetries = deps.maxRetries ?? MAX_RETRIES;
  const retries = deps.retryState ?? sharedRetryState;
  let reconnecting = false; // single-flight: overlapping closes must not stack

  return function handleConnectionUpdate(update: ConnectionUpdateLike): void {
    const { connection, lastDisconnect, qr } = update;

    if (qr && deps.onQr) deps.onQr(qr);

    if (connection === 'close') {
      const reason = (
        lastDisconnect?.error as { output?: { statusCode?: number } }
      )?.output?.statusCode;

      if (reason === DisconnectReason.loggedOut) {
        deps.fail('AUTH_STATUS: failed (logged_out)');
        return;
      }
      if (reason === 405) {
        deps.fail(
          `AUTH_STATUS: failed (error ${reason} — WhatsApp rejected the connection)\n` +
            'This usually means the baileys protocol version is outdated.\n' +
            'Try: rm -rf store/auth/ and re-run authentication.',
        );
        return;
      }

      if (reconnecting) return; // a reconnect is already scheduled for this socket
      retries.count++;
      if (retries.count >= maxRetries) {
        deps.fail(
          `AUTH_STATUS: failed (${retries.count} retries exhausted, last reason: ${reason})`,
        );
        return;
      }

      reconnecting = true;
      console.error(
        `Connection closed (reason: ${reason}${
          reason === RESTART_REQUIRED ? ' — restart required after pairing' : ''
        }), retrying (${retries.count}/${maxRetries})...`,
      );
      // The bug: main() was never re-invoked here, so the process simply exited.
      deps.teardown();
      deps.reconnect();
      return;
    }

    if (connection === 'open') {
      deps.succeed(undefined);
    }
  };
}

async function main() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  const { version } = await fetchLatestWaWebVersion({}).catch(() => ({
    version: undefined,
  }));

  console.log('Connecting to WhatsApp...');

  const sock = makeWASocket({
    version,
    auth: { creds: state.creds, keys: state.keys },
    printQRInTerminal: false,
    logger,
    browser:
      process.platform === 'win32'
        ? Browsers.windows('Chrome')
        : Browsers.macOS('Chrome'),
  });

  let pairingCodeRequested = false;

  const handler = createConnectionUpdateHandler({
    onQr: (qr) => {
      // Write QR data to file for external rendering (browser, image, etc.)
      fs.mkdirSync(path.dirname(QR_DATA_PATH), { recursive: true });
      fs.writeFileSync(QR_DATA_PATH, qr);

      // Request pairing code on first qr event (socket is now ready).
      // `state.creds.registered` guard: after a 515 the creds ARE registered, so
      // the reconnect must go straight to connecting rather than asking for a
      // second code the user cannot use.
      if (usePairingCode && !pairingCodeRequested && !state.creds.registered) {
        pairingCodeRequested = true;
        sock
          .requestPairingCode(phone!)
          .then((code) => {
            console.log(`\nPAIRING_CODE: ${code}`);
            const codePath = path.join(STORE_DIR, 'pairing-code.txt');
            fs.writeFileSync(codePath, code);
          })
          .catch((err) => {
            console.error('Failed to request pairing code:', err.message);
          });
        return; // Skip QR instructions when using pairing code
      }
      if (usePairingCode) return;

      qrcode.generate(qr, { small: true });
      console.log(`\nQR data written to ${QR_DATA_PATH}`);
      console.log('Scan the QR code shown above with WhatsApp.');
      console.log(
        'Open WhatsApp > Settings > Linked Devices > Link a Device\n',
      );
    },
    teardown: () => {
      // Drop the dead socket's listeners before reconnecting, so attempts do not
      // accumulate handlers bound to a closed socket (#305's failure mode).
      sock.ev.removeAllListeners('connection.update');
      sock.ev.removeAllListeners('creds.update');
      try {
        sock.end(undefined);
      } catch {}
    },
    reconnect: () => {
      // main() re-reads creds from disk via useMultiFileAuthState, so the
      // reconnect picks up the post-pairing registered state. That re-read is
      // what lets the 515 restart actually complete authentication.
      setTimeout(() => {
        main().catch((err) => {
          console.error('Auth failed:', err);
          cleanup();
          process.exit(1);
        });
      }, RECONNECT_DELAY_MS);
    },
    fail: (message) => {
      console.error(message);
      cleanup();
      process.exit(1);
    },
    succeed: () => {
      const id = sock.user?.id?.split(':')[0] || 'unknown';
      console.log(`\nAUTH_STATUS: authenticated`);
      console.log(`Phone: ${id}`);
      console.log('WhatsApp authentication successful!');
      cleanup();
      setTimeout(() => process.exit(0), 200);
    },
  });

  sock.ev.on('connection.update', handler);
  sock.ev.on('creds.update', saveCreds);
}

function cleanup() {
  try {
    fs.unlinkSync(QR_DATA_PATH);
  } catch {}
}

// Import-safe: only start authenticating when this file is executed directly.
// Without this guard, importing the module (e.g. from a test reaching
// createConnectionUpdateHandler) would start a live authentication attempt.
const isDirectRun =
  !!process.argv[1] && path.resolve(process.argv[1]) === __filename;

if (isDirectRun) {
  main().catch((err) => {
    console.error('Auth failed:', err);
    cleanup();
    process.exit(1);
  });
}
