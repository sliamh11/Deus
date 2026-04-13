import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { loadTradingConfig, getSafetyConfig } from './config.js';

describe('loadTradingConfig', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    // Clear trading-related env vars
    for (const key of Object.keys(process.env)) {
      if (
        key.startsWith('TRADING_') ||
        key.startsWith('IBKR_')
      ) {
        delete process.env[key];
      }
    }
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('returns safe defaults when no env vars set', () => {
    const config = loadTradingConfig();

    expect(config.safety.allowOrders).toBe(false);
    expect(config.safety.mode).toBe('paper');
    expect(config.safety.maxRiskPerTrade).toBe(0.02);
    expect(config.safety.maxDailyDrawdown).toBe(0.03);
    expect(config.safety.maxOpenPositions).toBe(5);
    expect(config.safety.maxCorrelation).toBe(0.7);
    expect(config.safety.earningsBlackoutHours).toBe(48);
    expect(config.safety.stalenessThreshold).toBe(0.005);
    expect(config.safety.approvalTtlMs).toBe(600000);
    expect(config.safety.minConfluenceFactors).toBe(3);
    expect(config.safety.minConfidence).toBe(0.6);
    expect(config.safety.minRiskReward).toBe(2.0);
    expect(config.safety.vixCircuitBreaker).toBe(35);
    expect(config.ibkrPort).toBe(4002);
    expect(config.analysisTimeframes).toEqual(['15m', '1h', '4h', '1D']);
  });

  it('reads IBKR_ALLOW_ORDERS from env', () => {
    process.env.IBKR_ALLOW_ORDERS = 'true';
    const config = loadTradingConfig();
    expect(config.safety.allowOrders).toBe(true);
  });

  it('sets live port when IBKR_MODE is live', () => {
    process.env.IBKR_MODE = 'live';
    const config = loadTradingConfig();
    expect(config.safety.mode).toBe('live');
    expect(config.ibkrPort).toBe(5000);
  });

  it('respects explicit IBKR_PORT override', () => {
    process.env.IBKR_PORT = '4003';
    const config = loadTradingConfig();
    expect(config.ibkrPort).toBe(4003);
  });

  it('parses comma-separated instrument lists', () => {
    process.env.TRADING_INSTRUMENT_WHITELIST = 'AAPL,MSFT,GOOGL';
    const config = loadTradingConfig();
    expect(config.safety.instrumentWhitelist).toEqual([
      'AAPL',
      'MSFT',
      'GOOGL',
    ]);
  });

  it('parses custom timeframes', () => {
    process.env.TRADING_TIMEFRAMES = '5m,15m,1h';
    const config = loadTradingConfig();
    expect(config.analysisTimeframes).toEqual(['5m', '15m', '1h']);
  });

  it('parses numeric config values', () => {
    process.env.TRADING_MAX_RISK_PER_TRADE = '0.01';
    process.env.TRADING_MAX_DAILY_DRAWDOWN = '0.05';
    process.env.TRADING_MAX_OPEN_POSITIONS = '3';
    const config = loadTradingConfig();
    expect(config.safety.maxRiskPerTrade).toBe(0.01);
    expect(config.safety.maxDailyDrawdown).toBe(0.05);
    expect(config.safety.maxOpenPositions).toBe(3);
  });
});

describe('getSafetyConfig', () => {
  it('returns the safety subset of config', () => {
    const config = loadTradingConfig();
    const safety = getSafetyConfig(config);
    expect(safety).toBe(config.safety);
    expect(safety.allowOrders).toBe(false);
  });
});
