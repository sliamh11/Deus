#!/usr/bin/env node
/**
 * One-time Google Calendar OAuth2 setup.
 *
 * Prerequisites:
 *   1. Create a GCP project at https://console.cloud.google.com
 *   2. Enable "Google Calendar API"
 *   3. Create OAuth 2.0 credentials (Desktop app) and download as JSON
 *   4. Save it to: integrations/gcal/credentials.json
 *
 * Then run: node scripts/setup-gcal-auth.mjs
 */

import { google } from 'googleapis';
import fs from 'fs';
import path from 'path';
import readline from 'readline';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CREDS_PATH = path.resolve(__dirname, '../integrations/gcal/credentials.json');
const TOKENS_PATH = path.resolve(__dirname, '../integrations/gcal/tokens.json');

const SCOPES = [
  'https://www.googleapis.com/auth/calendar',
];

if (!fs.existsSync(CREDS_PATH)) {
  console.error(`credentials.json not found at: ${CREDS_PATH}`);
  console.error('');
  console.error('Steps:');
  console.error('  1. Go to https://console.cloud.google.com');
  console.error('  2. Create a project (or select existing)');
  console.error('  3. APIs & Services > Enable APIs > search "Google Calendar API" > Enable');
  console.error('  4. APIs & Services > Credentials > Create Credentials > OAuth client ID');
  console.error('     - Application type: Desktop app');
  console.error('     - Name: Deus');
  console.error('  5. Download JSON and save to: integrations/gcal/credentials.json');
  process.exit(1);
}

const creds = JSON.parse(fs.readFileSync(CREDS_PATH, 'utf8'));
const { client_id, client_secret, redirect_uris } = creds.installed || creds.web;
const oAuth2Client = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);

const authUrl = oAuth2Client.generateAuthUrl({
  access_type: 'offline',
  scope: SCOPES,
  prompt: 'consent', // Force refresh_token to be returned
});

console.log('Open this URL in your browser to authorize Deus:');
console.log('');
console.log(authUrl);
console.log('');

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
rl.question('Paste the authorization code here: ', async (code) => {
  rl.close();
  try {
    const { tokens } = await oAuth2Client.getToken(code.trim());
    fs.mkdirSync(path.dirname(TOKENS_PATH), { recursive: true });
    fs.writeFileSync(TOKENS_PATH, JSON.stringify(tokens, null, 2));
    console.log('');
    console.log('Tokens saved to:', TOKENS_PATH);
    console.log('');
    console.log('Testing connection...');

    oAuth2Client.setCredentials(tokens);
    const calendar = google.calendar({ version: 'v3', auth: oAuth2Client });
    const res = await calendar.calendarList.get({ calendarId: 'primary' });
    console.log('Connected to calendar:', res.data.summary);
    console.log('');
    console.log('Setup complete! The agent can now use Google Calendar via:');
    console.log('  node /workspace/project/scripts/gcal.mjs list');
  } catch (err) {
    console.error('Failed to get tokens:', err.message);
    process.exit(1);
  }
});
