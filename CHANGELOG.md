# Changelog

All notable changes to Deus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.10.0](https://github.com/sliamh11/Deus/compare/v1.9.0...v1.10.0) (2026-04-27)


### Features

* **auth:** add Codex OAuth support for OpenAI backend ([#258](https://github.com/sliamh11/Deus/issues/258)) ([b87f043](https://github.com/sliamh11/Deus/commit/b87f043126387a2a8d2d981abf76680978ad03b0))
* backend-neutral agent runtime with registry and multi-backend docs ([#246](https://github.com/sliamh11/Deus/issues/246)) ([1d0ede7](https://github.com/sliamh11/Deus/commit/1d0ede76c28bfe8e606a30404e8c30f60245d0fb))
* **backends:** wire runTurn() dispatch and close AAG debt register ([#256](https://github.com/sliamh11/Deus/issues/256)) ([6527d37](https://github.com/sliamh11/Deus/commit/6527d3758746949561f474325f03bd62e389c830))
* **channels:** add image vision across all MCP channels ([#261](https://github.com/sliamh11/Deus/issues/261)) ([8bbbc4f](https://github.com/sliamh11/Deus/commit/8bbbc4fa5889742e81947321312a33689513cb5b))
* **eval:** backend parity testing across Claude and OpenAI/Codex ([#253](https://github.com/sliamh11/Deus/issues/253)) ([b3764fb](https://github.com/sliamh11/Deus/commit/b3764fbc345863cbb394fbc62c1d6a994c33fac6))
* **gcal:** add /add-gcal skill, CLI commands, and token keep-alive ([6153f88](https://github.com/sliamh11/Deus/commit/6153f88c138c187cc06edbca64e5bf1fd6c493e1))
* make wardens backend-neutral via agent sync script ([edf651b](https://github.com/sliamh11/Deus/commit/edf651bd360b455d389a68141fd89b3eeeb195e3))
* **memory-tree:** add FTS5 hybrid retrieval with BM25 + RRF fusion ([#245](https://github.com/sliamh11/Deus/issues/245)) ([87a8f60](https://github.com/sliamh11/Deus/commit/87a8f60db28fe2de72e0234b5bdc8fe35b03496a))
* **memory-tree:** add reindex-external for auto-memory population ([#244](https://github.com/sliamh11/Deus/issues/244)) ([5616b5a](https://github.com/sliamh11/Deus/commit/5616b5a8eb8c3f4605967696fe06886b592a8d80))
* **memory:** category-aware atom injection ([#264](https://github.com/sliamh11/Deus/issues/264)) ([d921b40](https://github.com/sliamh11/Deus/commit/d921b4076e87e72c6780a093125b6546b8147d79))
* **memory:** scalable 3-layer memory architecture (Lighthouse Phase 6) ([#262](https://github.com/sliamh11/Deus/issues/262)) ([93aa9a7](https://github.com/sliamh11/Deus/commit/93aa9a7bcc0015b9b8a73cef485db9633da0a17d))
* **security:** add shared-secret auth to credential proxy ([#254](https://github.com/sliamh11/Deus/issues/254)) ([cf1bd18](https://github.com/sliamh11/Deus/commit/cf1bd184d4ac7f9eaaf19b1f0485ea2957e4cd8b))
* **skills:** add /add-codex interactive skill for guided backend setup ([#252](https://github.com/sliamh11/Deus/issues/252)) ([a2d876c](https://github.com/sliamh11/Deus/commit/a2d876c8f085321b2cb6445f1c5c74badd1a3e9a))
* **skills:** add optional llama.cpp skill ([941735c](https://github.com/sliamh11/Deus/commit/941735cc7b314c2b56c0ddba9d2643d02d8f73e6))
* **wardens:** add session-retrospective warden ([d6effa1](https://github.com/sliamh11/Deus/commit/d6effa15d9a8962b72f1b4548784c5aef94680d2))
* **wardens:** add threat-modeler and architecture-snapshot wardens ([aeaf85a](https://github.com/sliamh11/Deus/commit/aeaf85a2090b507f286c7af441ad1702e684509c))


### Bug Fixes

* **build:** include MCP packages in npm run build ([5192b1b](https://github.com/sliamh11/Deus/commit/5192b1b49ca3b667315243dc43d2007bccfd1423))
* **ci:** add docs/ pattern to resolve drift check coverage gap ([a1a3621](https://github.com/sliamh11/Deus/commit/a1a36210512b43f69c0e027656bf8c836430f404))
* **container:** exclude test files from agent-runner build ([1a0a984](https://github.com/sliamh11/Deus/commit/1a0a984d37b8258aad5688b3372314f4d264c97f))
* **docs:** escape parentheses in Channel System Mermaid diagram ([6293911](https://github.com/sliamh11/Deus/commit/62939112dffd6fce7324756cbc4269732e61935a))
* **docs:** replace double dashes with single dashes in README ([3c6eb0c](https://github.com/sliamh11/Deus/commit/3c6eb0c26a1aa55c0fc81c7cae8afafd8d058aba))
* **memory-tree:** check embedding existence in reindex_external ([#249](https://github.com/sliamh11/Deus/issues/249)) ([c14d668](https://github.com/sliamh11/Deus/commit/c14d66846c76dd903fad0f507379c7db5eb30977))
* **memory-tree:** fix recall regression + recalibrate benchmark ([#248](https://github.com/sliamh11/Deus/issues/248)) ([4cf6da9](https://github.com/sliamh11/Deus/commit/4cf6da92178cf4770c3da9ece3cab2b7c761a256))
* startup instruction skips catch-up when user provides explicit directive ([40b8761](https://github.com/sliamh11/Deus/commit/40b8761364ebc9e045d9c082af2c0d26ca1cc6bc))
* **tests:** resolve 4 pre-existing script test failures ([#255](https://github.com/sliamh11/Deus/issues/255)) ([030fc5b](https://github.com/sliamh11/Deus/commit/030fc5b52b0f2590ad97757d0fa0ba3278ad5453))
* warden audit — false-green bench, portable sed, cross-platform guards ([4312497](https://github.com/sliamh11/Deus/commit/43124978fed3ad8fcbf8c9c8d2b399c4b3d955e8))
* **wardens:** add Mermaid entity rule and document agent discovery ([5c1682d](https://github.com/sliamh11/Deus/commit/5c1682dff30b91fd607ec20291ef72efe7f638c1))

## [1.9.0](https://github.com/sliamh11/Deus/compare/v1.8.0...v1.9.0) (2026-04-20)


### Features

* **agent-runner:** token-efficiency instrumentation (logging only, no behavior change) ([#200](https://github.com/sliamh11/Deus/issues/200)) ([385e4da](https://github.com/sliamh11/Deus/commit/385e4da0916a42c7c33553a864e38f6e308c49e8))
* **agent-runner:** token-efficiency tier 1 — prefix unpoisoning + tool-size logging ([#199](https://github.com/sliamh11/Deus/issues/199)) ([7609bce](https://github.com/sliamh11/Deus/commit/7609bce3588800dddec4e628a9e95fc5da64b014))
* **async:** boundary helpers for fire-and-forget, timeout, fanout (PR [#4](https://github.com/sliamh11/Deus/issues/4)/10) ([#216](https://github.com/sliamh11/Deus/issues/216)) ([6595830](https://github.com/sliamh11/Deus/commit/65958302d12933c0b5003c14e495f62828505076))
* **auth:** launchd-driven OAuth token auto-refresh ([#211](https://github.com/sliamh11/Deus/issues/211)) ([d788295](https://github.com/sliamh11/Deus/commit/d7882959e4fff3326f5570e35296e02a87819844))
* **bootstrap:** process-level entry-point harness (PR [#2](https://github.com/sliamh11/Deus/issues/2)/10) ([#215](https://github.com/sliamh11/Deus/issues/215)) ([5b10110](https://github.com/sliamh11/Deus/commit/5b101103eca58b9ba8e6fb42112c0ba644804263))
* **bootstrap:** wire process harness into entry points (PR [#3](https://github.com/sliamh11/Deus/issues/3)/10) ([#219](https://github.com/sliamh11/Deus/issues/219)) ([6187b49](https://github.com/sliamh11/Deus/commit/6187b4909d3b06028e66e01266ff017079198c3f))
* **cli:** add `deus web` subcommand for Claude-in-Chrome ([#209](https://github.com/sliamh11/Deus/issues/209)) ([988d3c6](https://github.com/sliamh11/Deus/commit/988d3c6eaa61021ad40703fdb304b214b789c1c3))
* **demo:** interactive memory-map visualization ([#210](https://github.com/sliamh11/Deus/issues/210)) ([3745a81](https://github.com/sliamh11/Deus/commit/3745a8124e167a16bc7462af4b68d3f97ed05603))
* **errors:** introduce four-class error taxonomy (PR [#1](https://github.com/sliamh11/Deus/issues/1)/10) ([#214](https://github.com/sliamh11/Deus/issues/214)) ([09b9c31](https://github.com/sliamh11/Deus/commit/09b9c312a1d0229595bb7e13d59a74721492b485))
* **scripts:** token-efficiency analyzer (container + CLI) ([#201](https://github.com/sliamh11/Deus/issues/201)) ([abecf2c](https://github.com/sliamh11/Deus/commit/abecf2cf6823fbe91beec591d83aecd0617b3e12))
* **skills:** harden compress + resume against edge cases ([#208](https://github.com/sliamh11/Deus/issues/208)) ([5c13deb](https://github.com/sliamh11/Deus/commit/5c13deb9d6dcc3a0b9ba6a7cb663a02f7103791b))
* **token-bench:** ci gate for CLAUDE.md keyword coverage ([#207](https://github.com/sliamh11/Deus/issues/207)) ([a8c9908](https://github.com/sliamh11/Deus/commit/a8c99085f260563fba35a7ee4d9ae8d8d348b6b1))
* **wardens:** add plan-reviewer + code-reviewer review agents ([#220](https://github.com/sliamh11/Deus/issues/220)) ([e85bb34](https://github.com/sliamh11/Deus/commit/e85bb34f491b713cb2b9d3ae6802e5ac3b4bd6f0))


### Bug Fixes

* **async:** migrate 12 floating-promise HIGHs (PR [#5](https://github.com/sliamh11/Deus/issues/5)/10) ([#221](https://github.com/sliamh11/Deus/issues/221)) ([c79bcda](https://github.com/sliamh11/Deus/commit/c79bcdabac9c8970ab4e7474b0416fafb650c4b1))
* **errors:** structured attribution on 9 connect sites + 10 false-positives documented (PR [#6](https://github.com/sliamh11/Deus/issues/6)/10) ([#223](https://github.com/sliamh11/Deus/issues/223)) ([7a04005](https://github.com/sliamh11/Deus/commit/7a04005d73ff5ef8f6abdeb35e32c0d76cac48d7))
* **evolution:** harden 13 SQL f-string sites with allow-list + regex + ADR (PR [#9](https://github.com/sliamh11/Deus/issues/9)/10) ([#226](https://github.com/sliamh11/Deus/issues/226)) ([d35750b](https://github.com/sliamh11/Deus/commit/d35750b2b568299920d8c7b73752a714990c5b02))
* **lint:** ban process.exit in long-lived libraries + convert pre-bootstrap exits (PR [#7](https://github.com/sliamh11/Deus/issues/7)/10) ([#224](https://github.com/sliamh11/Deus/issues/224)) ([9dc3f56](https://github.com/sliamh11/Deus/commit/9dc3f56df37e0212f34fcb0d618bbe917b728dcf))
* **memory-indexer:** cascade Gemini models on 429 for all gen sites ([#213](https://github.com/sliamh11/Deus/issues/213)) ([ed3ecd4](https://github.com/sliamh11/Deus/commit/ed3ecd4772a02982aac35bf6f672f5d6aa3f2359))
* **scripts:** datetime-TZ policy + migrate 25 naive datetime.now() sites (PR [#8](https://github.com/sliamh11/Deus/issues/8)/10) ([#225](https://github.com/sliamh11/Deus/issues/225)) ([69b84eb](https://github.com/sliamh11/Deus/commit/69b84eb455d72c97d4fd0281aae780ad2e2f9730))
* **scripts:** two NameError bugs in memory_indexer + review_benchmark ([#217](https://github.com/sliamh11/Deus/issues/217)) ([ff736f4](https://github.com/sliamh11/Deus/commit/ff736f46e39999a12dcee76a40a41aa7abd03392))
* **test:** isolate credential-proxy OAuth tests from host keychain ([#212](https://github.com/sliamh11/Deus/issues/212)) ([aeddd53](https://github.com/sliamh11/Deus/commit/aeddd53e1a9f802780dfc5822c8f848b5d57974b))

## [1.8.0](https://github.com/sliamh11/Deus/compare/v1.7.0...v1.8.0) (2026-04-18)


### Features

* **bench:** --label, diff subcommand, token budget-based scoring ([#185](https://github.com/sliamh11/Deus/issues/185)) ([6a91658](https://github.com/sliamh11/Deus/commit/6a916583f657d63818e798c49ec20761dd657ca2))
* **bench:** memory_tree suite adapter ([#189](https://github.com/sliamh11/Deus/issues/189)) ([0247286](https://github.com/sliamh11/Deus/commit/02472869086e4f136969d40322600e40f0095e8c))
* **bench:** MRR in recall, growth alerts in diff, hygiene suite ([#190](https://github.com/sliamh11/Deus/issues/190)) ([c004f81](https://github.com/sliamh11/Deus/commit/c004f818cbece45c6a3f5f9a894eebd305e0da18))
* **bench:** multi-turn token suite ([#188](https://github.com/sliamh11/Deus/issues/188)) ([9036ad4](https://github.com/sliamh11/Deus/commit/9036ad44aaa2b287f03eab7f4ab3982383f8b3ff))
* **bench:** paraphrased-query suite ([#195](https://github.com/sliamh11/Deus/issues/195)) ([6b07207](https://github.com/sliamh11/Deus/commit/6b072078d5279093f7add8644f6ed45ed3b4f164))
* **bench:** reflexion-retrieval-quality suite ([#197](https://github.com/sliamh11/Deus/issues/197)) ([fe087d0](https://github.com/sliamh11/Deus/commit/fe087d0a407bbc27a7e42d075519abda959737f4))
* **bench:** unified benchmarking harness with SQLite store ([#182](https://github.com/sliamh11/Deus/issues/182)) ([e9ec634](https://github.com/sliamh11/Deus/commit/e9ec63423c5af2db266a6d0023f8042af01da5fc))
* **memory-tree:** port evo exp_0006 retrieval policy ([#177](https://github.com/sliamh11/Deus/issues/177)) ([f8062c1](https://github.com/sliamh11/Deus/commit/f8062c1d074ddf1e3c70e8d71fe24bd7181272b9))
* **reactions:** emoji → userSignal foundation (PR A) ([#192](https://github.com/sliamh11/Deus/issues/192)) ([7dcfafa](https://github.com/sliamh11/Deus/commit/7dcfafa4e026dcb6e2bd8021f618d4ce539dafb4))
* **reactions:** wire WhatsApp + Telegram reactions to logReactionSignal (PR B) ([#194](https://github.com/sliamh11/Deus/issues/194)) ([aef7c8f](https://github.com/sliamh11/Deus/commit/aef7c8f1739213cfa3dc55f205c34887a571d809))
* **vault:** slim CLAUDE.md + STATE.md structure + drift-check index coverage ([#202](https://github.com/sliamh11/Deus/issues/202)) ([7d48e38](https://github.com/sliamh11/Deus/commit/7d48e380061d74de979a8ada327e7d3471ef6265))


### Bug Fixes

* **bench:** fail loud on indexer subprocess error ([#183](https://github.com/sliamh11/Deus/issues/183)) ([b4b0c61](https://github.com/sliamh11/Deus/commit/b4b0c6163e072481ffb6d989a0bf813f54b8a03f))
* **config:** fall back to ~/.config/deus/.env for GEMINI_API_KEY ([#184](https://github.com/sliamh11/Deus/issues/184)) ([0307578](https://github.com/sliamh11/Deus/commit/0307578d5d63630a4553ae86e60f40b731f7ef64))
* **embed:** batch + persistent HTTP + keep_alive for hours-long Ollama runs ([#198](https://github.com/sliamh11/Deus/issues/198)) ([246eb0a](https://github.com/sliamh11/Deus/commit/246eb0af16af57faee8156da7dd317355452603f))
* **embeddings:** retry Ollama embed on transient timeout ([#193](https://github.com/sliamh11/Deus/issues/193)) ([f0f4792](https://github.com/sliamh11/Deus/commit/f0f4792b43586c181178ba2e1789f49e9b433733))
* **evolution:** revive 20 failing tests + wire feedback loop ([#186](https://github.com/sliamh11/Deus/issues/186)) ([7f66580](https://github.com/sliamh11/Deus/commit/7f66580013e5d73b5b7b257a44d47d5303c5927f))
* **memory_tree:** flip default to raw retrieve ([#191](https://github.com/sliamh11/Deus/issues/191)) ([4b2d6bf](https://github.com/sliamh11/Deus/commit/4b2d6bf8e6fa55b754bc91308b857101bed1816c))
* **tests:** repair test_maintenance.py autouse fixture ([#187](https://github.com/sliamh11/Deus/issues/187)) ([341032b](https://github.com/sliamh11/Deus/commit/341032b6f35a8484e6c688cd44f29a1e268e1419))


### Performance Improvements

* token optimization — dedupe CLAUDE.md + .claudeignore (~20-35% turn-1 savings) ([#179](https://github.com/sliamh11/Deus/issues/179)) ([f9a39ba](https://github.com/sliamh11/Deus/commit/f9a39ba48392bea60db446b0ab93c7cca8d269ec))

## [1.7.0](https://github.com/sliamh11/Deus/compare/v1.6.0...v1.7.0) (2026-04-15)


### Features

* add Gemini OCR script and shadow check ([#172](https://github.com/sliamh11/Deus/issues/172)) ([49e850a](https://github.com/sliamh11/Deus/commit/49e850a9e6275d4131f729740758d97163cfde03))
* add src/private/ for local-only features ([2321b21](https://github.com/sliamh11/Deus/commit/2321b2191024c5b986ac867c747e736f269b4fd9))
* compression benchmark with fact classification ([#168](https://github.com/sliamh11/Deus/issues/168)) ([6c719ea](https://github.com/sliamh11/Deus/commit/6c719eaeb7ca30ae092ec99ad0bd5967f5357f74))
* **memory-tree:** add auto-discovery + check --auto-fix + coverage hardening ([#174](https://github.com/sliamh11/Deus/issues/174)) ([a113585](https://github.com/sliamh11/Deus/commit/a1135858bbd747ff40ea5dc4c91214962caa180f))
* **memory-tree:** hierarchical cold-start retrieval [WIP] ([#173](https://github.com/sliamh11/Deus/issues/173)) ([ce87b31](https://github.com/sliamh11/Deus/commit/ce87b311d89d835b91d504686308e0e96caa4871))
* multi-agent code review skill with benchmark and safety tests ([#170](https://github.com/sliamh11/Deus/issues/170)) ([fe201e5](https://github.com/sliamh11/Deus/commit/fe201e505241351e29b1962d7b074d6b52db98b1))
* **setup:** require Ollama + background auto-pull of all models ([#175](https://github.com/sliamh11/Deus/issues/175)) ([7ddfac3](https://github.com/sliamh11/Deus/commit/7ddfac33c26217010631f3b34fe306ff9dfc05d3))


### Bug Fixes

* sync compress skills to merge pending tasks instead of replacing ([28eafdc](https://github.com/sliamh11/Deus/commit/28eafdc095ae8fda83759ae47b8972705f8fee19))

## [1.6.0](https://github.com/sliamh11/Deus/compare/v1.5.1...v1.6.0) (2026-04-13)


### Features

* add automated KB maintenance via system scheduler ([#156](https://github.com/sliamh11/Deus/issues/156)) ([ea2202c](https://github.com/sliamh11/Deus/commit/ea2202c489ce34f13489bf5790a3f65989c2b07f))

## [1.5.1](https://github.com/sliamh11/Deus/compare/v1.5.0...v1.5.1) (2026-04-13)


### Bug Fixes

* **ci:** prevent cascading drift failures across sequential PRs ([#150](https://github.com/sliamh11/Deus/issues/150)) ([65c2305](https://github.com/sliamh11/Deus/commit/65c230578404703bc80dd875739735391949f220))
* **memory:** prevent silent data loss in rebuild, decay, and contradiction detection ([#152](https://github.com/sliamh11/Deus/issues/152)) ([9dc87e7](https://github.com/sliamh11/Deus/commit/9dc87e7e43034748bbe2291bf3b5774dae7acf80))
* **memory:** rebuild preserves runtime tables instead of deleting entire DB ([#153](https://github.com/sliamh11/Deus/issues/153)) ([6fd41fe](https://github.com/sliamh11/Deus/commit/6fd41fea8666beedf2b30705b56a13efadc87eb2))

## [1.5.0](https://github.com/sliamh11/Deus/compare/v1.4.0...v1.5.0) (2026-04-12)


### Features

* **memory:** kb phase 1 — temporal invalidation, domain tagging, confidence priors, gaps ([#145](https://github.com/sliamh11/Deus/issues/145)) ([fa560cb](https://github.com/sliamh11/Deus/commit/fa560cbba92e9d017ecfad3da46beab60bf1eaa5))
* **memory:** kb phase 2 — entity graph, contradiction detection, graph wander ([#146](https://github.com/sliamh11/Deus/issues/146)) ([b548e4a](https://github.com/sliamh11/Deus/commit/b548e4a9692fb3d9271b3bb535bc6356f8e8f794))
* **memory:** kb phase 3 — entity articles, compression, query routing ([#147](https://github.com/sliamh11/Deus/issues/147)) ([933d4e2](https://github.com/sliamh11/Deus/commit/933d4e21c461b20efa08a998445e7fc6725300fa))
* **memory:** kb phase 4 — forgetting curves, synthesis, privacy ([#148](https://github.com/sliamh11/Deus/issues/148)) ([cd5f67c](https://github.com/sliamh11/Deus/commit/cd5f67c451e15aa994f6567b455b37dc523fc908))
* **memory:** per-channel privacy configuration ([#149](https://github.com/sliamh11/Deus/issues/149)) ([4b54eb9](https://github.com/sliamh11/Deus/commit/4b54eb9e45bf1a6c903d412d844022ffa2288b3a))


### Bug Fixes

* auto-refresh OAuth tokens with cross-platform credential store fallback ([7179b33](https://github.com/sliamh11/Deus/commit/7179b33782f739d6a3aef871bba52612124af641))

## [1.4.0](https://github.com/sliamh11/Deus/compare/v1.3.0...v1.4.0) (2026-04-11)


### Features

* **evolution:** add routing patterns and context_tokens ([#135](https://github.com/sliamh11/Deus/issues/135)) ([32f1d43](https://github.com/sliamh11/Deus/commit/32f1d43ce5af67872af44f486ba483eb08e36508))
* **patterns:** add pattern verification system ([#138](https://github.com/sliamh11/Deus/issues/138)) ([f614673](https://github.com/sliamh11/Deus/commit/f614673eb01d134d05506e80f846210ffb27c605))
* **skill:** add-listen-hotkey — install deps + whisper model before hotkey setup ([fef98ef](https://github.com/sliamh11/Deus/commit/fef98effa36c6de6304e4ca03f2d9ba7298b0284))


### Bug Fixes

* resolve symlink in SCRIPT_DIR so `deus auth` works from any path ([57cff44](https://github.com/sliamh11/Deus/commit/57cff4452c522c847508d54520f9f63229dbc7de))
* **whatsapp:** event-driven group sync, eliminate redundant bulk fetch ([#134](https://github.com/sliamh11/Deus/issues/134)) ([5043405](https://github.com/sliamh11/Deus/commit/50434050a8566bbc92fef2cea41439e2926bc358))

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
