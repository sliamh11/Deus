import { describe, it, expect, beforeEach } from 'vitest';

import {
  _initTestDatabase,
  clearSession,
  createTask,
  deleteTask,
  getAllChats,
  getAllRegisteredGroups,
  getConsecutiveFailCount,
  getLastFailTime,
  getMessagesSince,
  getNewMessages,
  getAllSessions,
  getAllBackendSessions,
  getTaskById,
  getSession,
  logPipelineEvent,
  insertPipelineEventRow,
  getPipelineEvents,
  setSession,
  setRegisteredGroup,
  storeChatMetadata,
  storeMessage,
  updateTask,
  upsertIssueCache,
  softDeleteIssueCache,
  getIssueCacheCount,
  getMaxCachedAt,
  getIssuesFromCache,
  reconcileIssueCache,
} from './db.js';
import { getBus } from './events/bus.js';
import type { EventEnvelope } from './events/types.js';

beforeEach(() => {
  _initTestDatabase();
});

describe('logPipelineEvent -> pipeline.transition emit (Phase 3 cutover: emit-only)', () => {
  it('emits one pipeline.transition envelope and does NOT insert inline', () => {
    const seen: EventEnvelope[] = [];
    const unsub = getBus().subscribe('pipeline.transition', (env) => {
      seen.push(env);
    });
    try {
      // Phase 3: logPipelineEvent is emit-only and returns void. The durable
      // write is owned by the ObservabilitySink (not registered in this test),
      // so logPipelineEvent itself must NOT insert a row.
      const ret = logPipelineEvent(
        'ISS-emit',
        'LIA-emit',
        'agent_completed',
        'done',
      );
      expect(ret).toBeUndefined();
      expect(getPipelineEvents({ issueId: 'ISS-emit' })).toHaveLength(0);

      // Emit is unconditional now (no rowid gate). The bus delivers synchronously
      // up to its first await, so the listener has already run — no await needed.
      expect(seen).toHaveLength(1);
      const env = seen[0];
      expect(env.type).toBe('pipeline.transition');
      expect(env.source).toBe('db.logPipelineEvent');
      expect(env.actor).toBe('system');
      expect(env.correlationId).toEqual({
        kind: 'issue',
        id: 'ISS-emit',
        identifier: 'LIA-emit',
      });
      expect(env.payload).toEqual({
        eventType: 'agent_completed',
        detail: 'done',
      });
    } finally {
      unsub();
    }
  });
});

// Helper to store a message using the normalized NewMessage interface
function store(overrides: {
  id: string;
  chat_jid: string;
  sender: string;
  sender_name: string;
  content: string;
  timestamp: string;
  is_from_me?: boolean;
}) {
  storeMessage({
    id: overrides.id,
    chat_jid: overrides.chat_jid,
    sender: overrides.sender,
    sender_name: overrides.sender_name,
    content: overrides.content,
    timestamp: overrides.timestamp,
    is_from_me: overrides.is_from_me ?? false,
  });
}

// --- storeMessage (NewMessage format) ---

describe('storeMessage', () => {
  it('stores a message and retrieves it', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'msg-1',
      chat_jid: 'group@g.us',
      sender: '123@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'hello world',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    const messages = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe('msg-1');
    expect(messages[0].sender).toBe('123@s.whatsapp.net');
    expect(messages[0].sender_name).toBe('Alice');
    expect(messages[0].content).toBe('hello world');
  });

  it('filters out empty content', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'msg-2',
      chat_jid: 'group@g.us',
      sender: '111@s.whatsapp.net',
      sender_name: 'Dave',
      content: '',
      timestamp: '2024-01-01T00:00:04.000Z',
    });

    const messages = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    expect(messages).toHaveLength(0);
  });

  it('stores is_from_me flag', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'msg-3',
      chat_jid: 'group@g.us',
      sender: 'me@s.whatsapp.net',
      sender_name: 'Me',
      content: 'my message',
      timestamp: '2024-01-01T00:00:05.000Z',
      is_from_me: true,
    });

    // Message is stored (we can retrieve it — is_from_me doesn't affect retrieval)
    const messages = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    expect(messages).toHaveLength(1);
  });

  it('upserts on duplicate id+chat_jid', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'msg-dup',
      chat_jid: 'group@g.us',
      sender: '123@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'original',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    store({
      id: 'msg-dup',
      chat_jid: 'group@g.us',
      sender: '123@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'updated',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    const messages = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('updated');
  });
});

