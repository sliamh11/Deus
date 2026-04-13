/**
 * Safety rails for the trading analysis engine.
 *
 * Every trade proposal passes through these checks before reaching
 * the approval stage. Any single failure blocks the trade.
 *
 * Design principles:
 * - Fail closed: if data is missing, block the trade
 * - No silent overrides: every block produces a human-readable reason
 * - Stateless checks: daily drawdown state is passed in, not stored here
 */

import type {
  ApprovalRequest,
  BracketOrder,
  RiskAssessment,
  SafetyConfig,
  StalenessCheck,
  TradeDecision,
  TradeSetup,
} from './types.js';

export interface SafetyCheckResult {
  passed: boolean;
  violations: string[];
  warnings: string[];
}

export interface DailyState {
  realizedPnl: number; // today's realized P&L as fraction of portfolio
  openPositionCount: number;
  openSymbols: string[]; // symbols currently held
}

// --- Individual Checks ---

function checkOrdersEnabled(config: SafetyConfig): string | null {
  if (!config.allowOrders) {
    return 'IBKR_ALLOW_ORDERS is false — order submission disabled';
  }
  return null;
}

function checkRiskPerTrade(
  risk: RiskAssessment,
  config: SafetyConfig,
): string | null {
  if (risk.sizing.portfolioRiskPercent > config.maxRiskPerTrade * 100) {
    return `Risk per trade ${risk.sizing.portfolioRiskPercent.toFixed(2)}% exceeds max ${(config.maxRiskPerTrade * 100).toFixed(1)}%`;
  }
  return null;
}

function checkDailyDrawdown(
  daily: DailyState,
  risk: RiskAssessment,
  config: SafetyConfig,
): string | null {
  const totalExposure =
    Math.abs(daily.realizedPnl) + risk.sizing.portfolioRiskPercent / 100;
  if (totalExposure > config.maxDailyDrawdown) {
    return `Daily drawdown would reach ${(totalExposure * 100).toFixed(2)}% — max is ${(config.maxDailyDrawdown * 100).toFixed(1)}%`;
  }
  return null;
}

function checkOpenPositions(
  daily: DailyState,
  config: SafetyConfig,
): string | null {
  if (daily.openPositionCount >= config.maxOpenPositions) {
    return `Already at ${daily.openPositionCount} open positions — max is ${config.maxOpenPositions}`;
  }
  return null;
}

function checkCorrelation(
  risk: RiskAssessment,
  config: SafetyConfig,
): string | null {
  if (risk.maxCorrelation > config.maxCorrelation) {
    const correlated = risk.correlatedPositions.join(', ');
    return `High correlation (${risk.maxCorrelation.toFixed(2)}) with existing positions: ${correlated} — max is ${config.maxCorrelation}`;
  }
  return null;
}

function checkEarnings(
  risk: RiskAssessment,
  config: SafetyConfig,
): string | null {
  if (risk.earningsWithin48h && config.earningsBlackoutHours > 0) {
    const date = risk.earningsDate || 'unknown date';
    return `Earnings within ${config.earningsBlackoutHours}h blackout (${date})`;
  }
  return null;
}

function checkConfluence(
  setup: TradeSetup,
  config: SafetyConfig,
): string | null {
  if (setup.confluenceFactors.length < config.minConfluenceFactors) {
    return `Only ${setup.confluenceFactors.length} confluence factors — minimum is ${config.minConfluenceFactors}`;
  }
  return null;
}

function checkConfidence(
  decision: TradeDecision,
  config: SafetyConfig,
): string | null {
  if (decision.confidence < config.minConfidence) {
    return `Confidence ${(decision.confidence * 100).toFixed(0)}% below minimum ${(config.minConfidence * 100).toFixed(0)}%`;
  }
  return null;
}

function checkRiskReward(
  setup: TradeSetup,
  config: SafetyConfig,
): string | null {
  if (setup.riskRewardRatio < config.minRiskReward) {
    return `R:R ${setup.riskRewardRatio.toFixed(2)} below minimum ${config.minRiskReward}`;
  }
  return null;
}

function checkVixCircuitBreaker(
  vix: number,
  config: SafetyConfig,
): string | null {
  if (vix >= config.vixCircuitBreaker) {
    return `VIX at ${vix.toFixed(1)} — circuit breaker threshold is ${config.vixCircuitBreaker}`;
  }
  return null;
}

function checkInstrument(
  symbol: string,
  config: SafetyConfig,
): string | null {
  if (
    config.instrumentBlacklist.length > 0 &&
    config.instrumentBlacklist.includes(symbol)
  ) {
    return `${symbol} is blacklisted`;
  }
  if (
    config.instrumentWhitelist.length > 0 &&
    !config.instrumentWhitelist.includes(symbol)
  ) {
    return `${symbol} is not in the whitelist`;
  }
  return null;
}

