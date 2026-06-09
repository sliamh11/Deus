# Changelog

All notable changes to Deus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.20.0](https://github.com/sliamh11/Deus/compare/v1.19.0...v1.20.0) (2026-06-09)


### Features

* **channels:** odysseus /v1/chat/completions web channel (path-a GUI) (LIA-197) ([#732](https://github.com/sliamh11/Deus/issues/732)) ([446594e](https://github.com/sliamh11/Deus/commit/446594eb3e98a2d19b53ea53ddc6354af71dca0b))
* **evolution:** capture available_tools manifest per dispatch (LIA-154) ([#754](https://github.com/sliamh11/Deus/issues/754)) ([812be78](https://github.com/sliamh11/Deus/commit/812be78adc32cdda19af00703722e139da231b42))


### Bug Fixes

* **agent-runner:** harden HookDispatchService listener — loopback bind + token check (LIA-199) ([#752](https://github.com/sliamh11/Deus/issues/752)) ([f6f7557](https://github.com/sliamh11/Deus/commit/f6f7557f47b958dc058ca6dd7a3a710657c6fdd2))
* **pipeline:** render timestamps in machine-local time, not UTC (LIA-124) ([#756](https://github.com/sliamh11/Deus/issues/756)) ([ae3fe13](https://github.com/sliamh11/Deus/commit/ae3fe136c2e8b8a9bf621439278c598aad55255e))

## [1.19.0](https://github.com/sliamh11/Deus/compare/v1.18.0...v1.19.0) (2026-06-09)


### Features

* **warden:** codegraph-first gate on all threads + fresh-instance grep fallback (LIA-121) ([#753](https://github.com/sliamh11/Deus/issues/753)) ([ebb7005](https://github.com/sliamh11/Deus/commit/ebb7005fa19c23b3af3a7a9033b5783bdb4748c9))
* **wardens:** add keystone depth-warden spec (advisory, never-gate) ([#743](https://github.com/sliamh11/Deus/issues/743)) ([13c4bcc](https://github.com/sliamh11/Deus/commit/13c4bcc972b50520ddb8f12135b59ee3125d9a8d))


### Bug Fixes

* **agent-runner:** route PostToolUse consult to dispatchHost + harden deny-rule traversal (LIA-199) ([#747](https://github.com/sliamh11/Deus/issues/747)) ([9a73d32](https://github.com/sliamh11/Deus/commit/9a73d323bdff358e97e5a6ca0abe3169038858f1))
* **drift:** default to merge-base mode inside worktrees (LIA-146) ([#750](https://github.com/sliamh11/Deus/issues/750)) ([5872b31](https://github.com/sliamh11/Deus/commit/5872b3180f87ecdcd57017bf22267360a362c599))

## [1.18.0](https://github.com/sliamh11/Deus/compare/v1.17.0...v1.18.0) (2026-06-09)


### Features

* **agent-runner:** add blocking PreToolUse observer + fix consult host (LIA-197) ([#744](https://github.com/sliamh11/Deus/issues/744)) ([f5bf7ab](https://github.com/sliamh11/Deus/commit/f5bf7ab917fb83127fd51b3f8c71fa5be4a60867))
* **agent-runner:** add PreToolUse gate seam to non-Claude tool loops (LIA-197) ([#740](https://github.com/sliamh11/Deus/issues/740)) ([a8d2490](https://github.com/sliamh11/Deus/commit/a8d24901613092eb17db4580eef5a33dd13b79b6))
* **review:** local cross-family code-review advisory (Gemma 4 12B, never a gate) (LIA-179) ([#733](https://github.com/sliamh11/Deus/issues/733)) ([5197f7a](https://github.com/sliamh11/Deus/commit/5197f7a789a5663cbd3018cc306f7147a866c83c))


### Bug Fixes

* **ci:** repair [@deus-ai](https://github.com/deus-ai) MCP package publish pipeline ([#734](https://github.com/sliamh11/Deus/issues/734)) ([4af574a](https://github.com/sliamh11/Deus/commit/4af574a00274e969d8bfa54a72df06080f86e593))
* **memory:** lazy-resolve vault path so memory_indexer imports without a vault ([#745](https://github.com/sliamh11/Deus/issues/745)) ([2896e64](https://github.com/sliamh11/Deus/commit/2896e644de15882c4347fb761a157d6d79bb23af))
* **memory:** persist detected contradictions to pending_conflicts ([#741](https://github.com/sliamh11/Deus/issues/741)) ([458aa93](https://github.com/sliamh11/Deus/commit/458aa93ed2a025fb286f9106a97426d87df64da7))
* **telemetry:** decouple logInteraction from the output-marker parse (LIA-196) ([#729](https://github.com/sliamh11/Deus/issues/729)) ([6aadb87](https://github.com/sliamh11/Deus/commit/6aadb878f23b4d2f6c8ea9d73a3bc68f25cfcc75))

## [1.17.0](https://github.com/sliamh11/Deus/compare/v1.16.0...v1.17.0) (2026-06-08)


### Features

* **agents:** add code-explorer with codegraph-first enforcement ([#607](https://github.com/sliamh11/Deus/issues/607)) ([18dccb3](https://github.com/sliamh11/Deus/commit/18dccb324ed3b4c90ead06d07baa0b7afe387527))
* **agents:** add planner agent, deny Plan, rename research skill + fix drift gates ([#611](https://github.com/sliamh11/Deus/issues/611)) ([833cda0](https://github.com/sliamh11/Deus/commit/833cda0a7da48bdb1f7298dc385e47c05b960ea4))
* **agents:** deny built-in general-purpose, add custom replacement ([#608](https://github.com/sliamh11/Deus/issues/608)) ([ed142d1](https://github.com/sliamh11/Deus/commit/ed142d1ec754df4a37d08931c63ec0c82d692c6b))
* **auto-merge:** use GitHub auto-merge to eliminate polling loop ([#621](https://github.com/sliamh11/Deus/issues/621)) ([e4425a3](https://github.com/sliamh11/Deus/commit/e4425a3dddd72ea51fad34e2f6b6cf27d8667950))
* **cli:** add `deus sync` + live-command freshness nudge ([#649](https://github.com/sliamh11/Deus/issues/649)) ([9589ddb](https://github.com/sliamh11/Deus/commit/9589ddbc299b07188c408f10e6b2970de03eaf38))
* **cli:** deus init --seed — memory-seed an onboarded project (PR3) ([#697](https://github.com/sliamh11/Deus/issues/697)) ([85aeb36](https://github.com/sliamh11/Deus/commit/85aeb361f5d107cf355f577965b93f5aa5efb521))
* **cli:** deus init|onboard + per-project code_search DBs ([#693](https://github.com/sliamh11/Deus/issues/693)) ([f9fa209](https://github.com/sliamh11/Deus/commit/f9fa2092bf4afceebe7641051968e2b10a77c3d2))
* **diagnostics:** add split re-judge diagnostic ([#627](https://github.com/sliamh11/Deus/issues/627)) ([1607e48](https://github.com/sliamh11/Deus/commit/1607e48962573ac6f9c7a87a4a4e1188e149a0e1))
* **dispatch:** per-task git worktree for isolated agent runs (LIA-106) ([#601](https://github.com/sliamh11/Deus/issues/601)) ([b866b24](https://github.com/sliamh11/Deus/commit/b866b240b83df5665dfcc06e8ebb319e4d278772))
* **events:** add in-process EventBus + agent.done emit (Phase 1) ([#657](https://github.com/sliamh11/Deus/issues/657)) ([68a58ad](https://github.com/sliamh11/Deus/commit/68a58ad77a4b19571318a6728ae2ae7efd5ee9ee))
* **events:** cut over LinearUpdater live — delete inline In-Review write (Phase 1 Step 2) ([#670](https://github.com/sliamh11/Deus/issues/670)) ([abddc96](https://github.com/sliamh11/Deus/commit/abddc964bbbad34c5ed69c40ecb644a680a70b5c))
* **events:** emit pipeline.transition + dry-run ObservabilitySink (Phase 2) ([#660](https://github.com/sliamh11/Deus/issues/660)) ([69f14f6](https://github.com/sliamh11/Deus/commit/69f14f62ef62e08dd5f9cebdb999b7d742a24d2a))
* **events:** phase 3 cutover — ObservabilitySink owns the durable pipeline-event write (LIA-166) ([#700](https://github.com/sliamh11/Deus/issues/700)) ([60fb980](https://github.com/sliamh11/Deus/commit/60fb9802968164ff2f0bd8f40bc39f1f7d62ff29))
* **evolution:** capture structured tool calls for observability (LIA-154 inc[#1](https://github.com/sliamh11/Deus/issues/1)) ([#716](https://github.com/sliamh11/Deus/issues/716)) ([0c59706](https://github.com/sliamh11/Deus/commit/0c5970602225ac975fe45510078afbf32f443bb4))
* **evolution:** clean Gemini-labeled judge benchmark + per-dim fixture harness ([#713](https://github.com/sliamh11/Deus/issues/713)) ([8bcc082](https://github.com/sliamh11/Deus/commit/8bcc0820b2ddd0a116074f57728230f414e10fab))
* **evolution:** opt-in judge model override (EVOLUTION_OLLAMA_JUDGE_MODEL) ([#718](https://github.com/sliamh11/Deus/issues/718)) ([fca0811](https://github.com/sliamh11/Deus/commit/fca081142690ee3cfbe3e68be0d03083bd75f2b5))
* **evolution:** real judge metric + ship-if-better gate for DSPy optimizer (LIA-131 Phase 1) ([#651](https://github.com/sliamh11/Deus/issues/651)) ([d2d6ae5](https://github.com/sliamh11/Deus/commit/d2d6ae5af93004be6e6f8ea0efdfa3119a3c2aee))
* **evolution:** wire DSPy optimized-prompt consumer + LIA-152 sanitizer (LIA-131 Phase 2) ([#653](https://github.com/sliamh11/Deus/issues/653)) ([e652667](https://github.com/sliamh11/Deus/commit/e65266763e67266b31c42753823e516182356e78))
* **facade:** add local hybrid codegraph+grep orphan-sweep ([#650](https://github.com/sliamh11/Deus/issues/650)) ([634117e](https://github.com/sliamh11/Deus/commit/634117e7b76e1d7be65984d3f89bfe54628b3315))
* **integrations:** add Odysseus curated-memory MCP bridge ([#715](https://github.com/sliamh11/Deus/issues/715)) ([1ea081b](https://github.com/sliamh11/Deus/commit/1ea081b5d02e6efe518e15ab5144b323224181f4))
* **judge:** decompose personalization + simplify tool_use scoring ([#628](https://github.com/sliamh11/Deus/issues/628)) ([28e35ba](https://github.com/sliamh11/Deus/commit/28e35ba1f64ae8d05a54f8f6cd90db3b6c7aa70f))
* **judge:** per-dimension scoring formats and diagnostic triage ([#620](https://github.com/sliamh11/Deus/issues/620)) ([ba7dded](https://github.com/sliamh11/Deus/commit/ba7ddeda76ed7481e26e1a1e8a6903743f27eec5))
* **LIA-112:** add Zod runtime validation for IPC JSON parsing (TD-009/010) ([#632](https://github.com/sliamh11/Deus/issues/632)) ([a7860fe](https://github.com/sliamh11/Deus/commit/a7860fea99c692be7af5edc6b868f8f1a970e16b))
* **linear:** add write_allowlist to RoleSpec for post-run file scope enforcement ([#610](https://github.com/sliamh11/Deus/issues/610)) ([ae0169b](https://github.com/sliamh11/Deus/commit/ae0169bc5a290c96a3e918a7a6649977e346d40c))
* **log-review:** evolution logging heartbeat — alarm on silent outage (LIA-195) ([#726](https://github.com/sliamh11/Deus/issues/726)) ([af9958b](https://github.com/sliamh11/Deus/commit/af9958bb0717e94f9f1b7286e01b861276a05b26))
* **memory:** add GLiNER local entity extraction with Gemini fallback ([#618](https://github.com/sliamh11/Deus/issues/618)) ([f18b5c8](https://github.com/sliamh11/Deus/commit/f18b5c8c31c19ca323c3b2930d64c2b2651d3f64))
* **memory:** ollama fallback for atom extraction — keyless --extract/--add (LIA-170) ([#683](https://github.com/sliamh11/Deus/issues/683)) ([1d25b44](https://github.com/sliamh11/Deus/commit/1d25b44227bcb284746ec6f300b2c39c10ad9d96))
* **memory:** replace GLiNER with Gemma4 (Ollama) for entity extraction ([#629](https://github.com/sliamh11/Deus/issues/629)) ([3350d09](https://github.com/sliamh11/Deus/commit/3350d09a4d45ad5dcdb994da1bb7d1a0b38e642e))
* **memory:** wire learned retrieval params into live tree retrieval (LIA-136) ([#644](https://github.com/sliamh11/Deus/issues/644)) ([dccae03](https://github.com/sliamh11/Deus/commit/dccae03482598478eaeddf63a112c0115f9d466d))
* **output-quality-gate:** add User Activation Path check (LIA-102) ([#598](https://github.com/sliamh11/Deus/issues/598)) ([25cfc52](https://github.com/sliamh11/Deus/commit/25cfc529c9394e3f8464f5d938cf9a0b829c0024))
* **pipeline:** add liveness monitoring, health endpoints, and watchdog timers ([#606](https://github.com/sliamh11/Deus/issues/606)) ([c30adec](https://github.com/sliamh11/Deus/commit/c30adec6541285724ce182da70e9f0bf57165889))
* **scripts:** add stale branch cleanup via git cherry detection ([#622](https://github.com/sliamh11/Deus/issues/622)) ([5c2ebdc](https://github.com/sliamh11/Deus/commit/5c2ebdca501eb34c5b79a5978a7c59b8152aa5da))
* **scripts:** merge-train for sequential multi-PR landing (LIA-193) ([#711](https://github.com/sliamh11/Deus/issues/711)) ([2abfeca](https://github.com/sliamh11/Deus/commit/2abfecaece0a9e4521e6db8c9edff06bfcf5fa53))
* **security:** escape XML in Linear-sourced prompt fields (LIA-113) ([#631](https://github.com/sliamh11/Deus/issues/631)) ([37e1fc7](https://github.com/sliamh11/Deus/commit/37e1fc7382a0b1471c391f5379d201dfe8569366))
* **security:** validate Linear identifier before git/worktree path construction (LIA-115) ([#630](https://github.com/sliamh11/Deus/issues/630)) ([0af52c1](https://github.com/sliamh11/Deus/commit/0af52c1cb1c870f717bd134031ee913fe3fcd880))
* **setup:** auto-init codegraph + code-search MCP servers in /setup ([#685](https://github.com/sliamh11/Deus/issues/685)) ([ef4b578](https://github.com/sliamh11/Deus/commit/ef4b578e2886ae560a80a984d58bfd74a8d7a500))
* **skills:** add /add-asana — Asana MCP integration ([#699](https://github.com/sliamh11/Deus/issues/699)) ([c756f5b](https://github.com/sliamh11/Deus/commit/c756f5b6faacd6bcc1c69c5e856f76f4e3c87d65))
* **skills:** add /add-editor to wire Deus memory + evolution into editors ([#688](https://github.com/sliamh11/Deus/issues/688)) ([9fd3109](https://github.com/sliamh11/Deus/commit/9fd31098042fb20f7e33736375e647dd58cf8664))
* **skills:** add /add-understand-anything installer skill ([#698](https://github.com/sliamh11/Deus/issues/698)) ([c13528c](https://github.com/sliamh11/Deus/commit/c13528cbaca448d158b194157f533e98a49d759a))
* **skills:** add /design-to-dev skill for wireframe-driven development ([#655](https://github.com/sliamh11/Deus/issues/655)) ([7b1e4f8](https://github.com/sliamh11/Deus/commit/7b1e4f8d46c8ebe9c1c2c800f4416449c1186f40))
* **skills:** add /handoff skill for structured context transfer (LIA-120) ([#652](https://github.com/sliamh11/Deus/issues/652)) ([596d2c2](https://github.com/sliamh11/Deus/commit/596d2c298dcc3769ef2399a2b1f0acb36aa57fb1))
* **usage:** add `deus usage` token-efficiency + cost report ([#646](https://github.com/sliamh11/Deus/issues/646)) ([e4c94a7](https://github.com/sliamh11/Deus/commit/e4c94a72b80e45803b0996c221483b12c60e683e))
* **warden:** codegraph-first enforcement gate for code-explorer (LIA-121) ([#639](https://github.com/sliamh11/Deus/issues/639)) ([b9e62d2](https://github.com/sliamh11/Deus/commit/b9e62d2eb961928164751bce2dd4298f6664f7a3))
* **warden:** escalating deny message for codegraph-first gate (LIA-129) ([#640](https://github.com/sliamh11/Deus/issues/640)) ([93a94b2](https://github.com/sliamh11/Deus/commit/93a94b2b7e59dbb1718cd38732047814946835ce))
* **wardens:** add opt-in standing autonomy grant for the admin-merge gate ([#695](https://github.com/sliamh11/Deus/issues/695)) ([1df4afd](https://github.com/sliamh11/Deus/commit/1df4afde2de378e97a287e00893adc5e0cbeebed))
* **wardens:** adversarial, evidence-bound review stance with bounded termination ([acb674a](https://github.com/sliamh11/Deus/commit/acb674a2fd4f9a8127daa28c533602ad7186da77))
* **wardens:** auto-populate warden memo with edit context and import graph ([#624](https://github.com/sliamh11/Deus/issues/624)) ([c73d5f3](https://github.com/sliamh11/Deus/commit/c73d5f3c6e7cb71faaaf66af91f557af2acd0f05))
* **wardens:** fold verification evidence into the remote completion-gate ([#691](https://github.com/sliamh11/Deus/issues/691)) ([00d5f3d](https://github.com/sliamh11/Deus/commit/00d5f3d3ff5b21dbbd82e10cb42d973a6b6a03b3))
* **wardens:** isolate gate state per worktree (markers + verdict store) ([#679](https://github.com/sliamh11/Deus/issues/679)) ([736b55a](https://github.com/sliamh11/Deus/commit/736b55abbb43da0306c804f126219d6727089962))
* **wardens:** local cross-family review candidate generator (measure-first, parked) ([7fd1aba](https://github.com/sliamh11/Deus/commit/7fd1abaef7113e3774510c141bc392d9e39502de))


### Bug Fixes

* **agents:** enforce three-stage exploration protocol ([#617](https://github.com/sliamh11/Deus/issues/617)) ([17a74e0](https://github.com/sliamh11/Deus/commit/17a74e0ef216d5cbdbcde087e9c58019afe32854))
* **auth:** harden credential proxy with placeholder detection and startup validation ([#614](https://github.com/sliamh11/Deus/issues/614)) ([9df5937](https://github.com/sliamh11/Deus/commit/9df59379ae617dad443114b228a16d29e7438aca))
* **auth:** proactively refresh OAuth token to prevent overnight 401 ([#668](https://github.com/sliamh11/Deus/issues/668)) ([9c6bce8](https://github.com/sliamh11/Deus/commit/9c6bce8fe510ad32f281d6c2feb597e511096495))
* **auth:** serve freshest OAuth token across file + keychain (pipeline-gate 401) ([#656](https://github.com/sliamh11/Deus/issues/656)) ([ab31576](https://github.com/sliamh11/Deus/commit/ab31576c3056a0dea9b088ed0c9f5a21b9169118))
* **ci:** exempt dependency-only package.json bumps from deployment.md drift ([#727](https://github.com/sliamh11/Deus/issues/727)) ([86b1b32](https://github.com/sliamh11/Deus/commit/86b1b3225abb5f35e868877c1084a0a77eca34a5))
* **cleanup:** convert DEUS_MULTI_AGENT TODO to FIXME with LIA-127 tracking (LIA-116) ([#634](https://github.com/sliamh11/Deus/issues/634)) ([6082d47](https://github.com/sliamh11/Deus/commit/6082d47472eee1538ef4cbb24fca742475d10ce5))
* **code-search:** auto-reindex on pull + auto-calibrate on first index ([#597](https://github.com/sliamh11/Deus/issues/597)) ([a0f884e](https://github.com/sliamh11/Deus/commit/a0f884e9c90c292fa7d77b2e38ca178ea7bf3fe6))
* **code-search:** key index to canonical repo root from worktrees (LIA-189) ([#712](https://github.com/sliamh11/Deus/issues/712)) ([ed51848](https://github.com/sliamh11/Deus/commit/ed51848ba7996ff84783c2d17895a5bc2bba41ea))
* **dispatcher:** bound Linear poll fetch with a timeout (LIA-123) ([#641](https://github.com/sliamh11/Deus/issues/641)) ([f06b6df](https://github.com/sliamh11/Deus/commit/f06b6df46f9a5e01099c39ef6599837b31898e94))
* **dispatcher:** expand patch allowlist with two-tier blocking ([#623](https://github.com/sliamh11/Deus/issues/623)) ([43340ec](https://github.com/sliamh11/Deus/commit/43340ecc68ebe0e6aac7fb7b732c35cbed88f5ed))
* **evolution:** data exfiltration audit + judge payload truncation (LIA-73) ([#613](https://github.com/sliamh11/Deus/issues/613)) ([e19a8c7](https://github.com/sliamh11/Deus/commit/e19a8c71ae5ec13c26ea1bdae179c271cd8c71ac))
* **evolution:** exclude noisy tool_selection module from DSPy optimization (LIA-151) ([#654](https://github.com/sliamh11/Deus/issues/654)) ([623f29c](https://github.com/sliamh11/Deus/commit/623f29c709dffbdc26d711560ab14c3fc0a086b1))
* **evolution:** grade judge personalization via stored-preference injection ([#710](https://github.com/sliamh11/Deus/issues/710)) ([de01df1](https://github.com/sliamh11/Deus/commit/de01df1a7deabea3cbe571a0ab0f757b8fc06bc6))
* **evolution:** sanitize reflection injection with XML escaping and data envelope ([#619](https://github.com/sliamh11/Deus/issues/619)) ([8c9ad55](https://github.com/sliamh11/Deus/commit/8c9ad5536e7c4bb12142fb26f6e3eed4de02c735))
* **gcal:** add auth failure alerting and health check ([#615](https://github.com/sliamh11/Deus/issues/615)) ([c8c3862](https://github.com/sliamh11/Deus/commit/c8c386257b4efe9fb3122cc7a6e932f42be96a39))
* **ipc:** accept null contextStats tokens/pct to unblock dispatch logging (LIA-194) ([#721](https://github.com/sliamh11/Deus/issues/721)) ([3915821](https://github.com/sliamh11/Deus/commit/391582177e5a33313ae790a28a0889f5e9a4a699))
* **judge:** suppress Gemma4 thinking in the Ollama judge call ([#708](https://github.com/sliamh11/Deus/issues/708)) ([1cad54e](https://github.com/sliamh11/Deus/commit/1cad54ed90c53a432732e6ba2fa586779e23676e))
* **LIA-125:** recover Scoped label when enrichment-gate agent uses MCP write path ([#633](https://github.com/sliamh11/Deus/issues/633)) ([ad72b13](https://github.com/sliamh11/Deus/commit/ad72b135ceefa8a9f38c1c2baae54ea90079fc4a))
* **LIA-126:** clear stale Warden: Revise on Done/Cancelled; Warden: Error on gate failures ([#638](https://github.com/sliamh11/Deus/issues/638)) ([6332973](https://github.com/sliamh11/Deus/commit/6332973ed57884fccc6be58233ae662bb29ef509))
* **memory:** force category in Gemma4 atom schema + suppress Ollama thinking ([#707](https://github.com/sliamh11/Deus/issues/707)) ([abb7105](https://github.com/sliamh11/Deus/commit/abb7105b7fdcf6d50023eb7e3084fab60ab5544a))
* **pipeline:** allow bouncer-gate transitions from any state ([60fee48](https://github.com/sliamh11/Deus/commit/60fee48b667b6a822660c9b305ee0732612f90a7))
* **pipeline:** eliminate silent failures across pipeline components (LIA-108) ([#612](https://github.com/sliamh11/Deus/issues/612)) ([3bbd91f](https://github.com/sliamh11/Deus/commit/3bbd91f15d390107ea86f6e7d3eab6c86c4af359))
* **pipeline:** emit moved_done + drop dead gate-event branches (LIA-134, LIA-139) ([#642](https://github.com/sliamh11/Deus/issues/642)) ([7a159dc](https://github.com/sliamh11/Deus/commit/7a159dc5267affb054986b581ec940c72e628889))
* **pipeline:** gate container error halts in Manual Review, not REVISE loop (LIA-169) ([#689](https://github.com/sliamh11/Deus/issues/689)) ([b30440f](https://github.com/sliamh11/Deus/commit/b30440f7c83458e7b593d32cbdb776d9e4232495))
* **pipeline:** prevent duplicate mount, fix label discovery, add proxy auth logging ([#603](https://github.com/sliamh11/Deus/issues/603)) ([53af858](https://github.com/sliamh11/Deus/commit/53af85813eb42337ffe405b2a2840cd31c713e52))
* **pipeline:** revert issue state on REVISE regardless of gate mode ([0f6814f](https://github.com/sliamh11/Deus/commit/0f6814fcffc4c622007ff10b6b8a5e9917420935))
* **pipeline:** track gate infrastructure errors toward escalation ([#616](https://github.com/sliamh11/Deus/issues/616)) ([8f9de64](https://github.com/sliamh11/Deus/commit/8f9de64cf4576cbb734018a336e6d41823cf4eb9))
* **pipeline:** treat container timeout as infra failure → Manual Review, not Backlog (LIA-168) ([#690](https://github.com/sliamh11/Deus/issues/690)) ([6fed8c5](https://github.com/sliamh11/Deus/commit/6fed8c57223477b370ba841ebe6434cb8d3866e7))
* **pipeline:** visible REVISE comments + unblock RfA transitions ([#626](https://github.com/sliamh11/Deus/issues/626)) ([071bbc5](https://github.com/sliamh11/Deus/commit/071bbc5622841188d1999691c46949a9a2f610c6))
* **scripts:** add config.json vault resolution to drift_check and memory_tree ([#702](https://github.com/sliamh11/Deus/issues/702)) ([9960e71](https://github.com/sliamh11/Deus/commit/9960e712b7a0b5a28b9347de0e843032ae6df5bc))
* **security:** harden applyPatchArtifact pipeline (LIA-103, LIA-104, LIA-105) ([#599](https://github.com/sliamh11/Deus/issues/599)) ([ef66dfa](https://github.com/sliamh11/Deus/commit/ef66dfa5ced91a1e649d5284b12bfc186d070b3e))
* **security:** patch CVE-2026-48710 + npm audit fix ([#604](https://github.com/sliamh11/Deus/issues/604)) ([c68150b](https://github.com/sliamh11/Deus/commit/c68150b0468bfe94b7abf8c6393206a190ba8b3e))
* **setup:** gate code-search registration on the mcp server dep ([#687](https://github.com/sliamh11/Deus/issues/687)) ([0a5aca8](https://github.com/sliamh11/Deus/commit/0a5aca8b4aee6c835ae38f33b6e00f6143308888))
* **setup:** template gcal-keepalive plist to remove hardcoded personal paths ([#667](https://github.com/sliamh11/Deus/issues/667)) ([88a569f](https://github.com/sliamh11/Deus/commit/88a569fabe3283bbbb4494dad77fd9fdaf57197a))
* **skills:** make /wardens usable from any project ([#680](https://github.com/sliamh11/Deus/issues/680)) ([c0edd95](https://github.com/sliamh11/Deus/commit/c0edd95b9199643f63ac6e792b67f89114f4c58d))
* **skills:** resolve vault path from cwd .deus/config.json before global config ([#669](https://github.com/sliamh11/Deus/issues/669)) ([16f46b6](https://github.com/sliamh11/Deus/commit/16f46b6e594d77585c6b5e56795d3f69ac956e05))
* **warden:** fix marker invalidation loop on pattern-only commits (LIA-109) ([#609](https://github.com/sliamh11/Deus/issues/609)) ([9d6e44b](https://github.com/sliamh11/Deus/commit/9d6e44bd80e5616782ee3c068a9e5a7ff1c7ba72))
* **warden:** resolve workflow-agent transcripts in codegraph-first gate (RETRO-2026-05-31-01) ([#658](https://github.com/sliamh11/Deus/issues/658)) ([dbfefcc](https://github.com/sliamh11/Deus/commit/dbfefcc15f323d637d0345e0537bf9a5108ef79a))
* **wardens:** admin-merge gate blocks only on required checks (LIA-144) ([#709](https://github.com/sliamh11/Deus/issues/709)) ([ea12877](https://github.com/sliamh11/Deus/commit/ea1287790bd1d7d7ebe6cf4cdcc3c158f949281a))
* **whatsapp:** back off reconnects to stop the reason-408 storm ([#305](https://github.com/sliamh11/Deus/issues/305)) ([#705](https://github.com/sliamh11/Deus/issues/705)) ([9000f5a](https://github.com/sliamh11/Deus/commit/9000f5aa0e89648e8b853e606d7214b8f036fc74))

## [1.16.0](https://github.com/sliamh11/Deus/compare/v1.15.0...v1.16.0) (2026-05-27)


### Features

* **cli:** add multi-model proxy integration via free-claude-code ([#566](https://github.com/sliamh11/Deus/issues/566)) ([07e7c05](https://github.com/sliamh11/Deus/commit/07e7c0511453ead963eeb980e799f17ccb2e9020))
* **compaction:** auto-compact, /context command, llama-cpp overflow guard (LIA-94) ([#549](https://github.com/sliamh11/Deus/issues/549)) ([a08d0e7](https://github.com/sliamh11/Deus/commit/a08d0e71e6f1d6b4a69cf25764542cef634f8cc9))
* **compaction:** extend ContainerOutput with contextStats and compactionEvent (LIA-94) ([#547](https://github.com/sliamh11/Deus/issues/547)) ([5ee7ee0](https://github.com/sliamh11/Deus/commit/5ee7ee05d786204adb760d4b951b32cd0eb159cc))
* **compaction:** post-compact hook and host notifications (LIA-94) ([#548](https://github.com/sliamh11/Deus/issues/548)) ([e2b8ac0](https://github.com/sliamh11/Deus/commit/e2b8ac05b602d659d3ca2e0a8d04b57e10645760))
* **evolution:** add IRT-GRM judge reliability diagnostic ([#555](https://github.com/sliamh11/Deus/issues/555)) ([f29dd70](https://github.com/sliamh11/Deus/commit/f29dd70290046123b781b7af495aa94661c194db))
* **evolution:** add session-correction mining for implicit signals ([#564](https://github.com/sliamh11/Deus/issues/564)) ([ae805d1](https://github.com/sliamh11/Deus/commit/ae805d1d4f2c737aca535356dfe4251a21b5974d))
* **evolution:** add taste hypothesis profile and style consolidation ([#565](https://github.com/sliamh11/Deus/issues/565)) ([9721081](https://github.com/sliamh11/Deus/commit/97210818ea53e901436e18b032e2dee1f955e191))
* **evolution:** add tool-economy mechanical judge dimension ([#570](https://github.com/sliamh11/Deus/issues/570)) ([70ed0c7](https://github.com/sliamh11/Deus/commit/70ed0c7f4218d288f1f2e9121455c7f7920da44b))
* **evolution:** constrained decoding + deterministic judge scoring ([#552](https://github.com/sliamh11/Deus/issues/552)) ([365e32b](https://github.com/sliamh11/Deus/commit/365e32be877d40d4e18ffd48a93b7394fbda58c2))
* **evolution:** fix style reflection categorization and improve style awareness ([#557](https://github.com/sliamh11/Deus/issues/557)) ([c4d80b2](https://github.com/sliamh11/Deus/commit/c4d80b2f56540ac787d659cded258317a9f3c4e0))
* **evolution:** fix zero-signal bug, add has_code tagging, expand user signals ([#563](https://github.com/sliamh11/Deus/issues/563)) ([0769d91](https://github.com/sliamh11/Deus/commit/0769d91fffbfd228dd12b96014e122bcb42e078c))
* **evolution:** mechanical judge dimensions -- gate-audit + completion-honesty + diagnostic ([#572](https://github.com/sliamh11/Deus/issues/572)) ([3d55a52](https://github.com/sliamh11/Deus/commit/3d55a522477b824596557c783f500a52ba2ba665))
* **pipeline:** auto-apply container agent patches on the host ([#588](https://github.com/sliamh11/Deus/issues/588)) ([d080be3](https://github.com/sliamh11/Deus/commit/d080be3c320f45016f9abc997ea9dc3099b6d7ae))
* **pipeline:** detect and handle merge-conflicting PRs (LIA-90) ([#578](https://github.com/sliamh11/Deus/issues/578)) ([1eea7cf](https://github.com/sliamh11/Deus/commit/1eea7cf7639dc6a9db87745b542969f7067fabc9))
* **pipeline:** eliminate SHIP from gate fallback type union ([#553](https://github.com/sliamh11/Deus/issues/553)) ([6af478b](https://github.com/sliamh11/Deus/commit/6af478bea1d13a397502958417a6131327ee2da0))
* **pipeline:** enrichment gate detects already-implemented features (LIA-95) ([#571](https://github.com/sliamh11/Deus/issues/571)) ([f9b754c](https://github.com/sliamh11/Deus/commit/f9b754c0dea204e82206a853e178a3756a132bc2))
* **pipeline:** reconcile stale Agent Working issues (LIA-97) ([#583](https://github.com/sliamh11/Deus/issues/583)) ([d1e8b07](https://github.com/sliamh11/Deus/commit/d1e8b076e42a7a2fabee01471e90cf75478e4779))
* **pipeline:** two-gate architecture -- enrichment + bouncer gates ([#544](https://github.com/sliamh11/Deus/issues/544)) ([5bfafbc](https://github.com/sliamh11/Deus/commit/5bfafbc45861b6c9fdb9f6a3a8e3565c894cd11f))
* **pipeline:** viewport capping for dashboard overflow (LIA-96) ([#582](https://github.com/sliamh11/Deus/issues/582)) ([267f152](https://github.com/sliamh11/Deus/commit/267f152ca75fe9f69472f087ed5b19146b9ddf85))
* **search:** native semantic code search with weighted RRF fusion ([#589](https://github.com/sliamh11/Deus/issues/589)) ([82b4272](https://github.com/sliamh11/Deus/commit/82b4272ee73bbba5329dfd09f3c2d1dee7b5bd4c))
* **test:** add fast-check for property-based FSM testing ([#554](https://github.com/sliamh11/Deus/issues/554)) ([05e7f1a](https://github.com/sliamh11/Deus/commit/05e7f1ab2beb627f085c639fdca17bfd8f9e2fe5))
* **wardens:** add mark-batch + commit window to prevent O(N²) re-review cycles (LIA-98) ([#579](https://github.com/sliamh11/Deus/issues/579)) ([a1977a2](https://github.com/sliamh11/Deus/commit/a1977a2f7bf4d3719c8b3875ded261a631a484a3))
* **wardens:** enforce semantic-search-first in warden specs (LIA-92) ([#575](https://github.com/sliamh11/Deus/issues/575)) ([f55c508](https://github.com/sliamh11/Deus/commit/f55c508c18ce71ccb82b10aba87834317aedb77b))
* **wardens:** improve ai-eng-warden with audit mode, tracing, and 3 new rules ([#546](https://github.com/sliamh11/Deus/issues/546)) ([d4e2672](https://github.com/sliamh11/Deus/commit/d4e2672920d7221b6508a5b0e499d48a74b019a6))
* **wardens:** progressive disclosure + handoff memos ([#556](https://github.com/sliamh11/Deus/issues/556)) ([246fe35](https://github.com/sliamh11/Deus/commit/246fe35323680359cf36ff6a7edded8ddd23d70f))


### Bug Fixes

* **evolution:** harden taste profile with sibling-file arch and injection defense ([#567](https://github.com/sliamh11/Deus/issues/567)) ([e5b4fd0](https://github.com/sliamh11/Deus/commit/e5b4fd09f58b45d75c4a036ef9803b1fab460cad))
* **hooks:** resolve REPO_ROOT from git-common-dir for worktree sessions (LIA-70) ([#580](https://github.com/sliamh11/Deus/issues/580)) ([98c071c](https://github.com/sliamh11/Deus/commit/98c071cbfe8b7069d9f2ed66b23218ce091dfd1a))
* **hooks:** widen plan-review-gate scope to non-git and vault paths (LIA-77) ([#576](https://github.com/sliamh11/Deus/issues/576)) ([158dbf5](https://github.com/sliamh11/Deus/commit/158dbf5ee9b5ce182c794bdbe3e97d36c107432c))
* **mining:** adjacent-only correction mining and batch SQL updates ([#568](https://github.com/sliamh11/Deus/issues/568)) ([b9d6229](https://github.com/sliamh11/Deus/commit/b9d62290affdccc3e74db6c9e548cc53deabbaaa))
* **pipeline:** add logger.warn when gate fallback verdict is applied (LIA-75) ([#550](https://github.com/sliamh11/Deus/issues/550)) ([fc23460](https://github.com/sliamh11/Deus/commit/fc234605b18d6fc5310ffe342b0c0d63690f3595))
* **pipeline:** allow Done transitions from any state ([#587](https://github.com/sliamh11/Deus/issues/587)) ([e3e08f6](https://github.com/sliamh11/Deus/commit/e3e08f62649932b7ba2ca1d5cd5bf6bdaa517919))
* **session:** break stale session retry storm on dead conversations ([#592](https://github.com/sliamh11/Deus/issues/592)) ([d30912e](https://github.com/sliamh11/Deus/commit/d30912e92069e1a63e61f14b4d8ac07fa91f801a))
* use surrogate-safe string slicing for task prompts ([#591](https://github.com/sliamh11/Deus/issues/591)) ([fe75e6d](https://github.com/sliamh11/Deus/commit/fe75e6d89742db7982632cbbc3e55be0f91abb67))
* **user-signal:** remove false-positive signals; gitignore warden backups ([#569](https://github.com/sliamh11/Deus/issues/569)) ([aab2331](https://github.com/sliamh11/Deus/commit/aab233107e9193f5e829c774a61d9710139f965a))
* **warden-hooks:** add --repo-root to mark commands, generalize messages (LIA-77) ([#551](https://github.com/sliamh11/Deus/issues/551)) ([fffaddc](https://github.com/sliamh11/Deus/commit/fffaddc4c742e9994a09f5823c47fa615529dc12))

## [1.15.0](https://github.com/sliamh11/Deus/compare/v1.14.0...v1.15.0) (2026-05-25)


### Features

* agent-native protocol for Python CLIs and MCP servers ([#421](https://github.com/sliamh11/Deus/issues/421)) ([7de0b67](https://github.com/sliamh11/Deus/commit/7de0b67b8d4ca5a5a45e32782921f9a0f76bbfd5))
* **agent-runner:** doom-loop detection for container agents ([#442](https://github.com/sliamh11/Deus/issues/442)) ([33458e7](https://github.com/sliamh11/Deus/commit/33458e7ea00bafa74754b05675ef1382c981e770))
* **agent-runner:** smart tool result summarization ([#441](https://github.com/sliamh11/Deus/issues/441)) ([c38bc37](https://github.com/sliamh11/Deus/commit/c38bc37a4e078860529cfb6f36f8159312aa81f1))
* **agents:** add scope decomposition + self-audit to brainstormer ([#455](https://github.com/sliamh11/Deus/issues/455)) ([68d6998](https://github.com/sliamh11/Deus/commit/68d6998c755632adbda771b4144e9c3fcc13b356))
* **backends:** add llama.cpp as third agent backend ([#452](https://github.com/sliamh11/Deus/issues/452)) ([aeb10f0](https://github.com/sliamh11/Deus/commit/aeb10f082c00ee538e00a6ebeb0e3bc966c7448d))
* **bench:** add explore strategy and --output artifact to context_search suite (LIA-61) ([#533](https://github.com/sliamh11/Deus/issues/533)) ([032a3fb](https://github.com/sliamh11/Deus/commit/032a3fb5e5e89addaa835420645c8a9a88047ed2))
* **bench:** batched Gemini judging for TREC benchmark ([#411](https://github.com/sliamh11/Deus/issues/411)) ([a6215ff](https://github.com/sliamh11/Deus/commit/a6215ff83e1a002a74926c01629605fce573531a))
* **bench:** context_search token benchmark suite LIA-61 ([#497](https://github.com/sliamh11/Deus/issues/497)) ([bcbaae8](https://github.com/sliamh11/Deus/commit/bcbaae8ca8120e4c3fa3667ab408af58e5dd293c))
* **bench:** format sweep — measure tier1_coverage by (format, budget) ([#415](https://github.com/sliamh11/Deus/issues/415)) ([308d3ad](https://github.com/sliamh11/Deus/commit/308d3add28c6b6ad965da993bc2527ce8b3db606))
* **bench:** m1b rule-following judge benchmark infrastructure ([#420](https://github.com/sliamh11/Deus/issues/420)) ([e85cdd4](https://github.com/sliamh11/Deus/commit/e85cdd4d5ff2b1414b4c6556a481baa4f32f363b))
* **bench:** m1c v2 attention-dilution probe (5 arms, recall judge, padding) ([#467](https://github.com/sliamh11/Deus/issues/467)) ([fb8b530](https://github.com/sliamh11/Deus/commit/fb8b5303f037b94189d522aed589fd87202a9b2b))
* **bench:** methodology recall sweep for m4-prereq ([#417](https://github.com/sliamh11/Deus/issues/417)) ([e84bd94](https://github.com/sliamh11/Deus/commit/e84bd9496a5dc1c2a089d4b659eb10e4d366b595))
* **cache:** sqlite-fts5 cache layer with gcal pilot ([#432](https://github.com/sliamh11/Deus/issues/432)) ([7b4457f](https://github.com/sliamh11/Deus/commit/7b4457f6131d598b5b85e06df3a1f1233783d150))
* **cli:** add deus build command and auto-build post-merge hook ([#528](https://github.com/sliamh11/Deus/issues/528)) ([8a5e50f](https://github.com/sliamh11/Deus/commit/8a5e50fcc4b289de0cb0eede02d603c506222084))
* draft 10 agent role prompts (LIA-41) ([#490](https://github.com/sliamh11/Deus/issues/490)) ([4660f6a](https://github.com/sliamh11/Deus/commit/4660f6a2f660ec18a3df90ac6b47de433b137de0))
* **drift:** flip agent-native + description-hints checks to blocking ([#450](https://github.com/sliamh11/Deus/issues/450)) ([bad25eb](https://github.com/sliamh11/Deus/commit/bad25ebbb7258d7e4753111801ccf00b1f6d6ec9))
* enforce semantic-search-first workflow (LIA-60) ([#525](https://github.com/sliamh11/Deus/issues/525)) ([fa02d4b](https://github.com/sliamh11/Deus/commit/fa02d4b0fc8ba371bf3098123a94076dfb8143e9))
* **eval:** judge-LoRA pipeline steps 1+2 (dataset + training driver) ([#466](https://github.com/sliamh11/Deus/issues/466)) ([4d0d9f0](https://github.com/sliamh11/Deus/commit/4d0d9f0e09bc8f9456c6dfc5d5753bb2bf1c8a30))
* **eval:** judge-LoRA step-2.1 smoke-test gate + working defaults ([#469](https://github.com/sliamh11/Deus/issues/469)) ([edcf11f](https://github.com/sliamh11/Deus/commit/edcf11fdb2e2a7c8f312e7a01ea44a5fd0feccbb))
* **eval:** judge-LoRA step-3 post-LoRA bench (Adapter vs Base) ([#470](https://github.com/sliamh11/Deus/issues/470)) ([03f7061](https://github.com/sliamh11/Deus/commit/03f7061e1e52d1adf5fe0fba40dbc3416ea60443))
* **eval:** llama.cpp as eval-side generative + judge provider (Ideas [#2](https://github.com/sliamh11/Deus/issues/2)A + [#2](https://github.com/sliamh11/Deus/issues/2)B) ([#453](https://github.com/sliamh11/Deus/issues/453)) ([2f6b37a](https://github.com/sliamh11/Deus/commit/2f6b37a3f1608851108d40ed244f4438092a25be))
* **hooks:** add HookDispatchService and PreToolUse/PostToolUse observer layer (LIA-42) ([#499](https://github.com/sliamh11/Deus/issues/499)) ([708d388](https://github.com/sliamh11/Deus/commit/708d38828ace13247b3edf348b9e4b1010652964))
* **hooks:** add LinearPendingHook SessionStart sync (LIA-86) ([#526](https://github.com/sliamh11/Deus/issues/526)) ([3d43a66](https://github.com/sliamh11/Deus/commit/3d43a6651032b164d3c5511fa3b354f355721fc2))
* **hooks:** add Prettier format-check gate for git commit ([#518](https://github.com/sliamh11/Deus/issues/518)) ([52ccb58](https://github.com/sliamh11/Deus/commit/52ccb58f54b3cae509e13b5f08e9547cdb124506))
* **hooks:** cold-memory injection + structural checks + placement guard ([#446](https://github.com/sliamh11/Deus/issues/446)) ([7a91c76](https://github.com/sliamh11/Deus/commit/7a91c76f9f11a42cafab9933df85dcd3ea0b3712))
* **hooks:** priority frontmatter field for kind=standard atoms ([#416](https://github.com/sliamh11/Deus/issues/416)) ([979ecd8](https://github.com/sliamh11/Deus/commit/979ecd8ad9ba694948d4f1a1a1370451a7a7684d))
* **hooks:** vault context injection for non-CLI sessions ([#389](https://github.com/sliamh11/Deus/issues/389)) ([b36dc16](https://github.com/sliamh11/Deus/commit/b36dc160ed89978a3d49adcd880a39df3c44a73a))
* **integrations:** add Linear MCP skill and env documentation ([#473](https://github.com/sliamh11/Deus/issues/473)) ([cb7be89](https://github.com/sliamh11/Deus/commit/cb7be8938d05952ab4b0f95f8c3c763af14dfdb3))
* **integrations:** linear MCP phase 2 — container agent access ([#474](https://github.com/sliamh11/Deus/issues/474)) ([742d056](https://github.com/sliamh11/Deus/commit/742d056b62b893774c71008cfff98cb462eec9d7))
* **judge:** promote Gemini to primary + coT rubric for Ollama fallback ([#475](https://github.com/sliamh11/Deus/issues/475)) ([70f8b58](https://github.com/sliamh11/Deus/commit/70f8b58905be24414e0e40a0547504bec5c950ce))
* **LIA-59:** auto-reindex claude-context on PR merge ([#501](https://github.com/sliamh11/Deus/issues/501)) ([fe18f0d](https://github.com/sliamh11/Deus/commit/fe18f0d41a8e7ce25a780e5782146c24aadf272a))
* **LIA-80:** stage micro-bar ([#510](https://github.com/sliamh11/Deus/issues/510)) ([1d1fc9d](https://github.com/sliamh11/Deus/commit/1d1fc9dd3dd6bdef9c4f790bb0bfe06282296fb6))
* **LIA-83:** pipeline dashboard + Linear board UX improvements ([#537](https://github.com/sliamh11/Deus/issues/537)) ([c5163dd](https://github.com/sliamh11/Deus/commit/c5163dddd9ba1568a920d591ebb3c4e8707e2302))
* **linear:** add dispatch service for ticket-driven agent runs ([#477](https://github.com/sliamh11/Deus/issues/477)) ([04abf88](https://github.com/sliamh11/Deus/commit/04abf889d72d08879fad8d8bb50c817efe440008))
* **linear:** add webhook warden gates for Kanban column transitions ([#480](https://github.com/sliamh11/Deus/issues/480)) ([4b0d803](https://github.com/sliamh11/Deus/commit/4b0d8030962fddb1ced329b7240f2ef0c2dc3743))
* **linear:** bidirectional vault-Linear pending sync ([#508](https://github.com/sliamh11/Deus/issues/508)) ([bc994bc](https://github.com/sliamh11/Deus/commit/bc994bc8e10f81594552ded921684484211700ac))
* **linear:** container→GitHub push via tool proxy + auto-merge toggle ([#486](https://github.com/sliamh11/Deus/issues/486)) ([d1045e3](https://github.com/sliamh11/Deus/commit/d1045e33c8b422fc2e2b21cce3828a6f5da5aaa8))
* **linear:** enrichment gates - living document architecture ([#482](https://github.com/sliamh11/Deus/issues/482)) ([60a13e1](https://github.com/sliamh11/Deus/commit/60a13e190f07ee2d8b00632cdd3c7d67465ea702))
* **linear:** live pipeline monitor as default for deus pipeline ([#502](https://github.com/sliamh11/Deus/issues/502)) ([0a84efd](https://github.com/sliamh11/Deus/commit/0a84efddd51bf165f785239e09b567266540e8ba))
* **linear:** pipeline notifications (macOS + Linear comments) ([f2f7560](https://github.com/sliamh11/Deus/commit/f2f756046bbbf1c218e2cc2a99a05a039069aeee))
* **linear:** pipeline observability layer with unified event log and CLI ([#492](https://github.com/sliamh11/Deus/issues/492)) ([a80748d](https://github.com/sliamh11/Deus/commit/a80748d9fd0776ae2196dca2d5026ef5cd5e5fb3))
* **linear:** priority-ordered dispatch with slot throttling ([#487](https://github.com/sliamh11/Deus/issues/487)) ([8476187](https://github.com/sliamh11/Deus/commit/8476187b6caf10c1d76b58b2e879797702c63b53))
* **llama-cpp:** per-surface model env vars + router mode skill support ([#463](https://github.com/sliamh11/Deus/issues/463)) ([e07930e](https://github.com/sliamh11/Deus/commit/e07930edfb261b2c86e6c424dfb962335b9e0d56))
* **memory:** sync-atom-kinds CLI for lightweight kind reconciliation ([#419](https://github.com/sliamh11/Deus/issues/419)) ([470f1b6](https://github.com/sliamh11/Deus/commit/470f1b673d18164c91b28c4fb4ff5ebb2544d7e8))
* **migrations:** post-pull upgrade/migration system ([#436](https://github.com/sliamh11/Deus/issues/436)) ([eebfea0](https://github.com/sliamh11/Deus/commit/eebfea0066bf932a332c6e5b2aee08d6df7331dd))
* **patterns:** enforce agent-native protocol for new CLIs and MCPs ([#434](https://github.com/sliamh11/Deus/issues/434)) ([1d850f0](https://github.com/sliamh11/Deus/commit/1d850f0d10f4099af657511932bebaafd59c37a0))
* **pipeline:** circuit breaker for dispatch re-dispatch loops (LIA-65) ([#530](https://github.com/sliamh11/Deus/issues/530)) ([02ea630](https://github.com/sliamh11/Deus/commit/02ea630ebe86a0a2a250b99ed52fde47dae7977d))
* **pipeline:** container project mount + dispatch agent context ([#523](https://github.com/sliamh11/Deus/issues/523)) ([85bf133](https://github.com/sliamh11/Deus/commit/85bf1335d1e9e51b3a59dba38b9ab7550f84aea7))
* **pipeline:** dashboard overhaul — adaptive width, glyphs, AI status, SLA coloring ([#507](https://github.com/sliamh11/Deus/issues/507)) ([7f6b607](https://github.com/sliamh11/Deus/commit/7f6b607e9a7790e720d9d9f6634d324fb101cdcb))
* **pipeline:** dashboard UX - responsive width, poll, start, move autocomplete ([#542](https://github.com/sliamh11/Deus/issues/542)) ([1fa32c7](https://github.com/sliamh11/Deus/commit/1fa32c7b8da26583a92fc768aa0776f8eb57ac0f))
* **pipeline:** eta column with stage median prediction (LIA-82) ([#511](https://github.com/sliamh11/Deus/issues/511)) ([e2592a3](https://github.com/sliamh11/Deus/commit/e2592a3bb83a03f10bc5ce3f17c6a4aa270f6d47))
* **pipeline:** footer throughput band (LIA-81) ([#513](https://github.com/sliamh11/Deus/issues/513)) ([36f6b09](https://github.com/sliamh11/Deus/commit/36f6b0938cf55cb8fd4a50fc1a4949e2525f6bde))
* **pipeline:** inject failure dossier into re-dispatched agent prompts ([#539](https://github.com/sliamh11/Deus/issues/539)) ([48756db](https://github.com/sliamh11/Deus/commit/48756db0e74044449ec1bee57e09026e5d7a437c))
* **pipeline:** keyboard write-back actions for dashboard ([#517](https://github.com/sliamh11/Deus/issues/517)) ([331b1d8](https://github.com/sliamh11/Deus/commit/331b1d84cdcc335847600c3ca2ac3a29f0785435))
* **pipeline:** pre-dispatch triage skips issues with existing PRs ([#540](https://github.com/sliamh11/Deus/issues/540)) ([bd9a943](https://github.com/sliamh11/Deus/commit/bd9a943ccf154927c6a9d6e9d49510743d87f453))
* **pipeline:** startup sweep for stale gated issues ([#532](https://github.com/sliamh11/Deus/issues/532)) ([a21439c](https://github.com/sliamh11/Deus/commit/a21439c3d064816d30cf626ba5ef09696d7431c7))
* **pipeline:** strict gates, fail-closed fallbacks, intra-agent review ([#522](https://github.com/sliamh11/Deus/issues/522)) ([671be08](https://github.com/sliamh11/Deus/commit/671be0813be191aa7c977b8e70aa34cdac657dfd))
* **pipeline:** webhook-fed SQLite cache replaces API polling ([#515](https://github.com/sliamh11/Deus/issues/515)) ([7b80eef](https://github.com/sliamh11/Deus/commit/7b80eef241835c6bbc7bebec929d450c6b93aa57))
* **pp:** activate --select on MCP wire + drift_check sibling ([#438](https://github.com/sliamh11/Deus/issues/438)) ([ca45387](https://github.com/sliamh11/Deus/commit/ca45387541d2877de9ba4bfcf1a4ca3bc6c53e0e))
* **pp:** agent-native protocol for mcp-x + mcp-whatsapp + drift_check upgrade ([#448](https://github.com/sliamh11/Deus/issues/448)) ([4b41b06](https://github.com/sliamh11/Deus/commit/4b41b06d3b891b90b04131f025524616920c808f))
* **pp:** description hints for 4 channel-core common tools ([#445](https://github.com/sliamh11/Deus/issues/445)) ([c01606b](https://github.com/sliamh11/Deus/commit/c01606b7672ee8ee9db6672fe04370e3c5b4dd4e))
* **proxy:** host-side tool proxy for CLI binary execution ([#431](https://github.com/sliamh11/Deus/issues/431)) ([7de278a](https://github.com/sliamh11/Deus/commit/7de278ac5e63746651f46fe29414fc1db6feae04))
* **resilience:** retry backoff for container runtime + Docker auto-start ([#468](https://github.com/sliamh11/Deus/issues/468)) ([77c271f](https://github.com/sliamh11/Deus/commit/77c271fac74f01408625327cfda27b88476f32fb))
* **retrieval:** swap reranker to multilingual bge-reranker-v2-m3 ([#459](https://github.com/sliamh11/Deus/issues/459)) ([d604287](https://github.com/sliamh11/Deus/commit/d6042870ab1f420d442d3b63b03d0573f5c921d7))
* **rules:** add orchestration rules for agent dispatch pipelines ([#500](https://github.com/sliamh11/Deus/issues/500)) ([8dce69c](https://github.com/sliamh11/Deus/commit/8dce69c43e99e7009c4bf82ffc508badbce0c9d9))
* **scheduler:** add doc-gardener weekly cron agent ([#447](https://github.com/sliamh11/Deus/issues/447)) ([2f96d57](https://github.com/sliamh11/Deus/commit/2f96d57fc7a77505fa8995cddde23b9ed2746829))
* **scoping:** cached codebase map for token-efficient gate scoping (LIA-52) ([#489](https://github.com/sliamh11/Deus/issues/489)) ([80b8beb](https://github.com/sliamh11/Deus/commit/80b8beb1772a741f1eea409bc3caf38cf5234a0d))
* **scripts:** add sync_linear_pending.py with auto-invalidating cache ([#520](https://github.com/sliamh11/Deus/issues/520)) ([9225ca2](https://github.com/sliamh11/Deus/commit/9225ca26ad23f5de5d074c584026d2eefe8e763e))
* **settings:** rewrite_settings() for safe path substitutions (#RETRO-05) ([#427](https://github.com/sliamh11/Deus/issues/427)) ([a93f8b8](https://github.com/sliamh11/Deus/commit/a93f8b89998d9972411328e8c032056834405617))
* **setup:** add vault path to Claude Code sandbox allowlist ([#524](https://github.com/sliamh11/Deus/issues/524)) ([5e924f1](https://github.com/sliamh11/Deus/commit/5e924f152aa549da43b45c15998ecc5b33afdd80))
* **skills:** add /add-claude-context for local semantic code search ([#491](https://github.com/sliamh11/Deus/issues/491)) ([86df7a3](https://github.com/sliamh11/Deus/commit/86df7a33b13ab096367f7725b81d5343e832c939))
* **skills:** add deep-research pipeline ([#485](https://github.com/sliamh11/Deus/issues/485)) ([78af7e3](https://github.com/sliamh11/Deus/commit/78af7e3147ab5c15a29cee4f7ab9cbf7745bdb9f))
* **wardens:** add AI Engineering warden ([#543](https://github.com/sliamh11/Deus/issues/543)) ([9fc7354](https://github.com/sliamh11/Deus/commit/9fc735450b894083e8ca306d29999d28f72a66aa))
* **webhook:** add retry logic to webhook handler (LIA-51) ([#509](https://github.com/sliamh11/Deus/issues/509)) ([e218d02](https://github.com/sliamh11/Deus/commit/e218d023e735793d0f82de2cd760dd66b1c05e09))


### Bug Fixes

* **agent-runner:** exit query loop immediately for scheduled tasks (LIA-65) ([#527](https://github.com/sliamh11/Deus/issues/527)) ([6ca4880](https://github.com/sliamh11/Deus/commit/6ca48808b8bd5b0cb34962fcc0bfd97bbb67d104))
* **auth:** add macOS Keychain fallback for credential checks ([#481](https://github.com/sliamh11/Deus/issues/481)) ([a6e73b9](https://github.com/sliamh11/Deus/commit/a6e73b90bd478967c400e558307f8d4b076740ea))
* **ci:** increase TrueCourse heap to 8GB (OOM on baseline scan) ([#437](https://github.com/sliamh11/Deus/issues/437)) ([1f22752](https://github.com/sliamh11/Deus/commit/1f22752db1538037bdb6097c46929a840d893183))
* **ci:** skip personal-ID check for dependabot PRs ([#476](https://github.com/sliamh11/Deus/issues/476)) ([377c6da](https://github.com/sliamh11/Deus/commit/377c6da1a1db09ed905a14aa1a9a729c5dfbaf41))
* **hooks:** block ExitPlanMode without plan-reviewer SHIP ([#531](https://github.com/sliamh11/Deus/issues/531)) ([657d42d](https://github.com/sliamh11/Deus/commit/657d42d2c49dded4db8ff73ed77c771b36d2f12f))
* **hooks:** hard-block admin-merge when CI is red (#RETRO-06) ([#429](https://github.com/sliamh11/Deus/issues/429)) ([3d32871](https://github.com/sliamh11/Deus/commit/3d3287151c2d8337403d066eedfbdc9b50ea3c4c))
* **hooks:** standards_pack cache invalidates on atom content edits ([#414](https://github.com/sliamh11/Deus/issues/414)) ([9336865](https://github.com/sliamh11/Deus/commit/9336865e8e20b8e96031ca56fcdd6ac8a18e4343))
* **hooks:** standards_pack silently dropping non-negotiable atoms ([#413](https://github.com/sliamh11/Deus/issues/413)) ([6d52b3f](https://github.com/sliamh11/Deus/commit/6d52b3fe974ea7c3b3f12340d4446af4bdeb1572))
* **hooks:** strip post-merge auto-reindex and remove post-commit hook ([#536](https://github.com/sliamh11/Deus/issues/536)) ([ca3502e](https://github.com/sliamh11/Deus/commit/ca3502e24f0dfb35623f7c8dbfe8fad70ab2e805))
* **linear:** add --admin to auto-merge for branch protection ([31b9853](https://github.com/sliamh11/Deus/commit/31b9853e4124bca09fd0f7514193fc8463735859))
* **linear:** clean up warden labels on auto-merge Done transition ([49e33f5](https://github.com/sliamh11/Deus/commit/49e33f58b8d7dbd46345b2ab0d1e2ec7ccbd50eb))
* **linear:** gate label updates + enrichment fallback + observability ([#484](https://github.com/sliamh11/Deus/issues/484)) ([31179d5](https://github.com/sliamh11/Deus/commit/31179d5a0ed39ce9ec9c20bffcf8c2acbebf2f30))
* **linear:** gate running indicator + output parsing + ADR ([#483](https://github.com/sliamh11/Deus/issues/483)) ([0bdee5e](https://github.com/sliamh11/Deus/commit/0bdee5e57e07b98eafbfbbce75ecda20788749e5))
* **linear:** guard Scoped label to agent-readiness-gate ([#505](https://github.com/sliamh11/Deus/issues/505)) ([8ffb5c8](https://github.com/sliamh11/Deus/commit/8ffb5c8fa166aaf11de6deace4d782070fb0dcc3))
* **linear:** initialize database before pipeline CLI queries ([#494](https://github.com/sliamh11/Deus/issues/494)) ([0caba5b](https://github.com/sliamh11/Deus/commit/0caba5b5567ba771caba4b1431e3411748524a90))
* **linear:** lazy eval for LINEAR_AUTO_MERGE + gate allowed_from ([446203d](https://github.com/sliamh11/Deus/commit/446203d3aedb2ce26d1f8d7dee5e3ca0e3cf824c))
* **linear:** re-dispatch agent on CI failure instead of Backlog ([#512](https://github.com/sliamh11/Deus/issues/512)) ([65a6f0d](https://github.com/sliamh11/Deus/commit/65a6f0d262a66df93fa763af0cfee152a03c1684))
* **linear:** retry-aware prompt for scoped issue dispatch ([e8e836f](https://github.com/sliamh11/Deus/commit/e8e836fd0c44122aaea87324586cea52ba70ced2))
* **memory:** auto-sync atom-kinds at SessionStart ([#423](https://github.com/sliamh11/Deus/issues/423)) ([6583656](https://github.com/sliamh11/Deus/commit/65836564343d8bba30c735fa2c2353bf77393c94))
* **memory:** exclude_kinds honored in graph-expansion neighbors ([#418](https://github.com/sliamh11/Deus/issues/418)) ([8f1622f](https://github.com/sliamh11/Deus/commit/8f1622f1605d0e01847e4d68f7e6aec0a55ab934))
* **pipeline:** allow pre-implemented issues to close without gate revert (LIA-85) ([#538](https://github.com/sliamh11/Deus/issues/538)) ([916efec](https://github.com/sliamh11/Deus/commit/916efec6cd28ec05acaf997fdbbc701652a3bc05))
* **pipeline:** alternate screen buffer + rate limit backoff ([#514](https://github.com/sliamh11/Deus/issues/514)) ([d320a4f](https://github.com/sliamh11/Deus/commit/d320a4ffa9168fd9d7ed51b1be64532d107ca8a4))
* **pipeline:** auto-strip warden:skip on Ready for Agent entry ([#541](https://github.com/sliamh11/Deus/issues/541)) ([5e4e2de](https://github.com/sliamh11/Deus/commit/5e4e2de39b0cc655cc9331729831b7bc41ac8bc1))
* **pipeline:** completion-gate before auto-merge + LIA-51 Retry-After patch ([#519](https://github.com/sliamh11/Deus/issues/519)) ([654d2d6](https://github.com/sliamh11/Deus/commit/654d2d6797ce83f3de986fd1bec4cb45612d9aec))
* **pipeline:** dashboard TUI UX overhaul ([#535](https://github.com/sliamh11/Deus/issues/535)) ([3562ec6](https://github.com/sliamh11/Deus/commit/3562ec630a26554bb9ac413bfe26cefc1727c6e8))
* **pipeline:** resolve 6 orchestration bugs causing stuck In Review issues ([#516](https://github.com/sliamh11/Deus/issues/516)) ([c2cfd25](https://github.com/sliamh11/Deus/commit/c2cfd25a85a2b9f94aea3649e28ba04dda4b2b16))
* **pipeline:** strip newlines from event details in TUI ([#521](https://github.com/sliamh11/Deus/issues/521)) ([0914ae0](https://github.com/sliamh11/Deus/commit/0914ae0c9ffc0c722de74936a1638684fa1039dd))
* **rules:** auto-invoke plan-reviewer after plan drafting ([#503](https://github.com/sliamh11/Deus/issues/503)) ([9d81eb3](https://github.com/sliamh11/Deus/commit/9d81eb31da0986fdc435d6a767bb9856b0aaed3f))
* **setup:** exit after step completion to prevent CI hang ([#534](https://github.com/sliamh11/Deus/issues/534)) ([5d9b72b](https://github.com/sliamh11/Deus/commit/5d9b72bb5fbc456b0580a5b0a07f91a1ef019474))
* **skills:** fix add-claude-context Docker setup + security hardening ([#495](https://github.com/sliamh11/Deus/issues/495)) ([59aadfa](https://github.com/sliamh11/Deus/commit/59aadfadba0da1766b7a4bc6accfb9c3b2526ea4))
* **skills:** replace claude -p with in-session Agent in compress retrospective ([#451](https://github.com/sliamh11/Deus/issues/451)) ([83bc571](https://github.com/sliamh11/Deus/commit/83bc571ead5cd4471d988da1ab024b4f77abbf08))
* **wardens:** empty-paths handling in code-review + verification invalidators ([#433](https://github.com/sliamh11/Deus/issues/433)) ([7b5591c](https://github.com/sliamh11/Deus/commit/7b5591c0ca48fc5e2c4a67f36c3973d47f6d500b))
* **wardens:** plan-review gate fires on empty-paths edits in worktrees ([#430](https://github.com/sliamh11/Deus/issues/430)) ([c57c136](https://github.com/sliamh11/Deus/commit/c57c136260b07ee17c5db8c626e20342b8deffa2))
* **wardens:** plan-review-gate stops over-firing on outside-worktree targets ([#439](https://github.com/sliamh11/Deus/issues/439)) ([b481b31](https://github.com/sliamh11/Deus/commit/b481b3144299ffa5368e9a29bbd19233a7a1a118))
* **wardens:** restore worktree-edit exclusion for marker invalidators ([#458](https://github.com/sliamh11/Deus/issues/458)) ([78c69f8](https://github.com/sliamh11/Deus/commit/78c69f852a6b3823ff62b0ab5ba7722ce1fc037f))

## [1.14.0](https://github.com/sliamh11/Deus/compare/v1.13.0...v1.14.0) (2026-05-15)


### Features

* **bench:** add Ollama judge backend to TREC atom benchmark ([#395](https://github.com/sliamh11/Deus/issues/395)) ([8f5d355](https://github.com/sliamh11/Deus/commit/8f5d355611326d0342574bc88a3438bbad7dca23))
* **cli:** add --agents flag ([#386](https://github.com/sliamh11/Deus/issues/386)) ([fa50c6a](https://github.com/sliamh11/Deus/commit/fa50c6ae8f357c0f16bc0497161b67d9e5f59bc5))
* **evolution:** upgrade DSPy to v3, swap MIPROv2 → GEPA optimizer ([#388](https://github.com/sliamh11/Deus/issues/388)) ([d5263f9](https://github.com/sliamh11/Deus/commit/d5263f937cb51cb2773b0c69d6139a16015cd3df))
* **hooks:** auto-compress gate for background sessions ([1a4b3e3](https://github.com/sliamh11/Deus/commit/1a4b3e388a7a50b705b6b26d2b0d2c3f15d29c94))
* **memory:** add TREC benchmark, embedding shootout, implicit feedback tools ([#383](https://github.com/sliamh11/Deus/issues/383)) ([dd98302](https://github.com/sliamh11/Deus/commit/dd9830244e8e6703bf0e7a492c33fb327d751751))
* **memory:** auto-classify promoted atoms via classify_atom() ([#409](https://github.com/sliamh11/Deus/issues/409)) ([a764ae8](https://github.com/sliamh11/Deus/commit/a764ae89228bf76a7b77e9d5d7ad2b224e65e91e))
* **runtime:** per-group agent effort level ([#392](https://github.com/sliamh11/Deus/issues/392)) ([dc34e8d](https://github.com/sliamh11/Deus/commit/dc34e8d457301e32fd3945ef7a8dd2f637e333d1))
* **settings:** add locked merge-write contract for settings.json ([#404](https://github.com/sliamh11/Deus/issues/404)) ([0fd2826](https://github.com/sliamh11/Deus/commit/0fd2826b8dad8364bf51bd60ee8a5134e09a026a))
* **skills:** add session-log cross-link frontmatter fields ([#400](https://github.com/sliamh11/Deus/issues/400)) ([3e438e9](https://github.com/sliamh11/Deus/commit/3e438e9f7cec9dd07ac0fb65f408fc9e00b0a7c5))
* **wardens:** add hook schema citation and smoke test rules ([#401](https://github.com/sliamh11/Deus/issues/401)) ([70a1a5b](https://github.com/sliamh11/Deus/commit/70a1a5bc72cb8970abb1c92b6376d1f05811ac4b))
* **wardens:** portable warden gates with verdict tracking ([#396](https://github.com/sliamh11/Deus/issues/396)) ([d43d3e0](https://github.com/sliamh11/Deus/commit/d43d3e08219a04e921ec0aa670c5e6431e27dc6b))
* **wardens:** wire verification-gate as automatic pre-commit gate ([#405](https://github.com/sliamh11/Deus/issues/405)) ([9181fff](https://github.com/sliamh11/Deus/commit/9181fffabd4e5b39c9a429eeeb977da620b7cd67))


### Bug Fixes

* **bench:** align methodology probes to actual atom filenames ([#410](https://github.com/sliamh11/Deus/issues/410)) ([2f8ce18](https://github.com/sliamh11/Deus/commit/2f8ce18a45ebf174f4618983086031fdbb28adf1))
* **evolution:** lower auto-optimize threshold, optimize all modules ([#391](https://github.com/sliamh11/Deus/issues/391)) ([18ff414](https://github.com/sliamh11/Deus/commit/18ff414e86bc85bff87e3bf8014386ff00492645))
* **hooks:** prevent compress gate from blocking repeatedly ([#397](https://github.com/sliamh11/Deus/issues/397)) ([ca4ce8e](https://github.com/sliamh11/Deus/commit/ca4ce8efce22f68e0a81d51704ff516bd47e7749))
* **hooks:** standards_pack.py resolver references non-existent mt.EXTERNAL_DIR ([#402](https://github.com/sliamh11/Deus/issues/402)) ([9a3a32d](https://github.com/sliamh11/Deus/commit/9a3a32d32811abeb261807658c65059b4735d080))
* **hooks:** use decision:block for Stop hook compress gate ([#398](https://github.com/sliamh11/Deus/issues/398)) ([5bc561e](https://github.com/sliamh11/Deus/commit/5bc561e18efc7ef1c178e1d42070473d6cafd0ac))
* **hooks:** use declarative message for compress gate block ([#399](https://github.com/sliamh11/Deus/issues/399)) ([a169040](https://github.com/sliamh11/Deus/commit/a1690408f7dba1e7cf647c5cbecc6445174f2a41))
* **hooks:** use valid Stop hook output schema for compress gate ([1ff9543](https://github.com/sliamh11/Deus/commit/1ff95434acc5e86ccc179e0f97298cbb14b87b27))
* **wardens:** close TRIVIAL-bypass hole with bg-session gate and audit log ([#407](https://github.com/sliamh11/Deus/issues/407)) ([fd3a603](https://github.com/sliamh11/Deus/commit/fd3a6032fb180a43335899d18acef9285bb40830))

## [1.13.0](https://github.com/sliamh11/Deus/compare/v1.12.0...v1.13.0) (2026-05-13)


### Features

* **codex:** add Warden hook installer ([eafd56b](https://github.com/sliamh11/Deus/commit/eafd56b79ef5e23e638ad89e843a1f9a755526d1))
* **codex:** mirror Claude hook parity ([6309cc4](https://github.com/sliamh11/Deus/commit/6309cc44424b66f7b33e7e817984da6db88fe866))
* **codex:** wardens config, CLI, and interactive TUI ([#302](https://github.com/sliamh11/Deus/issues/302)) ([a7db6d0](https://github.com/sliamh11/Deus/commit/a7db6d01755e437e3c77d3e4f12ed77b64621700))
* **container:** per-group vault partitioning ([#357](https://github.com/sliamh11/Deus/issues/357)) ([e9062d8](https://github.com/sliamh11/Deus/commit/e9062d85c049f583fe02914650473135c78263fd))
* **drift:** add --bump flag to auto-fix drifted patterns ([#295](https://github.com/sliamh11/Deus/issues/295)) ([4a85fdc](https://github.com/sliamh11/Deus/commit/4a85fdc38101c615f732731113b697d1dd69ca97))
* **guardrails:** add pre-ingestion injection scanner ([#337](https://github.com/sliamh11/Deus/issues/337)) ([d4c9e02](https://github.com/sliamh11/Deus/commit/d4c9e02b070a8c0bbda72c9ab7558199c4b95f8a))
* **memory:** add threshold calibration sweep tool (deus sweep) ([#349](https://github.com/sliamh11/Deus/issues/349)) ([a2cc8cf](https://github.com/sliamh11/Deus/commit/a2cc8cf3f9db1eb0202d207131bc68face4a418c))
* **memory:** approach-angle coverage gate + tone-aware generation ([#348](https://github.com/sliamh11/Deus/issues/348)) ([dcad6a7](https://github.com/sliamh11/Deus/commit/dcad6a79c2e59281f17bc862bc81141063f5abbd))
* **memory:** atom retrieval pipeline — approach angles, cross-encoder, BM25 rescue ([#382](https://github.com/sliamh11/Deus/issues/382)) ([aff60be](https://github.com/sliamh11/Deus/commit/aff60be63c57825cd804ffaab19e3163ea3486c8))
* **memory:** auto-compress session to vault before idle reset ([#366](https://github.com/sliamh11/Deus/issues/366)) ([0efd8f5](https://github.com/sliamh11/Deus/commit/0efd8f59d72db7b520c09abb7a39c3270f49e99e))
* **memory:** coherence gate + RRF agreement for gap rescue ([#372](https://github.com/sliamh11/Deus/issues/372)) ([5faf80c](https://github.com/sliamh11/Deus/commit/5faf80cce20a6edadf03d33675708507ef631598))
* **memory:** context-aware retrieval via session concepts ([#327](https://github.com/sliamh11/Deus/issues/327)) ([fbc1566](https://github.com/sliamh11/Deus/commit/fbc1566095e8d453e43f69b753f2180d43684ffe))
* **memory:** fts5 angle injection + entity coverage + benchmark fixes ([#345](https://github.com/sliamh11/Deus/issues/345)) ([c593904](https://github.com/sliamh11/Deus/commit/c5939049a5a184cec2e91c44611d543983a809bb))
* **memory:** two-stage atom fallback when tree abstains ([#350](https://github.com/sliamh11/Deus/issues/350)) ([a409f51](https://github.com/sliamh11/Deus/commit/a409f51e7bda0d95ea2bd6bab2966d217f188ff7))
* **memory:** two-tier atom system with persistent methodology standards ([#380](https://github.com/sliamh11/Deus/issues/380)) ([a0f967f](https://github.com/sliamh11/Deus/commit/a0f967f2b861310e9da083a6ac65575fc9efa155))
* **multi-agent:** thin orchestrator with tiered parallel dispatch ([#340](https://github.com/sliamh11/Deus/issues/340)) ([512774f](https://github.com/sliamh11/Deus/commit/512774fd973d58a6d258f3781673dc72f28ad5d4))
* **multi-agent:** thin orchestrator with tiered parallel dispatch ([#342](https://github.com/sliamh11/Deus/issues/342)) ([7fa62f7](https://github.com/sliamh11/Deus/commit/7fa62f72d23fd5e51a9011fdfefcfe0f2cecdc43))
* **security:** add parry-guard setup for host Claude Code sessions ([#334](https://github.com/sliamh11/Deus/issues/334)) ([f438ef9](https://github.com/sliamh11/Deus/commit/f438ef9d22b05dbac31cb3e2d06e520799d78377))
* **security:** mcp action audit trail ([#326](https://github.com/sliamh11/Deus/issues/326)) ([8c5a961](https://github.com/sliamh11/Deus/commit/8c5a961ad8157f9b6ee41b987e5cbc2582666384))
* **security:** per-group proxy tokens ([#325](https://github.com/sliamh11/Deus/issues/325)) ([671a62b](https://github.com/sliamh11/Deus/commit/671a62baa58309c96a348ee4dfc032247fb90f0c))
* **solutions:** add structured lesson capture with bug-track and knowledge-track schemas ([#336](https://github.com/sliamh11/Deus/issues/336)) ([5eded26](https://github.com/sliamh11/Deus/commit/5eded2627d459a39fd3574c04dde5590f52f9115))
* **startup:** config-driven vault auto-loading + token efficiency ([#303](https://github.com/sliamh11/Deus/issues/303)) ([d8211fc](https://github.com/sliamh11/Deus/commit/d8211fc0ba3f3115d535dbf74ba3a938463698b1))
* **tui:** add @-file mentions with path autocomplete ([#374](https://github.com/sliamh11/Deus/issues/374)) ([0a0b3de](https://github.com/sliamh11/Deus/commit/0a0b3de0c738441e457d5848f18f5b8c048559c1))
* **tui:** add /copy command for code blocks ([#360](https://github.com/sliamh11/Deus/issues/360)) ([9ce3fdc](https://github.com/sliamh11/Deus/commit/9ce3fdc63704ee0090e94fa09093db6ff4db6bca))
* **tui:** add /recap session summary command ([#362](https://github.com/sliamh11/Deus/issues/362)) ([9776aa6](https://github.com/sliamh11/Deus/commit/9776aa6966948ee6f8d99d678b7950cc76ad283f))
* **tui:** add /rewind command to rewind conversation ([#373](https://github.com/sliamh11/Deus/issues/373)) ([394ef23](https://github.com/sliamh11/Deus/commit/394ef2318f79dbd3c086aa71c2b40dfaa8579dcd))
* **tui:** add blockquotes, numbered lists, and links ([#375](https://github.com/sliamh11/Deus/issues/375)) ([98b90b1](https://github.com/sliamh11/Deus/commit/98b90b106a396451f0850726602d09425b9f5c37))
* **tui:** add context alerts and desktop notifications ([#363](https://github.com/sliamh11/Deus/issues/363)) ([fc296a4](https://github.com/sliamh11/Deus/commit/fc296a44c30064c31998344e7fd9cd891840f6fb))
* **tui:** add Ctrl+F search in output ([#376](https://github.com/sliamh11/Deus/issues/376)) ([f746b6a](https://github.com/sliamh11/Deus/commit/f746b6af3827089a07ff51862add9237c09785ea))
* **tui:** add Ctrl+R reverse history search ([#361](https://github.com/sliamh11/Deus/issues/361)) ([fa71f17](https://github.com/sliamh11/Deus/commit/fa71f17deb2568c967ca1a62c793d9eb076cceab))
* **tui:** add markdown table rendering ([#377](https://github.com/sliamh11/Deus/issues/377)) ([024a2cf](https://github.com/sliamh11/Deus/commit/024a2cf2dda1a07939576f722c77e6f8be00de69))
* **tui:** add per-token syntax highlighting in code blocks ([#364](https://github.com/sliamh11/Deus/issues/364)) ([3af259d](https://github.com/sliamh11/Deus/commit/3af259df2ccc7d90a8c722c9ad3584a840d7e46b))
* **tui:** add Tasks panel with scheduled task dashboard ([#328](https://github.com/sliamh11/Deus/issues/328)) ([0f3ab80](https://github.com/sliamh11/Deus/commit/0f3ab809366056a0798e7749f7ceb38240a12f61))
* **tui:** clipboard image paste via Ctrl+V ([#321](https://github.com/sliamh11/Deus/issues/321)) ([a02dc1a](https://github.com/sliamh11/Deus/commit/a02dc1afa7e3f8ff8053993a70268af62a9b78b6))
* **tui:** context parity, subprocess visualization, and platform layer ([#308](https://github.com/sliamh11/Deus/issues/308)) ([ddb5ca5](https://github.com/sliamh11/Deus/commit/ddb5ca52fd76f5cc10ed887bacac59d1f80fa962))
* **tui:** dynamic terminal tab title with braille icons ([#319](https://github.com/sliamh11/Deus/issues/319)) ([77037b3](https://github.com/sliamh11/Deus/commit/77037b38e05e674d0bdb2ad777120b08da582839))
* **tui:** implement 15 UX improvements from v2 audit ([#341](https://github.com/sliamh11/Deus/issues/341)) ([7c91fab](https://github.com/sliamh11/Deus/commit/7c91fab468088fa1a43685cfbad8507e94a6a29d))
* **tui:** parallel agent orchestration + permission bridge ([#316](https://github.com/sliamh11/Deus/issues/316)) ([e03d73c](https://github.com/sliamh11/Deus/commit/e03d73c25845f544a7774076f91ae780dfa116b1))
* **tui:** permission management with mode selector, tool allowlists, and denial feedback ([#310](https://github.com/sliamh11/Deus/issues/310)) ([862df4d](https://github.com/sliamh11/Deus/commit/862df4d8d861f46bd51f524d0bc3469a1fb27715))
* **tui:** ratatui terminal UI with multi-backend model system ([#304](https://github.com/sliamh11/Deus/issues/304)) ([916ea80](https://github.com/sliamh11/Deus/commit/916ea805bd56fe766a44443efaf26b7f3cb24874))
* **tui:** reduce input chrome and add message spacing ([#359](https://github.com/sliamh11/Deus/issues/359)) ([8cfbc31](https://github.com/sliamh11/Deus/commit/8cfbc3115eca6488c4883b953f85add8ae1a2992))
* **tui:** rewrite inline markdown as single-pass parser ([#378](https://github.com/sliamh11/Deus/issues/378)) ([7817a7c](https://github.com/sliamh11/Deus/commit/7817a7ccda29a43de30766dca58dfe5d33980429))
* **tui:** session lifecycle, bounded transcripts, dynamic effort, spawn hints ([#315](https://github.com/sliamh11/Deus/issues/315)) ([c297f69](https://github.com/sliamh11/Deus/commit/c297f6901a9b1936b14556a65ac7fe37346ff4d7))
* **tui:** theme system, braille logo, auto-compact ([#317](https://github.com/sliamh11/Deus/issues/317)) ([b5cbc08](https://github.com/sliamh11/Deus/commit/b5cbc08f03975b028a168ebe294df7172ab59cc1))
* **tui:** ux v2 copy-writer improvements + editor/clipboard ([#353](https://github.com/sliamh11/Deus/issues/353)) ([a9aedf4](https://github.com/sliamh11/Deus/commit/a9aedf430b2af4e09a50f1e0cd78d37ca94a75d8))
* **wardens:** add /wardens settings skill and config ([#300](https://github.com/sliamh11/Deus/issues/300)) ([fcfcccd](https://github.com/sliamh11/Deus/commit/fcfcccdaa74675bacbec11f08e91d8c16d696915))
* **wardens:** add STRIDE checklists to threat-modeler ([#324](https://github.com/sliamh11/Deus/issues/324)) ([d14e778](https://github.com/sliamh11/Deus/commit/d14e7780088006e62db5016714cbef9bfd673e80))
* **wardens:** generalized warden learning loop ([#339](https://github.com/sliamh11/Deus/issues/339)) ([d889dd4](https://github.com/sliamh11/Deus/commit/d889dd4c7c79690867765b25ffbcc86c036e003a))
* **wardens:** warden ecosystem + retro-driven rules ([#358](https://github.com/sliamh11/Deus/issues/358)) ([cdbb3c2](https://github.com/sliamh11/Deus/commit/cdbb3c24a515fa923e2ec2cb938a84b87be7f3a8))
* **wardens:** wire session-retrospective auto-trigger into /compress ([#286](https://github.com/sliamh11/Deus/issues/286)) ([9a04ec6](https://github.com/sliamh11/Deus/commit/9a04ec600e5f0eaf0c2e1d7d0babf8abef967583))


### Bug Fixes

* **ci:** add --no-stash to truecourse analyze in CI workflow ([#343](https://github.com/sliamh11/Deus/issues/343)) ([850e287](https://github.com/sliamh11/Deus/commit/850e28749d52df044877fe94fe65f4e669d3f551))
* **codex:** require approval for admin PR merges ([ad9d03b](https://github.com/sliamh11/Deus/commit/ad9d03b2c19d12da3d67828254b2142aee58989a))
* **drift:** honor governs: frontmatter in coverage check ([#290](https://github.com/sliamh11/Deus/issues/290)) ([f70ffc7](https://github.com/sliamh11/Deus/commit/f70ffc75420e638c5925591c44def5bbc44e5843))
* **memory-indexer:** topic-diverse --recent selection ([#323](https://github.com/sliamh11/Deus/issues/323)) ([c7f5ae3](https://github.com/sliamh11/Deus/commit/c7f5ae320761be8552f01e9b2dee21052b2f03ae))
* **memory:** add deus-memory MCP launcher ([4d9027c](https://github.com/sliamh11/Deus/commit/4d9027c9505ec6b46c6802453316d9c917bbef6a))
* **memory:** disable atom fallback by default ([#351](https://github.com/sliamh11/Deus/issues/351)) ([0da350e](https://github.com/sliamh11/Deus/commit/0da350eef866177488871ec445aa6f1f775301be))
* **memory:** make STATE.md on-demand and fix compress fuzzy matching ([#296](https://github.com/sliamh11/Deus/issues/296)) ([327530f](https://github.com/sliamh11/Deus/commit/327530f3d141c0134607a4b6f3fe7cf28791da61))
* **rules:** add warden REVISE loop + quality-over-speed rules ([#346](https://github.com/sliamh11/Deus/issues/346)) ([d62ba3e](https://github.com/sliamh11/Deus/commit/d62ba3e79ab02bc981a35946a85f5fafbe611c56))
* **setup:** add 3s timeout to commandExists execSync ([#338](https://github.com/sliamh11/Deus/issues/338)) ([bcc23b1](https://github.com/sliamh11/Deus/commit/bcc23b168065f3e88b63a0b81eecc6d2fa553715))
* **tui:** enter sends exact-match commands, deduplicate model names ([#311](https://github.com/sliamh11/Deus/issues/311)) ([d5dfb71](https://github.com/sliamh11/Deus/commit/d5dfb711684e99443367a787d90ba95e0b2e3d2b))
* **tui:** remove SetTitle — Ghostty shell integration overrides it ([#322](https://github.com/sliamh11/Deus/issues/322)) ([79823f5](https://github.com/sliamh11/Deus/commit/79823f50d5e6623c32674273e1e668b36763d298))
* **tui:** replace /recap with automatic idle recap ([#365](https://github.com/sliamh11/Deus/issues/365)) ([d6dc2c9](https://github.com/sliamh11/Deus/commit/d6dc2c901f4cee6c8592c85009252121873caa72))
* **tui:** resolve clippy collapsible_if lint ([#379](https://github.com/sliamh11/Deus/issues/379)) ([0c5af92](https://github.com/sliamh11/Deus/commit/0c5af922bb2b245f7abb240e0633d9fbafbebe32))
* **tui:** write tab title to stdout for proper flush ([#320](https://github.com/sliamh11/Deus/issues/320)) ([58acfdd](https://github.com/sliamh11/Deus/commit/58acfdd9f35aa08210cf789820c44ec7d6f97f44))
* **wardens:** revert worktree exclusion from plan-review gate ([#293](https://github.com/sliamh11/Deus/issues/293)) ([9225751](https://github.com/sliamh11/Deus/commit/92257517acb4b43cf0ffb4ca819d7fc56eb3b439))

## [1.12.0](https://github.com/sliamh11/Deus/compare/v1.11.0...v1.12.0) (2026-04-28)


### Features

* **cli:** support chrome_default config flag ([#282](https://github.com/sliamh11/Deus/issues/282)) ([2f797ed](https://github.com/sliamh11/Deus/commit/2f797ed38d742951d8cf53b56b60b50ea5cc86e8))
* **evolution:** add parameter optimizer for memory retrieval ([#281](https://github.com/sliamh11/Deus/issues/281)) ([a55593c](https://github.com/sliamh11/Deus/commit/a55593cb468126dc08c4ff4cfd899fbed5feb18d))

## [1.11.0](https://github.com/sliamh11/Deus/compare/v1.10.0...v1.11.0) (2026-04-28)


### Features

* **memory:** add memory_query.py shared retrieval module ([#274](https://github.com/sliamh11/Deus/issues/274)) ([b2b087c](https://github.com/sliamh11/Deus/commit/b2b087c9e3f452aa0d86471ad2f3197d16a37207))
* **memory:** cross-interface memory parity (Phases 2-5) ([#280](https://github.com/sliamh11/Deus/issues/280)) ([78b317e](https://github.com/sliamh11/Deus/commit/78b317e9711c7a98f34cc3090a7aaa55cc40693c))
* **memory:** group cmd_learnings by category ([#265](https://github.com/sliamh11/Deus/issues/265)) ([891fdc3](https://github.com/sliamh11/Deus/commit/891fdc3076384032f4cab4901df169117ba54890))


### Bug Fixes

* **security:** harden exec calls, proxy bind validation, and token redaction ([090261c](https://github.com/sliamh11/Deus/commit/090261cd81cd95267bdb22cb8d19b7cdd3d83afb))

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
