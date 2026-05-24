<!-- sha: 671be0813be191aa7c977b8e70aa34cdac657dfd -->

# Deus Codebase Map

## Architecture

Deus is a multi-channel AI assistant (WhatsApp, Telegram) built on TypeScript with Python tooling. Key layers:

- **Channels** (`src/channels/`): MCP adapters per platform + shared registry
- **Agent runtimes** (`src/agent-runtimes/`): Claude, OpenAI, llama-cpp backends; unified registry + resolve
- **Message pipeline** (`src/message-orchestrator.ts`, `src/pipeline.*.ts`, `src/router.ts`): inbound routing → agent dispatch → outbound
- **Memory** (`scripts/memory_*.py`): atom storage, tree retrieval, indexer, GC
- **Linear integration** (`src/linear-*.ts`): webhook, dispatcher, gate specs, warden-driven issue flow
- **Warden gates** (`.claude/agents/wardens/`): readiness, enrichment, code-review, plan-review, threat-model
- **Evolution/eval** (`evolution/`): judge models, benchmarks, reflexion loop
- **Config & DB** (`src/config.ts`, `src/db.ts`): env-driven config, SQLite storage

## File Tree

### src/
```
agent-runtimes/
  claude-backend.ts
  container-backend.ts
  index.ts
  llama-cpp-backend.ts
  openai-backend.ts
  registry.ts
  resolve.ts
  types.ts
async/
  index.ts
auth-providers/
  anthropic.ts
  index.ts
  openai.ts
  types.ts
cache/
  cache-query.ts
  gcal-sync.ts
channels/
  index.ts
  mcp-adapter.ts
  mcp-discord.ts
  mcp-gmail.ts
  mcp-slack.ts
  mcp-telegram.ts
  mcp-whatsapp.ts
  registry.ts
errors/
  index.ts
guardrails/
  index.ts
  injection-scanner.ts
multi-agent/
  index.ts
  orchestrator.ts
  prompt-templates.ts
  types.ts
private/
  orchestrator/
  scripts/
  trading/
skills/
  index.ts
  registry.ts
solutions/
  cli.ts
  index.ts
  store.ts
tool-broker/
  types.ts
auth-refresh.ts
auto-compress.ts
bootstrap.ts
checks.ts
config.ts
container-mounter.ts
container-runner.ts
container-runtime.ts
credential-proxy.ts
db.ts
deus-listen.ts
doc-gardener-seed.ts
domain-presets.ts
env.ts
evolution-client.ts
group-folder.ts
group-queue.ts
group-tokens.ts
image.ts
index.ts
ipc.ts
linear-actions.ts
linear-auto-merge.ts
linear-dispatcher.ts
linear-gate-specs.ts
linear-notifications.ts
linear-pipeline-cli.ts
linear-vault-sync.ts
linear-webhook.ts
logger.ts
message-orchestrator.ts
mount-security.ts
platform.ts
pr-url-extractor.ts
project-registry.ts
reaction-signal.ts
remote-control.ts
router-state.ts
router.ts
sender-allowlist.ts
session-commands.ts
startup-gate.ts
task-scheduler.ts
timezone.ts
token-counter.ts
tool-proxy.ts
tool-registry.ts
transcription.ts
types.ts
user-signal.ts
x-integration.ts
```

### scripts/
```
token_bench/
  facts/
  results/
  aggregate_compression.py
  ci_coverage_gate.sh
  diff.py
  effort_probe.sh
  fixtures.json
  harness.py
  keyword_bench.py
  preservation_bench.py
  real_claude_probe.sh
_agent_io.py
_exit_codes.py
_time.py
analyze_token_efficiency.py
check-ascii.sh
claude-context-reindex.mjs
codebase_map.py
codex_warden_hooks.py
companion_to_braille.py
compression_benchmark.py
deus-git-push.sh
drift_check.py
embedding_shootout.py
gcal.mjs
gemini_ocr.py
import_seeds.py
linear_vault_sync.py
log_review.py
maintenance.py
memory_benchmark.py
memory_gc.py
memory_indexer.py
memory_mcp_server.py
memory_query.py
memory_retrieval_hook.py
memory_tree.py
memory_tree_hook.py
migrate.mjs
migrate_atom_tiers.py
mine_implicit_feedback.py
redact_session.py
rename-repo.sh
review_benchmark.py
session_concepts.py
settings_merge.py
setup-gcal-auth.mjs
setup-parry-guard.sh
standards_pack.py
stop_hook.py
sync_agent_skills.py
sync_linear_pending.py
trec_atom_benchmark.py
vault_context_hook.py
wardens.py
whatsapp-auth.ts
youtube_transcript_server.py
```