// --- getMessagesSince ---

describe('getMessagesSince', () => {
  beforeEach(() => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'm1',
      chat_jid: 'group@g.us',
      sender: 'Alice@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'first',
      timestamp: '2024-01-01T00:00:01.000Z',
    });
    store({
      id: 'm2',
      chat_jid: 'group@g.us',
      sender: 'Bob@s.whatsapp.net',
      sender_name: 'Bob',
      content: 'second',
      timestamp: '2024-01-01T00:00:02.000Z',
    });
    storeMessage({
      id: 'm3',
      chat_jid: 'group@g.us',
      sender: 'Bot@s.whatsapp.net',
      sender_name: 'Bot',
      content: 'bot reply',
      timestamp: '2024-01-01T00:00:03.000Z',
      is_bot_message: true,
    });
    store({
      id: 'm4',
      chat_jid: 'group@g.us',
      sender: 'Carol@s.whatsapp.net',
      sender_name: 'Carol',
      content: 'third',
      timestamp: '2024-01-01T00:00:04.000Z',
    });
  });

  it('returns messages after the given timestamp', () => {
    const msgs = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:02.000Z',
      'Deus',
    );
    // Should exclude m1, m2 (before/at timestamp), m3 (bot message)
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('third');
  });

  it('excludes bot messages via is_bot_message flag', () => {
    const msgs = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    const botMsgs = msgs.filter((m) => m.content === 'bot reply');
    expect(botMsgs).toHaveLength(0);
  });

  it('returns all non-bot messages when sinceTimestamp is empty', () => {
    const msgs = getMessagesSince('group@g.us', '', 'Deus');
    // 3 user messages (bot message excluded)
    expect(msgs).toHaveLength(3);
  });

  it('filters pre-migration bot messages via content prefix backstop', () => {
    // Simulate a message written before migration: has prefix but is_bot_message = 0
    store({
      id: 'm5',
      chat_jid: 'group@g.us',
      sender: 'Bot@s.whatsapp.net',
      sender_name: 'Bot',
      content: 'Deus: old bot reply',
      timestamp: '2024-01-01T00:00:05.000Z',
    });
    const msgs = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:04.000Z',
      'Deus',
    );
    expect(msgs).toHaveLength(0);
  });
});

// --- getNewMessages ---

describe('getNewMessages', () => {
  beforeEach(() => {
    storeChatMetadata('group1@g.us', '2024-01-01T00:00:00.000Z');
    storeChatMetadata('group2@g.us', '2024-01-01T00:00:00.000Z');

    store({
      id: 'a1',
      chat_jid: 'group1@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'User',
      content: 'g1 msg1',
      timestamp: '2024-01-01T00:00:01.000Z',
    });
    store({
      id: 'a2',
      chat_jid: 'group2@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'User',
      content: 'g2 msg1',
      timestamp: '2024-01-01T00:00:02.000Z',
    });
    storeMessage({
      id: 'a3',
      chat_jid: 'group1@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'User',
      content: 'bot reply',
      timestamp: '2024-01-01T00:00:03.000Z',
      is_bot_message: true,
    });
    store({
      id: 'a4',
      chat_jid: 'group1@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'User',
      content: 'g1 msg2',
      timestamp: '2024-01-01T00:00:04.000Z',
    });
  });

  it('returns new messages across multiple groups', () => {
    const { messages, newTimestamp } = getNewMessages(
      ['group1@g.us', 'group2@g.us'],
      '2024-01-01T00:00:00.000Z',
      'Deus',
    );
    // Excludes bot message, returns 3 user messages
    expect(messages).toHaveLength(3);
    expect(newTimestamp).toBe('2024-01-01T00:00:04.000Z');
  });

  it('filters by timestamp', () => {
    const { messages } = getNewMessages(
      ['group1@g.us', 'group2@g.us'],
      '2024-01-01T00:00:02.000Z',
      'Deus',
    );
    // Only g1 msg2 (after ts, not bot)
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('g1 msg2');
  });

  it('returns empty for no registered groups', () => {
    const { messages, newTimestamp } = getNewMessages([], '', 'Deus');
    expect(messages).toHaveLength(0);
    expect(newTimestamp).toBe('');
  });
});

// --- storeChatMetadata ---

