/**
 * Trading analysis engine type definitions.
 *
 * All types follow the 5-step analysis chain:
 * Regime Detection -> Multi-Timeframe Bias -> Setup Identification ->
 * Risk Assessment -> Decision
 */

// --- Enums & Literals ---

export type MarketRegime = 'trending' | 'ranging' | 'volatile';
export type Direction = 'bullish' | 'bearish' | 'neutral';
export type TradeAction = 'BUY' | 'SELL' | 'HOLD';
export type OrderType = 'LIMIT' | 'MARKET' | 'STOP_LIMIT';
export type TradingMode = 'paper' | 'live';
export type Timeframe = '1m' | '5m' | '15m' | '1h' | '4h' | '1D' | '1W';
export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'aborted';

// --- Step 1: Regime Detection ---

export interface RegimeState {
  regime: MarketRegime;
  confidence: number; // 0-1
  adx: number;
  vix: number;
  vixPercentile: number; // 0-100, relative to 252-day lookback
  hmmState: number; // raw HMM hidden state index
  transitionProb: number; // probability of regime change in next period
  timestamp: string; // ISO 8601
}

// --- Step 2: Multi-Timeframe Bias ---

export interface TimeframeBias {
  timeframe: Timeframe;
  direction: Direction;
  strength: number; // 0-1
  keyLevels: {
    support: number[];
    resistance: number[];
  };
  trendStructure: 'higher-highs' | 'lower-lows' | 'consolidation';
}

export interface MultiTimeframeAnalysis {
  biases: TimeframeBias[];
  alignment: number; // 0-1, how well timeframes agree
  dominantBias: Direction;
  conflictingTimeframes: Timeframe[]; // which TFs disagree with dominant
  timestamp: string;
}

// --- Step 3: Setup Identification ---

export interface ConfluenceFactor {
  name: string; // e.g. "EMA crossover", "volume spike", "support bounce"
  weight: number; // 0-1
  description: string;
}

export interface TradeSetup {
  symbol: string;
  direction: Direction;
  entry: number;
  stop: number;
  targets: number[]; // multiple take-profit levels
  riskRewardRatio: number;
  confluenceFactors: ConfluenceFactor[];
  confluenceScore: number; // sum of weights, minimum 3 factors required
  pattern?: string; // e.g. "bull flag", "double bottom"
  timeframe: Timeframe; // execution timeframe
  validUntil: string; // ISO 8601, setup expiration
  timestamp: string;
}

// --- Step 4: Risk Assessment ---

export interface PositionSizing {
  shares: number;
  dollarRisk: number; // max $ at risk
  portfolioRiskPercent: number; // % of portfolio at risk
  kellyFraction: number; // raw Kelly
  adjustedKelly: number; // VIX-adjusted (half-Kelly baseline)
  positionValue: number; // total position $ value
}

export interface RiskAssessment {
  sizing: PositionSizing;
  maxCorrelation: number; // highest correlation with existing positions
  correlatedPositions: string[]; // symbols of correlated positions
  dailyDrawdownUsed: number; // % of daily drawdown budget consumed
  dailyDrawdownRemaining: number; // % remaining
  openPositionCount: number;
  earningsWithin48h: boolean;
  earningsDate?: string;
  riskScore: number; // 0-1, aggregate risk (lower = safer)
  warnings: string[]; // human-readable risk warnings
  timestamp: string;
}

// --- Step 5: Decision ---

export interface BracketOrder {
  symbol: string;
  action: TradeAction;
  orderType: OrderType;
  quantity: number;
  entryPrice: number;
  stopPrice: number;
  targetPrices: number[];
  timeInForce: 'DAY' | 'GTC' | 'GTD';
  gtdDate?: string; // ISO 8601, for GTD orders
}

export interface TradeDecision {
  action: TradeAction;
  confidence: number; // 0-1
  regime: RegimeState;
  timeframeAnalysis: MultiTimeframeAnalysis;
  setup: TradeSetup | null; // null when action is HOLD
  risk: RiskAssessment | null;
  order: BracketOrder | null;
  reasoning: string; // concise explanation of the decision
  holdReasons: string[]; // reasons for HOLD (empty if BUY/SELL)
  timestamp: string;
}

// --- Approval Flow ---

export interface ApprovalRequest {
  id: string; // unique request ID
  decision: TradeDecision;
  summary: string; // human-readable WhatsApp message
  expiresAt: string; // ISO 8601, 10min TTL
  status: ApprovalStatus;
  sentAt: string;
  respondedAt?: string;
  stalenesCheck?: StalenessCheck;
}

export interface StalenessCheck {
  originalPrice: number;
  currentPrice: number;
  priceDrift: number; // absolute % change
  isStale: boolean; // true if drift > threshold
  checkedAt: string;
}

// --- Safety Configuration ---

export interface SafetyConfig {
  allowOrders: boolean; // IBKR_ALLOW_ORDERS, default false
  mode: TradingMode; // paper (port 4002) or live (port 5000)
  maxRiskPerTrade: number; // fraction, default 0.02 (2%)
  maxDailyDrawdown: number; // fraction, default 0.03 (3%)
  maxOpenPositions: number; // default 5
  maxCorrelation: number; // default 0.7
  earningsBlackoutHours: number; // default 48
  stalenessThreshold: number; // fraction, default 0.005 (0.5%)
  approvalTtlMs: number; // default 600000 (10 min)
  minConfluenceFactors: number; // default 3
  minConfidence: number; // fraction, default 0.6
  minRiskReward: number; // default 2.0
  marketOpenBufferMinutes: number; // no entries first N min, default 15
  marketCloseBufferMinutes: number; // no entries last N min, default 15
  vixCircuitBreaker: number; // VIX level above which all entries blocked, default 35
  instrumentWhitelist: string[]; // empty = all allowed
  instrumentBlacklist: string[];
}

// --- Pipeline State ---

export interface PipelineContext {
  symbol: string;
  requestedAt: string;
  timeframes: Timeframe[];
  regime?: RegimeState;
  timeframeAnalysis?: MultiTimeframeAnalysis;
  setup?: TradeSetup;
  risk?: RiskAssessment;
  decision?: TradeDecision;
  approval?: ApprovalRequest;
  abortReason?: string;
  completedAt?: string;
}

// --- Chart Data (from TradingView MCP) ---

export interface OhlcvBar {
  time: number; // unix timestamp
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ChartData {
  symbol: string;
  timeframe: Timeframe;
  bars: OhlcvBar[];
  indicators: Record<string, number>; // name -> current value
  timestamp: string;
}

export interface IndicatorValues {
  rsi?: number;
  macd?: { value: number; signal: number; histogram: number };
  adx?: number;
  atr?: number;
  ema20?: number;
  ema50?: number;
  ema200?: number;
  vwap?: number;
  bollingerBands?: { upper: number; middle: number; lower: number };
  volume?: number;
  avgVolume?: number;
}
