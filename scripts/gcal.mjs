#!/usr/bin/env node
/**
 * gcal - Google Calendar CLI tool for Deus container agents
 *
 * Usage (from inside the container):
 *   node /workspace/project/scripts/gcal.mjs <command> [options]
 *
 * Commands:
 *   list   [--days N]                         List upcoming events (default: next 7 days)
 *   get    --id <eventId>                     Get a single event
 *   create --title "..." --start "..." --end "..." [--desc "..."] [--location "..."]
 *   update --id <eventId> [--title "..."] [--start "..."] [--end "..."] [--desc "..."]
 *   delete --id <eventId>
 *   search --q "..."  [--days N]              Search events by text
 *
 * Dates: ISO 8601 or natural strings like "2026-03-25T14:00:00"
 * Tokens: read from /workspace/project/integrations/gcal/tokens.json
 */

import { google } from 'googleapis';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Tokens path works both on host (scripts/) and in container (/workspace/project/scripts/)
const TOKENS_PATH = path.resolve(__dirname, '../integrations/gcal/tokens.json');
const CREDS_PATH = path.resolve(__dirname, '../integrations/gcal/credentials.json');

function loadAuth() {
  if (!fs.existsSync(TOKENS_PATH)) {
    console.error('ERROR: Google Calendar not set up. Run: node scripts/setup-gcal-auth.mjs');
    process.exit(1);
  }
  if (!fs.existsSync(CREDS_PATH)) {
    console.error('ERROR: credentials.json missing at integrations/gcal/credentials.json');
    process.exit(1);
  }

  const creds = JSON.parse(fs.readFileSync(CREDS_PATH, 'utf8'));
  const { client_id, client_secret, redirect_uris } = creds.installed || creds.web;
  const oAuth2Client = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);

  const tokens = JSON.parse(fs.readFileSync(TOKENS_PATH, 'utf8'));
  oAuth2Client.setCredentials(tokens);

  // Auto-save refreshed tokens (only works when tokens file is writable — i.e., on host)
  oAuth2Client.on('tokens', (newTokens) => {
    const merged = { ...tokens, ...newTokens };
    try { fs.writeFileSync(TOKENS_PATH, JSON.stringify(merged, null, 2)); } catch {}
  });

  return oAuth2Client;
}

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      args[key] = argv[i + 1] || true;
      i++;
    } else {
      args._.push(argv[i]);
    }
  }
  return args;
}

function isoOrNow(str) {
  if (!str) return new Date().toISOString();
  // If no timezone info, assume local (append nothing — Calendar API handles it)
  return new Date(str).toISOString();
}

async function cmdList(calendar, args) {
  const days = parseInt(args.days || '7', 10);
  const now = new Date();
  const end = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);

  const res = await calendar.events.list({
    calendarId: 'primary',
    timeMin: now.toISOString(),
    timeMax: end.toISOString(),
    singleEvents: true,
    orderBy: 'startTime',
    maxResults: 50,
  });

  const events = res.data.items || [];
  if (events.length === 0) {
    console.log('No events found in the next', days, 'days.');
    return;
  }

  for (const e of events) {
    const start = e.start.dateTime || e.start.date;
    console.log(`[${e.id}] ${start} — ${e.summary || '(no title)'}`);
    if (e.location) console.log(`  Location: ${e.location}`);
    if (e.description) console.log(`  Desc: ${e.description.slice(0, 100)}`);
  }
}

async function cmdGet(calendar, args) {
  if (!args.id) { console.error('--id required'); process.exit(1); }
  const res = await calendar.events.get({ calendarId: 'primary', eventId: args.id });
  console.log(JSON.stringify(res.data, null, 2));
}

async function cmdCreate(calendar, args) {
  if (!args.title) { console.error('--title required'); process.exit(1); }
  if (!args.start) { console.error('--start required'); process.exit(1); }

  const startDt = new Date(args.start);
  const endDt = args.end ? new Date(args.end) : new Date(startDt.getTime() + 60 * 60 * 1000);

  const event = {
    summary: args.title,
    start: { dateTime: startDt.toISOString() },
    end: { dateTime: endDt.toISOString() },
  };
  if (args.desc) event.description = args.desc;
  if (args.location) event.location = args.location;

  const res = await calendar.events.insert({ calendarId: 'primary', resource: event });
  console.log(`Created: [${res.data.id}] ${res.data.summary}`);
  console.log(`Link: ${res.data.htmlLink}`);
}

async function cmdUpdate(calendar, args) {
  if (!args.id) { console.error('--id required'); process.exit(1); }

  // Fetch existing event first
  const existing = (await calendar.events.get({ calendarId: 'primary', eventId: args.id })).data;

  if (args.title) existing.summary = args.title;
  if (args.desc) existing.description = args.desc;
  if (args.location) existing.location = args.location;
  if (args.start) existing.start = { dateTime: new Date(args.start).toISOString() };
  if (args.end) existing.end = { dateTime: new Date(args.end).toISOString() };

  const res = await calendar.events.update({
    calendarId: 'primary',
    eventId: args.id,
    resource: existing,
  });
  console.log(`Updated: [${res.data.id}] ${res.data.summary}`);
}

async function cmdDelete(calendar, args) {
  if (!args.id) { console.error('--id required'); process.exit(1); }
  await calendar.events.delete({ calendarId: 'primary', eventId: args.id });
  console.log(`Deleted event ${args.id}`);
}

async function cmdSearch(calendar, args) {
  if (!args.q) { console.error('--q required'); process.exit(1); }
  const days = parseInt(args.days || '30', 10);
  const now = new Date();
  const end = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);

  const res = await calendar.events.list({
    calendarId: 'primary',
    q: args.q,
    timeMin: now.toISOString(),
    timeMax: end.toISOString(),
    singleEvents: true,
    orderBy: 'startTime',
    maxResults: 20,
  });

  const events = res.data.items || [];
  if (events.length === 0) { console.log('No matching events found.'); return; }

  for (const e of events) {
    const start = e.start.dateTime || e.start.date;
    console.log(`[${e.id}] ${start} — ${e.summary || '(no title)'}`);
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const args = parseArgs(argv.slice(1));

  const auth = loadAuth();
  const calendar = google.calendar({ version: 'v3', auth });

  switch (command) {
    case 'list':   await cmdList(calendar, args); break;
    case 'get':    await cmdGet(calendar, args); break;
    case 'create': await cmdCreate(calendar, args); break;
    case 'update': await cmdUpdate(calendar, args); break;
    case 'delete': await cmdDelete(calendar, args); break;
    case 'search': await cmdSearch(calendar, args); break;
    default:
      console.error(`Unknown command: ${command || '(none)'}`);
      console.error('Commands: list, get, create, update, delete, search');
      process.exit(1);
  }
}

main().catch((err) => {
  console.error('ERROR:', err.message || err);
  process.exit(1);
});
