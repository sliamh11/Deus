import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalysisAdapter,
  ApprovalAdapter,
  BrokerAdapter,
  ChartAdapter,
} from './pipeline.js';
import { TradingPipeline } from './pipeline.js';
import type { TradingConfig } from './config.js';
import type {
  ApprovalStatus,
  BracketOrder,
  ChartData,
  IndicatorValues,
  MultiTimeframeAnalysis,
  RegimeState,
  RiskAssessment,
  Timeframe,
  TradeSetup,
} from './types.js';

// --- Mock Adapters ---

function makeChartData(tf: Timeframe): ChartData {
  return {
    symbol: 'AAPL',
    timeframe: tf,
    bars: Array.from({ length: 50 }, (_, i) => ({
      time: Date.now() - (50 - i) * 86400000,
      open: 148 + i * 0.1,
      high: 149 + i * 0.1,
      low: 147 + i * 0.1,
      close: 148.5 + i * 0.1,
      volume: 50000000,
    })),
    indicators: {},
    timestamp: new Date().toISOString(),
  };
}

function makeIndicators(): IndicatorValues {
  return {
    rsi: 55,
    adx: 28,
    atr: 2.5,
    ema20: 150,
    ema50: 148,
    ema200: 145,
    volume: 55000000,
    avgVolume: 50000000,
  };
}

function mockChartAdapter(): ChartAdapter {
  return {
    getChartData: vi.fn((_symbol: string, tf: Timeframe) =>
      Promise.resolve(makeChartData(tf)),
    ),
    getIndicators: vi.fn(() => Promise.resolve(makeIndicators())),
    getVix: vi.fn(() => Promise.resolve(18)),
    getCurrentPrice: vi.fn(() => Promise.resolve(150.2)),
  };
}

function mockAnalysisAdapter(responses: Record<string, unknown>): AnalysisAdapter {
  let callIndex = 0;
  const responseOrder = [
    'regime',
    'mtf',
    'setup',
    'risk',
    'decision',
  ];

  return {
    analyze: vi.fn(() => {
      const key = responseOrder[callIndex] || 'decision';
      callIndex++;
      return Promise.resolve(responses[key] as never);
    }),
  };
}

function mockBrokerAdapter(): BrokerAdapter {
  return {
    getOpenPositions: vi.fn(() =>
      Promise.resolve([
        { symbol: 'MSFT', value: 15000, direction: 'bullish' },
      ]),
    ),
    getDailyPnl: vi.fn(() => Promise.resolve(-0.005)),
    submitBracketOrder: vi.fn((_order: BracketOrder) =>
      Promise.resolve('ORDER-123'),
    ),
  };
}

function mockApprovalAdapter(status: ApprovalStatus = 'approved'): ApprovalAdapter {
  return {
    requestApproval: vi.fn(() => Promise.resolve(status)),
  };
}

function makeConfig(overrides: Partial<TradingConfig> = {}): TradingConfig {
  return {
    safety: {
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
    },
    ibkrPort: 4002,
    analysisTimeframes: ['15m', '1h', '4h', '1D'],
    portfolioValue: 100000,
    analysisModel: 'claude-sonnet-4-20250514',
    decisionModel: 'claude-opus-4-20250514',
    ...overrides,
  };
}

// --- Analysis Responses ---

const analysisResponses = {
  regime: {
    regime: 'trending',
    confidence: 0.85,
    adx: 30,
    vix: 18,
    vixPercentile: 40,
    hmmState: 0,
    transitionProb: 0.1,
    timestamp: new Date().toISOString(),
    reasoning: 'Strong uptrend with ADX above 25',
  } satisfies RegimeState & { reasoning: string },

  mtf: {
    biases: [
      {
        timeframe: '1D' as Timeframe,
        direction: 'bullish' as const,
        strength: 0.8,
        keyLevels: { support: [145, 148], resistance: [155, 160] },
        trendStructure: 'higher-highs' as const,
      },
    ],
    alignment: 0.85,
    dominantBias: 'bullish' as const,
    conflictingTimeframes: [] as Timeframe[],
    timestamp: new Date().toISOString(),
    reasoning: 'All timeframes aligned bullish',
  } satisfies MultiTimeframeAnalysis & { reasoning: string },

  setup: {
    setup: {
      symbol: 'AAPL',
      direction: 'bullish' as const,
      entry: 150,
      stop: 147,
      targets: [156, 162],
      riskRewardRatio: 2.5,
      confluenceFactors: [
        { name: 'EMA alignment', weight: 0.3, description: 'Price above EMAs' },
        { name: 'Volume spike', weight: 0.25, description: 'Above average' },
        { name: 'Support bounce', weight: 0.3, description: 'Key level' },
      ],
      confluenceScore: 0.85,
      pattern: 'bull flag',
      timeframe: '1h' as Timeframe,
      validUntil: new Date(Date.now() + 14400000).toISOString(),
      timestamp: new Date().toISOString(),
      reasoning: 'Bull flag at support with volume',
    } satisfies TradeSetup & { reasoning: string },
    reasoning: 'Valid setup found',
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
    openPositionCount: 1,
    earningsWithin48h: false,
    riskScore: 0.3,
    warnings: [],
    timestamp: new Date().toISOString(),
    reasoning: 'Acceptable risk profile',
  } satisfies RiskAssessment & { reasoning: string },

  decision: {
    action: 'BUY' as const,
    confidence: 0.78,
    order: {
      symbol: 'AAPL',
      action: 'BUY' as const,
      orderType: 'LIMIT' as const,
      quantity: 100,
      entryPrice: 150,
      stopPrice: 147,
      targetPrices: [156, 162],
      timeInForce: 'DAY' as const,
    },
    reasoning: 'Strong setup with favorable risk',
    holdReasons: [],
  },
};

