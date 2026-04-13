/**
 * Trading analysis pipeline orchestrator.
 *
 * Connects the 5-step analysis chain:
 * 1. Regime Detection
 * 2. Multi-Timeframe Bias
 * 3. Setup Identification
 * 4. Risk Assessment
 * 5. Decision
 *
 * Then validates against safety rails, prepares bracket order,
 * and manages the approval flow.
 *
 * This module is the skeleton — actual MCP calls to TradingView and IBKR
 * are injected via the adapter interfaces so the pipeline can be tested
 * independently.
 */

import { logger } from '../logger.js';

import { loadTradingConfig, type TradingConfig } from './config.js';
import {
  decisionPrompt,
  formatApprovalMessage,
  multiTimeframeBiasPrompt,
  regimeDetectionPrompt,
  riskAssessmentPrompt,
  setupIdentificationPrompt,
  type PromptPair,
} from './prompts.js';
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
  ApprovalStatus,
  BracketOrder,
  ChartData,
  IndicatorValues,
  MultiTimeframeAnalysis,
  PipelineContext,
  RegimeState,
  RiskAssessment,
  Timeframe,
  TradeDecision,
  TradeSetup,
} from './types.js';

// --- Adapter Interfaces ---
// These abstract over MCP tool calls so the pipeline is testable.

export interface ChartAdapter {
  /** Fetch OHLCV + indicator data for a symbol/timeframe from TradingView MCP. */
  getChartData(symbol: string, timeframe: Timeframe): Promise<ChartData>;
  /** Fetch current indicator values for a symbol/timeframe. */
  getIndicators(symbol: string, timeframe: Timeframe): Promise<IndicatorValues>;
  /** Fetch current VIX value. */
  getVix(): Promise<number>;
  /** Fetch current price for staleness check. */
  getCurrentPrice(symbol: string): Promise<number>;
}

export interface AnalysisAdapter {
  /** Send a prompt pair to Claude and get structured JSON response. */
  analyze<T>(prompt: PromptPair, model: string): Promise<T>;
}

export interface BrokerAdapter {
  /** Get current open positions. */
  getOpenPositions(): Promise<
    { symbol: string; value: number; direction: string }[]
  >;
  /** Get today's realized P&L as fraction of portfolio. */
  getDailyPnl(): Promise<number>;
  /** Submit a bracket order. Returns order ID. */
  submitBracketOrder(order: BracketOrder): Promise<string>;
}

export interface ApprovalAdapter {
  /** Send approval request via WhatsApp and wait for response. */
  requestApproval(message: string, ttlMs: number): Promise<ApprovalStatus>;
}

// --- Pipeline Result ---

export interface PipelineResult {
  context: PipelineContext;
  success: boolean;
  error?: string;
}

// --- Pipeline ---

export class TradingPipeline {
  private config: TradingConfig;
  private chart: ChartAdapter;
  private analysis: AnalysisAdapter;
  private broker: BrokerAdapter;
  private approval: ApprovalAdapter;

  constructor(
    chart: ChartAdapter,
    analysis: AnalysisAdapter,
    broker: BrokerAdapter,
    approval: ApprovalAdapter,
    config?: TradingConfig,
  ) {
    this.chart = chart;
    this.analysis = analysis;
    this.broker = broker;
    this.approval = approval;
    this.config = config ?? loadTradingConfig();
  }

