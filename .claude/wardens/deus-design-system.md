# Deus Design System — UX/UI Reference

> Loaded by ux-reviewer and ui-copy-writer wardens alongside their rules files.
> Living document — updated with every UX review, user bug report, and competitive insight.

## Brand Personality

Deus is a **knowledgeable companion**, not a corporate assistant. It understands you, adapts to you, and gets better over time.

- **Tone:** Direct, confident, warm but not bubbly. Like a smart friend who happens to know everything.
- **Error voice:** Honest and helpful. "Something broke" not "An error occurred." Always say what to do next.
- **System messages:** Minimal, informative. No fluff, no emojis, no "Great!". State what happened.

## Visual Design Principles

### Color System
- **Brand:** EMBER (#E8723A), FLAME (#F4A261), DEEP_TEAL (#1B7A6E), OCEAN (#2EC4B6)
- **Semantic:** Good = #4EC990, Warn = #F4A261, Bad = #E85D5D
- **Text:** White on dark, Dim = #6C6C8A, Muted = border color
- **Rule:** Semantic colors carry meaning. Don't use warn-color for decoration.

### Information Density
- **Default: show less.** Details are opt-in (Ctrl+O toggle, expand, drill-down).
- **Status bar:** Most important info only. Each span must earn its space.
- **Chat area:** 2-space indent for all content. No wall-of-text — break with blank lines.

### Typography (Terminal)
- **Monospace only** (terminal constraint). Use spacing and symbols for hierarchy, not font weight.
- **Hebrew:** BiDi visual reorder for display. Force LTR base. Never assume terminal handles RTL natively.

## Interaction Patterns (Codified from Experience)

### Permission Prompts
- **Inline in chat flow**, not popup overlays (decided 2026-05-06, supersedes tui-permission-bridge ADR)
- Always visible: sticky at bottom when user scrolled up
- Show FULL tool input — never truncate
- Keys only active when input field is empty (prevents accidental approval)

### Input Field
- Char-level wrapping (not word-wrap) — cursor must match what user sees
- Dynamic height: up to 2/3 of screen, with cursor-follow scroll
- Ghost text for command completion

### Scrolling
- Auto-scroll when pinned to bottom (following new content)
- User scroll up unpins — new content doesn't fight scroll position
- Account for wrapped lines in scroll calculation (visual rows, not logical lines)

### Streaming / Loading
- Spinner during "thinking" (no text yet)
- "working..." indicator when text has started but agent still active
- Status bar dot: green = idle, yellow = streaming

### Keyboard
- Every shortcut must be discoverable from the UI (status bar, footer, /help)
- Permission keys: Y/N/A only when input empty
- Esc-Esc: quit with "press again" feedback
- Tab/Shift+Tab: cycle between panels

## Known Pain Points (from real usage)

| Issue | Status | Date |
|-------|--------|------|
| Cursor drift on wrapped text with spaces | Fixed (char-wrap) | 2026-05-06 |
| Permission popup shifts chat | Fixed (inline + gated) | 2026-05-06 |
| Scroll stuck on long messages | Fixed (visual row count) | 2026-05-06 |
| Queued messages invisible | Fixed (show text + mark_chat_changed) | 2026-05-06 |
| Mixed Hebrew+English in input | Open — needs reproduction case | 2026-05-06 |
| Text selection undiscoverable | Fixed (Shift+drag hint in status bar) | 2026-05-06 |

## Competitive Benchmarks

### Claude Code CLI
- `/` on empty prompt discovers commands (we match this)
- Ctrl+G opens $EDITOR for long prompts (we don't have this yet)
- Shift+drag for text selection documented prominently (we added hint)
- Permission prompts are first-class UX element

### Warp Terminal
- Block-based output model — Cmd+Shift+C copies one block
- Semantic text selection (URLs, paths as units)
- Separate Terminal and Agent modes

### Zed Editor
- Tool approval as distinct from conversation flow
- Permission controls in dedicated Agent Panel

## Design Decisions Log

| Decision | Rationale | Date |
|----------|-----------|------|
| Inline permissions over popup | Matches Claude Code pattern; prevents chat shift; shows full input | 2026-05-06 |
| Char-wrap over word-wrap for input | Cursor position must match rendered position exactly | 2026-05-06 |
| Dynamic input height (2/3 max) | Hard cap of 10 lines was too restrictive for long prompts | 2026-05-06 |
| Session timer over fake % | Meaningless percentage confused users into thinking it was context usage | 2026-05-06 |
| show_tools=false default | Reduces noise for most users; power users toggle with Ctrl+O | 2026-05-06 |
