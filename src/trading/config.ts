/**
 * Trading analysis engine configuration.
 *
 * All risk parameters are environment-configurable with safe defaults.
 * Paper trading mode is the default — live mode requires explicit opt-in.
 */

import { z } from 'zod/v4';

import { readEnvFile } from '../env.js';

import type { SafetyConfig, Timeframe, TradingMode } from './types.js';

// --- Zod Schemas ---

const TradingModeSchema = z.enum(['paper', 'live']);

const SafetyConfigSchema = z.object({
  allowOrders: z.boolean().default(false),
  mode: TradingModeSchema.default('paper'),
  maxRiskPerTrade: z.number().min(0).max(1).default(0.02),
  maxDailyDrawdown: z.number().min(0).max(1).default(0.03),
  maxOpenPositions: z.number().int().min(1).default(5),
  maxCorrelation: z.number().min(0).max(1).default(0.7),
  earningsBlackoutHours: z.number().int().min(0).default(48),
  stalenessThreshold: z.number().min(0).max(1).default(0.005),
  approvalTtlMs: z.number().int().min(60000).default(600000),
  minConfluenceFactors: z.number().int().min(1).default(3),
  minConfidence: z.number().min(0).max(1).default(0.6),
  minRiskReward: z.number().min(0).default(2.0),
  marketOpenBufferMinutes: z.number().int().min(0).default(15),
  marketCloseBufferMinutes: z.number().int().min(0).default(15),
  vixCircuitBreaker: z.number().min(0).default(35),
  instrumentWhitelist: z.array(z.string()).default([]),
  instrumentBlacklist: z.array(z.string()).default([]),
});

const TradingConfigSchema = z.object({
  safety: SafetyConfigSchema,
  ibkrPort: z.number().int().default(4002),
  analysisTimeframes: z
    .array(z.enum(['1m', '5m', '15m', '1h', '4h', '1D', '1W']))
    .default(['15m', '1h', '4h', '1D']),
  portfolioValue: z.number().min(0).default(0),
  analysisModel: z.string().default('claude-sonnet-4-20250514'),
  decisionModel: z.string().default('claude-opus-4-20250514'),
});

export type TradingConfig = z.infer<typeof TradingConfigSchema>;

// --- Environment Parsing ---

const TRADING_ENV_KEYS = [
  'IBKR_ALLOW_ORDERS',
  'IBKR_MODE',
  'IBKR_PORT',
  'TRADING_MAX_RISK_PER_TRADE',
  'TRADING_MAX_DAILY_DRAWDOWN',
  'TRADING_MAX_OPEN_POSITIONS',
  'TRADING_MAX_CORRELATION',
  'TRADING_EARNINGS_BLACKOUT_HOURS',
  'TRADING_STALENESS_THRESHOLD',
  'TRADING_APPROVAL_TTL_MS',
  'TRADING_MIN_CONFLUENCE',
  'TRADING_MIN_CONFIDENCE',
  'TRADING_MIN_RISK_REWARD',
  'TRADING_MARKET_OPEN_BUFFER',
  'TRADING_MARKET_CLOSE_BUFFER',
  'TRADING_VIX_CIRCUIT_BREAKER',
  'TRADING_INSTRUMENT_WHITELIST',
  'TRADING_INSTRUMENT_BLACKLIST',
  'TRADING_TIMEFRAMES',
  'TRADING_PORTFOLIO_VALUE',
  'TRADING_ANALYSIS_MODEL',
  'TRADING_DECISION_MODEL',
];

function parseCommaSeparated(value: string | undefined): string[] {
  if (!value) return [];
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseFloat_(
  value: string | undefined,
  fallback: number,
): number {
  if (!value) return fallback;
  const n = parseFloat(value);
  return Number.isNaN(n) ? fallback : n;
}

function parseInt_(
  value: string | undefined,
  fallback: number,
): number {
  if (!value) return fallback;
  const n = parseInt(value, 10);
  return Number.isNaN(n) ? fallback : n;
}

/** Load trading config from environment + .env file with Zod validation. */
export function loadTradingConfig(): TradingConfig {
  const env = readEnvFile(TRADING_ENV_KEYS);
  const get = (key: string) => process.env[key] || env[key];

  const mode: TradingMode =
    get('IBKR_MODE') === 'live' ? 'live' : 'paper';

  const raw = {
    safety: {
      allowOrders: get('IBKR_ALLOW_ORDERS') === 'true',
      mode,
      maxRiskPerTrade: parseFloat_(get('TRADING_MAX_RISK_PER_TRADE'), 0.02),
      maxDailyDrawdown: parseFloat_(
        get('TRADING_MAX_DAILY_DRAWDOWN'),
        0.03,
      ),
      maxOpenPositions: parseInt_(get('TRADING_MAX_OPEN_POSITIONS'), 5),
      maxCorrelation: parseFloat_(get('TRADING_MAX_CORRELATION'), 0.7),
      earningsBlackoutHours: parseInt_(
        get('TRADING_EARNINGS_BLACKOUT_HOURS'),
        48,
      ),
      stalenessThreshold: parseFloat_(
        get('TRADING_STALENESS_THRESHOLD'),
        0.005,
      ),
      approvalTtlMs: parseInt_(get('TRADING_APPROVAL_TTL_MS'), 600000),
      minConfluenceFactors: parseInt_(get('TRADING_MIN_CONFLUENCE'), 3),
      minConfidence: parseFloat_(get('TRADING_MIN_CONFIDENCE'), 0.6),
      minRiskReward: parseFloat_(get('TRADING_MIN_RISK_REWARD'), 2.0),
      marketOpenBufferMinutes: parseInt_(
        get('TRADING_MARKET_OPEN_BUFFER'),
        15,
      ),
      marketCloseBufferMinutes: parseInt_(
        get('TRADING_MARKET_CLOSE_BUFFER'),
        15,
      ),
      vixCircuitBreaker: parseFloat_(
        get('TRADING_VIX_CIRCUIT_BREAKER'),
        35,
      ),
      instrumentWhitelist: parseCommaSeparated(
        get('TRADING_INSTRUMENT_WHITELIST'),
      ),
      instrumentBlacklist: parseCommaSeparated(
        get('TRADING_INSTRUMENT_BLACKLIST'),
      ),
    },
    ibkrPort: mode === 'live' ? 5000 : 4002,
    analysisTimeframes: parseCommaSeparated(
      get('TRADING_TIMEFRAMES'),
    ) as Timeframe[],
    portfolioValue: parseFloat_(get('TRADING_PORTFOLIO_VALUE'), 0),
    analysisModel: get('TRADING_ANALYSIS_MODEL') || 'claude-sonnet-4-20250514',
    decisionModel: get('TRADING_DECISION_MODEL') || 'claude-opus-4-20250514',
  };

  // Override port if explicitly set
  const explicitPort = parseInt_(get('IBKR_PORT'), 0);
  if (explicitPort > 0) {
    raw.ibkrPort = explicitPort;
  }

  // Default timeframes if none specified
  if (raw.analysisTimeframes.length === 0) {
    raw.analysisTimeframes = ['15m', '1h', '4h', '1D'];
  }

  return TradingConfigSchema.parse(raw);
}

/** Get safety config subset for quick access. */
export function getSafetyConfig(config: TradingConfig): SafetyConfig {
  return config.safety;
}