describe('storeChatMetadata', () => {
  it('stores chat with JID as default name', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');
    const chats = getAllChats();
    expect(chats).toHaveLength(1);
    expect(chats[0].jid).toBe('group@g.us');
    expect(chats[0].name).toBe('group@g.us');
  });

  it('stores chat with explicit name', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z', 'My Group');
    const chats = getAllChats();
    expect(chats[0].name).toBe('My Group');
  });

  it('updates name on subsequent call with name', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');
    storeChatMetadata('group@g.us', '2024-01-01T00:00:01.000Z', 'Updated Name');
    const chats = getAllChats();
    expect(chats).toHaveLength(1);
    expect(chats[0].name).toBe('Updated Name');
  });

  it('preserves newer timestamp on conflict', () => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:05.000Z');
    storeChatMetadata('group@g.us', '2024-01-01T00:00:01.000Z');
    const chats = getAllChats();
    expect(chats[0].last_message_time).toBe('2024-01-01T00:00:05.000Z');
  });
});

// --- Task CRUD ---

describe('task CRUD', () => {
  it('creates and retrieves a task', () => {
    createTask({
      id: 'task-1',
      group_folder: 'main',
      chat_jid: 'group@g.us',
      prompt: 'do something',
      schedule_type: 'once',
      schedule_value: '2024-06-01T00:00:00.000Z',
      context_mode: 'isolated',
      next_run: '2024-06-01T00:00:00.000Z',
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
    });

    const task = getTaskById('task-1');
    expect(task).toBeDefined();
    expect(task!.prompt).toBe('do something');
    expect(task!.status).toBe('active');
  });

  it('updates task status', () => {
    createTask({
      id: 'task-2',
      group_folder: 'main',
      chat_jid: 'group@g.us',
      prompt: 'test',
      schedule_type: 'once',
      schedule_value: '2024-06-01T00:00:00.000Z',
      context_mode: 'isolated',
      next_run: null,
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
    });

    updateTask('task-2', { status: 'paused' });
    expect(getTaskById('task-2')!.status).toBe('paused');
  });

  it('deletes a task and its run logs', () => {
    createTask({
      id: 'task-3',
      group_folder: 'main',
      chat_jid: 'group@g.us',
      prompt: 'delete me',
      schedule_type: 'once',
      schedule_value: '2024-06-01T00:00:00.000Z',
      context_mode: 'isolated',
      next_run: null,
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
    });

    deleteTask('task-3');
    expect(getTaskById('task-3')).toBeUndefined();
  });
});

// --- LIMIT behavior ---

describe('message query LIMIT', () => {
  beforeEach(() => {
    storeChatMetadata('group@g.us', '2024-01-01T00:00:00.000Z');

    for (let i = 1; i <= 10; i++) {
      store({
        id: `lim-${i}`,
        chat_jid: 'group@g.us',
        sender: 'user@s.whatsapp.net',
        sender_name: 'User',
        content: `message ${i}`,
        timestamp: `2024-01-01T00:00:${String(i).padStart(2, '0')}.000Z`,
      });
    }
  });

  it('getNewMessages caps to limit and returns most recent in chronological order', () => {
    const { messages, newTimestamp } = getNewMessages(
      ['group@g.us'],
      '2024-01-01T00:00:00.000Z',
      'Deus',
      3,
    );
    expect(messages).toHaveLength(3);
    expect(messages[0].content).toBe('message 8');
    expect(messages[2].content).toBe('message 10');
    // Chronological order preserved
    expect(messages[1].timestamp > messages[0].timestamp).toBe(true);
    // newTimestamp reflects latest returned row
    expect(newTimestamp).toBe('2024-01-01T00:00:10.000Z');
  });

  it('getMessagesSince caps to limit and returns most recent in chronological order', () => {
    const messages = getMessagesSince(
      'group@g.us',
      '2024-01-01T00:00:00.000Z',
      'Deus',
      3,
    );
    expect(messages).toHaveLength(3);
    expect(messages[0].content).toBe('message 8');
    expect(messages[2].content).toBe('message 10');
    expect(messages[1].timestamp > messages[0].timestamp).toBe(true);
  });

  it('returns all messages when count is under the limit', () => {
    const { messages } = getNewMessages(
      ['group@g.us'],
      '2024-01-01T00:00:00.000Z',
      'Deus',
      50,
    );
    expect(messages).toHaveLength(10);
  });
});

