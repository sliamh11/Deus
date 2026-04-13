/**
 * Trading analysis engine — public API.
 *
 * Re-exports the pipeline, configuration, safety, and types
 * needed by consumers (channel handlers, CLI commands).
 */

export { loadTradingConfig, getSafetyConfig } from './config.js';
export type { TradingConfig } from './config.js';

export {
  TradingPipeline,
  type ChartAdapter,
  type AnalysisAdapter,
  type BrokerAdapter,
  type ApprovalAdapter,
  type PipelineResult,
} from './pipeline.js';

export {
  runSafetyChecks,
  validateBracketOrder,
  checkStaleness,
  isApprovalExpired,
  createExpirationTime,
  type SafetyCheckResult,
  type DailyState,
} from './safety.js';

export {
  regimeDetectionPrompt,
  multiTimeframeBiasPrompt,
  setupIdentificationPrompt,
  riskAssessmentPrompt,
  decisionPrompt,
  formatApprovalMessage,
  type PromptPair,
} from './prompts.js';

export type {
  MarketRegime,
  Direction,
  TradeAction,
  OrderType,
  TradingMode,
  Timeframe,
  ApprovalStatus,
  RegimeState,
  TimeframeBias,
  MultiTimeframeAnalysis,
  ConfluenceFactor,
  TradeSetup,
  PositionSizing,
  RiskAssessment,
  BracketOrder,
  TradeDecision,
  ApprovalRequest,
  StalenessCheck,
  SafetyConfig,
  PipelineContext,
  OhlcvBar,
  ChartData,
  IndicatorValues,
} from './types.js';
