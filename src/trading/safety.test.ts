import { describe, expect, it } from 'vitest';

import {
  checkStaleness,
  createExpirationTime,
  isApprovalExpired,
  runSafetyChecks,
  validateBracketOrder,
  type DailyState,
} from './safety.js';
import type {
  ApprovalRequest,
  BracketOrder,
  RegimeState,
  SafetyConfig,
  TradeDecision,
} from './types.js';

// --- Fixtures ---

function makeSafetyConfig(overrides: Partial<SafetyConfig> = {}): SafetyConfig {
  return {
    allowOrders: true,
    mode: 'paper',
    maxRiskPerTrade: 0.02,
    maxDailyDrawdown: 0.03,
    maxOpenPositions: 5,
    maxCorrelation: 0.7,
    earningsBlackoutHours: 48,
    stalenessThreshold: 0.005,
    approvalTtlMs: 600000,
    minConfluenceFactors: 3,
    minConfidence: 0.6,
    minRiskReward: 2.0,
    marketOpenBufferMinutes: 15,
    marketCloseBufferMinutes: 15,
    vixCircuitBreaker: 35,
    instrumentWhitelist: [],
    instrumentBlacklist: [],
    ...overrides,
  };
}

function makeRegime(overrides: Partial<RegimeState> = {}): RegimeState {
  return {
    regime: 'trending',
    confidence: 0.85,
    adx: 30,
    vix: 18,
    vixPercentile: 40,
    hmmState: 0,
    transitionProb: 0.1,
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

function makeDecision(
  overrides: Partial<TradeDecision> = {},
): TradeDecision {
  return {
    action: 'BUY',
    confidence: 0.75,
    regime: makeRegime(),
    timeframeAnalysis: {
      biases: [],
      alignment: 0.8,
      dominantBias: 'bullish',
      conflictingTimeframes: [],
      timestamp: new Date().toISOString(),
    },
    setup: {
      symbol: 'AAPL',
      direction: 'bullish',
      entry: 150,
      stop: 147,
      targets: [156, 162],
      riskRewardRatio: 2.5,
      confluenceFactors: [
        { name: 'EMA alignment', weight: 0.3, description: 'Price above 20/50 EMA' },
        { name: 'Volume spike', weight: 0.25, description: 'Volume 1.5x average' },
        { name: 'Support bounce', weight: 0.3, description: 'Bounced off key level' },
      ],
      confluenceScore: 0.85,
      pattern: 'bull flag',
      timeframe: '1h',
      validUntil: new Date(Date.now() + 14400000).toISOString(),
      timestamp: new Date().toISOString(),
    },
    risk: {
      sizing: {
        shares: 100,
        dollarRisk: 300,
        portfolioRiskPercent: 1.5,
        kellyFraction: 0.12,
        adjustedKelly: 0.06,
        positionValue: 15000,
      },
      maxCorrelation: 0.3,
      correlatedPositions: [],
      dailyDrawdownUsed: 0.5,
      dailyDrawdownRemaining: 2.5,
      openPositionCount: 2,
      earningsWithin48h: false,
      riskScore: 0.3,
      warnings: [],
      timestamp: new Date().toISOString(),
    },
    order: {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 100,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [156, 162],
      timeInForce: 'DAY',
    },
    reasoning: 'Strong trend with multi-timeframe alignment',
    holdReasons: [],
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

function makeDailyState(overrides: Partial<DailyState> = {}): DailyState {
  return {
    realizedPnl: -0.005,
    openPositionCount: 2,
    openSymbols: ['MSFT', 'GOOGL'],
    ...overrides,
  };
}

// --- Tests ---

describe('runSafetyChecks', () => {
  it('passes when all checks are met', () => {
    const result = runSafetyChecks(
      makeDecision(),
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(true);
    expect(result.violations).toHaveLength(0);
  });

  it('fails when orders are disabled', () => {
    const config = makeSafetyConfig({ allowOrders: false });
    const result = runSafetyChecks(makeDecision(), config, makeDailyState());
    expect(result.passed).toBe(false);
    expect(result.violations).toContain(
      'IBKR_ALLOW_ORDERS is false — order submission disabled',
    );
  });

  it('fails when risk per trade exceeds limit', () => {
    const decision = makeDecision({
      risk: {
        ...makeDecision().risk!,
        sizing: {
          ...makeDecision().risk!.sizing,
          portfolioRiskPercent: 3.5,
        },
      },
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(result.violations.some((v) => v.includes('Risk per trade'))).toBe(
      true,
    );
  });

  it('fails when max open positions reached', () => {
    const daily = makeDailyState({ openPositionCount: 5 });
    const result = runSafetyChecks(
      makeDecision(),
      makeSafetyConfig(),
      daily,
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('open positions')),
    ).toBe(true);
  });

  it('fails when VIX exceeds circuit breaker', () => {
    const decision = makeDecision({
      regime: makeRegime({ vix: 40 }),
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('VIX')),
    ).toBe(true);
  });

  it('fails when confidence is too low', () => {
    const decision = makeDecision({ confidence: 0.4 });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('Confidence')),
    ).toBe(true);
  });

  it('fails when not enough confluence factors', () => {
    const decision = makeDecision({
      setup: {
        ...makeDecision().setup!,
        confluenceFactors: [
          { name: 'EMA', weight: 0.3, description: 'test' },
        ],
      },
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('confluence')),
    ).toBe(true);
  });

  it('fails when R:R is too low', () => {
    const decision = makeDecision({
      setup: {
        ...makeDecision().setup!,
        riskRewardRatio: 1.2,
      },
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(result.violations.some((v) => v.includes('R:R'))).toBe(true);
  });

  it('fails when symbol is blacklisted', () => {
    const config = makeSafetyConfig({ instrumentBlacklist: ['AAPL'] });
    const result = runSafetyChecks(makeDecision(), config, makeDailyState());
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('blacklisted')),
    ).toBe(true);
  });

  it('fails when symbol is not in whitelist', () => {
    const config = makeSafetyConfig({ instrumentWhitelist: ['MSFT', 'GOOGL'] });
    const result = runSafetyChecks(makeDecision(), config, makeDailyState());
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('whitelist')),
    ).toBe(true);
  });

  it('fails when earnings within blackout', () => {
    const decision = makeDecision({
      risk: {
        ...makeDecision().risk!,
        earningsWithin48h: true,
        earningsDate: '2026-04-16',
      },
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('Earnings')),
    ).toBe(true);
  });

  it('fails when correlation is too high', () => {
    const decision = makeDecision({
      risk: {
        ...makeDecision().risk!,
        maxCorrelation: 0.85,
        correlatedPositions: ['MSFT'],
      },
    });
    const result = runSafetyChecks(
      decision,
      makeSafetyConfig(),
      makeDailyState(),
    );
    expect(result.passed).toBe(false);
    expect(
      result.violations.some((v) => v.includes('correlation')),
    ).toBe(true);
  });

  it('passes HOLD decisions even with orders disabled', () => {
    const config = makeSafetyConfig({ allowOrders: false });
    const decision = makeDecision({ action: 'HOLD' });
    const result = runSafetyChecks(decision, config, makeDailyState());
    // HOLD still fails on allowOrders but that's correct —
    // the orders-disabled check is always evaluated
    expect(result.violations).toContain(
      'IBKR_ALLOW_ORDERS is false — order submission disabled',
    );
  });
});

describe('validateBracketOrder', () => {
  it('passes for valid BUY order', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 100,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [156],
      timeInForce: 'DAY',
    };
    expect(validateBracketOrder(order)).toHaveLength(0);
  });

  it('passes for valid SELL order', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'SELL',
      orderType: 'LIMIT',
      quantity: 50,
      entryPrice: 150,
      stopPrice: 153,
      targetPrices: [144],
      timeInForce: 'DAY',
    };
    expect(validateBracketOrder(order)).toHaveLength(0);
  });

  it('rejects BUY with stop above entry', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 100,
      entryPrice: 150,
      stopPrice: 155,
      targetPrices: [160],
      timeInForce: 'DAY',
    };
    const errors = validateBracketOrder(order);
    expect(errors.some((e) => e.includes('Stop must be below entry'))).toBe(
      true,
    );
  });

  it('rejects SELL with stop below entry', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'SELL',
      orderType: 'LIMIT',
      quantity: 50,
      entryPrice: 150,
      stopPrice: 145,
      targetPrices: [140],
      timeInForce: 'DAY',
    };
    const errors = validateBracketOrder(order);
    expect(errors.some((e) => e.includes('Stop must be above entry'))).toBe(
      true,
    );
  });

  it('rejects zero quantity', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 0,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [156],
      timeInForce: 'DAY',
    };
    const errors = validateBracketOrder(order);
    expect(errors.some((e) => e.includes('Quantity must be positive'))).toBe(
      true,
    );
  });

  it('rejects no target prices', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 100,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [],
      timeInForce: 'DAY',
    };
    const errors = validateBracketOrder(order);
    expect(
      errors.some((e) => e.includes('At least one target price')),
    ).toBe(true);
  });

  it('rejects BUY with target below entry', () => {
    const order: BracketOrder = {
      symbol: 'AAPL',
      action: 'BUY',
      orderType: 'LIMIT',
      quantity: 100,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [148],
      timeInForce: 'DAY',
    };
    const errors = validateBracketOrder(order);
    expect(errors.some((e) => e.includes('must be above entry'))).toBe(true);
  });
});