// --- RegisteredGroup isControlGroup round-trip ---

describe('registered group isControlGroup', () => {
  it('persists isControlGroup=true through set/get round-trip', () => {
    setRegisteredGroup('main@s.whatsapp.net', {
      name: 'Main Chat',
      folder: 'whatsapp_main',
      trigger: '@Deus',
      added_at: '2024-01-01T00:00:00.000Z',
      isControlGroup: true,
    });

    const groups = getAllRegisteredGroups();
    const group = groups['main@s.whatsapp.net'];
    expect(group).toBeDefined();
    expect(group.isControlGroup).toBe(true);
    expect(group.folder).toBe('whatsapp_main');
  });

  it('omits isControlGroup for non-main groups', () => {
    setRegisteredGroup('group@g.us', {
      name: 'Family Chat',
      folder: 'whatsapp_family-chat',
      trigger: '@Deus',
      added_at: '2024-01-01T00:00:00.000Z',
    });

    const groups = getAllRegisteredGroups();
    const group = groups['group@g.us'];
    expect(group).toBeDefined();
    expect(group.isControlGroup).toBeUndefined();
  });
});

describe('scheduled task backend overrides', () => {
  it('persists agent_backend on create and update', () => {
    createTask({
      id: 'task-backend',
      group_folder: 'main',
      chat_jid: 'group@g.us',
      prompt: 'run with openai',
      schedule_type: 'once',
      schedule_value: '2024-06-01T00:00:00.000Z',
      context_mode: 'isolated',
      next_run: null,
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
      agent_backend: 'openai',
    });

    expect(getTaskById('task-backend')?.agent_backend).toBe('openai');

    updateTask('task-backend', { agent_backend: 'claude' });
    expect(getTaskById('task-backend')?.agent_backend).toBe('claude');
  });
});

describe('backend-aware sessions', () => {
  it('round-trips backend session refs', () => {
    setSession('group-folder', {
      backend: 'openai',
      session_id: 'resp_123',
      resume_cursor: 'cursor_1',
      metadata_json: '{"model":"gpt-4o"}',
    });

    expect(getSession('group-folder')).toEqual({
      backend: 'openai',
      session_id: 'resp_123',
      resume_cursor: 'cursor_1',
      metadata_json: '{"model":"gpt-4o"}',
    });
  });

  it('wraps legacy session strings as Claude refs', () => {
    setSession('legacy-folder', 'claude-session-1');

    expect(getSession('legacy-folder')).toEqual({
      backend: 'claude',
      session_id: 'claude-session-1',
    });
    expect(getAllSessions()).toEqual({
      'legacy-folder': {
        backend: 'claude',
        session_id: 'claude-session-1',
      },
    });
  });

  it('keeps separate sessions for each backend in the same group', () => {
    setSession('shared-folder', {
      backend: 'claude',
      session_id: 'claude-session',
    });
    setSession('shared-folder', {
      backend: 'openai',
      session_id: 'resp_456',
    });

    expect(getSession('shared-folder', 'claude')?.session_id).toBe(
      'claude-session',
    );
    expect(getSession('shared-folder', 'openai')?.session_id).toBe('resp_456');
    expect(getAllBackendSessions()).toEqual({
      'shared-folder': {
        claude: {
          backend: 'claude',
          session_id: 'claude-session',
        },
        openai: {
          backend: 'openai',
          session_id: 'resp_456',
        },
      },
    });
  });

  it('round-trips a llama-cpp session row without silent coercion', () => {
    // Regression test for the binary-ternary trap fixed in src/db.ts:685.
    // Prior to the parseAgentBackend gate, a stored backend='llama-cpp'
    // row was silently read back as backend='claude', breaking resume.
    setSession('llama-folder', {
      backend: 'llama-cpp',
      session_id: 'llama-cpp-abc',
    });

    const ref = getSession('llama-folder', 'llama-cpp');
    expect(ref?.backend).toBe('llama-cpp');
    expect(ref?.session_id).toBe('llama-cpp-abc');

    expect(getAllBackendSessions()['llama-folder']?.['llama-cpp']).toEqual({
      backend: 'llama-cpp',
      session_id: 'llama-cpp-abc',
    });
  });

  it('clears only the requested backend session', () => {
    setSession('shared-folder', {
      backend: 'claude',
      session_id: 'claude-session',
    });
    setSession('shared-folder', {
      backend: 'openai',
      session_id: 'resp_456',
    });

    clearSession('shared-folder', 'openai');

    expect(getSession('shared-folder', 'openai')).toBeUndefined();
    expect(getSession('shared-folder', 'claude')?.session_id).toBe(
      'claude-session',
    );
  });

  it('replaces the active session for one backend without touching the other', () => {
    setSession('shared-folder', {
      backend: 'claude',
      session_id: 'claude-session',
    });
    setSession('shared-folder', {
      backend: 'openai',
      session_id: 'resp_old',
    });
    setSession('shared-folder', {
      backend: 'openai',
      session_id: 'resp_new',
    });

    expect(getSession('shared-folder', 'openai')?.session_id).toBe('resp_new');
    expect(getSession('shared-folder', 'claude')?.session_id).toBe(
      'claude-session',
    );
  });
});

