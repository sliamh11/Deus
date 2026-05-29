---
name: research
description: Multi-stage research pipeline with evidence-quality ratings and citations. Classifies intent, clarifies scope, then routes to shallow (single-pass) or deep (parallel fan-out) research. Composes with Parallel AI MCP when available.
user_invocable: true
triggers:
  - research
  - deep.?research
  - research.+(for|about|on|into)
  - produce.+report
  - deep.?dive.+into
  - write.+brief.+on
  - comprehensive.+analysis
---

# Deep Research Pipeline

Host-side only (Claude Code). See `docs/agent-agnostic-debt.md` AAG-012 for backend parity status.

A 4-stage research pipeline. Classifies research depth, clarifies scope when ambiguous, then routes to shallow or deep retrieval with structured citation-backed output.

**Design pattern: Mediator.** This skill is the central coordinator. Research scouts and brainstormer are independent peers invoked through the Agent tool without cross-coupling. Neither agent knows about the other or about the pipeline stages.

Composes existing infrastructure — does NOT duplicate it:
- **Research scout agents** for evidence-classified source finding (deep path)
- **brainstormer** agent for creative synthesis (when the user wants ideas, not just facts)
- **Parallel AI MCP** for web search when available (graceful fallback to WebSearch)
- **Vault memory** for prior decisions and research
- **NotebookLM** for querying existing notebooks when relevant

## Instructions

When the user invokes `/deep-research <topic>` or a trigger phrase matches:

### Stage 1: Classify intent

Read the query and classify into one of:

| Depth | Signal | Example |
|-------|--------|---------|
| **SHALLOW** | Single fact, narrow scope, recent event, quick comparison | "What's the latest on X?", "Compare A vs B briefly" |
| **DEEP** | Multi-source synthesis, historical overview, regulatory landscape, decision brief, "research X" | "Research the regulatory landscape for Y", "Produce a brief on Z" |
| **CREATIVE** | Solution design, brainstorming, "how could we", exploration of alternatives | "How could we improve X?", "What are creative approaches to Y?" |

State the classification and proceed. Do NOT ask the user to confirm depth — only ask if the topic itself is ambiguous (Stage 2).

### Stage 2: Clarify scope (conditional)

**Skip this stage** if the query is specific enough to research directly. Most queries are.

Only use `AskUserQuestion` when genuinely ambiguous — when researching the wrong scope would waste significant effort:

```
AskUserQuestion:
  question: "Your query could mean several things. Which scope should I research?"
  options:
    - { label: "<interpretation 1>", description: "<what this covers>" }
    - { label: "<interpretation 2>", description: "<what this covers>" }
    - { label: "Both", description: "Research both angles" }
```

Also clarify if the user hasn't indicated:
- **Time horizon** — for queries where recency matters ("last 6 months" vs "all time")
- **Output format** — only if unclear (default: structured report with citations)

### Stage 3: Retrieve (route by depth)

---

#### SHALLOW path

Single-pass retrieval. No subagents. Target: 30-60 seconds.

1. **Vault memory** — check for prior work:
   ```bash
   python3 ~/deus/scripts/memory_tree.py query "<topic keywords>" 2>/dev/null
   ```
   Read top 2-3 results if confidence > 0.4.

2. **Web search** — run 2-3 queries with different phrasings:
   - If Parallel AI is available: prefer `mcp__parallel-search__search` (faster, structured results)
   - If that tool is not registered (tool-not-found error): fall back to `WebSearch`
   - Use `WebFetch` to read the top 2-3 most relevant results in full

3. **Synthesize inline** — produce a concise answer with source citations. Go directly to Stage 4 output format (abbreviated — no evidence taxonomy needed for shallow).

---

#### DEEP path

Parallel fan-out via subagents. Target: 2-5 minutes.

**Step 1: Parallel retrieval — launch in a single message**

Launch these retrieval tasks in parallel using the Agent tool:

