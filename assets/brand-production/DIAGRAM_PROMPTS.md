# Architecture Diagram Prompts

Use these prompts to generate architecture diagrams for the Deus README and docs. Each prompt produces one diagram. Style should be consistent across all four.

## Style Guide

- **Colors**: Ember (#E8723A), Flame (#F4A261), Deep Teal (#1B7A6E), Ocean (#2EC4B6), Night (#1A1A2E), Cloud (#F8F9FA)
- **Background**: White or very light (#F8F9FA) — must look good on both GitHub light and dark mode
- **Typography**: Clean sans-serif (Inter, SF Pro, or system default). Labels should be readable at 700px width.
- **Style**: Flat/modern, rounded corners, subtle shadows. No 3D, no skeuomorphism. Thin connecting lines with directional arrows.
- **Output size**: 1400×800px PNG (will be displayed at 700px width in README, so 2x for retina)
- **No text smaller than 14px at display size** (28px at 1400px render size)

---

## 1. Message Flow Diagram

Show how a message travels from the user to a response.

```
Prompt:

Create a clean, modern architecture diagram showing the message flow in an AI assistant system. Use these exact colors: orange (#E8723A, #F4A261) for user/input elements, teal (#1B7A6E, #2EC4B6) for system/processing elements, dark (#1A1A2E) for text, light (#F8F9FA) for background.

The flow is left-to-right with these stages:

1. USER (left side) — show icons for WhatsApp, Telegram, Slack, Discord, and Gmail as input channels
2. Arrow labeled "message" →
3. HOST (Node.js) — a rounded box containing:
   - "Channel Registry" at top
   - "SQLite" in middle
   - "Message Loop" below
   - "Domain Detector" branching off (detects topic via keywords: marketing, engineering, study, etc.)
   - "Reflection Retrieval" step: fetches relevant past reflections and prepends them to prompt
4. Arrow labeled "spawn container" →
5. CONTAINER (Linux VM) — a rounded box with dashed border containing:
   - "Claude Agent SDK" at top
   - Below it, three tool boxes: "Calendar (MCP)", "YouTube (MCP)", "Filesystem"
   - A small label: "reflections prepended to prompt"
   - Another label: "domain tags attached as metadata (not in prompt)"
6. Arrow labeled "response" →
7. Back to USER
8. Below the response arrow, a small "Credential Proxy (:3001)" box with arrow labeled "tokens injected at runtime" pointing into the container

Keep it minimal. No decorative elements. White background. The diagram should be self-explanatory without needing the README text.

Output: 1400×800px PNG, flat style, rounded corners, subtle drop shadows on boxes.
```

---

## 2. Memory Tiers Diagram

Show how the tiered memory system works.

```
Prompt:

Create a clean, modern diagram showing a tiered memory retrieval system. Use these exact colors: orange (#E8723A, #F4A261) for active/hot elements, teal (#1B7A6E, #2EC4B6) for storage elements, dark (#1A1A2E) for text, light (#F8F9FA) for background.

Show three horizontal layers stacked vertically, with the most accessible at the top:

TOP — "Always Loaded" tier (orange accent):
  - "CLAUDE.md" — core identity and preferences
  - "Persona files" — user personality and tastes
  - Label: "Free — loaded every session"

MIDDLE — "Warm" tier (lighter orange):
  - "Last 3 session logs" — retrieved by date
  - "Today's checkpoint" — mid-session state
  - Label: "Free — no embedding call"

BOTTOM — "Cold" tier (teal):
  - "SQLite-vec database" — all indexed sessions
  - "Semantic search + recency boost"
  - "Atomic facts (extracted knowledge)"
  - Label: "1 Gemini embedding call"

On the left side, show a vertical arrow pointing down labeled "Retrieval cost increases".
On the right side, show a vertical arrow pointing down labeled "Scope increases".

At the top, a small "Session Start" trigger box with arrows flowing down into each tier.

Output: 1400×800px PNG, flat style, white background, rounded corners.
```

---

## 3. Evolution / Self-Improvement Loop

Show the feedback loop that scores and improves responses.

```
Prompt:

Create a clean, modern circular diagram showing an AI self-improvement loop. Use these exact colors: orange (#E8723A, #F4A261) for scoring/evaluation, teal (#1B7A6E, #2EC4B6) for improvement/learning, dark (#1A1A2E) for text, light (#F8F9FA) for background.

Show a circular flow with these stages connected by arrows:

1. "User Message" (top) — incoming message
2. → "Retrieve Reflections" — fetch relevant past reflections (with IDs for tracking)
3. → "Agent Response" — Claude generates a response (reflections prepended to prompt)
4. → "User Signal Detection" — detect follow-up feedback ("perfect", "wrong", etc.)
5. → "Judge Scores" (orange) — Ollama or Gemini rates the response (0-1 scale)
6. Decision diamond: "Score threshold?"
   - Score < 0.6 → "Reflexion" (teal) — generates a corrective reflection (deduplicated via L2 distance < 0.4)
   - Score ≥ 0.85 → "Positive Pattern" (teal) — extracts what worked well
7. → "times_helpful" — if retrieved reflections correlate with high scores, mark them as helpful (feedback loop arrow back to step 2)
8. → "Domain Principles" (auto-triggered, 24h cooldown per domain) — accumulated learnings per domain
9. → "DSPy Optimizer" (auto-triggered every 50 new interactions) — optimizes the system prompt. User-praised interactions receive 2x weight.
10. Arrow back to step 3, showing the optimized prompt feeding into the next response

In the center of the loop, show: "Continuous self-improvement" as a label.

Keep it clean and circular. The loop should visually convey continuous improvement. Make the times_helpful feedback arrow visually distinct (dashed or different color) to show it's a secondary feedback path.

Output: 1400×800px PNG, flat style, white background, rounded corners.
```

---

## 4. Container Isolation Diagram

Show the security boundary between host and containers.

```
Prompt:

Create a clean, modern security architecture diagram showing container isolation. Use these exact colors: orange (#E8723A, #F4A261) for trusted/host elements, teal (#1B7A6E, #2EC4B6) for isolated/container elements, red (#E63946) for security boundaries, dark (#1A1A2E) for text, light (#F8F9FA) for background.

Layout — two zones separated by a bold dashed red line labeled "Security Boundary":

LEFT ZONE — "Host" (orange accent):
  - "Node.js Process" box
  - ".env secrets" box (with a lock icon)
  - "Credential Proxy (:3001)" box
  - "SQLite databases" box
  - "Mount Security Validator" box (validates additional mounts against allowlist)
  - "Obsidian vault" box (labeled "optional mount")

RIGHT ZONE — "Container" (teal accent, dashed border):
  - "Claude Agent SDK" box
  - "Mounted: /app/src (read-only)" label
  - "Mounted: /workspace/group (read-write)" label
  - "Mounted: /workspace/global (read-only)" label
  - "Mounted: /workspace/ipc (IPC messages)" label
  - "No direct .env access" label with X mark
  - "No host filesystem access" label with X mark

Arrows crossing the boundary:
  - "Credential Proxy" → Container: arrow labeled "tokens injected at runtime"
  - Container → Host: arrow labeled "IPC response files"

Small callout boxes:
  - "Docker, Apple Container, or Podman runtime"
  - "Each conversation = separate container"
  - "Main group can message any group; non-main restricted to own JID"

Output: 1400×800px PNG, flat style, white background, rounded corners, the security boundary should be visually prominent.
```
