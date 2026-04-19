import { describe, expect, it } from 'vitest';

import {
  DeusError,
  FatalError,
  RetryableError,
  UserError,
  isDeusError,
} from './index.js';

describe('DeusError', () => {
  it('captures message, name, and empty default context', () => {
    const err = new DeusError('boom');
    expect(err.message).toBe('boom');
    expect(err.name).toBe('DeusError');
    expect(err.context).toEqual({});
    expect(err instanceof Error).toBe(true);
    expect(err instanceof DeusError).toBe(true);
  });

  it('attaches structured context', () => {
    const err = new DeusError('fail', { context: { url: '/x', status: 502 } });
    expect(err.context).toEqual({ url: '/x', status: 502 });
  });

  it('preserves cause via ES2022 Error.cause', () => {
    const inner = new Error('inner');
    const err = new DeusError('wrap', { cause: inner });
    expect(err.cause).toBe(inner);
  });

  it('toJSON flattens cause chain', () => {
    const root = new Error('root');
    const mid = new DeusError('mid', { cause: root, context: { step: 1 } });
    const top = new RetryableError('top', {
      cause: mid,
      context: { attempt: 3 },
    });
    const json = top.toJSON();
    expect(json).toMatchObject({
      name: 'RetryableError',
      message: 'top',
      context: { attempt: 3 },
      cause: {
        name: 'DeusError',
        message: 'mid',
        context: { step: 1 },
        cause: { name: 'Error', message: 'root' },
      },
    });
  });

  it('handles non-Error causes', () => {
    const err = new DeusError('fail', { cause: 'plain string' });
    expect(err.toJSON().cause).toBe('plain string');
  });

  it('handles no cause', () => {
    const err = new DeusError('fail');
    expect(err.toJSON().cause).toBeUndefined();
  });
});

describe('subclasses', () => {
  it('each subclass has its own name and is instanceof DeusError', () => {
    const retry = new RetryableError('r');
    const fatal = new FatalError('f');
    const user = new UserError('u');

    expect(retry.name).toBe('RetryableError');
    expect(fatal.name).toBe('FatalError');
    expect(user.name).toBe('UserError');

    for (const err of [retry, fatal, user]) {
      expect(err instanceof DeusError).toBe(true);
      expect(err instanceof Error).toBe(true);
    }
  });

  it('subclasses are disjoint (RetryableError is not a FatalError)', () => {
    const retry = new RetryableError('r');
    expect(retry instanceof FatalError).toBe(false);
    expect(retry instanceof UserError).toBe(false);
  });
});

describe('isDeusError', () => {
  it('true for every Deus error class', () => {
    expect(isDeusError(new DeusError('x'))).toBe(true);
    expect(isDeusError(new RetryableError('x'))).toBe(true);
    expect(isDeusError(new FatalError('x'))).toBe(true);
    expect(isDeusError(new UserError('x'))).toBe(true);
  });

  it('false for plain Error and non-errors', () => {
    expect(isDeusError(new Error('x'))).toBe(false);
    expect(isDeusError('string')).toBe(false);
    expect(isDeusError(null)).toBe(false);
    expect(isDeusError(undefined)).toBe(false);
    expect(isDeusError({ message: 'fake' })).toBe(false);
  });
});
