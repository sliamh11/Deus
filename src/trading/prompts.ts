/**
 * Expert analysis prompts for the 5-step trading chain.
 *
 * Each prompt is a template function that accepts structured input from
 * the previous step and returns a system+user message pair. Prompts are
 * designed for token efficiency — no fluff, compact directives, structured
 * JSON output schemas inline.
 *
 * Prompt diversity (per Lopez-Lira arXiv:2504.10789): each step uses a
 * distinct analytical persona to avoid correlated failure modes.
 */

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

export interface PromptPair {
  system: string;
  user: string;
}

// --- Step 1: Regime Detection ---

export function regimeDetectionPrompt(
  chartData: ChartData,
  indicators: IndicatorValues,
  vix: number,
): PromptPair {
  return {
    system: `You are a quantitative regime analyst. Your sole task: classify the current market regime from price data and volatility indicators. You think in statistical terms — HMM states, volatility clustering, trend persistence.

Output ONLY valid JSON matching this schema:
{
  "regime": "trending" | "ranging" | "volatile",
  "confidence": 0-1,
  "adx": number,
  "vix": number,
  "vixPercentile": 0-100,
  "hmmState": 0 | 1 | 2,
  "transitionProb": 0-1,
  "timestamp": "ISO8601",
  "reasoning": "1-2 sentences"
}

Classification rules:
- trending: ADX > 25 AND price making directional higher-highs or lower-lows over 20+ bars
- ranging: ADX < 20 AND price oscillating between identifiable S/R levels
- volatile: VIX > 75th percentile of 252-day range OR ATR expanding >1.5x 20-period average OR sudden regime transition detected
- When indicators conflict, weight ADX 40%, price structure 35%, VIX context 25%
- hmmState mapping: 0=low-vol trending, 1=mean-reverting range, 2=high-vol crisis
- transitionProb: estimate probability the regime changes within 5 sessions based on ADX slope and VIX trajectory

Confidence scoring:
- 0.9+: all indicators agree, clear regime
- 0.7-0.9: majority agree, minor conflicts
- 0.5-0.7: mixed signals, regime possibly transitioning
- <0.5: regime indeterminate, recommend HOLD`,

    user: `Analyze regime for ${chartData.symbol} on ${chartData.timeframe}:

Bars (last 50): ${JSON.stringify(chartData.bars.slice(-50).map((b) => ({ t: b.time, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume })))}

Indicators:
- ADX: ${indicators.adx ?? 'N/A'}
- ATR: ${indicators.atr ?? 'N/A'}
- RSI: ${indicators.rsi ?? 'N/A'}
- EMA20: ${indicators.ema20 ?? 'N/A'}
- EMA50: ${indicators.ema50 ?? 'N/A'}
- EMA200: ${indicators.ema200 ?? 'N/A'}
- Bollinger: ${indicators.bollingerBands ? JSON.stringify(indicators.bollingerBands) : 'N/A'}
- Volume: ${indicators.volume ?? 'N/A'} (avg: ${indicators.avgVolume ?? 'N/A'})

VIX: ${vix}
Timestamp: ${chartData.timestamp}`,
  };
}

// --- Step 2: Multi-Timeframe Bias ---

export function multiTimeframeBiasPrompt(
  chartDataByTimeframe: Map<Timeframe, ChartData>,
  indicatorsByTimeframe: Map<Timeframe, IndicatorValues>,
  regime: RegimeState,
): PromptPair {
  const timeframeEntries: string[] = [];
  for (const [tf, data] of chartDataByTimeframe) {
    const ind = indicatorsByTimeframe.get(tf);
    timeframeEntries.push(
      `### ${tf}
Last 20 bars: ${JSON.stringify(data.bars.slice(-20).map((b) => ({ t: b.time, o: b.open, h: b.high, l: b.low, c: b.close })))}
EMA20: ${ind?.ema20 ?? 'N/A'}, EMA50: ${ind?.ema50 ?? 'N/A'}, EMA200: ${ind?.ema200 ?? 'N/A'}
RSI: ${ind?.rsi ?? 'N/A'}, VWAP: ${ind?.vwap ?? 'N/A'}`,
    );
  }

  return {
    system: `You are a multi-timeframe technical analyst. You read price structure across timeframes top-down (daily first, then intraday) to establish directional bias and identify alignment or conflict.

Output ONLY valid JSON matching this schema:
{
  "biases": [
    {
      "timeframe": string,
      "direction": "bullish" | "bearish" | "neutral",
      "strength": 0-1,
      "keyLevels": { "support": [numbers], "resistance": [numbers] },
      "trendStructure": "higher-highs" | "lower-lows" | "consolidation"
    }
  ],
  "alignment": 0-1,
  "dominantBias": "bullish" | "bearish" | "neutral",
  "conflictingTimeframes": [timeframes that disagree],
  "timestamp": "ISO8601",
  "reasoning": "2-3 sentences"
}

Rules:
- Process timeframes top-down: daily sets the primary bias, 4H confirms, 1H/15m refine entry timing
- direction: based on price vs EMAs, swing structure, and momentum (RSI)
- strength: 0.8+ = strong trend, 0.5-0.8 = moderate, <0.5 = weak/transitioning
- keyLevels: identify 2-3 most significant S/R per timeframe from swing points and EMAs
- alignment: fraction of timeframes agreeing with dominantBias (1.0 = perfect alignment)
- If alignment < 0.5, dominantBias should be "neutral" regardless of individual TF readings

Current regime: ${regime.regime} (confidence: ${regime.confidence})
Regime context matters: in volatile regime, weight higher timeframes more heavily.`,

    user: `Multi-timeframe analysis:

${timeframeEntries.join('\n\n')}`,
  };
}