**A. Research scout (primary domain):**
```
Agent(subagent_type="general-purpose", model="sonnet"):
  You are a research scout. Find and classify sources with evidence-quality ratings.
  You cast a wide net but NEVER synthesize — list and classify only.

  Research topic: <primary domain framing of the query>
  Focus: primary domain sources — papers, benchmarks, implementations, authoritative references.

  Procedure:
  1. Run 2-3 WebSearch queries with different phrasings
  2. Read top results with WebFetch
  3. Search GitHub (via WebSearch) for relevant implementations
  4. Classify every source using this taxonomy:
     - empirical: peer-reviewed or reproducible experiment
     - benchmark: quantitative comparison on a standard dataset
     - implementation: working code that demonstrates the approach
     - anecdotal: first-person experience without controlled methodology
     - vendor: claims from the entity selling the tool
     - theoretical: formal analysis without empirical validation

  Output for each source (mandatory schema):
  ### <source number>. <source title>
  - **URL**: <url>
  - **Evidence quality**: <label from taxonomy>
  - **Date**: <publication date>
  - **Core claim**: <one sentence>
  - **Relevance**: <why this matters for the research question>
  - **Limitations**: <what this source doesn't cover>

  Minimum 5 sources. Never fabricate URLs. Include a source count by evidence quality at the end.
```

**B. Research scout (adjacent domain):**
```
Agent(subagent_type="general-purpose", model="sonnet"):
  You are a research scout. Find and classify sources with evidence-quality ratings.
  You cast a wide net but NEVER synthesize — list and classify only.

  Research topic: <adjacent domain framing — a related field with transferable insights>
  Focus: cross-pollination from <adjacent field>. Look for techniques, patterns, or solutions
  from this domain that could transfer to the primary question.

  Procedure:
  1. Run 1-2 WebSearch queries in the adjacent domain
  2. Read top results with WebFetch
  3. Search GitHub (via WebSearch) for relevant implementations
  4. Classify every source using the evidence taxonomy:
     empirical | benchmark | implementation | anecdotal | vendor | theoretical

  Output for each source (mandatory schema):
  ### <source number>. <source title>
  - **URL**: <url>
  - **Evidence quality**: <label from taxonomy>
  - **Date**: <publication date>
  - **Core claim**: <one sentence>
  - **Relevance**: <why this matters for the research question>
  - **Limitations**: <what this source doesn't cover>

  Document what was searched even if nothing relevant was found. Include coverage gaps.
```

**C. Vault + internal context:**
```bash
python3 ~/deus/scripts/memory_tree.py query "<topic keywords>" 2>/dev/null
```
Read top results. Also check `~/deus/docs/decisions/INDEX.md` for relevant ADRs.

**D. Parallel AI deep research (optional — only when available AND topic warrants it):**

If Parallel AI MCP is available (tool resolves without error) and the topic is complex enough to justify async research:
```
AskUserQuestion:
  question: "I can also run Parallel AI deep research on this topic (2-5 min, higher cost). Include it?"
  options:
    - { label: "Yes", description: "Adds a comprehensive AI-generated analysis to the source pool" }
    - { label: "No, existing sources are enough", description: "Proceed with web research results only" }
```

