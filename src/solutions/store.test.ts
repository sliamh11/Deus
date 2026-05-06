import fs from 'fs';
import os from 'os';
import path from 'path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  getSolution,
  listSolutions,
  loadSolutionContext,
  searchSolutions,
  writeSolution,
} from './store.js';

let tmpDir: string;

describe('solution store', () => {
  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-solutions-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('writes a bug solution and reads it back by ID', () => {
    const id = writeSolution(
      {
        title: 'Fix container timeout',
        tags: ['docker', 'timeout'],
        problemType: 'bug',
        module: 'src/container-runner.ts',
        severity: 'high',
        symptoms: 'Container hangs after 30s',
        deadEnds: 'Tried increasing CONTAINER_TIMEOUT env — no effect',
        solution: 'The timeout was in the IPC polling loop, not the container',
        prevention: 'Check IPC_POLL_INTERVAL when debugging container timeouts',
      },
      tmpDir,
    );

    expect(id).toBeTruthy();

    const sol = getSolution(id, tmpDir);
    expect(sol).not.toBeNull();
    expect(sol!.title).toBe('Fix container timeout');
    expect(sol!.tags).toEqual(['docker', 'timeout']);
    expect(sol!.problemType).toBe('bug');
    expect(sol!.severity).toBe('high');
    expect(sol!.symptoms).toBe('Container hangs after 30s');
    expect(sol!.solution).toBe(
      'The timeout was in the IPC polling loop, not the container',
    );
  });

  it('writes a knowledge solution with correct sections', () => {
    const id = writeSolution(
      {
        title: 'Memory indexer batch API limits',
        tags: ['memory', 'api'],
        problemType: 'knowledge',
        severity: 'medium',
        symptoms: 'Gemini embedding API has a 100-text batch limit',
        deadEnds: '',
        solution: 'Split into chunks of 100 before calling embed_batch',
        prevention: 'When processing > 100 documents at once',
      },
      tmpDir,
    );

    const sol = getSolution(id, tmpDir);
    expect(sol).not.toBeNull();
    expect(sol!.problemType).toBe('knowledge');
    // Knowledge type reads Context/Guidance/When to Apply sections
    expect(sol!.symptoms).toBe(
      'Gemini embedding API has a 100-text batch limit',
    );
    expect(sol!.solution).toBe(
      'Split into chunks of 100 before calling embed_batch',
    );
    expect(sol!.prevention).toBe('When processing > 100 documents at once');
  });

  it('searches by text query', () => {
    writeSolution(
      {
        title: 'OAuth token refresh loop',
        tags: ['auth'],
        problemType: 'bug',
        severity: 'high',
        symptoms: 'Login loop after token expiry',
        deadEnds: 'Tried clearing .env',
        solution: 'Read credentials.json directly',
        prevention: 'Never freeze tokens in .env',
      },
      tmpDir,
    );
    writeSolution(
      {
        title: 'Container mount paths',
        tags: ['docker'],
        problemType: 'pattern',
        severity: 'low',
        symptoms: 'Mounts fail on relative paths',
        deadEnds: 'None',
        solution: 'Always use path.resolve',
        prevention: 'Use absolute paths for mounts',
      },
      tmpDir,
    );

    const results = searchSolutions('token', undefined, tmpDir);
    expect(results).toHaveLength(1);
    expect(results[0].title).toBe('OAuth token refresh loop');
  });

  it('filters by tags', () => {
    writeSolution(
      {
        title: 'Auth bug',
        tags: ['auth', 'oauth'],
        problemType: 'bug',
        severity: 'medium',
        symptoms: 'x',
        deadEnds: 'y',
        solution: 'z',
        prevention: 'w',
      },
      tmpDir,
    );
    writeSolution(
      {
        title: 'Docker bug',
        tags: ['docker'],
        problemType: 'bug',
        severity: 'medium',
        symptoms: 'x',
        deadEnds: 'y',
        solution: 'z',
        prevention: 'w',
      },
      tmpDir,
    );

    const authResults = searchSolutions('', ['auth'], tmpDir);
    expect(authResults).toHaveLength(1);
    expect(authResults[0].title).toBe('Auth bug');

    const allResults = searchSolutions('', undefined, tmpDir);
    expect(allResults).toHaveLength(2);
  });

  it('returns null for nonexistent ID', () => {
    expect(getSolution('nonexistent-id', tmpDir)).toBeNull();
  });

  it('lists solutions sorted by mtime (newest first)', () => {
    writeSolution(
      {
        title: 'First',
        tags: [],
        problemType: 'bug',
        severity: 'low',
        symptoms: 'a',
        deadEnds: 'b',
        solution: 'c',
        prevention: 'd',
      },
      tmpDir,
    );
    // Slight delay to ensure different mtime
    const files = fs.readdirSync(tmpDir);
    const firstFile = path.join(tmpDir, files[0]);
    // Backdate the first file
    const past = new Date(Date.now() - 10_000);
    fs.utimesSync(firstFile, past, past);

    writeSolution(
      {
        title: 'Second',
        tags: [],
        problemType: 'bug',
        severity: 'low',
        symptoms: 'a',
        deadEnds: 'b',
        solution: 'c',
        prevention: 'd',
      },
      tmpDir,
    );

    const list = listSolutions(10, tmpDir);
    expect(list).toHaveLength(2);
    expect(list[0].title).toBe('Second');
    expect(list[1].title).toBe('First');
  });

  it('limits results in listSolutions', () => {
    for (let i = 0; i < 5; i++) {
      writeSolution(
        {
          title: `Solution ${i}`,
          tags: [],
          problemType: 'bug',
          severity: 'low',
          symptoms: 'a',
          deadEnds: 'b',
          solution: 'c',
          prevention: 'd',
        },
        tmpDir,
      );
    }

    expect(listSolutions(3, tmpDir)).toHaveLength(3);
  });

  it('loadSolutionContext returns formatted strings', () => {
    writeSolution(
      {
        title: 'Test solution',
        tags: ['test'],
        problemType: 'bug',
        severity: 'medium',
        symptoms: 'Test fails intermittently',
        deadEnds: 'Tried retries',
        solution: 'Race condition in setup',
        prevention: 'Use beforeEach isolation',
      },
      tmpDir,
    );

    const ctx = loadSolutionContext(3, tmpDir);
    expect(ctx).toHaveLength(1);
    expect(ctx[0]).toContain('Solution: Test solution [test]');
    expect(ctx[0]).toContain('Symptoms: Test fails intermittently');
    expect(ctx[0]).toContain('Fix: Race condition in setup');
  });

  it('loadSolutionContext formats knowledge type differently', () => {
    writeSolution(
      {
        title: 'API rate limits',
        tags: ['api'],
        problemType: 'knowledge',
        severity: 'low',
        symptoms: 'Rate limit is 100/min',
        deadEnds: '',
        solution: 'Use exponential backoff',
        prevention: 'When calling external APIs',
      },
      tmpDir,
    );

    const ctx = loadSolutionContext(3, tmpDir);
    expect(ctx).toHaveLength(1);
    expect(ctx[0]).toContain('Knowledge: API rate limits [api]');
    expect(ctx[0]).toContain('Context: Rate limit is 100/min');
    expect(ctx[0]).toContain('Guidance: Use exponential backoff');
  });

  it('returns empty results for empty directory', () => {
    expect(searchSolutions('anything', undefined, tmpDir)).toEqual([]);
    expect(listSolutions(10, tmpDir)).toEqual([]);
    expect(getSolution('any-id', tmpDir)).toBeNull();
  });

  it('preserves solution files (append-only)', () => {
    const id = writeSolution(
      {
        title: 'Append only test',
        tags: [],
        problemType: 'bug',
        severity: 'low',
        symptoms: 'a',
        deadEnds: 'b',
        solution: 'c',
        prevention: 'd',
      },
      tmpDir,
    );

    // File should exist
    const files = fs.readdirSync(tmpDir);
    expect(files).toHaveLength(1);

    // Writing another solution should create a new file, not overwrite
    writeSolution(
      {
        title: 'Second solution',
        tags: [],
        problemType: 'bug',
        severity: 'low',
        symptoms: 'e',
        deadEnds: 'f',
        solution: 'g',
        prevention: 'h',
      },
      tmpDir,
    );

    expect(fs.readdirSync(tmpDir)).toHaveLength(2);
    // Original still intact
    expect(getSolution(id, tmpDir)).not.toBeNull();
  });

  it('handles solutions with special characters in title', () => {
    const id = writeSolution(
      {
        title: 'Fix "quoted" title & special <chars>',
        tags: ['special'],
        problemType: 'bug',
        severity: 'low',
        symptoms: 'a',
        deadEnds: 'b',
        solution: 'c',
        prevention: 'd',
      },
      tmpDir,
    );

    const sol = getSolution(id, tmpDir);
    expect(sol).not.toBeNull();
    expect(sol!.title).toBe('Fix "quoted" title & special <chars>');
  });
});