// --- Step 3: Setup Identification ---

export function setupIdentificationPrompt(
  chartData: ChartData,
  indicators: IndicatorValues,
  regime: RegimeState,
  mtfAnalysis: MultiTimeframeAnalysis,
): PromptPair {
  return {
    system: `You are a pattern recognition specialist and trade setup architect. You identify high-probability setups ONLY when sufficient confluence exists. You are conservative — no setup is better than a bad setup.

Output ONLY valid JSON matching this schema (or null if no valid setup):
{
  "symbol": string,
  "direction": "bullish" | "bearish",
  "entry": number,
  "stop": number,
  "targets": [number, number],
  "riskRewardRatio": number,
  "confluenceFactors": [
    { "name": string, "weight": 0-1, "description": string }
  ],
  "confluenceScore": number,
  "pattern": string | null,
  "timeframe": string,
  "validUntil": "ISO8601",
  "timestamp": "ISO8601",
  "reasoning": "2-3 sentences"
}

Return {"setup": null, "reasoning": "..."} if no valid setup exists.

Confluence factors (must identify at least 3):
- Key level interaction (support/resistance bounce/break)
- EMA alignment (price vs 20/50/200)
- Volume confirmation (above average on setup bar)
- RSI divergence or extreme
- VWAP interaction
- Pattern completion (flag, wedge, double top/bottom, etc.)
- Multi-timeframe alignment (from prior step)
- Momentum shift (MACD crossover, RSI inflection)

Entry rules:
- Entry at limit price, not market — calculate optimal entry from structure
- Stop placement: beyond the invalidation level (swing point + ATR buffer)
- Primary target: nearest significant resistance/support
- Secondary target: 2x the risk distance or next major level
- R:R must be >= 2.0 or setup is invalid
- validUntil: setup expires in 4 hours for intraday, 2 days for daily TF

Regime-specific behavior:
- trending: favor pullback entries in trend direction, tighter stops
- ranging: favor mean-reversion at range extremes, wider stops
- volatile: ONLY take setups with R:R >= 3.0 and 4+ confluence factors`,

    user: `Identify setup for ${chartData.symbol} on ${chartData.timeframe}:

Regime: ${regime.regime} (confidence: ${regime.confidence})
MTF dominant bias: ${mtfAnalysis.dominantBias} (alignment: ${mtfAnalysis.alignment})
Conflicting TFs: ${mtfAnalysis.conflictingTimeframes.join(', ') || 'none'}

Key levels from MTF analysis:
${mtfAnalysis.biases
  .map(
    (b) =>
      `${b.timeframe}: S=${b.keyLevels.support.join(', ')} R=${b.keyLevels.resistance.join(', ')}`,
  )
  .join('\n')}

Recent bars (last 30): ${JSON.stringify(chartData.bars.slice(-30).map((b) => ({ t: b.time, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume })))}

Indicators:
- RSI: ${indicators.rsi ?? 'N/A'}
- MACD: ${indicators.macd ? JSON.stringify(indicators.macd) : 'N/A'}
- ADX: ${indicators.adx ?? 'N/A'}
- ATR: ${indicators.atr ?? 'N/A'}
- EMA20: ${indicators.ema20 ?? 'N/A'}, EMA50: ${indicators.ema50 ?? 'N/A'}, EMA200: ${indicators.ema200 ?? 'N/A'}
- VWAP: ${indicators.vwap ?? 'N/A'}
- Bollinger: ${indicators.bollingerBands ? JSON.stringify(indicators.bollingerBands) : 'N/A'}
- Volume: ${indicators.volume ?? 'N/A'} (avg: ${indicators.avgVolume ?? 'N/A'})`,
  };
}