// --- Issue cache ---

function sampleIssue(id: string, stateName: string) {
  return {
    issue_id: id,
    identifier: `LIA-${id}`,
    title: `Issue ${id}`,
    state_name: stateName,
    team_id: 'team-1',
    priority: 0,
    created_at: '2026-05-20T10:00:00.000Z',
    updated_at: '2026-05-20T12:00:00.000Z',
  };
}

describe('linear_issue_cache', () => {
  describe('upsertIssueCache', () => {
    it('inserts a new row', () => {
      upsertIssueCache(sampleIssue('1', 'Ready for Agent'));
      expect(getIssueCacheCount()).toBe(1);
    });

    it('updates an existing row', () => {
      upsertIssueCache(sampleIssue('1', 'Ready for Agent'));
      upsertIssueCache({
        ...sampleIssue('1', 'Agent Working'),
        title: 'Updated',
      });
      expect(getIssueCacheCount()).toBe(1);
      const rows = getIssuesFromCache(['Agent Working']);
      expect(rows).toHaveLength(1);
      expect(rows[0].title).toBe('Updated');
    });

    it('re-activates soft-deleted rows', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      softDeleteIssueCache('1');
      expect(getIssueCacheCount()).toBe(0);
      upsertIssueCache(sampleIssue('1', 'Todo'));
      expect(getIssueCacheCount()).toBe(1);
    });
  });

  describe('softDeleteIssueCache', () => {
    it('marks a row as deleted', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      softDeleteIssueCache('1');
      expect(getIssueCacheCount()).toBe(0);
    });

    it('does not crash on nonexistent row', () => {
      expect(() => softDeleteIssueCache('nonexistent')).not.toThrow();
    });
  });

  describe('getIssueCacheCount', () => {
    it('returns 0 on empty table', () => {
      expect(getIssueCacheCount()).toBe(0);
    });

    it('excludes soft-deleted rows', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      upsertIssueCache(sampleIssue('2', 'Todo'));
      softDeleteIssueCache('1');
      expect(getIssueCacheCount()).toBe(1);
    });
  });

  describe('getMaxCachedAt', () => {
    it('returns null on empty table', () => {
      expect(getMaxCachedAt()).toBeNull();
    });

    it('returns the most recent cached_at', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      upsertIssueCache(sampleIssue('2', 'Backlog'));
      const result = getMaxCachedAt();
      expect(result).not.toBeNull();
      expect(typeof result).toBe('string');
    });
  });

  describe('getIssuesFromCache', () => {
    it('returns empty for no matching states', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      expect(getIssuesFromCache(['Done'])).toHaveLength(0);
    });

    it('filters by state name', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      upsertIssueCache(sampleIssue('2', 'Ready for Agent'));
      upsertIssueCache(sampleIssue('3', 'Done'));
      const result = getIssuesFromCache(['Todo', 'Ready for Agent']);
      expect(result).toHaveLength(2);
    });

    it('excludes soft-deleted rows', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      upsertIssueCache(sampleIssue('2', 'Todo'));
      softDeleteIssueCache('1');
      expect(getIssuesFromCache(['Todo'])).toHaveLength(1);
    });

    it('returns empty array for empty stateNames', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      expect(getIssuesFromCache([])).toHaveLength(0);
    });
  });

  describe('reconcileIssueCache', () => {
    it('upserts all issues and soft-deletes stale ones', () => {
      upsertIssueCache(sampleIssue('old', 'Todo'));
      reconcileIssueCache(new Set(['1', '2']), [
        sampleIssue('1', 'Todo'),
        sampleIssue('2', 'Ready for Agent'),
      ]);
      expect(getIssueCacheCount()).toBe(2);
      expect(getIssuesFromCache(['Todo'])).toHaveLength(1);
      expect(getIssuesFromCache(['Todo'])[0].identifier).toBe('LIA-1');
    });

    it('skips soft-delete when liveIssueIds is empty', () => {
      upsertIssueCache(sampleIssue('1', 'Todo'));
      reconcileIssueCache(new Set(), []);
      expect(getIssueCacheCount()).toBe(1);
    });

    it('handles large sets without hitting SQLite variable limit', () => {
      const ids = Array.from({ length: 50 }, (_, i) => `id-${i}`);
      const upserts = ids.map((id) => sampleIssue(id, 'Backlog'));
      upsertIssueCache(sampleIssue('stale', 'Backlog'));
      reconcileIssueCache(new Set(ids), upserts);
      expect(getIssueCacheCount()).toBe(50);
    });
  });
});