  /** Run the full analysis pipeline for a symbol. */
  async run(symbol: string): Promise<PipelineResult> {
    const ctx: PipelineContext = {
      symbol,
      requestedAt: new Date().toISOString(),
      timeframes: this.config.analysisTimeframes as Timeframe[],
    };

    try {
      // Step 1: Regime Detection
      logger.info({ symbol }, 'Step 1: Regime detection');
      ctx.regime = await this.detectRegime(symbol);
      logger.info(
        { regime: ctx.regime.regime, confidence: ctx.regime.confidence },
        'Regime detected',
      );

      // Early exit: regime indeterminate
      if (ctx.regime.confidence < 0.5) {
        ctx.abortReason = `Regime indeterminate (confidence ${(ctx.regime.confidence * 100).toFixed(0)}%)`;
        logger.warn({ reason: ctx.abortReason }, 'Pipeline aborted');
        return this.buildHoldResult(ctx);
      }

      // Step 2: Multi-Timeframe Bias
      logger.info({ symbol }, 'Step 2: Multi-timeframe bias');
      ctx.timeframeAnalysis = await this.analyzeTimeframes(
        symbol,
        ctx.regime,
      );
      logger.info(
        {
          bias: ctx.timeframeAnalysis.dominantBias,
          alignment: ctx.timeframeAnalysis.alignment,
        },
        'MTF analysis complete',
      );

      // Early exit: no alignment
      if (ctx.timeframeAnalysis.alignment < 0.5) {
        ctx.abortReason = `MTF alignment too low (${(ctx.timeframeAnalysis.alignment * 100).toFixed(0)}%)`;
        logger.warn({ reason: ctx.abortReason }, 'Pipeline aborted');
        return this.buildHoldResult(ctx);
      }

      // Step 3: Setup Identification
      logger.info({ symbol }, 'Step 3: Setup identification');
      const executionTf = this.selectExecutionTimeframe();
      const maybeSetup = await this.identifySetup(
        symbol,
        executionTf,
        ctx.regime,
        ctx.timeframeAnalysis,
      );

      if (!maybeSetup) {
        ctx.abortReason = 'No valid setup identified';
        logger.info({ reason: ctx.abortReason }, 'No setup found');
        return this.buildHoldResult(ctx);
      }

      ctx.setup = maybeSetup;

      logger.info(
        {
          direction: ctx.setup.direction,
          entry: ctx.setup.entry,
          rr: ctx.setup.riskRewardRatio,
        },
        'Setup identified',
      );

      // Step 4: Risk Assessment
      logger.info({ symbol }, 'Step 4: Risk assessment');
      ctx.risk = await this.assessRisk(ctx.setup);
      logger.info(
        {
          riskScore: ctx.risk.riskScore,
          shares: ctx.risk.sizing.shares,
        },
        'Risk assessed',
      );

      // Step 5: Decision
      logger.info({ symbol }, 'Step 5: Final decision');
      ctx.decision = await this.makeDecision(
        ctx.regime,
        ctx.timeframeAnalysis,
        ctx.setup,
        ctx.risk,
      );
      logger.info(
        {
          action: ctx.decision.action,
          confidence: ctx.decision.confidence,
        },
        'Decision made',
      );

      if (ctx.decision.action === 'HOLD') {
        return this.buildHoldResult(ctx);
      }

      // Safety validation
      logger.info({ symbol }, 'Running safety checks');
      const daily = await this.getDailyState();
      const safetyResult = runSafetyChecks(
        ctx.decision,
        this.config.safety,
        daily,
      );

      if (!safetyResult.passed) {
        ctx.abortReason = `Safety violation: ${safetyResult.violations.join('; ')}`;
        logger.warn(
          { violations: safetyResult.violations },
          'Safety check failed',
        );
        return this.buildHoldResult(ctx);
      }

      if (safetyResult.warnings.length > 0) {
        logger.warn({ warnings: safetyResult.warnings }, 'Safety warnings');
      }

      // Validate bracket order
      if (ctx.decision.order) {
        const orderErrors = validateBracketOrder(ctx.decision.order);
        if (orderErrors.length > 0) {
          ctx.abortReason = `Invalid order: ${orderErrors.join('; ')}`;
          logger.error({ errors: orderErrors }, 'Order validation failed');
          return this.buildHoldResult(ctx);
        }
      }

      // Approval flow
      logger.info({ symbol }, 'Requesting approval');
      const expiresAt = createExpirationTime(this.config.safety.approvalTtlMs);
      const message = formatApprovalMessage(ctx.decision, expiresAt);

      ctx.approval = {
        id: crypto.randomUUID(),
        decision: ctx.decision,
        summary: message,
        expiresAt,
        status: 'pending',
        sentAt: new Date().toISOString(),
      };

      const approvalStatus = await this.approval.requestApproval(
        message,
        this.config.safety.approvalTtlMs,
      );
      ctx.approval.status = approvalStatus;
      ctx.approval.respondedAt = new Date().toISOString();

      if (approvalStatus !== 'approved') {
        ctx.abortReason = `Approval ${approvalStatus}`;
        logger.info({ status: approvalStatus }, 'Trade not approved');
        ctx.completedAt = new Date().toISOString();
        return { context: ctx, success: true };
      }

      // Staleness check before execution
      logger.info({ symbol }, 'Staleness check');
      const currentPrice = await this.chart.getCurrentPrice(symbol);
      const staleness = checkStaleness(
        ctx.decision.order!.entryPrice,
        currentPrice,
        this.config.safety.stalenessThreshold,
      );
      ctx.approval.stalenesCheck = staleness;

      if (staleness.isStale) {
        ctx.abortReason = `Price stale: drifted ${(staleness.priceDrift * 100).toFixed(2)}% since proposal`;
        ctx.approval.status = 'aborted';
        logger.warn(
          { drift: staleness.priceDrift, threshold: this.config.safety.stalenessThreshold },
          'Trade aborted due to staleness',
        );
        ctx.completedAt = new Date().toISOString();
        return { context: ctx, success: true };
      }

      // Execute
      if (this.config.safety.allowOrders && ctx.decision.order) {
        logger.info({ symbol, order: ctx.decision.order }, 'Submitting order');
        const orderId = await this.broker.submitBracketOrder(
          ctx.decision.order,
        );
        logger.info({ orderId }, 'Order submitted');
      } else {
        logger.info('Orders disabled — skipping execution');
      }

      ctx.completedAt = new Date().toISOString();
      return { context: ctx, success: true };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.error({ err, symbol }, 'Pipeline error');
      ctx.abortReason = `Pipeline error: ${msg}`;
      ctx.completedAt = new Date().toISOString();
      return { context: ctx, success: false, error: msg };
    }
  }