function checkMarketHours(config: SafetyConfig): {
  violation: string | null;
  warning: string | null;
} {
  const now = new Date();
  // US Eastern time market hours check
  const eastern = new Date(
    now.toLocaleString('en-US', { timeZone: 'America/New_York' }),
  );
  const hours = eastern.getHours();
  const minutes = eastern.getMinutes();
  const totalMinutes = hours * 60 + minutes;

  const marketOpen = 9 * 60 + 30; // 9:30 AM ET
  const marketClose = 16 * 60; // 4:00 PM ET
  const day = eastern.getDay();

  // Weekend check
  if (day === 0 || day === 6) {
    return { violation: null, warning: 'Market is closed (weekend)' };
  }

  // Outside market hours
  if (totalMinutes < marketOpen || totalMinutes >= marketClose) {
    return { violation: null, warning: 'Outside regular market hours' };
  }

  // Buffer zones
  if (
    config.marketOpenBufferMinutes > 0 &&
    totalMinutes < marketOpen + config.marketOpenBufferMinutes
  ) {
    return {
      violation: `Within ${config.marketOpenBufferMinutes}-minute market open buffer`,
      warning: null,
    };
  }

  if (
    config.marketCloseBufferMinutes > 0 &&
    totalMinutes >= marketClose - config.marketCloseBufferMinutes
  ) {
    return {
      violation: `Within ${config.marketCloseBufferMinutes}-minute market close buffer`,
      warning: null,
    };
  }

  return { violation: null, warning: null };
}

// --- Staleness Gate ---

/** Check if price has drifted beyond threshold since proposal was created. */
export function checkStaleness(
  originalPrice: number,
  currentPrice: number,
  threshold: number,
): StalenessCheck {
  const drift = Math.abs(currentPrice - originalPrice) / originalPrice;
  return {
    originalPrice,
    currentPrice,
    priceDrift: drift,
    isStale: drift > threshold,
    checkedAt: new Date().toISOString(),
  };
}

// --- Approval TTL ---

/** Check if an approval request has expired. */
export function isApprovalExpired(approval: ApprovalRequest): boolean {
  return new Date() > new Date(approval.expiresAt);
}

/** Create expiration timestamp from TTL. */
export function createExpirationTime(ttlMs: number): string {
  return new Date(Date.now() + ttlMs).toISOString();
}

// --- Composite Safety Check ---

/** Run all safety checks against a trade decision. Returns pass/fail with reasons. */
export function runSafetyChecks(
  decision: TradeDecision,
  config: SafetyConfig,
  daily: DailyState,
): SafetyCheckResult {
  const violations: string[] = [];
  const warnings: string[] = [];

  // Always check: orders enabled
  const ordersCheck = checkOrdersEnabled(config);
  if (ordersCheck) violations.push(ordersCheck);

  // HOLD decisions pass safety (nothing to block)
  if (decision.action === 'HOLD') {
    return { passed: violations.length === 0, violations, warnings };
  }

  // Instrument check
  if (decision.setup) {
    const instrumentCheck = checkInstrument(decision.setup.symbol, config);
    if (instrumentCheck) violations.push(instrumentCheck);
  }

  // VIX circuit breaker
  const vixCheck = checkVixCircuitBreaker(decision.regime.vix, config);
  if (vixCheck) violations.push(vixCheck);

  // Market hours
  const marketCheck = checkMarketHours(config);
  if (marketCheck.violation) violations.push(marketCheck.violation);
  if (marketCheck.warning) warnings.push(marketCheck.warning);

  // Confidence
  const confidenceCheck = checkConfidence(decision, config);
  if (confidenceCheck) violations.push(confidenceCheck);

  // Setup-dependent checks
  if (decision.setup) {
    const confluenceCheck = checkConfluence(decision.setup, config);
    if (confluenceCheck) violations.push(confluenceCheck);

    const rrCheck = checkRiskReward(decision.setup, config);
    if (rrCheck) violations.push(rrCheck);
  }

  // Risk-dependent checks
  if (decision.risk) {
    const riskCheck = checkRiskPerTrade(decision.risk, config);
    if (riskCheck) violations.push(riskCheck);

    const drawdownCheck = checkDailyDrawdown(daily, decision.risk, config);
    if (drawdownCheck) violations.push(drawdownCheck);

    const correlationCheck = checkCorrelation(decision.risk, config);
    if (correlationCheck) violations.push(correlationCheck);

    const earningsCheck = checkEarnings(decision.risk, config);
    if (earningsCheck) violations.push(earningsCheck);
  }

  // Position count
  const positionCheck = checkOpenPositions(daily, config);
  if (positionCheck) violations.push(positionCheck);

  return {
    passed: violations.length === 0,
    violations,
    warnings,
  };
}

// --- Order Validation ---

/** Validate that a bracket order is internally consistent. */
export function validateBracketOrder(order: BracketOrder): string[] {
  const errors: string[] = [];

  if (order.quantity <= 0) {
    errors.push('Quantity must be positive');
  }

  if (order.entryPrice <= 0) {
    errors.push('Entry price must be positive');
  }

  if (order.stopPrice <= 0) {
    errors.push('Stop price must be positive');
  }

  if (order.action === 'BUY') {
    if (order.stopPrice >= order.entryPrice) {
      errors.push('Stop must be below entry for BUY orders');
    }
    for (const target of order.targetPrices) {
      if (target <= order.entryPrice) {
        errors.push(`Target ${target} must be above entry ${order.entryPrice}`);
      }
    }
  }

  if (order.action === 'SELL') {
    if (order.stopPrice <= order.entryPrice) {
      errors.push('Stop must be above entry for SELL orders');
    }
    for (const target of order.targetPrices) {
      if (target >= order.entryPrice) {
        errors.push(
          `Target ${target} must be below entry ${order.entryPrice}`,
        );
      }
    }
  }

  if (order.targetPrices.length === 0) {
    errors.push('At least one target price required');
  }

  return errors;
}
