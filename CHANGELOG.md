# Changelog

All notable changes to Deus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0](https://github.com/sliamh11/Deus/compare/v1.0.0...v1.1.0) (2026-04-05)


### Features

* **channels:** add Discord MCP package ([#66](https://github.com/sliamh11/Deus/issues/66)) ([3d07584](https://github.com/sliamh11/Deus/commit/3d075849cc7f54e240392b2f127e17995b69a650))
* **channels:** add Gmail MCP package with OAuth polling and email tools ([#67](https://github.com/sliamh11/Deus/issues/67)) ([1a167be](https://github.com/sliamh11/Deus/commit/1a167be5c33def3142466a783d40cd4c115f897c))
* **channels:** add Slack MCP package ([#68](https://github.com/sliamh11/Deus/issues/68)) ([363451f](https://github.com/sliamh11/Deus/commit/363451f48670ad06ccf5452831b163df6dd69743))


### Bug Fixes

* **channels:** auto-import all channel factories to prevent git pull breakage ([ae11032](https://github.com/sliamh11/Deus/commit/ae11032ea1bb23086138d136ac7841d582be89da))
* **channels:** auto-import all channel factories to prevent git pull breakage ([1a7b649](https://github.com/sliamh11/Deus/commit/1a7b64956bf1fb771cd0d470c9416ce47d61332d))
* **ci:** use npm install and resolve file: deps for npm publish workflow ([54e4bbf](https://github.com/sliamh11/Deus/commit/54e4bbf712325c6c8c8c4a9fb47a679ea5ebea8b))

## 1.0.0 (2026-04-04)


### Features

* add brand assets and README hero banner ([3e33dba](https://github.com/sliamh11/Deus/commit/3e33dba1938ee6123494a44454bfc22bfc306800))
* **auth:** auto-refresh OAuth token from ~/.claude/.credentials.json ([a7d7e87](https://github.com/sliamh11/Deus/commit/a7d7e87595d1c339449a9b3b0d677cbdf6fe5b13))
* **cli:** add /preferences command and preference-aware launchers ([#41](https://github.com/sliamh11/Deus/issues/41)) ([75aa29c](https://github.com/sliamh11/Deus/commit/75aa29cff4cc81dedd3b64737a2f7fa7ba95547d))
* **cli:** add `deus listen` — mic-to-text via whisper.cpp ([5d50617](https://github.com/sliamh11/Deus/commit/5d506179286579ce9e45b6f87e79207490093dc0))
* **cli:** add loading progress and catch-me-up greeting to Windows launcher ([#40](https://github.com/sliamh11/Deus/issues/40)) ([36eb638](https://github.com/sliamh11/Deus/commit/36eb638497618158dfad79567e1fc80d286c8626))
* domain presets + expanded self-improvement loop ([85d9808](https://github.com/sliamh11/Deus/commit/85d980846e1193d3d4858cd5c4f58cc39196add8))
* **eval:** add MockJudge for CI and wire Gemini judge in workflow ([f42128c](https://github.com/sliamh11/Deus/commit/f42128c10a4251dd34ffdd3baa09a697f543f916))
* **evolution:** add Ollama fallback embedding provider ([2e04eb4](https://github.com/sliamh11/Deus/commit/2e04eb4a3358060bff9806ff9127a71f92232d9a))
* **evolution:** add reflection lifecycle cleanup with soft-delete archival ([de3913e](https://github.com/sliamh11/Deus/commit/de3913ea1fd32d69b7c8b9867e8184256103fd7b))
* **evolution:** data-driven principle extraction trigger ([c1e35e6](https://github.com/sliamh11/Deus/commit/c1e35e6b5758f92e8a30f265f611a6c9fd218ab2))
* **evolution:** fix broken signals, add auto-triggers, close feedback loop ([1d3eb71](https://github.com/sliamh11/Deus/commit/1d3eb7169562b8466e0ca31694bb835ce7c1c526))
* external environment mode — project registry, CLI mode, context-aware resume ([#1](https://github.com/sliamh11/Deus/issues/1)) ([e060622](https://github.com/sliamh11/Deus/commit/e060622423c378727fc00dd1f5223777927cb97e))
* **external-env:** Phase 2 project-settings improvements, Phase 3 auto-redaction ([b64acd8](https://github.com/sliamh11/Deus/commit/b64acd8365df3e823ce530a4d0062ddab4e27c21))
* generate group CLAUDE.md from templates during setup ([2d53289](https://github.com/sliamh11/Deus/commit/2d532894971200ea05c8665e3329f828532e9a5b))
* **mcp:** add custom YouTube transcript server ([f98f7ed](https://github.com/sliamh11/Deus/commit/f98f7ed0048241320dd79acbfecaa5f3520242ce))
* **memory:** add --learnings flag to surface emerging patterns in /resume ([1f88f49](https://github.com/sliamh11/Deus/commit/1f88f498d08dc3ba3e85c77d9a8be0cbd2971ce6))
* **memory:** add continuity indicator, session clustering, and cold start welcome ([26eb42f](https://github.com/sliamh11/Deus/commit/26eb42fc8bf81baa8ad31e5e4448eabe053e52f4))
* **memory:** improve /resume session loading, learnings, and UX ([86a1f95](https://github.com/sliamh11/Deus/commit/86a1f9548b354478a3228907e918dbec12f786a4))
* **memory:** make vault Obsidian-independent with auto-mount and location options ([#57](https://github.com/sliamh11/Deus/issues/57)) ([7891a35](https://github.com/sliamh11/Deus/commit/7891a35340bd5e4381bc3d8aaea58f2d4e5ff1ea))
* promote vault skills to user-level, clean up CLI, fix .env upsert ([083128b](https://github.com/sliamh11/Deus/commit/083128b058a11760aa6e875a255c5b8104535ab9))
* **security:** OllamaJudge, message limits, container hardening, docs ([8d80bf8](https://github.com/sliamh11/Deus/commit/8d80bf8e4965b14da581eaa2d892c1966876070d))
* **sessions:** idle-based session reset for all channels ([91f9b4c](https://github.com/sliamh11/Deus/commit/91f9b4c3fd4b0b1ff03cc4e2faa9154645987432))
* **settings:** /settings command + per-channel session_idle_hours ([6972355](https://github.com/sliamh11/Deus/commit/6972355ec0bc961cc914fec7e2a722b4304bd005))
* **setup:** onboarding gaps, kickstarter defaults, first-steps guide ([d28e2b6](https://github.com/sliamh11/Deus/commit/d28e2b6b28b3f1214754a48adedd54803f6566bd))
* **setup:** personality kickstarter — bundles, à la carte behaviors, seed reflections ([1354964](https://github.com/sliamh11/Deus/commit/13549648610df2cb859b95f9e1efade590070729))
* **tests:** complete remaining test coverage gaps; add GitHub Sponsors ([24a657f](https://github.com/sliamh11/Deus/commit/24a657f1bcec012c5ceb25cb8cdbd638c98ddb78))
* **tests:** comprehensive test coverage for security, core, and evolution layers ([3baf3e5](https://github.com/sliamh11/Deus/commit/3baf3e54a414d61921c0eac5079eb155fa2386e5))
* **windows:** add proxy bind host, service status checks, setup docs ([ebd83dc](https://github.com/sliamh11/Deus/commit/ebd83dc6ddd2a52862c6aea92799aa6473dfccd7))
* **windows:** add Windows platform detection and service management ([a27ba85](https://github.com/sliamh11/Deus/commit/a27ba850f4d2381b91e72e26f1cec1ab8ce582c1))
* **windows:** Windows support via Docker Desktop + NSSM/Servy ([5e5b941](https://github.com/sliamh11/Deus/commit/5e5b94170fe28e512f78bbde947c8c0558a08038))


### Bug Fixes

* **auth:** break login loop by checking ~/.claude/.credentials.json ([3404d71](https://github.com/sliamh11/Deus/commit/3404d716d8c01215234fbff08655262c6716587c))
* **auth:** check ~/.claude/.credentials.json in hasApiCredentials to break login loop ([840ccf7](https://github.com/sliamh11/Deus/commit/840ccf7925c52b69ebf9f20211547181285f6c39))
* **auth:** move OAuth credentials into session dir ([71a77bd](https://github.com/sliamh11/Deus/commit/71a77bdb71df1596b66d2efdf44d40879f7a1691))
* **auth:** move OAuth credentials into session dir to avoid Docker mount conflict ([3880a34](https://github.com/sliamh11/Deus/commit/3880a340b64f7fa07761d6817ccf7cf502a26362))
* **auth:** stop writing OAuth token to .env to prevent login loop on auto-refresh ([619a4bc](https://github.com/sliamh11/Deus/commit/619a4bcedd1488ee47c84edf1344a667fa70d8bf))
* **auth:** switch container OAuth from create_api_key to session-based auth ([0b37caa](https://github.com/sliamh11/Deus/commit/0b37caa025fd359f7172e544f62723172f82d74c))
* **auth:** switch container OAuth to session-based auth ([841a196](https://github.com/sliamh11/Deus/commit/841a196c70b8dcaacb258a61ed877d0bc4ea84a6))
* **channels:** add exponential backoff to Telegram reconnect and clarify startup hint ([#49](https://github.com/sliamh11/Deus/issues/49)) ([fdc9b95](https://github.com/sliamh11/Deus/commit/fdc9b95f0d9c31c7b5c4079e5a951e3ffeb83d58))
* **channels:** defer pairing code request until WebSocket is ready ([#42](https://github.com/sliamh11/Deus/issues/42)) ([3737415](https://github.com/sliamh11/Deus/commit/3737415b81abc0f4aa981ae4ab922fb0c35ebd24))
* **channels:** Telegram polling resilience + startup hint clarity ([#48](https://github.com/sliamh11/Deus/issues/48)) ([bd3b3d7](https://github.com/sliamh11/Deus/commit/bd3b3d737b69dc647280c2665db5426b8f97e761))
* **ci:** disable body line-length rule for dependabot compatibility ([#27](https://github.com/sliamh11/Deus/issues/27)) ([6ab8469](https://github.com/sliamh11/Deus/commit/6ab84691df9e5c932661a88a55324d852ecef079))
* **ci:** make husky hooks executable ([50ee00a](https://github.com/sliamh11/Deus/commit/50ee00afa4e388797fe09bc713b5646e026693ac))
* **ci:** rename commitlint config to .mjs for GitHub Action v6 compatibility ([c4be2ab](https://github.com/sliamh11/Deus/commit/c4be2ab53b6ef92496c16397beaefa3d94c37d63))
* **cli:** add comprehensive Deus identity to startup prompt ([#38](https://github.com/sliamh11/Deus/issues/38)) ([5fb36ee](https://github.com/sliamh11/Deus/commit/5fb36ee14e9951b47dc4fd71341ecfcd826fce2e))
* **cli:** fall back to normal mode when bypass is declined ([#37](https://github.com/sliamh11/Deus/issues/37)) ([5231e61](https://github.com/sliamh11/Deus/commit/5231e61449d6de19c0770523c13eb275a8887569))
* **cli:** pass system prompt as explicit array to avoid arg splitting ([#39](https://github.com/sliamh11/Deus/issues/39)) ([02b0554](https://github.com/sliamh11/Deus/commit/02b05547c5ef4767be7121cbc2b81a4215f2552e))
* **cli:** replace non-ASCII chars in deus-cmd.ps1 and add pre-commit guard ([#36](https://github.com/sliamh11/Deus/issues/36)) ([f6d273f](https://github.com/sliamh11/Deus/commit/f6d273f0c429806ef71d06c63896145cadbf520b))
* **commands:** intercept host slash commands before container in message loop; make handler registry extensible ([97779c0](https://github.com/sliamh11/Deus/commit/97779c0d1b0acdfe762ed4b551f4d809af3922df))
* **container:** resolve build failures from JSDoc glob and TS version conflicts ([#33](https://github.com/sliamh11/Deus/issues/33)) ([572e96e](https://github.com/sliamh11/Deus/commit/572e96e850ca67765965e15f0fabfa5b482371d3))
* **eval:** add langchain dependency and relax pytest pin for deepeval ([#60](https://github.com/sliamh11/Deus/issues/60)) ([c36350a](https://github.com/sliamh11/Deus/commit/c36350aea9dde31529e320b7075222470eacc2dd))
* **evolution:** fix 8 critical flaws in reflexion loop ([ab27b97](https://github.com/sliamh11/Deus/commit/ab27b97956f438e7c8d6d3098e21eda9c187456d))
* **memory:** use mtime tiebreaker and add --recent-days flag for session loading ([7859d29](https://github.com/sliamh11/Deus/commit/7859d29827e9452930ea93f496a9cda59c6cb627))
* pre-publish quick wins — security hardening, generic defaults, repo quality ([b0ae396](https://github.com/sliamh11/Deus/commit/b0ae3960c6ac0265ae0dc0807c7178da093f963e))
* prevent session ID poisoning and stale agent-runner cache ([3f9a4a4](https://github.com/sliamh11/Deus/commit/3f9a4a45ffafd51fc92721846fcc9bf56e958e06))
* rename Andy→Deus in plist, telegram channel, and test fixtures ([5e37292](https://github.com/sliamh11/Deus/commit/5e37292f12325b48c62d6e974d8a0e1e2d757fe8))
* **security:** eliminate shell injection and harden input validation ([#26](https://github.com/sliamh11/Deus/issues/26)) ([6ab0eec](https://github.com/sliamh11/Deus/commit/6ab0eec0cc5c1e25e77cf33e7519448060bab38d))
* **security:** resolve all Dependabot vulnerabilities ([4dd9787](https://github.com/sliamh11/Deus/commit/4dd9787f658171d97f7d34301c137f73d4b8334d))
* **setup:** auto-configure PATH and resolve CLI home dynamically ([cec13a5](https://github.com/sliamh11/Deus/commit/cec13a5da01b5b5579ea69aef0a0d450f492314a))
* **setup:** cross-platform Docker build + async setup flow ([#30](https://github.com/sliamh11/Deus/issues/30)) ([e59784b](https://github.com/sliamh11/Deus/commit/e59784ba69623079ad07714f9c3123b46d166210))
* **setup:** speed up WhatsApp auth and register deus CLI globally ([#35](https://github.com/sliamh11/Deus/issues/35)) ([eb6c9df](https://github.com/sliamh11/Deus/commit/eb6c9df9df74db996048ff8abe5170ec95224355))
* **setup:** update channel skills for MCP architecture, add auth script ([#32](https://github.com/sliamh11/Deus/issues/32)) ([a710324](https://github.com/sliamh11/Deus/commit/a710324f38f68f071bcab3f1531609754842c1ea))
* **setup:** use platform-aware PATH delimiter and anchor channel paths ([#45](https://github.com/sliamh11/Deus/issues/45)) ([4e51947](https://github.com/sliamh11/Deus/commit/4e51947f38742395f34033d108268e29b2d07011))
* **setup:** use platform-aware shell and bash for Windows container builds ([#44](https://github.com/sliamh11/Deus/issues/44)) ([cc9550b](https://github.com/sliamh11/Deus/commit/cc9550bb207e4590360013e20f4f69bf965cba16))
* **setup:** use template literals for Python command interpolation ([#46](https://github.com/sliamh11/Deus/issues/46)) ([cd1fd5c](https://github.com/sliamh11/Deus/commit/cd1fd5cf9f8d9f3c002bfec3a89387a72994048e))
* **skills:** don't add upstream remote for source repos in setup ([#31](https://github.com/sliamh11/Deus/issues/31)) ([3f13092](https://github.com/sliamh11/Deus/commit/3f130926d221a4054d716f8795c6db7b22f58e60))
* **skills:** only add upstream remote when user owns the origin repo ([#34](https://github.com/sliamh11/Deus/issues/34)) ([b308550](https://github.com/sliamh11/Deus/commit/b308550af07ef724e7997ab8ad1594f26946610a))
* **test:** mock async dependencies in container-runner timeout tests ([8314141](https://github.com/sliamh11/Deus/commit/8314141d6e02a63ef2a04b5d2c508fe988dc3845))
* **tests:** fix Windows path handling and platform validation in tests ([8d9cde9](https://github.com/sliamh11/Deus/commit/8d9cde9283f3f3afcf0e79b65c17e3dc9e65e311))
* **tests:** platform-aware process kill assertions in remote-control tests ([aed8953](https://github.com/sliamh11/Deus/commit/aed8953f4ade3352a172fbf9d0d0097296bca584))
* **tests:** skip Unix-path Docker tests on Windows, fix mount-security path ([8ddaf81](https://github.com/sliamh11/Deus/commit/8ddaf818d4ffeb1cb549e97c529d41919eda9f0b))
* **tests:** use path.resolve for cross-platform path comparison in mount-security ([f094f60](https://github.com/sliamh11/Deus/commit/f094f600d1c5fd5fc4129881908a17e1aa8f104e))
* **types:** resolve pre-existing TypeScript errors exposed by TS upgrade ([5e737d6](https://github.com/sliamh11/Deus/commit/5e737d653648b1d646c728af3dc5feac9c80019f))
* **windows:** complete cross-platform gaps ([#5](https://github.com/sliamh11/Deus/issues/5)) ([af6240c](https://github.com/sliamh11/Deus/commit/af6240c0fde6c886f7c9d4e6ae5dc29e26a97020))


### Performance Improvements

* **agent-runner:** exclude swarm tools for non-orchestration queries ([88d0804](https://github.com/sliamh11/Deus/commit/88d0804edfa4dc2c39cd6d3fac1cf27301ee055f))
* compress diagram PNGs (26MB → 950KB) ([4604515](https://github.com/sliamh11/Deus/commit/46045156671b10ffa0e7a89ddde96b993d72fab3))
* **evolution:** add missing SQLite indexes for hot query paths ([#58](https://github.com/sliamh11/Deus/issues/58)) ([d966b64](https://github.com/sliamh11/Deus/commit/d966b6482e38ae50631adf6c8df80747647a678e))

## [Unreleased]

## [0.1.0] - 2026-03-30

### Added
- Semantic memory system with sqlite-vec and Gemini embeddings (tiered retrieval)
- Evolution loop: interaction scoring, reflexion, DSPy optimization
- Eval layer with DeepEval test suite for containerized agents
- Voice transcription via local Whisper on Apple Silicon
- Image vision support (multimodal content in containers)
- Google Calendar integration (MCP server)
- Telegram channel support
- Task scheduler (cron/interval scheduled prompts)
- IPC system for cross-group container communication
- Session checkpoint system (auto-save on session end)
- Startup validation gate (checks prerequisites before launch)
- Credential proxy (injects API keys at runtime, never in container env)
- Mount security (allowlist-based volume mount validation)
- Dynamic concurrency (machine-adaptive worker counts)

### Changed
- Docker container runtime (cross-platform, default runtime)

---

*Entries before v0.1.0 are from the upstream NanoClaw project and preserved for historical reference.*
