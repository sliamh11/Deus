/**
 * Google Calendar provider — handles OAuth2 auth and Calendar API operations.
 *
 * Config (env vars):
 *   GCAL_CREDENTIALS_PATH — OAuth client credentials file (default: integrations/gcal/credentials.json)
 *   GCAL_TOKENS_PATH      — OAuth tokens file (default: integrations/gcal/tokens.json)
 */

import fs from 'fs';
import path from 'path';

import { OAuth2Client } from 'google-auth-library';
import { calendar_v3, google } from 'googleapis';
import pino from 'pino';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

export interface CalendarEvent {
  id: string;
  summary: string;
  start: string;
  end: string;
  location?: string;
  description?: string;
  htmlLink?: string;
}

export class GCalProvider {
  private auth: OAuth2Client | null = null;
  private calendar: calendar_v3.Calendar | null = null;
  private credentialsPath: string;
  private tokensPath: string;

  constructor() {
    const projectRoot = process.env.DEUS_PROJECT_ROOT || process.cwd();
    this.credentialsPath =
      process.env.GCAL_CREDENTIALS_PATH ||
      path.join(projectRoot, 'integrations', 'gcal', 'credentials.json');
    this.tokensPath =
      process.env.GCAL_TOKENS_PATH ||
      path.join(projectRoot, 'integrations', 'gcal', 'tokens.json');
  }

  hasCredentials(): boolean {
    return (
      fs.existsSync(this.credentialsPath) && fs.existsSync(this.tokensPath)
    );
  }

  async connect(): Promise<void> {
    if (!this.hasCredentials()) {
      throw new Error(
        `Google Calendar credentials not found. Expected:\n` +
          `  OAuth client: ${this.credentialsPath}\n` +
          `  Tokens: ${this.tokensPath}\n` +
          `Run: node scripts/setup-gcal-auth.mjs`,
      );
    }

    const creds = JSON.parse(fs.readFileSync(this.credentialsPath, 'utf8'));
    const { client_id, client_secret, redirect_uris } =
      creds.installed || creds.web;
    this.auth = new google.auth.OAuth2(
      client_id,
      client_secret,
      redirect_uris[0],
    );

    const tokens = JSON.parse(fs.readFileSync(this.tokensPath, 'utf8'));
    this.auth.setCredentials(tokens);

    // Auto-persist refreshed tokens
    this.auth.on('tokens', (newTokens) => {
      const merged = { ...tokens, ...newTokens };
      try {
        fs.writeFileSync(this.tokensPath, JSON.stringify(merged, null, 2));
        logger.info('Refreshed OAuth tokens saved');
      } catch {
        // Tokens path may be read-only (e.g., inside container)
      }
    });

    this.calendar = google.calendar({ version: 'v3', auth: this.auth });

    // Verify connection
    const profile = await this.calendar.calendarList.get({
      calendarId: 'primary',
    });
    logger.info(
      { calendar: profile.data.summary },
      'Google Calendar connected',
    );
  }

  isConnected(): boolean {
    return this.calendar !== null;
  }

  private ensureConnected(): calendar_v3.Calendar {
    if (!this.calendar) throw new Error('Not connected to Google Calendar');
    return this.calendar;
  }

  async listEvents(days: number = 7): Promise<CalendarEvent[]> {
    const cal = this.ensureConnected();
    const now = new Date();
    const end = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);

    const res = await cal.events.list({
      calendarId: 'primary',
      timeMin: now.toISOString(),
      timeMax: end.toISOString(),
      singleEvents: true,
      orderBy: 'startTime',
      maxResults: 50,
    });

    return (res.data.items || []).map(this.mapEvent);
  }

  async getEvent(eventId: string): Promise<CalendarEvent> {
    const cal = this.ensureConnected();
    const res = await cal.events.get({ calendarId: 'primary', eventId });
    return this.mapEvent(res.data);
  }

  async createEvent(opts: {
    title: string;
    start: string;
    end?: string;
    description?: string;
    location?: string;
  }): Promise<CalendarEvent> {
    const cal = this.ensureConnected();
    const startDt = new Date(opts.start);
    const endDt = opts.end
      ? new Date(opts.end)
      : new Date(startDt.getTime() + 60 * 60 * 1000); // default 1h

    const event: calendar_v3.Schema$Event = {
      summary: opts.title,
      start: { dateTime: startDt.toISOString() },
      end: { dateTime: endDt.toISOString() },
    };
    if (opts.description) event.description = opts.description;
    if (opts.location) event.location = opts.location;

    const res = await cal.events.insert({
      calendarId: 'primary',
      requestBody: event,
    });
    logger.info(
      { id: res.data.id, summary: res.data.summary },
      'Event created',
    );
    return this.mapEvent(res.data);
  }

  async updateEvent(
    eventId: string,
    patches: {
      title?: string;
      start?: string;
      end?: string;
      description?: string;
      location?: string;
    },
  ): Promise<CalendarEvent> {
    const cal = this.ensureConnected();
    const existing = (await cal.events.get({ calendarId: 'primary', eventId }))
      .data;

    if (patches.title) existing.summary = patches.title;
    if (patches.description) existing.description = patches.description;
    if (patches.location) existing.location = patches.location;
    if (patches.start)
      existing.start = { dateTime: new Date(patches.start).toISOString() };
    if (patches.end)
      existing.end = { dateTime: new Date(patches.end).toISOString() };

    const res = await cal.events.update({
      calendarId: 'primary',
      eventId,
      requestBody: existing,
    });
    logger.info(
      { id: res.data.id, summary: res.data.summary },
      'Event updated',
    );
    return this.mapEvent(res.data);
  }

  async deleteEvent(eventId: string): Promise<void> {
    const cal = this.ensureConnected();
    await cal.events.delete({ calendarId: 'primary', eventId });
    logger.info({ id: eventId }, 'Event deleted');
  }

  async searchEvents(
    query: string,
    days: number = 30,
  ): Promise<CalendarEvent[]> {
    const cal = this.ensureConnected();
    const now = new Date();
    const end = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);

    const res = await cal.events.list({
      calendarId: 'primary',
      q: query,
      timeMin: now.toISOString(),
      timeMax: end.toISOString(),
      singleEvents: true,
      orderBy: 'startTime',
      maxResults: 20,
    });

    return (res.data.items || []).map(this.mapEvent);
  }

  private mapEvent(e: calendar_v3.Schema$Event): CalendarEvent {
    return {
      id: e.id || '',
      summary: e.summary || '(no title)',
      start: e.start?.dateTime || e.start?.date || '',
      end: e.end?.dateTime || e.end?.date || '',
      location: e.location || undefined,
      description: e.description || undefined,
      htmlLink: e.htmlLink || undefined,
    };
  }
}