  // --- Step Implementations ---

  private async detectRegime(symbol: string): Promise<RegimeState> {
    const dailyTf: Timeframe = '1D';
    const [chartData, indicators, vix] = await Promise.all([
      this.chart.getChartData(symbol, dailyTf),
      this.chart.getIndicators(symbol, dailyTf),
      this.chart.getVix(),
    ]);

    const prompt = regimeDetectionPrompt(chartData, indicators, vix);
    const result = await this.analysis.analyze<
      RegimeState & { reasoning: string }
    >(prompt, this.config.analysisModel);

    return {
      regime: result.regime,
      confidence: result.confidence,
      adx: result.adx,
      vix: result.vix,
      vixPercentile: result.vixPercentile,
      hmmState: result.hmmState,
      transitionProb: result.transitionProb,
      timestamp: result.timestamp,
    };
  }

  private async analyzeTimeframes(
    symbol: string,
    regime: RegimeState,
  ): Promise<MultiTimeframeAnalysis> {
    const timeframes = this.config.analysisTimeframes as Timeframe[];

    // Fetch all timeframes in parallel
    const chartDataMap = new Map<Timeframe, ChartData>();
    const indicatorsMap = new Map<Timeframe, IndicatorValues>();

    const fetches = timeframes.map(async (tf) => {
      const [data, ind] = await Promise.all([
        this.chart.getChartData(symbol, tf),
        this.chart.getIndicators(symbol, tf),
      ]);
      chartDataMap.set(tf, data);
      indicatorsMap.set(tf, ind);
    });
    await Promise.all(fetches);

    const prompt = multiTimeframeBiasPrompt(
      chartDataMap,
      indicatorsMap,
      regime,
    );
    const result = await this.analysis.analyze<
      MultiTimeframeAnalysis & { reasoning: string }
    >(prompt, this.config.analysisModel);

    return {
      biases: result.biases,
      alignment: result.alignment,
      dominantBias: result.dominantBias,
      conflictingTimeframes: result.conflictingTimeframes,
      timestamp: result.timestamp,
    };
  }

  private async identifySetup(
    symbol: string,
    timeframe: Timeframe,
    regime: RegimeState,
    mtfAnalysis: MultiTimeframeAnalysis,
  ): Promise<TradeSetup | null> {
    const [chartData, indicators] = await Promise.all([
      this.chart.getChartData(symbol, timeframe),
      this.chart.getIndicators(symbol, timeframe),
    ]);

    const prompt = setupIdentificationPrompt(
      chartData,
      indicators,
      regime,
      mtfAnalysis,
    );
    const result = await this.analysis.analyze<{
      setup: (TradeSetup & { reasoning: string }) | null;
      reasoning: string;
    }>(prompt, this.config.analysisModel);

    if (!result.setup) {
      logger.info({ reasoning: result.reasoning }, 'No setup found');
      return null;
    }

    return {
      symbol: result.setup.symbol,
      direction: result.setup.direction,
      entry: result.setup.entry,
      stop: result.setup.stop,
      targets: result.setup.targets,
      riskRewardRatio: result.setup.riskRewardRatio,
      confluenceFactors: result.setup.confluenceFactors,
      confluenceScore: result.setup.confluenceScore,
      pattern: result.setup.pattern,
      timeframe: result.setup.timeframe as Timeframe,
      validUntil: result.setup.validUntil,
      timestamp: result.setup.timestamp,
    };
  }

