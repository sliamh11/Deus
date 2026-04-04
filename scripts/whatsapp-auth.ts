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
import pino from 'pino';
import fs from 'fs';
import path from 'path';

const AUTH_DIR = path.join(process.cwd(), 'store', 'auth');
const QR_DATA_PATH = path.join(process.cwd(), 'store', 'qr-data.txt');
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
let retryCount = 0;

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
    printQRInTerminal: !usePairingCode,
    logger,
    browser: Browsers.macOS('Chrome'),
  });

  if (usePairingCode && !sock.authState.creds.registered) {
    // Request pairing code
    const code = await sock.requestPairingCode(phone!);
    console.log(`\nPAIRING_CODE: ${code}`);
    // Write to file for polling by setup process
    const codePath = path.join(process.cwd(), 'store', 'pairing-code.txt');
    fs.writeFileSync(codePath, code);
  }

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Write QR data to file for external rendering (browser, image, etc.)
      fs.mkdirSync(path.dirname(QR_DATA_PATH), { recursive: true });
      fs.writeFileSync(QR_DATA_PATH, qr);
      console.log(`\nQR data written to ${QR_DATA_PATH}`);
      console.log('Scan the QR code shown above with WhatsApp.');
      console.log(
        'Open WhatsApp > Settings > Linked Devices > Link a Device\n',
      );
    }

    if (connection === 'close') {
      const reason = (
        lastDisconnect?.error as { output?: { statusCode?: number } }
      )?.output?.statusCode;

      if (reason === DisconnectReason.loggedOut) {
        console.error('AUTH_STATUS: failed (logged_out)');
        cleanup();
        process.exit(1);
      } else if (reason === 405 || reason === 428) {
        console.error(
          `AUTH_STATUS: failed (error ${reason} — WhatsApp rejected the connection)`,
        );
        console.error(
          'This usually means the baileys protocol version is outdated.',
        );
        console.error('Try: rm -rf store/auth/ and re-run authentication.');
        cleanup();
        process.exit(1);
      } else {
        retryCount++;
        if (retryCount >= MAX_RETRIES) {
          console.error(
            `AUTH_STATUS: failed (${retryCount} retries exhausted, last reason: ${reason})`,
          );
          cleanup();
          process.exit(1);
        }
        console.error(
          `Connection closed (reason: ${reason}), retrying (${retryCount}/${MAX_RETRIES})...`,
        );
      }
    } else if (connection === 'open') {
      const id = sock.user?.id?.split(':')[0] || 'unknown';
      console.log(`\nAUTH_STATUS: authenticated`);
      console.log(`Phone: ${id}`);
      console.log('WhatsApp authentication successful!');
      cleanup();
      setTimeout(() => process.exit(0), 2000);
    }
  });

  sock.ev.on('creds.update', saveCreds);
}

function cleanup() {
  try {
    fs.unlinkSync(QR_DATA_PATH);
  } catch {}
}

main().catch((err) => {
  console.error('Auth failed:', err);
  cleanup();
  process.exit(1);
});