## Key Exports

_Format: `path/to/file` → exported symbols_

- `scripts/_agent_io.py` → `is_agent_context`, `compact_json`, `select_fields`, `agent_output`
- `scripts/_time.py` → `utc_now`, `local_now`
- `scripts/analyze_token_efficiency.py` → `UsageEntry`, `ToolSizeEntry`, `InteractionRow`, `parse_iso`, `in_window`, ...
- `scripts/codebase_map.py` → `generate_map`, `main`
- `scripts/codex_warden_hooks.py` → `HookSpec`, `approve_admin_merge`, `regenerate_codebase_map`, `run_session_init`, `run_plan_mode_invalidator`, ...
- `scripts/companion_to_braille.py` → `rgb_distance`, `classify_pixel`, `image_to_braille`, `image_to_halfblock`, `strip_ansi`, ...
- `scripts/compression_benchmark.py` → `llm_call`, `parse_json`, `extract_and_classify_facts`, `verify_facts`, `compute_weighted_score`, ...
- `scripts/drift_check.py` → `parse_governs`, `discover_patterns`, `main`, `extract_body_paths`, `check_paths`, ...
- `scripts/embedding_shootout.py` → `ollama_embed`, `l2_dist`, `cosine_sim`, `load_benchmark`, `evaluate_model`, ...
- `scripts/gemini_ocr.py` → `main`
- `scripts/import_seeds.py` → `main`
- `scripts/linear_vault_sync.py` → `main`
- `scripts/log_review.py` → `parse_pino_log`, `parse_container_log`, `rotate_container_logs`, `rotate_main_logs`, `run_review`, ...
- `scripts/maintenance.py` → `run_task`, `main`
- `scripts/memory_benchmark.py` → `recall_at_k`, `mean_reciprocal_rank`, `run_outbound`, `print_outbound_results`, `run_internal`, ...
- `scripts/memory_gc.py` → `find_memory_dirs`, `parse_frontmatter`, `set_frontmatter_field`, `archive_file`, `run_gc`, ...
- `scripts/memory_indexer.py` → `load_api_key`, `embed`, `embed_batch`, `serialize`, `deserialize`, ...
- `scripts/memory_mcp_server.py` → `memory_recall`
- `scripts/memory_query.py` → `recall`, `main`
- `scripts/memory_retrieval_hook.py` → `main`
- `scripts/memory_tree.py` → `is_external_namespace`, `make_id`, `content_hash`, `serialize`, `deserialize`, ...
- `scripts/memory_tree_hook.py` → `dispatch`, `main`
- `scripts/migrate.mjs` → `run`, `pendingCount`
- `scripts/migrate_atom_tiers.py` → `classify_atom`, `migrate_atoms`, `rollback_atoms`, `main`
- `scripts/mine_implicit_feedback.py` → `load_queries`, `deduplicate_sequential`, `split_sessions`, `compute_similarity`, `mine_signals`, ...
- `scripts/redact_session.py` → `redact`, `main`
- `scripts/review_benchmark.py` → `InjectionResult`, `BugPattern`, `PathTraversalBypass`, `HardcodedSecret`, `CommandInjection`, ...
- `scripts/session_concepts.py` → `extract_terms`, `load_concepts`, `update_concepts`
- `scripts/settings_merge.py` → `merge_settings`, `rewrite_settings`
- `scripts/standards_pack.py` → `load_standards`, `main`
- `scripts/stop_hook.py` → `should_checkpoint`, `read_transcript`, `extract_topic`, `write_checkpoint`, `main`
- `scripts/sync_agent_skills.py` → `transform_markdown`, `render_agents_tree`, `check_skill_inventory`, `check_agents_tree`, `sync_agents_tree`, ...
- `scripts/sync_linear_pending.py` → `main`
- `scripts/token_bench/aggregate_compression.py` → `parse_log`, `main`
- `scripts/token_bench/diff.py` → `main`
- `scripts/token_bench/harness.py` → `est_tokens`, `file_info`, `main`
- `scripts/token_bench/keyword_bench.py` → `keywords`, `parse_facts`, `check_fact`, `main`
- `scripts/token_bench/preservation_bench.py` → `ollama_ask`, `parse_fact_file`, `check_fact`, `main`
- `scripts/trec_atom_benchmark.py` → `stage_sample`, `stage_pool`, `stage_judge`, `stage_export`, `main`
- `scripts/vault_context_hook.py` → `main`
- `scripts/wardens.py` → `cmd_show`, `cmd_enable`, `cmd_disable`, `cmd_triggers`, `cmd_reset`, ...
- `scripts/youtube_transcript_server.py` → `get_transcript`
- `src/agent-runtimes/claude-backend.ts` → `createClaudeRuntime`
- `src/agent-runtimes/container-backend.ts` → `ContainerRuntimeDeps`, `ContainerRuntime`
- `src/agent-runtimes/index.ts` → `defaultSession`, `resolveAgentRuntime`, `resolveAgentEffort`, `ContainerRuntime`, `createClaudeRuntime`, ...
- `src/agent-runtimes/llama-cpp-backend.ts` → `createLlamaCppRuntime`
- `src/agent-runtimes/openai-backend.ts` → `createOpenAIRuntime`
- `src/agent-runtimes/registry.ts` → `RuntimeRegistry`, `initRuntimeRegistry`
- `src/agent-runtimes/resolve.ts` → `resolveAgentRuntime`, `resolveAgentEffort`
- `src/agent-runtimes/types.ts` → `AgentRuntimeId`, `parseAgentBackend`, `RuntimeCapabilities`, `RuntimeSession`, `RunContext`, ...
- `src/async/index.ts` → `FireAndForgetOptions`, `fireAndForget`, `WithTimeoutOptions`, `withTimeout`, `AllSettledThrowPolicy`, ...
- `src/auth-providers/anthropic.ts` → `CREDENTIALS_PATH`, `EARLY_EXPIRE_WINDOW_MS`, `OAuthCredentials`, `_resetCredentialsCacheForTest`, `readCredentialsFile`, ...
- `src/auth-providers/index.ts` → `ensureDefaultProviders`, `AuthProvider`, `AuthProviderRegistry`, `NoProviderAvailableError`, `AnthropicAuthProvider`, ...
- `src/auth-providers/openai.ts` → `CODEX_AUTH_PATH`, `CodexAuthFile`, `CodexOAuthCredentials`, `_resetCodexCacheForTest`, `readCodexAuthFile`, ...
- `src/auth-providers/types.ts` → `AuthProvider`, `NoProviderAvailableError`, `AuthProviderRegistry`
- `src/auth-refresh.ts` → `RefreshResult`, `runRefresh`, `runCodexRefresh`
- `src/auto-compress.ts` → `autoCompressSession`
- `src/bootstrap.ts` → `BootstrapOptions`, `bootstrap`, `__resetBootstrapForTesting`
- `src/cache/cache-query.ts` → `CachedEvent`, `SearchOptions`, `BusyWindow`, `searchEvents`, `getUpcomingEvents`, ...
- `src/cache/gcal-sync.ts` → `CACHE_GCAL_DB_PATH`, `GcalSyncOptions`, `openCacheDb`, `startGcalSync`, `stopGcalSync`
- `src/channels/mcp-adapter.ts` → `McpChannelAdapterOpts`, `McpChannelAdapter`
- `src/channels/registry.ts` → `ChannelOpts`, `ChannelFactory`, `registerChannel`, `getChannelFactory`, `getRegisteredChannelNames`
- `src/checks.ts` → `hasApiCredentials`, `hasGeminiApiKey`, `readDeusConfig`, `hasMemoryVault`, `resolvePython`, ...
- `src/config.ts` → `ASSISTANT_NAME`, `ASSISTANT_HAS_OWN_NUMBER`, `POLL_INTERVAL`, `SCHEDULER_POLL_INTERVAL`, `PROJECT_ROOT`, ...
- `src/container-mounter.ts` → `VolumeMount`, `buildVolumeMounts`, `buildFanOutMounts`
- `src/container-runner.ts` → `ContainerInput`, `ContainerOutput`, `runContainerAgent`, `writeTasksSnapshot`, `AvailableGroup`, ...
- `src/container-runtime.ts` → `CONTAINER_RUNTIME_BIN`, `CONTAINER_HOST_GATEWAY`, `PROXY_BIND_HOST`, `readonlyMountArgs`, `stopContainerSync`, ...
- `src/credential-proxy.ts` → `AuthMode`, `ProxyConfig`, `_resetCredentialsCacheForTest`, `_resetRateLimiterForTest`, `resolveProviderRoute`, ...
- `src/db.ts` → `initDatabase`, `_initTestDatabase`, `storeChatMetadata`, `updateChatName`, `ChatInfo`, ...
- `src/deus-listen.ts` → `computeRms`, `renderBar`, `buildWavHeader`, `copyToClipboard`, `readClipboard`, ...
- `src/doc-gardener-seed.ts` → `seedDocGardener`
- `src/domain-presets.ts` → `parseCustomDomains`, `getAllDomainNames`, `detectDomains`, `detectDomainsWithFallback`
- `src/env.ts` → `readEnvFile`
- `src/errors/index.ts` → `ErrorContext`, `DeusErrorOptions`, `DeusError`, `RetryableError`, `FatalError`, ...
- `src/evolution-client.ts` → `LogInteractionParams`, `ReflectionsResult`, `getReflections`, `logInteraction`, `ReactionSignalParams`, ...
- `src/group-folder.ts` → `isValidGroupFolder`, `assertValidGroupFolder`, `resolveGroupFolderPath`, `resolveGroupIpcPath`
- `src/group-queue.ts` → `GroupQueue`
- `src/group-tokens.ts` → `getOrCreateGroupToken`, `validateGroupToken`, `_clearTokens`
- `src/guardrails/index.ts` → `scanForInjection`, `loadDefaultConfig`
- `src/guardrails/injection-scanner.ts` → `ScanResult`, `InjectionScannerConfig`, `loadDefaultConfig`, `scanForInjection`
- `src/image.ts` → `ProcessedImage`, `ImageAttachment`, `isImageMessage`, `processImage`, `parseImageReferences`
- `src/index.ts` → `getAvailableGroups`
- `src/ipc.ts` → `IpcDeps`, `startIpcWatcher`, `processTaskIpc`
- `src/linear-actions.ts` → `ActionContext`, `ActionResult`, `initActionContext`, `handleOpenInBrowser`, `toggleWardenSkip`, ...
- `src/linear-auto-merge.ts` → `queryPrChecks`, `attemptAutoMerge`, `sweepPendingAutoMerges`, `CompletionChecker`, `sweepStaleInReview`, ...
- `src/linear-dispatcher.ts` → `LinearDispatcherDependencies`, `WorkflowState`, `GateLabels`, `LinearContext`, `extractFrontmatter`, ...
- `src/linear-gate-specs.ts` → `GateSpec`, `loadGateSpecs`
- `src/linear-notifications.ts` → `macosNotify`, `EVENT_LABELS`, `buildPipelineCommentBody`, `updateUnifiedComment`, `notifyPipelineStep`
- `src/linear-pipeline-cli.ts` → `elapsedMs`, `computeColumnWidths`, `parseDuration`, `formatElapsed`
- `src/linear-vault-sync.ts` → `fetchActiveIssues`, `syncVaultPending`
- `src/linear-webhook.ts` → `_setSleepFnForTests`, `retryWithBackoff`, `parseVerdict`, `parseEnrichment`, `parseRatings`, ...
- `src/logger.ts` → `logger`
- `src/message-orchestrator.ts` → `OrchestratorDeps`, `createMessageOrchestrator`
- `src/mount-security.ts` → `_resetAllowlistCacheForTests`, `loadMountAllowlist`, `MountValidationResult`, `validateMount`, `validateAdditionalMounts`, ...
- `src/multi-agent/index.ts` → `MultiAgentOrchestrator`, `buildPrompt`
- `src/multi-agent/orchestrator.ts` → `MultiAgentOrchestrator`
- `src/multi-agent/prompt-templates.ts` → `buildPrompt`
- `src/multi-agent/types.ts` → `SubagentStatus`, `SubagentResult`, `OrchestratorResult`, `SubagentTask`
- `src/platform.ts` → `IS_WINDOWS`, `IS_MACOS`, `IS_LINUX`, `IS_WSL`, `PYTHON_BIN`, ...
- `src/pr-url-extractor.ts` → `extractPrUrl`
- `src/private/orchestrator/classifier.ts` → `ClassificationContext`, `classifyByLabel`, `classifyByDescriptionLength`, `classifyByFileCount`, `classifyByDependencyDepth`, ...
- `src/private/orchestrator/cli.ts` → `runApproveMode`, `findArg`, `createTracker`, `createStore`
- `src/private/orchestrator/config.ts` → `loadConfig`
- `src/private/orchestrator/context-builder.ts` → `ContextPackage`, `AgentRole`, `buildContextPackage`, `flattenContext`
- `src/private/orchestrator/cost-tracker.ts` → `IssueCost`, `CostSummary`, `CostTracker`
- `src/private/orchestrator/dispatcher.ts` → `DispatcherDeps`, `Dispatcher`
- `src/private/orchestrator/event-bus.ts` → `EventListener`, `EventBus`, `consoleLogger`
- `src/private/orchestrator/git-merger.ts` → `GitMerger`, `parseDiffNameStatus`
- `src/private/orchestrator/github-adapter.ts` → `GitHubAdapterConfig`, `GitHubIssueTracker`
- `src/private/orchestrator/index.ts` → `Dispatcher`, `MockIssueTracker`, `SandcastleRunner`, `InMemoryStore`, `EventBus`, ...
- `src/private/orchestrator/instrumented-runner.ts` → `RunMetrics`, `MetricsCallback`, `InstrumentedRunner`
- `src/private/orchestrator/issue-adapter.ts` → `MockIssueTracker`
- `src/private/orchestrator/json-logger.ts` → `JsonLoggerOptions`, `generateSpanId`, `jsonLogger`
- `src/private/orchestrator/label-map.ts` → `LABEL_PREFIX`, `labelToState`, `stateToLabel`, `extractStateLabels`, `extractState`
- `src/private/orchestrator/langfuse-observer.ts` → `LangfuseConfig`, `LangfuseClient`, `LangfuseTrace`, `LangfuseObserver`
- `src/private/orchestrator/loop-detector.ts` → `RunOutcome`, `LoopVerdict`, `LoopDetectorConfig`, `LoopDetector`
- `src/private/orchestrator/merge-parser.ts` → `MergeResult`, `parseMergeOutput`
- `src/private/orchestrator/model-router.ts` → `ModelTier`, `ModelSelection`, `routeModel`, `getReviewModel`, `getModelId`
- `src/private/orchestrator/otel-tracer.ts` → `OtelExporter`, `ConsoleOtelExporter`, `OtelTracer`
- `src/private/orchestrator/prompt-builder.ts` → `buildImplementPrompt`, `buildReviewPrompt`, `buildFixPrompt`
- `src/private/orchestrator/retry-policy.ts` → `RetryTiming`, `computeRetryDelay`, `shouldRetry`
- `src/private/orchestrator/review-gate.ts` → `ReviewTier`, `ChangeStats`, `ReviewGateConfig`, `ReviewGate`, `parseDiffStat`
- `src/private/orchestrator/run-history.ts` → `RunHistoryEntry`, `RunHistoryStore`, `SqliteRunHistory`
- `src/private/orchestrator/sandcastle-runner.ts` → `SandcastleDeps`, `SandcastleRunner`
- `src/private/orchestrator/sqlite-store.ts` → `SqliteStore`
- `src/private/orchestrator/state-machine.ts` → `dispatchTransition`, `reviewTransition`
- `src/private/orchestrator/store.ts` → `InMemoryStore`
- `src/private/orchestrator/token-budget.ts` → `BudgetVerdict`, `TokenBudgetConfig`, `TokenBudget`, `extractTokenUsage`, `extractTokenUsageDetails`
- `src/private/orchestrator/types.ts` → `Issue`, `BlockerRef`, `IssueTracker`, `DispatchState`, `ReleaseReason`, ...
- `src/private/orchestrator/verdict-parser.ts` → `parseTag`, `parseReviewVerdict`, `formatFindings`
- `src/private/scripts/gemini_ocr.py` → `main`
- `src/private/trading/analysis.ts` → `AnalysisInput`, `runAnalysis`
- `src/private/trading/approval.ts` → `formatApprovalMessage`, `createApprovalRequest`, `isApprovalExpired`, `parseApprovalResponse`, `buildBracketOrder`, ...
- `src/private/trading/bars-to-studies.ts` → `barsToStudies`
- `src/private/trading/chat-trigger.ts` → `TriggerMatch`, `parseTriggerMessage`, `TriggerHandlerOptions`, `TriggerHandlerResult`, `handleTriggerMatch`, ...
- `src/private/trading/config.ts` → `TRADING_ENABLED`, `loadSafetyConfig`
- `src/private/trading/driver.ts` → `OHLCVBar`, `TvCapture`, `PortfolioCapture`, `TvSource`, `IbkrSource`, ...
- `src/private/trading/earnings.ts` → `EarningsResult`, `clearEarningsCache`, `checkEarnings`, `checkEarningsBatch`
- `src/private/trading/gateway-client.ts` → `GatewayClientOptions`, `RawGatewayPosition`, `RawGatewayAccountSummary`, `RawGatewayAuthStatus`, `RawGatewayContract`, ...
- `src/private/trading/gateway-tv-source.ts` → `Timeframe`, `GatewayTvSourceOptions`, `createGatewayTvSource`
- `src/private/trading/ibkr-data.ts` → `IBKRPositionsResponse`, `IBKRPosition`, `IBKRAccountSummary`, `IBKROrder`, `getSector`, ...
- `src/private/trading/ibkr-gateway-source.ts` → `GatewaySourceOptions`, `createGatewayIbkrSource`
- `src/private/trading/index.ts` → `TRADING_ENABLED`, `loadSafetyConfig`, `detectRegime`, `regimeAllowsTrading`, `getRegimeParams`, ...
- `src/private/trading/indicators.ts` → `OHLCVBar`, `sma`, `ema`, `emaSeries`, `stdev`, ...
- `src/private/trading/llm-chain.ts` → `extractJSON`, `parseMultiTFResponse`, `parseSetupResponse`, `QualitativeRisk`, `parseRiskResponse`, ...
- `src/private/trading/logger.ts` → `logger`
- `src/private/trading/order-submission.ts` → `SubmitOptions`, `SubmitResult`, `buildOrdersPayload`, `submitBracketOrder`
- `src/private/trading/pipeline.ts` → `PipelineInput`, `PipelineResult`, `runPipeline`
- `src/private/trading/prompts.ts` → `buildRegimePrompt`, `buildMultiTFPrompt`, `buildSetupPrompt`, `buildRiskPrompt`, `buildDecisionPrompt`, ...
- `src/private/trading/regime.ts` → `RegimeInput`, `detectRegime`, `regimeAllowsTrading`, `getRegimeParams`
- `src/private/trading/safety.ts` → `checkSafetyRails`, `calculatePositionSize`, `estimateCorrelation`, `calculatePortfolioHeat`, `checkMarketHoursBuffer`, ...
- `src/private/trading/scheduler.ts` → `ScheduleOptions`, `scheduleAnalysis`
- `src/private/trading/symbol-lock.ts` → `LockMode`, `SymbolLock`, `globalSymbolLock`
- `src/private/trading/tv-data.ts` → `TVQuote`, `TVStudyEntry`, `TVOHLCVSummary`, `TVPriceLine`, `TVPriceLabel`, ...
- `src/private/trading/tv-fixture-source.ts` → `loadTvFixture`, `createTvFixtureSource`, `createTvFixtureSourceFromFile`
- `src/private/trading/types.ts` → `MarketRegime`, `RegimeSignal`, `TimeframeBias`, `TimeframeReading`, `MultiTFResult`, ...
- `src/private/trading/vix-yahoo.ts` → `YahooVixOptions`, `fetchYahooVix`, `fetchYahooVixStrict`
- `src/private/trading/vix.ts` → `VixFetchResult`, `IbkrVixProvider`, `TvVixProvider`, `fetchVix`, `isPlausibleVix`
- `src/project-registry.ts` → `detectProjectType`, `SENSITIVE_FILE_PATTERNS`, `SENSITIVE_DIR_PATTERNS`, `registerProject`, `associateProject`, ...
- `src/reaction-signal.ts` → `emojiToSignal`
- `src/remote-control.ts` → `restoreRemoteControl`, `getActiveSession`, `_resetForTesting`, `_getStateFilePath`, `startRemoteControl`, ...
- `src/router-state.ts` → `RouterState`, `getAvailableGroups`
- `src/router.ts` → `escapeXml`, `formatMessages`, `stripInternalTags`, `formatOutbound`, `routeOutbound`, ...
- `src/sender-allowlist.ts` → `ChatAllowlistEntry`, `SenderAllowlistConfig`, `loadSenderAllowlist`, `isSenderAllowed`, `shouldDropMessage`, ...
- `src/session-commands.ts` → `extractSettingsCommand`, `SettingsCommandResult`, `handleSettingsCommand`, `HostCommandHandler`, `HOST_COMMAND_HANDLERS`, ...
- `src/skills/index.ts` → `loadSkillIpcHandlers`
- `src/skills/registry.ts` → `SkillIpcHandler`, `registerSkillIpcHandler`, `getSkillIpcHandlers`, `getRegisteredSkillNames`
- `src/solutions/index.ts` → `writeSolution`, `searchSolutions`, `getSolution`, `listSolutions`, `loadSolutionContext`, ...
- `src/solutions/store.ts` → `ProblemType`, `Severity`, `Solution`, `resolveVaultPath`, `solutionsDir`, ...
- `src/startup-gate.ts` → `StartupCheck`, `CheckResult`, `registerStartupCheck`, `StartupCheckReport`, `runStartupChecks`, ...
- `src/task-scheduler.ts` → `computeNextRun`, `SchedulerDependencies`, `startSchedulerLoop`, `_resetSchedulerLoopForTests`
- `src/timezone.ts` → `formatLocalTime`
- `src/token-counter.ts` → `estimateTokens`, `sumTokens`
- `src/tool-broker/types.ts` → `ToolCapability`, `ToolDescriptor`, `ToolCallRequest`, `ToolCallResult`, `ToolBroker`, ...
- `src/tool-proxy.ts` → `startToolProxy`
- `src/tool-registry.ts` → `ToolConfig`, `loadRegistry`, `isAllowed`, `getToolConfig`
- `src/transcription.ts` → `TranscribeOptions`, `TranscriptionError`, `resolveDefaultModelPath`, `ensureWhisperModel`, `transcribeFile`, ...
- `src/types.ts` → `AdditionalMount`, `MountAllowlist`, `AllowedRoot`, `AgentEffortLevel`, `VALID_EFFORT_LEVELS`, ...
- `src/user-signal.ts` → `detectUserSignal`
- `src/x-integration.ts` → `handleXIpc`
