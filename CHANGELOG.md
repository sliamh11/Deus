# Changelog

All notable changes to Deus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## 1.0.0 (2026-04-10)


### Features

* add brand assets and README hero banner ([d115e2f](https://github.com/sliamh11/Deus/commit/d115e2f054abeb0eac4b3a745603c0e2645309b5))
* **agents:** compact system prompts 126→64 lines each (-49% tokens) ([#130](https://github.com/sliamh11/Deus/issues/130)) ([3f4ae96](https://github.com/sliamh11/Deus/commit/3f4ae963561cda7bead191c8ed6e8591a67b2fdc))
* **auth:** auto-refresh OAuth token from ~/.claude/.credentials.json ([c39c14f](https://github.com/sliamh11/Deus/commit/c39c14f50c3f030f44a227617a95e159c17b112f))
* **channels:** add Discord MCP package ([#66](https://github.com/sliamh11/Deus/issues/66)) ([8ffe0a5](https://github.com/sliamh11/Deus/commit/8ffe0a5ca9a2bfcb90ec8c52f4cc80f2bfed2857))
* **channels:** add Gmail MCP package with OAuth polling and email tools ([#67](https://github.com/sliamh11/Deus/issues/67)) ([7f6b5d5](https://github.com/sliamh11/Deus/commit/7f6b5d55601873efce81d41e2a49af2d2169ce7e))
* **channels:** add Slack MCP package ([#68](https://github.com/sliamh11/Deus/issues/68)) ([01c6dd2](https://github.com/sliamh11/Deus/commit/01c6dd2520f652ec585dac5f73a940ee6f81cad7))
* **channels:** add X (Twitter) MCP server ([#126](https://github.com/sliamh11/Deus/issues/126)) ([f177dfd](https://github.com/sliamh11/Deus/commit/f177dfdd39f4b44001af9b1257256cab8db3cf36))
* **cli:** add /preferences command and preference-aware launchers ([#41](https://github.com/sliamh11/Deus/issues/41)) ([e48b973](https://github.com/sliamh11/Deus/commit/e48b973509081df7fb92d4d51618557ab1bcc46d))
* **cli:** add `deus listen` — mic-to-text via whisper.cpp ([c3171e0](https://github.com/sliamh11/Deus/commit/c3171e0025699e9ff34d3783d907818955f624a7))
* **cli:** add `deus listen` — mic-to-text via whisper.cpp ([c2aa574](https://github.com/sliamh11/Deus/commit/c2aa574558292bb1547caeb7472c72d014908e0b))
* **cli:** add loading progress and catch-me-up greeting to Windows launcher ([#40](https://github.com/sliamh11/Deus/issues/40)) ([10d183a](https://github.com/sliamh11/Deus/commit/10d183a026a3d4c9f154f2cac13b44af6ccc52be))
* **container:** add Google Calendar MCP server for container agents ([#93](https://github.com/sliamh11/Deus/issues/93)) ([107373e](https://github.com/sliamh11/Deus/commit/107373e4087a487b5f5188a3127c6799ead89221))
* domain presets + expanded self-improvement loop ([f16620b](https://github.com/sliamh11/Deus/commit/f16620bc9c8ef79c61eb4e4b5ffcb9b398bde86c))
* **eval:** add MockJudge for CI and wire Gemini judge in workflow ([5f07cc1](https://github.com/sliamh11/Deus/commit/5f07cc12350da24f4fed84e103bbe092e210dad2))
* **evolution:** add Claude Code session ingestion via cc-backfill ([#108](https://github.com/sliamh11/Deus/issues/108)) ([6877390](https://github.com/sliamh11/Deus/commit/6877390c4195c157555d9ed5f5fea33c67f116f0))
* **evolution:** add configurable reflection count and score analytics ([#129](https://github.com/sliamh11/Deus/issues/129)) ([63fc89c](https://github.com/sliamh11/Deus/commit/63fc89caf69fded418ac76c96d34539636d17910))
* **evolution:** add generative provider/registry pattern ([#87](https://github.com/sliamh11/Deus/issues/87)) ([aa45bea](https://github.com/sliamh11/Deus/commit/aa45beaa1bab1d6950dcdbc32a5b1b7c1c5a6ff5))
* **evolution:** add interaction compaction and batch judging ([#107](https://github.com/sliamh11/Deus/issues/107)) ([e4b31ac](https://github.com/sliamh11/Deus/commit/e4b31ac6e7f73d27c9cc0240708e835cf9e92b03))
* **evolution:** add LLM domain fallback and reflection maintenance ([#104](https://github.com/sliamh11/Deus/issues/104)) ([6621db3](https://github.com/sliamh11/Deus/commit/6621db324a6d602ce4fca2c6b977ac2049b2f17d))
* **evolution:** add Ollama fallback embedding provider ([7000f25](https://github.com/sliamh11/Deus/commit/7000f251cc70f0045ee2c4b723c344301a1279bc))
* **evolution:** add reflection lifecycle cleanup with soft-delete archival ([a7e0f63](https://github.com/sliamh11/Deus/commit/a7e0f63368ea0f5f2b9c91972fb6dab3d1dd07b7))
* **evolution:** add routing patterns and context_tokens ([#135](https://github.com/sliamh11/Deus/issues/135)) ([32f1d43](https://github.com/sliamh11/Deus/commit/32f1d43ce5af67872af44f486ba483eb08e36508))
* **evolution:** add storage provider/registry pattern for database abstraction ([#91](https://github.com/sliamh11/Deus/issues/91)) ([84f0d87](https://github.com/sliamh11/Deus/commit/84f0d87ede6c88969cb08b0c1091c8ffaf5fb8d7))
* **evolution:** data-driven principle extraction trigger ([de744c5](https://github.com/sliamh11/Deus/commit/de744c5ac46cc8b09e1fce43a457cf34fad91eba))
* **evolution:** document EVOLUTION_SKIP_GROUPS env var and add config constant ([#131](https://github.com/sliamh11/Deus/issues/131)) ([b4299a8](https://github.com/sliamh11/Deus/commit/b4299a8ab1edfce3ca0a8d3a2f6596da4ccab989))
* **evolution:** document exchange-pair chunking + add --chunk-stats and context_window ([#111](https://github.com/sliamh11/Deus/issues/111)) ([cd50db1](https://github.com/sliamh11/Deus/commit/cd50db18a2b4dfb56bccb04dd36f262a9b6e5e6a))
* **evolution:** fix broken signals, add auto-triggers, close feedback loop ([6b26743](https://github.com/sliamh11/Deus/commit/6b267434d6e276566972494f0e8872c609ab9d0a))
* **evolution:** prefer local EmbeddingGemma over Gemini API ([#105](https://github.com/sliamh11/Deus/issues/105)) ([a3ff8ce](https://github.com/sliamh11/Deus/commit/a3ff8ce53966ce15680807202e07719cdd0be040))
* **evolution:** switch default Ollama judge from qwen3.5:4b to gemma4:e4b ([#84](https://github.com/sliamh11/Deus/issues/84)) ([04849a2](https://github.com/sliamh11/Deus/commit/04849a2bdb61634cc016b90bdb811bc9375177da))
* external environment mode — project registry, CLI mode, context-aware resume ([#1](https://github.com/sliamh11/Deus/issues/1)) ([b9ca679](https://github.com/sliamh11/Deus/commit/b9ca679afc7d0db24952bf1b065aad4e4317f591))
* **external-env:** Phase 2 project-settings improvements, Phase 3 auto-redaction ([58215e3](https://github.com/sliamh11/Deus/commit/58215e31ac18806b7adb30c8a754eb638fe6b1cd))
* generate group CLAUDE.md from templates during setup ([9524d87](https://github.com/sliamh11/Deus/commit/9524d87799410566bf6acb91d747818eb8757eef))
* **mcp:** add custom YouTube transcript server ([ebd5a46](https://github.com/sliamh11/Deus/commit/ebd5a46dd6c157fce67f437f13fc7383a125572b))
* **mcp:** add custom YouTube transcript server ([aa350e8](https://github.com/sliamh11/Deus/commit/aa350e8f90a8fe4957f2e9536fabd6db2b45d5db))
* **memory:** add --health analytics to track system improvement over time ([#113](https://github.com/sliamh11/Deus/issues/113)) ([94c5ed4](https://github.com/sliamh11/Deus/commit/94c5ed47fd513aa9ca637c56159697ad19a74e81))
* **memory:** add --learnings flag to surface emerging patterns in /resume ([d9df9d9](https://github.com/sliamh11/Deus/commit/d9df9d91d117f92f44356c32aaa39b3c4655215d))
* **memory:** add atom extraction, turn chunking, and hybrid FTS5+RRF retrieval ([#122](https://github.com/sliamh11/Deus/issues/122)) ([4f2d7c1](https://github.com/sliamh11/Deus/commit/4f2d7c1b5dff40c105f130751fe4f15f1e0e138e))
* **memory:** add continuity indicator, session clustering, and cold start welcome ([efb13c0](https://github.com/sliamh11/Deus/commit/efb13c0d5a6d0eb67d81b7617d0bb2b54981f2c6))
* **memory:** add LongMemEval benchmark runner and internal benchmarks ([#117](https://github.com/sliamh11/Deus/issues/117)) ([c1bdef5](https://github.com/sliamh11/Deus/commit/c1bdef5d25d3b961a05fcbb6d336dc450cc85092))
* **memory:** improve /resume session loading, learnings, and UX ([db80a5e](https://github.com/sliamh11/Deus/commit/db80a5eb0f097c32025b7f6e7c279d975aea43b6))
* **memory:** make vault Obsidian-independent with auto-mount and location options ([#57](https://github.com/sliamh11/Deus/issues/57)) ([ddbe84e](https://github.com/sliamh11/Deus/commit/ddbe84eeb31d01139b4adb7bd9c7173c6e427f34))
* **memory:** preserve source excerpt alongside extracted atoms ([#109](https://github.com/sliamh11/Deus/issues/109)) ([2398073](https://github.com/sliamh11/Deus/commit/2398073e88b92579d440dd618248a56d4bf490fd))
* **patterns:** add pattern verification system ([#138](https://github.com/sliamh11/Deus/issues/138)) ([f614673](https://github.com/sliamh11/Deus/commit/f614673eb01d134d05506e80f846210ffb27c605))
* promote vault skills to user-level, clean up CLI, fix .env upsert ([4bd88f0](https://github.com/sliamh11/Deus/commit/4bd88f080d71c61492d738e7faceb82301b6ad2a))
* **security:** OllamaJudge, message limits, container hardening, docs ([f038356](https://github.com/sliamh11/Deus/commit/f0383569738d8d223d2e3b10f2b88a40a6a89458))
* **sessions:** idle-based session reset for all channels ([7b4f833](https://github.com/sliamh11/Deus/commit/7b4f8331478e34c1538beb0b551051bfb4f882cc))
* **settings:** /settings command + per-channel session_idle_hours ([7bb9b19](https://github.com/sliamh11/Deus/commit/7bb9b19e9319ab2d40c1c64a021ad845b32e0b12))
* **setup,evolution:** add Ollama model advisor step ([#103](https://github.com/sliamh11/Deus/issues/103)) ([f9ffee1](https://github.com/sliamh11/Deus/commit/f9ffee140db40407c67f50c9dc56ea4c1c27c490))
* **setup:** add channel smoke test and decouple channels from /setup ([#92](https://github.com/sliamh11/Deus/issues/92)) ([daa454c](https://github.com/sliamh11/Deus/commit/daa454c36f76fdbb9e8e9d7bbbcbda1b43b09850))
* **setup:** onboarding gaps, kickstarter defaults, first-steps guide ([73dce73](https://github.com/sliamh11/Deus/commit/73dce73c450a5044cd5982f9289c752d176a49c9))
* **setup:** personality kickstarter — bundles, à la carte behaviors, seed reflections ([b24e246](https://github.com/sliamh11/Deus/commit/b24e246f8c5b4d71201761a241537c7309395c07))
* **skill:** add-listen-hotkey — install deps + whisper model before hotkey setup ([fef98ef](https://github.com/sliamh11/Deus/commit/fef98effa36c6de6304e4ca03f2d9ba7298b0284))
* **skills:** add 6 core memory skills to repo and install via setup ([#125](https://github.com/sliamh11/Deus/issues/125)) ([e419b92](https://github.com/sliamh11/Deus/commit/e419b92d4cbb0e6ead1302c7c920b964a2294533))
* **tests:** complete remaining test coverage gaps; add GitHub Sponsors ([da588e3](https://github.com/sliamh11/Deus/commit/da588e37a961979e15dc1cac08b27a750240052f))
* **tests:** comprehensive test coverage for security, core, and evolution layers ([591287b](https://github.com/sliamh11/Deus/commit/591287be322fe1331f78c32ecd78bde9fd1d35ae))
* **windows:** add proxy bind host, service status checks, setup docs ([c0f8489](https://github.com/sliamh11/Deus/commit/c0f848955c63f5db9935f12722663dc7266f191c))
* **windows:** add Windows platform detection and service management ([9a3b5d1](https://github.com/sliamh11/Deus/commit/9a3b5d1bf7f95381a8c9cc15887587c31e065046))
* **windows:** Windows support via Docker Desktop + NSSM/Servy ([7a2ac5c](https://github.com/sliamh11/Deus/commit/7a2ac5c13ed6c29a8f4060d8f85ece5f8d34309f))
* **windows:** Windows support via Docker Desktop + NSSM/Servy ([7a2ac5c](https://github.com/sliamh11/Deus/commit/7a2ac5c13ed6c29a8f4060d8f85ece5f8d34309f))
* **x-integration:** add delete script and install deps in skill ([#128](https://github.com/sliamh11/Deus/issues/128)) ([96761e3](https://github.com/sliamh11/Deus/commit/96761e3ddd4cffcc46b735b1aca88310822ae474))


### Bug Fixes

* **auth:** break login loop by checking ~/.claude/.credentials.json ([58b7b4e](https://github.com/sliamh11/Deus/commit/58b7b4ed8f60c9fd4edb0c9bcee4c7e911c38dbf))
* **auth:** check ~/.claude/.credentials.json in hasApiCredentials to break login loop ([107fc9d](https://github.com/sliamh11/Deus/commit/107fc9df27681985d51e690f7c0137346a2f121b))
* **auth:** move OAuth credentials into session dir ([c82313b](https://github.com/sliamh11/Deus/commit/c82313baedc13b48e1bfe4612958891308f42d95))
* **auth:** move OAuth credentials into session dir to avoid Docker mount conflict ([3e5006d](https://github.com/sliamh11/Deus/commit/3e5006d88f4e6fcc63bc288db2f0686b501b0c96))
* **auth:** stop writing OAuth token to .env to prevent login loop on auto-refresh ([a766dd3](https://github.com/sliamh11/Deus/commit/a766dd3af9fb5108372732185198109a7fb771e6))
* **auth:** switch container OAuth from create_api_key to session-based auth ([d1814ed](https://github.com/sliamh11/Deus/commit/d1814ede9a0638c27c3e30c77c995d6209debcbb))
* **auth:** switch container OAuth to session-based auth ([d0dfa71](https://github.com/sliamh11/Deus/commit/d0dfa71916250bf2e8819fd3f68bb3f4f29d9ca0))
* **channels:** add exponential backoff to Telegram reconnect and clarify startup hint ([#49](https://github.com/sliamh11/Deus/issues/49)) ([3bc5f65](https://github.com/sliamh11/Deus/commit/3bc5f65b1818cf57aff9e37c23c6534ff9702fcb))
* **channels:** auto-import all channel factories to prevent git pull breakage ([223fe7a](https://github.com/sliamh11/Deus/commit/223fe7abd5a2a8fac45d36377aab9bb21c3d7285))
* **channels:** auto-import all channel factories to prevent git pull breakage ([0c19928](https://github.com/sliamh11/Deus/commit/0c19928bbc3e315d2f7a74a8c8637e85d6f4b3d3))
* **channels:** defer pairing code request until WebSocket is ready ([#42](https://github.com/sliamh11/Deus/issues/42)) ([0f51a56](https://github.com/sliamh11/Deus/commit/0f51a56efb609fedc386148bb6a1d2d83c5112cb))
* **channels:** enable MCP logging capability for message delivery ([#88](https://github.com/sliamh11/Deus/issues/88)) ([93b90d2](https://github.com/sliamh11/Deus/commit/93b90d2ecfdf0aed7851be77254a533c357d8599))
* **channels:** fix Windows path handling across all channel adapters and startup ([#101](https://github.com/sliamh11/Deus/issues/101)) ([462d278](https://github.com/sliamh11/Deus/commit/462d278b07e945407f85319002e94ab7969d86ac))
* **channels:** Telegram polling resilience + startup hint clarity ([#48](https://github.com/sliamh11/Deus/issues/48)) ([04ce00d](https://github.com/sliamh11/Deus/commit/04ce00d9f6d2d981224320335fef27d7c8230500))
* **ci:** disable body line-length rule for dependabot compatibility ([#27](https://github.com/sliamh11/Deus/issues/27)) ([fb41c1d](https://github.com/sliamh11/Deus/commit/fb41c1d2810e0accb8c89b0966444914cdca63ee))
* **ci:** make husky hooks executable ([84cab67](https://github.com/sliamh11/Deus/commit/84cab6759e49ee2b69976ba3b6e601ba65641f77))
* **ci:** make publish idempotent and use PAT for release-please ([#76](https://github.com/sliamh11/Deus/issues/76)) ([291f208](https://github.com/sliamh11/Deus/commit/291f208f3af5caacc6a4f89ccdec92aba23604c3))
* **ci:** rename commitlint config to .mjs for GitHub Action v6 compatibility ([c286803](https://github.com/sliamh11/Deus/commit/c286803c210f2a352773d95bc0b3d7201a35c16a))
* **ci:** use npm install and resolve file: deps for npm publish workflow ([318ee71](https://github.com/sliamh11/Deus/commit/318ee71157c952923238c9a58208c8321f2d42c6))
* **cli:** add comprehensive Deus identity to startup prompt ([#38](https://github.com/sliamh11/Deus/issues/38)) ([8d21328](https://github.com/sliamh11/Deus/commit/8d21328c89f26cf58ca71c6f95278be43a2d9d77))
* **cli:** fall back to normal mode when bypass is declined ([#37](https://github.com/sliamh11/Deus/issues/37)) ([d1ddcc5](https://github.com/sliamh11/Deus/commit/d1ddcc5a465e8c25883fba506e1b9aaa7666ef33))
* **cli:** guard against overwriting foreign binaries at CLI symlink path ([#82](https://github.com/sliamh11/Deus/issues/82)) ([99d7ebc](https://github.com/sliamh11/Deus/commit/99d7ebc0f2ea49f4f05c2546673980e9937602c4))
* **cli:** make CLI symlink resilient to repo moves and stale shadows ([#81](https://github.com/sliamh11/Deus/issues/81)) ([83afdad](https://github.com/sliamh11/Deus/commit/83afdad6442063c7a6ef3c11b0257517d82f443f))
* **cli:** pass system prompt as explicit array to avoid arg splitting ([#39](https://github.com/sliamh11/Deus/issues/39)) ([a2e7542](https://github.com/sliamh11/Deus/commit/a2e754200834f545977d894c7eb2e94ccc036a8f))
* **cli:** remove frozen OAuth token export that causes 401 after /login ([#100](https://github.com/sliamh11/Deus/issues/100)) ([e896108](https://github.com/sliamh11/Deus/commit/e896108c59d6a19407d08375b87bf5c3becb1c22))
* **cli:** replace non-ASCII chars in deus-cmd.ps1 and add pre-commit guard ([#36](https://github.com/sliamh11/Deus/issues/36)) ([9eb0c26](https://github.com/sliamh11/Deus/commit/9eb0c2601e29a8ec89c79878339ac5d868c2e10d))
* **commands:** intercept host slash commands before container in message loop; make handler registry extensible ([1bee13c](https://github.com/sliamh11/Deus/commit/1bee13cd69eb9a9fdf7a3284251050a09fd6c3e0))
* **container:** resolve build failures from JSDoc glob and TS version conflicts ([#33](https://github.com/sliamh11/Deus/issues/33)) ([a242928](https://github.com/sliamh11/Deus/commit/a24292869f2c454825f27293e02f837a56a5ca94))
* **eval:** add langchain dependency and relax pytest pin for deepeval ([#60](https://github.com/sliamh11/Deus/issues/60)) ([9d79816](https://github.com/sliamh11/Deus/commit/9d798169872dd48d9bb895ea954ac57f101e0e18))
* **evolution:** add provider fallback, Ollama timeout, and scoring helpers ([#119](https://github.com/sliamh11/Deus/issues/119)) ([5459649](https://github.com/sliamh11/Deus/commit/5459649c60bfa060205803271a41cfc604d763b7))
* **evolution:** drop deepeval dependency — use plain Python judge classes ([#115](https://github.com/sliamh11/Deus/issues/115)) ([609c444](https://github.com/sliamh11/Deus/commit/609c444781aff4cce915b9cc0cb58e5ec6277c04))
* **evolution:** fix 8 critical flaws in reflexion loop ([f83dd17](https://github.com/sliamh11/Deus/commit/f83dd1799445e895850b5568ed7b41e32cfe35ff))
* **evolution:** split evolution DB from shared memory.db to prevent data loss ([#123](https://github.com/sliamh11/Deus/issues/123)) ([3827402](https://github.com/sliamh11/Deus/commit/382740281d5723d3c74d485d18033910484dfbe7))
* **memory:** add safety guard to prevent rebuild from deleting evolution data ([#127](https://github.com/sliamh11/Deus/issues/127)) ([e188159](https://github.com/sliamh11/Deus/commit/e1881590110193aee395ce8a7ca2a4d1a62352c2))
* **memory:** resolve Obsidian wikilinks before embedding ([#124](https://github.com/sliamh11/Deus/issues/124)) ([477ec54](https://github.com/sliamh11/Deus/commit/477ec54060a808b61fd56478fecc03a5cbdc7fba))
* **memory:** use mtime tiebreaker and add --recent-days flag for session loading ([42cecc1](https://github.com/sliamh11/Deus/commit/42cecc10b2cd45f6ee68f00125deda791728bc7f))
* **ops:** surface silent failures + add daily Ollama log review + log rotation ([#116](https://github.com/sliamh11/Deus/issues/116)) ([f18d080](https://github.com/sliamh11/Deus/commit/f18d080e6af4979a71b67891e52d88dd05c660a1))
* post.ts tweet URL, resume time-label rule, finetune gitignore ([#132](https://github.com/sliamh11/Deus/issues/132)) ([5a9383a](https://github.com/sliamh11/Deus/commit/5a9383a102a42b295e60b054d94a9017394fbd75))
* pre-publish quick wins — security hardening, generic defaults, repo quality ([0d30005](https://github.com/sliamh11/Deus/commit/0d3000564f21e4e38867c55cbc9abf953a24cd6b))
* prevent session ID poisoning and stale agent-runner cache ([c9b5473](https://github.com/sliamh11/Deus/commit/c9b5473ac28ca5be24de568e7689ba1728594336))
* rename Andy→Deus in plist, telegram channel, and test fixtures ([ffdd4f7](https://github.com/sliamh11/Deus/commit/ffdd4f7170b9bf32db95dcda6b5e0b6011ebee25))
* **security:** eliminate shell injection and harden input validation ([#26](https://github.com/sliamh11/Deus/issues/26)) ([94a79d5](https://github.com/sliamh11/Deus/commit/94a79d511aedcf8cd03c44cadba7d9a0f162425c))
* **security:** resolve all Dependabot vulnerabilities ([a2b3a84](https://github.com/sliamh11/Deus/commit/a2b3a84416d76daf98b630f3b7c3cb6c09f3a6e7))
* **setup:** add /opt/homebrew/bin to launchd plist PATH for Apple Silicon ([#80](https://github.com/sliamh11/Deus/issues/80)) ([558a4ef](https://github.com/sliamh11/Deus/commit/558a4ef46a2573b887d2fc4487eb8110d6bf41fb))
* **setup:** auto-configure PATH and resolve CLI home dynamically ([cd3b8f3](https://github.com/sliamh11/Deus/commit/cd3b8f3d28c02086bf486be27acbd53a05411b97))
* **setup:** cross-platform Docker build + async setup flow ([#30](https://github.com/sliamh11/Deus/issues/30)) ([b45e151](https://github.com/sliamh11/Deus/commit/b45e151a5eb0cfe238a84f42707e731103314ab2))
* **setup:** speed up WhatsApp auth and register deus CLI globally ([#35](https://github.com/sliamh11/Deus/issues/35)) ([46e1b3b](https://github.com/sliamh11/Deus/commit/46e1b3b948168523ee5fcf975fef8f75a9c05642))
* **setup:** update channel skills for MCP architecture, add auth script ([#32](https://github.com/sliamh11/Deus/issues/32)) ([30c839a](https://github.com/sliamh11/Deus/commit/30c839aca04b538d381323422a4d433468e97d46))
* **setup:** use platform-aware PATH delimiter and anchor channel paths ([#45](https://github.com/sliamh11/Deus/issues/45)) ([1cbb480](https://github.com/sliamh11/Deus/commit/1cbb480f446c6e7ee3a082dd7bebe277dfc3540c))
* **setup:** use platform-aware shell and bash for Windows container builds ([#44](https://github.com/sliamh11/Deus/issues/44)) ([9bae465](https://github.com/sliamh11/Deus/commit/9bae4655d206915e44a0ad1498e45bff6fd88125))
* **setup:** use template literals for Python command interpolation ([#46](https://github.com/sliamh11/Deus/issues/46)) ([69da654](https://github.com/sliamh11/Deus/commit/69da6542b7fc2e8082b1b31f9383c32fa34250b5))
* **skills:** don't add upstream remote for source repos in setup ([#31](https://github.com/sliamh11/Deus/issues/31)) ([7fb8551](https://github.com/sliamh11/Deus/commit/7fb8551f715dfe708323c700a707a4d83138f8ad))
* **skills:** only add upstream remote when user owns the origin repo ([#34](https://github.com/sliamh11/Deus/issues/34)) ([78ec3ad](https://github.com/sliamh11/Deus/commit/78ec3adace09fda678fa9fbe6685a43af2789de6))
* **test:** make container-mounter tests cross-platform for Windows CI ([#94](https://github.com/sliamh11/Deus/issues/94)) ([167a7df](https://github.com/sliamh11/Deus/commit/167a7dffabfe367f0aa7cf8db96f59952cce6fa8))
* **test:** mock async dependencies in container-runner timeout tests ([a6c3d27](https://github.com/sliamh11/Deus/commit/a6c3d272ff276e6dd961c4df15fb5db0b59f0700))
* **tests:** fix Windows path handling and platform validation in tests ([3ea8e4e](https://github.com/sliamh11/Deus/commit/3ea8e4e0719c0cc4fb3db79cbf5f321686b37d85))
* **tests:** platform-aware process kill assertions in remote-control tests ([b93955e](https://github.com/sliamh11/Deus/commit/b93955eba68f95cde2bc962cc0c3dd3e2a453fa4))
* **tests:** platform-aware process kill assertions in remote-control tests ([7be3dd6](https://github.com/sliamh11/Deus/commit/7be3dd63f19a11af4090393881aa7dcc2942cd68))
* **tests:** skip Unix-path Docker tests on Windows, fix mount-security path ([e1587d9](https://github.com/sliamh11/Deus/commit/e1587d9eae6748fdeb53bba82e7a7d136da50c0c))
* **tests:** use path.resolve for cross-platform path comparison in mount-security ([f0cc894](https://github.com/sliamh11/Deus/commit/f0cc894c35c5d4a47e2f1f9351a5c93b0996991f))
* **types:** resolve pre-existing TypeScript errors exposed by TS upgrade ([f8efe20](https://github.com/sliamh11/Deus/commit/f8efe203b69738e285fe888a86f1dea38f9afbad))
* **whatsapp:** event-driven group sync, eliminate redundant bulk fetch ([#134](https://github.com/sliamh11/Deus/issues/134)) ([5043405](https://github.com/sliamh11/Deus/commit/50434050a8566bbc92fef2cea41439e2926bc358))
* **windows:** complete cross-platform gaps ([#5](https://github.com/sliamh11/Deus/issues/5)) ([7d6cfbb](https://github.com/sliamh11/Deus/commit/7d6cfbb5cec60e8c8072655512f663b0fc6f9eb7))


### Performance Improvements

* **agent-runner:** exclude swarm tools for non-orchestration queries ([439e211](https://github.com/sliamh11/Deus/commit/439e211f45e5113063a946a1b79d64ab1bdc8bd4))
* compress diagram PNGs (26MB → 950KB) ([68ddedb](https://github.com/sliamh11/Deus/commit/68ddedb6d45a63461b9f19ac986ab0fc2cf72170))
* **evolution:** add missing SQLite indexes for hot query paths ([#58](https://github.com/sliamh11/Deus/issues/58)) ([43371b6](https://github.com/sliamh11/Deus/commit/43371b6c556a15a21ea5311c691e8b1323814647))
* **evolution:** compact LLM prompts and fix parse error tracking ([#121](https://github.com/sliamh11/Deus/issues/121)) ([1c50434](https://github.com/sliamh11/Deus/commit/1c504341343156c96c1c8fd3866a75f843273991))
* **memory:** add compact mode for --recent/--recent-days output ([#110](https://github.com/sliamh11/Deus/issues/110)) ([a290eca](https://github.com/sliamh11/Deus/commit/a290eca9e8fb3a4d3a4d95e4b3f4823b5f2c211b))

## [1.3.0](https://github.com/sliamh11/Deus/compare/v1.2.0...v1.3.0) (2026-04-09)


### Features

* **agents:** compact system prompts 126→64 lines each (-49% tokens) ([#130](https://github.com/sliamh11/Deus/issues/130)) ([aca6e87](https://github.com/sliamh11/Deus/commit/aca6e870ea26e51ce9f00143999e0b1fc99bfa91))
* **channels:** add X (Twitter) MCP server ([#126](https://github.com/sliamh11/Deus/issues/126)) ([92edc97](https://github.com/sliamh11/Deus/commit/92edc97ee253a83a965aa2582ebdac943bc43058))
* **evolution:** add configurable reflection count and score analytics ([#129](https://github.com/sliamh11/Deus/issues/129)) ([15a6ee7](https://github.com/sliamh11/Deus/commit/15a6ee7d0062a00cda930eb35e60e37fd6fe30f1))
* **evolution:** document EVOLUTION_SKIP_GROUPS env var and add config constant ([#131](https://github.com/sliamh11/Deus/issues/131)) ([13fe4c2](https://github.com/sliamh11/Deus/commit/13fe4c22fd420f5d321f3539e0b91bf358f7b561))
* **memory:** add atom extraction, turn chunking, and hybrid FTS5+RRF retrieval ([#122](https://github.com/sliamh11/Deus/issues/122)) ([76a7a67](https://github.com/sliamh11/Deus/commit/76a7a679a2e3cbf72019b617a8a0e49249928aac))
* **memory:** add LongMemEval benchmark runner and internal benchmarks ([#117](https://github.com/sliamh11/Deus/issues/117)) ([d312b03](https://github.com/sliamh11/Deus/commit/d312b0318d9255a321e52c6ee9070378d1fd9769))
* **skills:** add 6 core memory skills to repo and install via setup ([#125](https://github.com/sliamh11/Deus/issues/125)) ([63f171d](https://github.com/sliamh11/Deus/commit/63f171d81282531f2b125dc0093ccee670d632ff))
* **x-integration:** add delete script and install deps in skill ([#128](https://github.com/sliamh11/Deus/issues/128)) ([b6bb720](https://github.com/sliamh11/Deus/commit/b6bb720e8fb66c67608b7f46a93d20de7d58d95d))


### Bug Fixes

* **evolution:** add provider fallback, Ollama timeout, and scoring helpers ([#119](https://github.com/sliamh11/Deus/issues/119)) ([72ca907](https://github.com/sliamh11/Deus/commit/72ca90769c05813375cfd5e1de0fef3ee275b239))
* **evolution:** split evolution DB from shared memory.db to prevent data loss ([#123](https://github.com/sliamh11/Deus/issues/123)) ([2cb7e6e](https://github.com/sliamh11/Deus/commit/2cb7e6e921d443823abbc1dc7bbcb9d8dd9ab24d))
* **memory:** add safety guard to prevent rebuild from deleting evolution data ([#127](https://github.com/sliamh11/Deus/issues/127)) ([3ad089c](https://github.com/sliamh11/Deus/commit/3ad089c063a45b391e8f5745c99ef4b2c5c0d9ed))
* **memory:** resolve Obsidian wikilinks before embedding ([#124](https://github.com/sliamh11/Deus/issues/124)) ([b81b7cf](https://github.com/sliamh11/Deus/commit/b81b7cf23115cf12f82a3b104b689685ce3aa94d))


### Performance Improvements

* **evolution:** compact LLM prompts and fix parse error tracking ([#121](https://github.com/sliamh11/Deus/issues/121)) ([588c36a](https://github.com/sliamh11/Deus/commit/588c36a0d982cd1fac67e40c20f0b24350fe9e96))

## [1.2.0](https://github.com/sliamh11/Deus/compare/v1.1.0...v1.2.0) (2026-04-07)


### Features

* **container:** add Google Calendar MCP server for container agents ([#93](https://github.com/sliamh11/Deus/issues/93)) ([b7ae997](https://github.com/sliamh11/Deus/commit/b7ae99707cc8d45c81a66401d7ecaf8ca01d3117))
* **evolution:** add Claude Code session ingestion via cc-backfill ([#108](https://github.com/sliamh11/Deus/issues/108)) ([39e1ee4](https://github.com/sliamh11/Deus/commit/39e1ee458e6eb9dc08c80455b488e201b24dac6e))
* **evolution:** add generative provider/registry pattern ([#87](https://github.com/sliamh11/Deus/issues/87)) ([d9e9c1c](https://github.com/sliamh11/Deus/commit/d9e9c1c5fb092860e3e20a4597e03e61fac7d2c7))
* **evolution:** add interaction compaction and batch judging ([#107](https://github.com/sliamh11/Deus/issues/107)) ([b1ced70](https://github.com/sliamh11/Deus/commit/b1ced70d2d7d4f43de3183b058ed13fe97199984))
* **evolution:** add LLM domain fallback and reflection maintenance ([#104](https://github.com/sliamh11/Deus/issues/104)) ([c65eb53](https://github.com/sliamh11/Deus/commit/c65eb539a6004824c4be82ef7776964fbde22f88))
* **evolution:** add storage provider/registry pattern for database abstraction ([#91](https://github.com/sliamh11/Deus/issues/91)) ([1dc3788](https://github.com/sliamh11/Deus/commit/1dc3788d1875cb289df129a910880f308e50683c))
* **evolution:** document exchange-pair chunking + add --chunk-stats and context_window ([#111](https://github.com/sliamh11/Deus/issues/111)) ([d86344c](https://github.com/sliamh11/Deus/commit/d86344cf0e8d9946b5283f793260cf2a23c6bca8))
* **evolution:** prefer local EmbeddingGemma over Gemini API ([#105](https://github.com/sliamh11/Deus/issues/105)) ([38e7c8b](https://github.com/sliamh11/Deus/commit/38e7c8b93b9fca080dd413ffef3c83b71709aad0))
* **evolution:** switch default Ollama judge from qwen3.5:4b to gemma4:e4b ([#84](https://github.com/sliamh11/Deus/issues/84)) ([67865a2](https://github.com/sliamh11/Deus/commit/67865a2a76cefa4865313ebd225566df1bdc38e4))
* **memory:** add --health analytics to track system improvement over time ([#113](https://github.com/sliamh11/Deus/issues/113)) ([7fbda4b](https://github.com/sliamh11/Deus/commit/7fbda4b38e83c3a906778bbaa9523240afa01ab5))
* **memory:** preserve source excerpt alongside extracted atoms ([#109](https://github.com/sliamh11/Deus/issues/109)) ([52ceffb](https://github.com/sliamh11/Deus/commit/52ceffbdebccc49d6425bfdb138fe034646b4c54))
* **setup,evolution:** add Ollama model advisor step ([#103](https://github.com/sliamh11/Deus/issues/103)) ([f1c8a23](https://github.com/sliamh11/Deus/commit/f1c8a238bf7d24deead639575d7d7dcce1986a3d))
* **setup:** add channel smoke test and decouple channels from /setup ([#92](https://github.com/sliamh11/Deus/issues/92)) ([3216ff1](https://github.com/sliamh11/Deus/commit/3216ff152234a59edca2010feaf96d228453cbdb))


### Bug Fixes

* **channels:** enable MCP logging capability for message delivery ([#88](https://github.com/sliamh11/Deus/issues/88)) ([d38d7fa](https://github.com/sliamh11/Deus/commit/d38d7fad0419a7453e5739d5c244f1c0fc3ab01c))
* **channels:** fix Windows path handling across all channel adapters and startup ([#101](https://github.com/sliamh11/Deus/issues/101)) ([05d3523](https://github.com/sliamh11/Deus/commit/05d3523fd7b65bc8ac34357bfad0b1dc92456202))
* **ci:** make publish idempotent and use PAT for release-please ([#76](https://github.com/sliamh11/Deus/issues/76)) ([ccf12f6](https://github.com/sliamh11/Deus/commit/ccf12f69d3bf69afcd1b1e96a475ba9630d89e6e))
* **cli:** guard against overwriting foreign binaries at CLI symlink path ([#82](https://github.com/sliamh11/Deus/issues/82)) ([574fa7f](https://github.com/sliamh11/Deus/commit/574fa7ff4e98f8885d89603ed3a17341c234adee))
* **cli:** make CLI symlink resilient to repo moves and stale shadows ([#81](https://github.com/sliamh11/Deus/issues/81)) ([153d787](https://github.com/sliamh11/Deus/commit/153d78708a0e39dc92672fe795b7d9ce6c5591ab))
* **cli:** remove frozen OAuth token export that causes 401 after /login ([#100](https://github.com/sliamh11/Deus/issues/100)) ([5e73ace](https://github.com/sliamh11/Deus/commit/5e73ace3c5c7610bb880668acfd6d0dbe3113978))
* **evolution:** drop deepeval dependency — use plain Python judge classes ([#115](https://github.com/sliamh11/Deus/issues/115)) ([b16ab33](https://github.com/sliamh11/Deus/commit/b16ab33b87dadc6dbd2af4ec56bcfd8e1d02ea39))
* **setup:** add /opt/homebrew/bin to launchd plist PATH for Apple Silicon ([#80](https://github.com/sliamh11/Deus/issues/80)) ([cbbf214](https://github.com/sliamh11/Deus/commit/cbbf214e56c66d20904ae33f828693689a821ca6))
* **test:** make container-mounter tests cross-platform for Windows CI ([#94](https://github.com/sliamh11/Deus/issues/94)) ([68d468a](https://github.com/sliamh11/Deus/commit/68d468a92fb5ab014fa2a43345d38d0c4a10315f))


### Performance Improvements

* **memory:** add compact mode for --recent/--recent-days output ([#110](https://github.com/sliamh11/Deus/issues/110)) ([0f6fab2](https://github.com/sliamh11/Deus/commit/0f6fab24eb9aaae50edcb00b602e796be6904914))

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