// --- Step 4: Risk Assessment ---

export function riskAssessmentPrompt(
  setup: TradeSetup,
  portfolioValue: number,
  openPositions: { symbol: string; value: number; direction: string }[],
  safetyConfig: SafetyConfig,
): PromptPair {
  const riskPerShare = Math.abs(setup.entry - setup.stop);
  const riskPercent = riskPerShare / setup.entry;

  return {
    system: `You are a risk manager and position sizing specialist. Your job: calculate exact position size using hybrid Kelly-VIX and check portfolio-level risk. You are the last line of defense — conservative by nature.

Output ONLY valid JSON matching this schema:
{
  "sizing": {
    "shares": integer,
    "dollarRisk": number,
    "portfolioRiskPercent": number,
    "kellyFraction": 0-1,
    "adjustedKelly": 0-1,
    "positionValue": number
  },
  "maxCorrelation": 0-1,
  "correlatedPositions": [string],
  "dailyDrawdownUsed": number,
  "dailyDrawdownRemaining": number,
  "openPositionCount": integer,
  "earningsWithin48h": boolean,
  "earningsDate": string | null,
  "riskScore": 0-1,
  "warnings": [string],
  "timestamp": "ISO8601",
  "reasoning": "2-3 sentences"
}

Position sizing algorithm (hybrid Kelly-VIX):
1. Estimate win rate from confluence score: winRate = min(0.65, 0.4 + setup.confluenceScore * 0.05)
2. Calculate Kelly fraction: f = (winRate * R:R - (1 - winRate)) / R:R
3. Apply half-Kelly: adjustedKelly = f * 0.5
4. VIX adjustment: if VIX > 20, scale down by (20 / VIX); if VIX < 15, allow up to 1.2x
5. Cap at maxRiskPerTrade: final risk = min(adjustedKelly, ${safetyConfig.maxRiskPerTrade})
6. Shares = floor(portfolioValue * finalRisk / riskPerShare)

Correlation check:
- Estimate sector correlation between new trade and each open position
- Same sector = 0.7+, adjacent sectors = 0.4-0.7, uncorrelated < 0.4
- Flag if max correlation > ${safetyConfig.maxCorrelation}

Risk score (aggregate):
- 0.0-0.3: low risk, high confidence setup
- 0.3-0.6: moderate risk, acceptable
- 0.6-0.8: elevated risk, proceed with caution
- 0.8-1.0: high risk, recommend HOLD`,

    user: `Assess risk for proposed trade:

Setup: ${setup.direction} ${setup.symbol}
Entry: ${setup.entry}, Stop: ${setup.stop}, Targets: ${setup.targets.join(', ')}
R:R: ${setup.riskRewardRatio}
Risk per share: $${riskPerShare.toFixed(2)} (${(riskPercent * 100).toFixed(2)}%)
Confluence: ${setup.confluenceFactors.length} factors (score: ${setup.confluenceScore})

Portfolio value: $${portfolioValue.toFixed(2)}
Max risk per trade: ${(safetyConfig.maxRiskPerTrade * 100).toFixed(1)}%
Max daily drawdown: ${(safetyConfig.maxDailyDrawdown * 100).toFixed(1)}%

Open positions (${openPositions.length}):
${openPositions.length > 0 ? openPositions.map((p) => `- ${p.symbol}: $${p.value.toFixed(2)} (${p.direction})`).join('\n') : 'None'}

Max open positions: ${safetyConfig.maxOpenPositions}`,
  };
}

// --- Step 5: Decision ---