If yes: call `mcp__parallel-task__create_task_run` and poll for results. Include the Parallel AI output as one source in the synthesis (with `vendor` evidence quality label since it's AI-generated).

**E. NotebookLM (optional — only when the user has notebooks related to the topic):**

If the topic overlaps with known NotebookLM notebooks:
```
mcp__notebooklm-mcp__notebook_query: query the most relevant notebook
```
Include results as internal source material.

**Step 2: Wait for all parallel tasks to complete.**

Collect all findings. You should now have:
- Primary domain findings (5+ classified sources)
- Adjacent domain findings (additional sources)
- Vault memory hits (prior decisions, research notes)
- Optionally: Parallel AI analysis, NotebookLM results

**Step 3: Synthesize.**

This is where you add value beyond what the research scouts provide. Scouts find and classify — you synthesize, connect, and conclude.

Synthesis rules:
- Every factual claim must cite a specific source from the findings
- When sources disagree, present both positions with their evidence quality labels
- Highlight cross-domain connections (insights from adjacent domain that apply to primary)
- Flag coverage gaps — what the research couldn't find
- Distinguish between what's well-established (multiple `empirical`/`benchmark` sources) and what's speculative (single `anecdotal`/`vendor` source)

---

#### CREATIVE path

Route to brainstormer with the research context.

1. Run the SHALLOW retrieval first (vault + web search) to gather context.
2. Launch brainstormer with the enriched prompt:
   ```
   Agent(subagent_type="brainstormer"):
     Problem statement: <user's query>
     Pre-gathered context: <vault memory hits and web search findings>
     Generate 3-5 ranked solution ideas with effort/impact/risk.
   ```
3. Present brainstormer output directly — it already has a structured format.

---

### Stage 4: Output

#### Shallow output format

```markdown
# Research: <topic>

<2-4 paragraph synthesis answering the query>

## Sources
1. [<title>](<url>) — <one-line summary>
2. ...

## Vault Context
- <relevant prior decisions or research, if any>
```

#### Deep output format

```markdown
# Research Report: <topic>

**Date:** YYYY-MM-DD
**Depth:** Deep | Research time: ~Xm
**Sources reviewed:** N (M empirical, N benchmark, O implementation, P anecdotal, Q vendor)

## Executive Summary

<3-5 sentences capturing the key findings and their confidence level>

## Findings

### <Theme/Section 1>

<Synthesis paragraph with inline citations [1][2]>

### <Theme/Section 2>

<Synthesis paragraph with inline citations [3][4]>

### Cross-Domain Insights

<Connections from adjacent domain research that apply here>

## Evidence Map

| # | Source | Evidence Quality | Core Claim |
|---|--------|-----------------|------------|
| 1 | [<title>](<url>) | empirical | <one sentence> |
| 2 | [<title>](<url>) | benchmark | <one sentence> |
| ... | ... | ... | ... |

## Confidence Assessment

- **High confidence:** <claims backed by multiple empirical/benchmark sources>
- **Medium confidence:** <claims with implementation evidence but limited empirical>
- **Low confidence / Speculative:** <claims from single anecdotal/vendor sources>

## Coverage Gaps

- <what was searched for but not found>
- <domains that might have relevant work but weren't explored>

## Prior Decisions (Vault)

- <relevant ADRs, research notes, or past decisions — with memory path citations>
- <or "No prior decisions found for this topic">
```

#### Creative output format

Use the brainstormer's native output format (ranked ideas with effort/impact/risk table).

### Stage 5: Follow-up

After presenting results, offer next steps via `AskUserQuestion`:

```
AskUserQuestion:
  question: "Research complete. What would you like to do next?"
  options:
    - { label: "Save to vault", description: "Persist this research to Deus memory for future reference" }
    - { label: "Go deeper on a section", description: "Run targeted deep research on a specific finding" }
    - { label: "Done", description: "No further action needed" }
```

**If "Save to vault":** Use the preserve skill to save the research report as a durable memory artifact.

**If "Go deeper":** Ask which section, then re-run the DEEP path scoped to that subtopic.

## Constraints

- NEVER fabricate URLs or citations. Every source must come from actual retrieval results.
- NEVER present AI-generated content (from Parallel AI or your own synthesis) as primary evidence — always label it.
- NEVER skip the evidence quality classification on deep path. Every source gets a label.
- NEVER run the deep path for queries that are clearly shallow — respect the user's time.
- If vault memory has a relevant prior decision that contradicts web sources, flag the conflict explicitly rather than silently favoring either.
- Minimum 5 sources for deep path reports. If fewer found, document what was searched.
- Adjacent domain search is mandatory on deep path, even if it yields nothing — document the attempt.