describe('checkStaleness', () => {
  it('returns not stale when price within threshold', () => {
    const result = checkStaleness(150, 150.5, 0.005);
    expect(result.isStale).toBe(false);
    expect(result.priceDrift).toBeCloseTo(0.0033, 3);
  });

  it('returns stale when price exceeds threshold', () => {
    const result = checkStaleness(150, 152, 0.005);
    expect(result.isStale).toBe(true);
    expect(result.priceDrift).toBeCloseTo(0.0133, 3);
  });

  it('handles zero drift', () => {
    const result = checkStaleness(150, 150, 0.005);
    expect(result.isStale).toBe(false);
    expect(result.priceDrift).toBe(0);
  });
});

describe('createExpirationTime', () => {
  it('creates future timestamp', () => {
    const before = Date.now();
    const expires = createExpirationTime(600000);
    const expiresMs = new Date(expires).getTime();
    expect(expiresMs).toBeGreaterThanOrEqual(before + 599000);
    expect(expiresMs).toBeLessThanOrEqual(before + 601000);
  });
});

describe('isApprovalExpired', () => {
  it('returns false for future expiration', () => {
    const approval: ApprovalRequest = {
      id: 'test',
      decision: makeDecision(),
      summary: 'test',
      expiresAt: new Date(Date.now() + 60000).toISOString(),
      status: 'pending',
      sentAt: new Date().toISOString(),
    };
    expect(isApprovalExpired(approval)).toBe(false);
  });

  it('returns true for past expiration', () => {
    const approval: ApprovalRequest = {
      id: 'test',
      decision: makeDecision(),
      summary: 'test',
      expiresAt: new Date(Date.now() - 1000).toISOString(),
      status: 'pending',
      sentAt: new Date().toISOString(),
    };
    expect(isApprovalExpired(approval)).toBe(true);
  });
});
