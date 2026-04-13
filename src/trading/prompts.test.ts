import { describe, expect, it } from 'vitest';

import {
  decisionPrompt,
  formatApprovalMessage,
  multiTimeframeBiasPrompt,
  regimeDetectionPrompt,
  riskAssessmentPrompt,
  setupIdentificationPrompt,
} from './prompts.js';
import type {
  ChartData,
  IndicatorValues,
  MultiTimeframeAnalysis,
  RegimeState,
  RiskAssessment,
  SafetyConfig,
  Timeframe,
  TradeDecision,
  TradeSetup,
} from './types.js';

// --- Fixtures ---

function makeChartData(
  overrides: Partial<ChartData> = {},
): ChartData {
  return {
    symbol: 'AAPL',
    timeframe: '1D',
    bars: Array.from({ length: 50 }, (_, i) => ({
      time: Date.now() - (50 - i) * 86400000,
      open: 148 + i * 0.1,
      high: 149 + i * 0.1,
      low: 147 + i * 0.1,
      close: 148.5 + i * 0.1,
      volume: 50000000 + i * 100000,
    })),
    indicators: {},
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

function makeIndicators(
  overrides: Partial<IndicatorValues> = {},
): IndicatorValues {
  return {
    rsi: 55,
    adx: 28,
    atr: 2.5,
    ema20: 150,
    ema50: 148,
    ema200: 145,
    vwap: 149.5,
    volume: 55000000,
    avgVolume: 50000000,
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

// --- Tests ---

describe('regimeDetectionPrompt', () => {
  it('produces system and user messages', () => {
    const prompt = regimeDetectionPrompt(
      makeChartData(),
      makeIndicators(),
      18,
    );
    expect(prompt.system).toContain('regime analyst');
    expect(prompt.user).toContain('AAPL');
    expect(prompt.user).toContain('ADX: 28');
    expect(prompt.user).toContain('VIX: 18');
  });

  it('includes JSON schema in system prompt', () => {
    const prompt = regimeDetectionPrompt(
      makeChartData(),
      makeIndicators(),
      18,
    );
    expect(prompt.system).toContain('"regime"');
    expect(prompt.system).toContain('"confidence"');
  });
});

describe('multiTimeframeBiasPrompt', () => {
  it('includes all timeframes in user message', () => {
    const chartMap = new Map<Timeframe, ChartData>();
    const indMap = new Map<Timeframe, IndicatorValues>();
    for (const tf of ['15m', '1h', '4h', '1D'] as Timeframe[]) {
      chartMap.set(tf, makeChartData({ timeframe: tf }));
      indMap.set(tf, makeIndicators());
    }

    const prompt = multiTimeframeBiasPrompt(
      chartMap,
      indMap,
      makeRegime(),
    );
    expect(prompt.user).toContain('### 15m');
    expect(prompt.user).toContain('### 1h');
    expect(prompt.user).toContain('### 4h');
    expect(prompt.user).toContain('### 1D');
  });

  it('includes regime context in system prompt', () => {
    const chartMap = new Map<Timeframe, ChartData>();
    const indMap = new Map<Timeframe, IndicatorValues>();
    chartMap.set('1D', makeChartData());
    indMap.set('1D', makeIndicators());

    const prompt = multiTimeframeBiasPrompt(
      chartMap,
      indMap,
      makeRegime({ regime: 'volatile' }),
    );
    expect(prompt.system).toContain('volatile');
  });
});

describe('setupIdentificationPrompt', () => {
  it('includes regime and MTF analysis', () => {
    const mtf: MultiTimeframeAnalysis = {
      biases: [
        {
          timeframe: '1D',
          direction: 'bullish',
          strength: 0.8,
          keyLevels: { support: [145, 148], resistance: [155, 160] },
          trendStructure: 'higher-highs',
        },
      ],
      alignment: 0.85,
      dominantBias: 'bullish',
      conflictingTimeframes: [],
      timestamp: new Date().toISOString(),
    };

    const prompt = setupIdentificationPrompt(
      makeChartData({ timeframe: '1h' }),
      makeIndicators(),
      makeRegime(),
      mtf,
    );
    expect(prompt.user).toContain('trending');
    expect(prompt.user).toContain('bullish');
    expect(prompt.user).toContain('145');
    expect(prompt.system).toContain('confluence');
  });
});

describe('riskAssessmentPrompt', () => {
  it('includes setup details and portfolio info', () => {
    const setup: TradeSetup = {
      symbol: 'AAPL',
      direction: 'bullish',
      entry: 150,
      stop: 147,
      targets: [156, 162],
      riskRewardRatio: 2.5,
      confluenceFactors: [
        { name: 'EMA', weight: 0.3, description: 'test' },
        { name: 'Volume', weight: 0.25, description: 'test' },
        { name: 'Support', weight: 0.3, description: 'test' },
      ],
      confluenceScore: 0.85,
      timeframe: '1h',
      validUntil: new Date().toISOString(),
      timestamp: new Date().toISOString(),
    };

    const prompt = riskAssessmentPrompt(
      setup,
      100000,
      [{ symbol: 'MSFT', value: 15000, direction: 'bullish' }],
      makeSafetyConfig(),
    );
    expect(prompt.user).toContain('AAPL');
    expect(prompt.user).toContain('$100000');
    expect(prompt.user).toContain('MSFT');
    expect(prompt.system).toContain('Kelly');
  });
});

describe('decisionPrompt', () => {
  it('produces decision prompt with all inputs', () => {
    const mtf: MultiTimeframeAnalysis = {
      biases: [],
      alignment: 0.8,
      dominantBias: 'bullish',
      conflictingTimeframes: [],
      timestamp: new Date().toISOString(),
    };

    const setup: TradeSetup = {
      symbol: 'AAPL',
      direction: 'bullish',
      entry: 150,
      stop: 147,
      targets: [156],
      riskRewardRatio: 2.5,
      confluenceFactors: [
        { name: 'A', weight: 0.3, description: '' },
        { name: 'B', weight: 0.3, description: '' },
        { name: 'C', weight: 0.3, description: '' },
      ],
      confluenceScore: 0.9,
      timeframe: '1h',
      validUntil: new Date().toISOString(),
      timestamp: new Date().toISOString(),
    };

    const risk: RiskAssessment = {
      sizing: {
        shares: 100,
        dollarRisk: 300,
        portfolioRiskPercent: 1.5,
        kellyFraction: 0.12,
        adjustedKelly: 0.06,
        positionValue: 15000,
      },
      maxCorrelation: 0.2,
      correlatedPositions: [],
      dailyDrawdownUsed: 0.5,
      dailyDrawdownRemaining: 2.5,
      openPositionCount: 1,
      earningsWithin48h: false,
      riskScore: 0.3,
      warnings: [],
      timestamp: new Date().toISOString(),
    };

    const prompt = decisionPrompt(
      makeRegime(),
      mtf,
      setup,
      risk,
      makeSafetyConfig(),
    );
    expect(prompt.system).toContain('portfolio manager');
    expect(prompt.user).toContain('REGIME');
    expect(prompt.user).toContain('SETUP');
    expect(prompt.user).toContain('RISK');
  });

  it('handles null setup and risk', () => {
    const mtf: MultiTimeframeAnalysis = {
      biases: [],
      alignment: 0.3,
      dominantBias: 'neutral',
      conflictingTimeframes: ['1h'],
      timestamp: new Date().toISOString(),
    };

    const prompt = decisionPrompt(
      makeRegime(),
      mtf,
      null,
      null,
      makeSafetyConfig(),
    );
    expect(prompt.user).toContain('No valid setup identified');
    expect(prompt.user).toContain('Not assessed');
  });
});

describe('formatApprovalMessage', () => {
  it('formats BUY decision as WhatsApp message', () => {
    const decision: TradeDecision = {
      action: 'BUY',
      confidence: 0.78,
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
          { name: 'EMA alignment', weight: 0.3, description: '' },
          { name: 'Volume', weight: 0.25, description: '' },
          { name: 'Support', weight: 0.3, description: '' },
        ],
        confluenceScore: 0.85,
        pattern: 'bull flag',
        timeframe: '1h',
        validUntil: new Date().toISOString(),
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
        maxCorrelation: 0.2,
        correlatedPositions: [],
        dailyDrawdownUsed: 0.5,
        dailyDrawdownRemaining: 2.5,
        openPositionCount: 1,
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
    };

    const msg = formatApprovalMessage(
      decision,
      new Date(Date.now() + 600000).toISOString(),
    );
    expect(msg).toContain('*BUY AAPL*');
    expect(msg).toContain('Entry: $150.00');
    expect(msg).toContain('Stop: $147.00');
    expect(msg).toContain('R:R: 2.5');
    expect(msg).toContain('Qty: 100');
    expect(msg).toContain('Reply YES');
  });

  it('formats HOLD decision as simple message', () => {
    const decision: TradeDecision = {
      action: 'HOLD',
      confidence: 0,
      regime: makeRegime(),
      timeframeAnalysis: {
        biases: [],
        alignment: 0.3,
        dominantBias: 'neutral',
        conflictingTimeframes: [],
        timestamp: new Date().toISOString(),
      },
      setup: null,
      risk: null,
      order: null,
      reasoning: 'No valid setup found',
      holdReasons: ['No confluence'],
      timestamp: new Date().toISOString(),
    };

    const msg = formatApprovalMessage(decision, new Date().toISOString());
    expect(msg).toContain('HOLD');
    expect(msg).toContain('No valid setup found');
  });
});
