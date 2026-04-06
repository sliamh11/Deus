import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('googleapis', () => {
  const mockCalendar = {
    calendarList: {
      get: vi.fn().mockResolvedValue({ data: { summary: 'test@example.com' } }),
    },
    events: {
      list: vi.fn(),
      get: vi.fn(),
      insert: vi.fn(),
      update: vi.fn(),
      delete: vi.fn(),
    },
  };
  return {
    google: {
      auth: {
        OAuth2: class MockOAuth2 {
          setCredentials = vi.fn();
          on = vi.fn();
        },
      },
      calendar: () => mockCalendar,
    },
    calendar_v3: {},
  };
});

vi.mock('google-auth-library', () => ({
  OAuth2Client: class MockOAuth2Client {
    setCredentials = vi.fn();
    on = vi.fn();
  },
}));

vi.mock('pino', () => {
  const mockLogger = {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
    fatal: vi.fn(),
  };
  const pinoFn: any = () => mockLogger;
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import fs from 'fs';
import { GCalProvider } from './gcal.js';

describe('GCalProvider', () => {
  let provider: GCalProvider;

  beforeEach(() => {
    vi.restoreAllMocks();
    provider = new GCalProvider();
  });

  describe('isConnected', () => {
    it('returns false before connect', () => {
      expect(provider.isConnected()).toBe(false);
    });
  });

  describe('hasCredentials', () => {
    it('returns false when files do not exist', () => {
      expect(provider.hasCredentials()).toBe(false);
    });

    it('returns true when both credential files exist', () => {
      vi.spyOn(fs, 'existsSync').mockReturnValue(true);
      const p = new GCalProvider();
      expect(p.hasCredentials()).toBe(true);
    });
  });

  describe('connect', () => {
    it('throws when credentials are missing', async () => {
      await expect(provider.connect()).rejects.toThrow('credentials not found');
    });

    it('connects when credential files exist', async () => {
      vi.spyOn(fs, 'existsSync').mockReturnValue(true);
      vi.spyOn(fs, 'readFileSync').mockImplementation((filePath: any) => {
        if (filePath.includes('credentials')) {
          return JSON.stringify({
            installed: {
              client_id: 'test-id',
              client_secret: 'test-secret',
              redirect_uris: ['http://localhost'],
            },
          });
        }
        return JSON.stringify({
          access_token: 'test-token',
          refresh_token: 'test-refresh',
        });
      });

      const p = new GCalProvider();
      await p.connect();
      expect(p.isConnected()).toBe(true);
    });
  });

  describe('operations throw when not connected', () => {
    it('listEvents throws', async () => {
      await expect(provider.listEvents()).rejects.toThrow('Not connected');
    });

    it('getEvent throws', async () => {
      await expect(provider.getEvent('test-id')).rejects.toThrow(
        'Not connected',
      );
    });

    it('createEvent throws', async () => {
      await expect(
        provider.createEvent({ title: 'Test', start: '2026-04-07T14:00:00' }),
      ).rejects.toThrow('Not connected');
    });

    it('updateEvent throws', async () => {
      await expect(
        provider.updateEvent('id', { title: 'New' }),
      ).rejects.toThrow('Not connected');
    });

    it('deleteEvent throws', async () => {
      await expect(provider.deleteEvent('id')).rejects.toThrow('Not connected');
    });

    it('searchEvents throws', async () => {
      await expect(provider.searchEvents('query')).rejects.toThrow(
        'Not connected',
      );
    });
  });

  describe('operations after connect', () => {
    let calendarMock: any;

    beforeEach(async () => {
      vi.spyOn(fs, 'existsSync').mockReturnValue(true);
      vi.spyOn(fs, 'readFileSync').mockImplementation((filePath: any) => {
        if (filePath.includes('credentials')) {
          return JSON.stringify({
            installed: {
              client_id: 'id',
              client_secret: 'secret',
              redirect_uris: ['http://localhost'],
            },
          });
        }
        return JSON.stringify({ access_token: 'tok' });
      });

      provider = new GCalProvider();
      await provider.connect();

      // Get the mock calendar instance
      const { google } = await import('googleapis');
      calendarMock = (google.calendar as any)();
    });

    it('listEvents returns mapped events', async () => {
      calendarMock.events.list.mockResolvedValue({
        data: {
          items: [
            {
              id: 'e1',
              summary: 'Meeting',
              start: { dateTime: '2026-04-07T14:00:00Z' },
              end: { dateTime: '2026-04-07T15:00:00Z' },
              htmlLink: 'https://calendar.google.com/e1',
            },
          ],
        },
      });

      const events = await provider.listEvents(7);
      expect(events).toHaveLength(1);
      expect(events[0]).toEqual({
        id: 'e1',
        summary: 'Meeting',
        start: '2026-04-07T14:00:00Z',
        end: '2026-04-07T15:00:00Z',
        location: undefined,
        description: undefined,
        htmlLink: 'https://calendar.google.com/e1',
      });
    });

    it('listEvents handles empty list', async () => {
      calendarMock.events.list.mockResolvedValue({ data: { items: [] } });
      const events = await provider.listEvents();
      expect(events).toEqual([]);
    });

    it('getEvent returns a mapped event', async () => {
      calendarMock.events.get.mockResolvedValue({
        data: {
          id: 'e1',
          summary: 'Test',
          start: { dateTime: '2026-04-07T10:00:00Z' },
          end: { dateTime: '2026-04-07T11:00:00Z' },
        },
      });

      const event = await provider.getEvent('e1');
      expect(event.id).toBe('e1');
      expect(event.summary).toBe('Test');
    });

    it('createEvent sends correct data', async () => {
      calendarMock.events.insert.mockResolvedValue({
        data: {
          id: 'new-1',
          summary: 'New Event',
          start: { dateTime: '2026-04-07T14:00:00.000Z' },
          end: { dateTime: '2026-04-07T15:00:00.000Z' },
        },
      });

      const event = await provider.createEvent({
        title: 'New Event',
        start: '2026-04-07T14:00:00',
        description: 'A test event',
      });

      expect(event.id).toBe('new-1');
      expect(calendarMock.events.insert).toHaveBeenCalledWith(
        expect.objectContaining({
          calendarId: 'primary',
          requestBody: expect.objectContaining({
            summary: 'New Event',
            description: 'A test event',
          }),
        }),
      );
    });

    it('createEvent defaults end to start + 1 hour', async () => {
      calendarMock.events.insert.mockResolvedValue({
        data: {
          id: 'new-2',
          summary: 'Quick',
          start: { dateTime: '2026-04-07T14:00:00.000Z' },
          end: { dateTime: '2026-04-07T15:00:00.000Z' },
        },
      });

      await provider.createEvent({
        title: 'Quick',
        start: '2026-04-07T14:00:00Z',
      });

      const call = calendarMock.events.insert.mock.calls[0][0];
      const startMs = new Date(call.requestBody.start.dateTime).getTime();
      const endMs = new Date(call.requestBody.end.dateTime).getTime();
      expect(endMs - startMs).toBe(60 * 60 * 1000);
    });

    it('updateEvent patches only provided fields', async () => {
      calendarMock.events.get.mockResolvedValue({
        data: {
          id: 'e1',
          summary: 'Original',
          start: { dateTime: '2026-04-07T10:00:00Z' },
          end: { dateTime: '2026-04-07T11:00:00Z' },
        },
      });
      calendarMock.events.update.mockResolvedValue({
        data: {
          id: 'e1',
          summary: 'Updated',
          start: { dateTime: '2026-04-07T10:00:00Z' },
          end: { dateTime: '2026-04-07T11:00:00Z' },
        },
      });

      const event = await provider.updateEvent('e1', { title: 'Updated' });
      expect(event.summary).toBe('Updated');

      const call = calendarMock.events.update.mock.calls[0][0];
      expect(call.requestBody.summary).toBe('Updated');
      // Start/end should remain from original
      expect(call.requestBody.start.dateTime).toBe('2026-04-07T10:00:00Z');
    });

    it('deleteEvent calls API', async () => {
      calendarMock.events.delete.mockResolvedValue({});
      await provider.deleteEvent('e1');
      expect(calendarMock.events.delete).toHaveBeenCalledWith({
        calendarId: 'primary',
        eventId: 'e1',
      });
    });

    it('searchEvents passes query parameter', async () => {
      calendarMock.events.list.mockResolvedValue({ data: { items: [] } });
      await provider.searchEvents('standup', 14);
      expect(calendarMock.events.list).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'standup' }),
      );
    });

    it('mapEvent handles all-day events (date instead of dateTime)', async () => {
      calendarMock.events.list.mockResolvedValue({
        data: {
          items: [
            {
              id: 'allday',
              summary: 'Holiday',
              start: { date: '2026-04-07' },
              end: { date: '2026-04-08' },
            },
          ],
        },
      });

      const events = await provider.listEvents();
      expect(events[0].start).toBe('2026-04-07');
      expect(events[0].end).toBe('2026-04-08');
    });
  });
});