  private async assessRisk(setup: TradeSetup): Promise<RiskAssessment> {
    const openPositions = await this.broker.getOpenPositions();

    const prompt = riskAssessmentPrompt(
      setup,
      this.config.portfolioValue,
      openPositions,
      this.config.safety,
    );
    const result = await this.analysis.analyze<
      RiskAssessment & { reasoning: string }
    >(prompt, this.config.analysisModel);

    return {
      sizing: result.sizing,
      maxCorrelation: result.maxCorrelation,
      correlatedPositions: result.correlatedPositions,
      dailyDrawdownUsed: result.dailyDrawdownUsed,
      dailyDrawdownRemaining: result.dailyDrawdownRemaining,
      openPositionCount: result.openPositionCount,
      earningsWithin48h: result.earningsWithin48h,
      earningsDate: result.earningsDate,
      riskScore: result.riskScore,
      warnings: result.warnings,
      timestamp: result.timestamp,
    };
  }

  private async makeDecision(
    regime: RegimeState,
    mtfAnalysis: MultiTimeframeAnalysis,
    setup: TradeSetup | null,
    risk: RiskAssessment | null,
  ): Promise<TradeDecision> {
    const prompt = decisionPrompt(
      regime,
      mtfAnalysis,
      setup,
      risk,
      this.config.safety,
    );
    const result = await this.analysis.analyze<{
      action: 'BUY' | 'SELL' | 'HOLD';
      confidence: number;
      order: BracketOrder | null;
      reasoning: string;
      holdReasons: string[];
    }>(prompt, this.config.decisionModel);

    return {
      action: result.action,
      confidence: result.confidence,
      regime,
      timeframeAnalysis: mtfAnalysis,
      setup,
      risk,
      order: result.order,
      reasoning: result.reasoning,
      holdReasons: result.holdReasons || [],
      timestamp: new Date().toISOString(),
    };
  }

  // --- Helpers ---

  private async getDailyState(): Promise<DailyState> {
    const [positions, dailyPnl] = await Promise.all([
      this.broker.getOpenPositions(),
      this.broker.getDailyPnl(),
    ]);

    return {
      realizedPnl: dailyPnl,
      openPositionCount: positions.length,
      openSymbols: positions.map((p) => p.symbol),
    };
  }

  private selectExecutionTimeframe(): Timeframe {
    // Use the lowest available timeframe for entry precision
    const priority: Timeframe[] = ['15m', '1h', '4h', '1D'];
    const available = new Set(this.config.analysisTimeframes);
    for (const tf of priority) {
      if (available.has(tf)) return tf;
    }
    return '1h'; // fallback
  }

  private buildHoldResult(ctx: PipelineContext): PipelineResult {
    if (!ctx.decision) {
      ctx.decision = {
        action: 'HOLD',
        confidence: 0,
        regime: ctx.regime ?? {
          regime: 'ranging',
          confidence: 0,
          adx: 0,
          vix: 0,
          vixPercentile: 0,
          hmmState: 1,
          transitionProb: 0,
          timestamp: new Date().toISOString(),
        },
        timeframeAnalysis: ctx.timeframeAnalysis ?? {
          biases: [],
          alignment: 0,
          dominantBias: 'neutral',
          conflictingTimeframes: [],
          timestamp: new Date().toISOString(),
        },
        setup: null,
        risk: null,
        order: null,
        reasoning: ctx.abortReason || 'No actionable setup',
        holdReasons: [ctx.abortReason || 'No actionable setup'],
        timestamp: new Date().toISOString(),
      };
    }
    ctx.completedAt = new Date().toISOString();
    return { context: ctx, success: true };
  }
}