export function decisionPrompt(
  regime: RegimeState,
  mtfAnalysis: MultiTimeframeAnalysis,
  setup: TradeSetup | null,
  risk: RiskAssessment | null,
  safetyConfig: SafetyConfig,
): PromptPair {
  return {
    system: `You are the senior portfolio manager making the final trade decision. You synthesize all prior analysis — regime, multi-timeframe bias, setup quality, and risk assessment — into a single actionable decision. You are accountable for outcomes.

Output ONLY valid JSON matching this schema:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0-1,
  "order": {
    "symbol": string,
    "action": "BUY" | "SELL",
    "orderType": "LIMIT" | "STOP_LIMIT",
    "quantity": integer,
    "entryPrice": number,
    "stopPrice": number,
    "targetPrices": [number],
    "timeInForce": "DAY" | "GTC" | "GTD",
    "gtdDate": string | null
  } | null,
  "reasoning": "2-4 sentences",
  "holdReasons": [string]
}

Decision framework:
1. If no valid setup exists → HOLD
2. If risk score > 0.8 → HOLD
3. If regime confidence < 0.5 → HOLD (regime indeterminate)
4. If MTF alignment < 0.5 → HOLD (conflicting timeframes)
5. If setup direction conflicts with MTF dominant bias → HOLD
6. Otherwise → BUY or SELL per setup direction

Order construction (bracket order):
- orderType: LIMIT for entries at support/resistance, STOP_LIMIT for breakout entries
- quantity: from risk assessment sizing
- stopPrice: from setup stop level
- targetPrices: from setup targets
- timeInForce: DAY for intraday setups, GTC for swing (2-5 day), GTD for position trades
- gtdDate: setup.validUntil for GTD orders

Confidence calibration:
- Start at regime.confidence
- Multiply by MTF alignment
- Multiply by (confluenceScore / maxPossibleScore)
- Adjust for risk score: multiply by (1 - riskScore * 0.3)
- Final confidence must be explicit and honest — overconfidence causes trust collapse

HOLD is always valid. When in doubt, HOLD. Document all hold reasons explicitly.`,

    user: `Make final decision:

REGIME: ${regime.regime} (confidence: ${regime.confidence}, VIX: ${regime.vix})
MTF: dominant=${mtfAnalysis.dominantBias}, alignment=${mtfAnalysis.alignment}, conflicts=${mtfAnalysis.conflictingTimeframes.join(', ') || 'none'}

${
  setup
    ? `SETUP: ${setup.direction} ${setup.symbol}
Entry: ${setup.entry}, Stop: ${setup.stop}, Targets: ${setup.targets.join(', ')}
R:R: ${setup.riskRewardRatio}, Confluence: ${setup.confluenceFactors.length} factors (score: ${setup.confluenceScore})
Pattern: ${setup.pattern || 'none'}`
    : 'SETUP: No valid setup identified'
}

${
  risk
    ? `RISK: Score ${risk.riskScore}, Shares: ${risk.sizing.shares}, Position: $${risk.sizing.positionValue.toFixed(2)}
Portfolio risk: ${risk.sizing.portfolioRiskPercent.toFixed(2)}%, Daily DD remaining: ${risk.dailyDrawdownRemaining.toFixed(2)}%
Correlation: ${risk.maxCorrelation.toFixed(2)} (${risk.correlatedPositions.join(', ') || 'none'})
Earnings: ${risk.earningsWithin48h ? 'WITHIN BLACKOUT' : 'clear'}
Warnings: ${risk.warnings.join('; ') || 'none'}`
    : 'RISK: Not assessed (no setup)'
}

Safety limits: maxRisk=${(safetyConfig.maxRiskPerTrade * 100).toFixed(1)}%, maxDD=${(safetyConfig.maxDailyDrawdown * 100).toFixed(1)}%, minRR=${safetyConfig.minRiskReward}, minConfidence=${(safetyConfig.minConfidence * 100).toFixed(0)}%`,
  };
}

// --- Approval Message Formatter ---

/** Format a trade decision into a human-readable WhatsApp approval message. */
export function formatApprovalMessage(
  decision: TradeDecision,
  expiresAt: string,
): string {
  if (decision.action === 'HOLD' || !decision.setup || !decision.order) {
    return `HOLD: ${decision.reasoning}`;
  }

  const setup = decision.setup;
  const order = decision.order;
  const risk = decision.risk!;
  const riskPerShare = Math.abs(order.entryPrice - order.stopPrice);
  const totalRisk = riskPerShare * order.quantity;

  const lines = [
    `*${order.action} ${order.symbol}*`,
    ``,
    `Entry: $${order.entryPrice.toFixed(2)} (${order.orderType})`,
    `Stop: $${order.stopPrice.toFixed(2)}`,
    `Targets: ${order.targetPrices.map((t) => `$${t.toFixed(2)}`).join(' / ')}`,
    `R:R: ${setup.riskRewardRatio.toFixed(1)}`,
    ``,
    `Qty: ${order.quantity} shares ($${(order.entryPrice * order.quantity).toFixed(0)})`,
    `Risk: $${totalRisk.toFixed(0)} (${risk.sizing.portfolioRiskPercent.toFixed(1)}% of portfolio)`,
    ``,
    `Regime: ${decision.regime.regime} (${(decision.regime.confidence * 100).toFixed(0)}%)`,
    `Confidence: ${(decision.confidence * 100).toFixed(0)}%`,
    `Confluence: ${setup.confluenceFactors.map((f) => f.name).join(', ')}`,
    ``,
    `${decision.reasoning}`,
    ``,
    `Reply YES to execute, NO to cancel.`,
    `Expires: ${new Date(expiresAt).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit' })} ET`,
  ];

  return lines.join('\n');
}