// --- Tests ---

describe('TradingPipeline', () => {
  it('runs full pipeline to approval', async () => {
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(analysisResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter('approved');
    const config = makeConfig();

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      config,
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.decision?.action).toBe('BUY');
    expect(result.context.approval?.status).toBe('approved');
    expect(approval.requestApproval).toHaveBeenCalled();
  });

  it('aborts when regime confidence is low', async () => {
    const lowConfidenceResponses = {
      ...analysisResponses,
      regime: { ...analysisResponses.regime, confidence: 0.3 },
    };
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(lowConfidenceResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter();

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.decision?.action).toBe('HOLD');
    expect(result.context.abortReason).toContain('indeterminate');
    expect(approval.requestApproval).not.toHaveBeenCalled();
  });

  it('aborts when MTF alignment is low', async () => {
    const lowAlignmentResponses = {
      ...analysisResponses,
      mtf: { ...analysisResponses.mtf, alignment: 0.3 },
    };
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(lowAlignmentResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter();

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.decision?.action).toBe('HOLD');
    expect(result.context.abortReason).toContain('alignment');
  });

  it('aborts when no setup is found', async () => {
    const noSetupResponses = {
      ...analysisResponses,
      setup: { setup: null, reasoning: 'No confluence' },
    };
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(noSetupResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter();

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.decision?.action).toBe('HOLD');
    expect(result.context.abortReason).toContain('No valid setup');
  });

  it('respects approval rejection', async () => {
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(analysisResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter('rejected');

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.approval?.status).toBe('rejected');
    expect(broker.submitBracketOrder).not.toHaveBeenCalled();
  });

  it('aborts on stale price after approval', async () => {
    const chart = mockChartAdapter();
    // Return a price that drifted significantly
    (chart.getCurrentPrice as ReturnType<typeof vi.fn>).mockResolvedValue(160);

    const analysis = mockAnalysisAdapter(analysisResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter('approved');

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(true);
    expect(result.context.approval?.status).toBe('aborted');
    expect(result.context.approval?.stalenesCheck?.isStale).toBe(true);
    expect(broker.submitBracketOrder).not.toHaveBeenCalled();
  });

  it('handles pipeline errors gracefully', async () => {
    const chart = mockChartAdapter();
    (chart.getChartData as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('TradingView disconnected'),
    );

    const analysis = mockAnalysisAdapter(analysisResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter();

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      makeConfig(),
    );

    const result = await pipeline.run('AAPL');

    expect(result.success).toBe(false);
    expect(result.error).toContain('TradingView disconnected');
  });

  it('skips order submission when allowOrders is false', async () => {
    const chart = mockChartAdapter();
    const analysis = mockAnalysisAdapter(analysisResponses);
    const broker = mockBrokerAdapter();
    const approval = mockApprovalAdapter('approved');
    const config = makeConfig({
      safety: { ...makeConfig().safety, allowOrders: false },
    });

    const pipeline = new TradingPipeline(
      chart,
      analysis,
      broker,
      approval,
      config,
    );

    const result = await pipeline.run('AAPL');

    // Safety check blocks because allowOrders is false
    expect(result.context.abortReason).toContain('IBKR_ALLOW_ORDERS');
    expect(broker.submitBracketOrder).not.toHaveBeenCalled();
  });
});