describe('circuit breaker', () => {
  // Post-Phase-3 (LIA-166): rows are written by insertPipelineEventRow (the sink's
  // and notifyPipelineStep's writer); logPipelineEvent is emit-only. These tests
  // seed rows via the actual writer so they exercise getConsecutiveFailCount /
  // getLastFailTime against real rows (independent of any registered sink).
  describe('getConsecutiveFailCount', () => {
    it('returns 0 when no events exist', () => {
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(0);
    });

    it('counts consecutive failures', () => {
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'automerge_failed',
        'CI failed',
      );
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'automerge_failed',
        'CI failed again',
      );
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(2);
    });

    it('resets count after agent_completed', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 1');
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 2');
      insertPipelineEventRow('issue-1', 'LIA-1', 'agent_completed');
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'automerge_failed',
        'fail after reset',
      );
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(1);
    });

    it('resets count after circuit_breaker_reset', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 1');
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 2');
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'circuit_breaker_reset',
        'manual',
      );
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(0);
    });

    it('counts agent failures separately from automerge failures', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'CI fail');
      insertPipelineEventRow('issue-1', 'LIA-1', 'agent_failed', 'agent crash');
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'agent_failed',
        'agent crash 2',
      );
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(1);
      expect(getConsecutiveFailCount('issue-1', 'agent_failed')).toBe(2);
    });

    it('isolates counts between different issues', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail');
      insertPipelineEventRow('issue-2', 'LIA-2', 'automerge_failed', 'fail');
      insertPipelineEventRow('issue-2', 'LIA-2', 'automerge_failed', 'fail 2');
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(1);
      expect(getConsecutiveFailCount('issue-2', 'automerge_failed')).toBe(2);
    });
  });

  describe('getLastFailTime', () => {
    it('returns null when no events exist', () => {
      expect(getLastFailTime('issue-1', 'automerge_failed')).toBeNull();
    });

    it('returns the most recent event time', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 1');
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 2');
      const lastTime = getLastFailTime('issue-1', 'automerge_failed');
      expect(lastTime).not.toBeNull();
      expect(typeof lastTime).toBe('string');
    });

    it('returns time for correct event type only', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'agent_failed', 'crash');
      expect(getLastFailTime('issue-1', 'automerge_failed')).toBeNull();
      expect(getLastFailTime('issue-1', 'agent_failed')).not.toBeNull();
    });

    it('returns null after reset clears the window', () => {
      insertPipelineEventRow('issue-1', 'LIA-1', 'automerge_failed', 'fail 1');
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'circuit_breaker_reset',
        'manual',
      );
      expect(getLastFailTime('issue-1', 'automerge_failed')).toBeNull();
    });

    it('returns time only from current failure epoch', () => {
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'automerge_failed',
        'old fail',
      );
      insertPipelineEventRow('issue-1', 'LIA-1', 'agent_completed');
      insertPipelineEventRow(
        'issue-1',
        'LIA-1',
        'automerge_failed',
        'new fail',
      );
      const lastTime = getLastFailTime('issue-1', 'automerge_failed');
      expect(lastTime).not.toBeNull();
      expect(getConsecutiveFailCount('issue-1', 'automerge_failed')).toBe(1);
    });
  });
});
