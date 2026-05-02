use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};

use crate::app::{App, MessageBlock, COMMANDS};
use crate::bidi;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let layout = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .split(area);

    render_messages(frame, app, layout[0]);
    render_input(frame, app, layout[1]);

    if app.has_suggestions() {
        render_suggestions(frame, app, layout[1]);
    }
}

fn render_markdown_line(text: &str, in_code_block: bool, in_diff: bool) -> (Line<'static>, bool, bool) {
    let mut code_block = in_code_block;
    let mut diff = in_diff;
    let text = bidi::visual_reorder(text);

    if text.starts_with("```") {
        let lang = text.trim_start_matches('`').trim();
        if code_block || diff {
            code_block = false;
            diff = false;
            return (Line::from(Span::styled("  ───", theme::muted())), code_block, diff);
        }
        if lang == "diff" {
            diff = true;
            return (Line::from(Span::styled("  ─── diff", theme::muted())), code_block, diff);
        }
        code_block = true;
        let label = if lang.is_empty() { "───".to_string() } else { format!("─── {}", lang) };
        return (Line::from(Span::styled(format!("  {}", label), theme::muted())), code_block, diff);
    }

    if diff {
        let style = if text.starts_with('+') {
            theme::diff_add()
        } else if text.starts_with('-') {
            theme::diff_del()
        } else if text.starts_with("@@") {
            theme::diff_hunk()
        } else {
            theme::dim()
        };
        return (Line::from(Span::styled(format!("  │ {}", text), style)), code_block, diff);
    }

    if code_block {
        return (Line::from(Span::styled(
            format!("  │ {}", text),
            theme::code(),
        )), code_block, diff);
    }

    if text.contains("**") {
        let mut spans: Vec<Span<'static>> = vec![Span::raw("  ")];
        let mut remaining = text.to_string();
        while let Some(start) = remaining.find("**") {
            if start > 0 {
                spans.push(Span::raw(remaining[..start].to_string()));
            }
            remaining = remaining[start + 2..].to_string();
            if let Some(end) = remaining.find("**") {
                spans.push(Span::styled(remaining[..end].to_string(), theme::bold()));
                remaining = remaining[end + 2..].to_string();
            } else {
                spans.push(Span::raw("**".to_string()));
            }
        }
        if !remaining.is_empty() {
            spans.push(Span::raw(remaining));
        }
        return (Line::from(spans), code_block, diff);
    }

    if text.contains('`') {
        let mut spans: Vec<Span<'static>> = vec![Span::raw("  ")];
        let mut remaining = text.to_string();
        while let Some(start) = remaining.find('`') {
            if start > 0 {
                spans.push(Span::raw(remaining[..start].to_string()));
            }
            remaining = remaining[start + 1..].to_string();
            if let Some(end) = remaining.find('`') {
                spans.push(Span::styled(
                    remaining[..end].to_string(),
                    theme::code(),
                ));
                remaining = remaining[end + 1..].to_string();
            } else {
                spans.push(Span::raw("`".to_string()));
            }
        }
        if !remaining.is_empty() {
            spans.push(Span::raw(remaining));
        }
        return (Line::from(spans), code_block, diff);
    }

    if text.starts_with("## ") {
        return (Line::from(Span::styled(
            format!("  {}", &text[3..]),
            theme::heading2(),
        )), code_block, diff);
    }
    if text.starts_with("# ") {
        return (Line::from(Span::styled(
            format!("  {}", &text[2..]),
            theme::heading1(),
        )), code_block, diff);
    }

    if text.starts_with("- ") || text.starts_with("* ") {
        return (Line::from(vec![
            Span::raw("  "),
            Span::styled("• ", theme::bullet()),
            Span::raw(text[2..].to_string()),
        ]), code_block, diff);
    }

    (Line::from(format!("  {}", text)), code_block, diff)
}

fn render_welcome(lines: &mut Vec<Line<'static>>) {
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::raw("   "),
        Span::styled("▄▀▀▄", Style::default().fg(theme::EMBER)),
        Span::raw("  "),
        Span::styled("D E U S", theme::accent_bold()),
    ]));
    lines.push(Line::from(vec![
        Span::raw("   "),
        Span::styled("▀▄▄▀", Style::default().fg(theme::EMBER)),
    ]));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "  Type a message or / for commands.",
        theme::dim(),
    )));
    lines.push(Line::from(""));
}

fn render_messages(frame: &mut Frame, app: &App, area: Rect) {
    let mut lines: Vec<Line> = Vec::new();

    for msg in &app.chat_messages {
        if msg.role == "system" && msg.content.starts_with("Welcome to Deus") {
            render_welcome(&mut lines);
            continue;
        }

        if msg.role == "assistant" && !msg.blocks.is_empty() {
            for block in &msg.blocks {
                match block {
                    MessageBlock::Thinking(text) => {
                        let preview: String = text.chars().take(80).collect();
                        lines.push(Line::from(vec![
                            Span::styled(" ⟡ ", theme::muted()),
                            Span::styled(
                                if preview.len() < text.len() { format!("{}...", preview) } else { preview },
                                theme::thinking(),
                            ),
                        ]));
                    }
                    MessageBlock::ToolUse { tool, detail } => {
                        lines.push(Line::from(vec![
                            Span::styled(" ▸ ", Style::default().fg(theme::FLAME)),
                            Span::styled(tool.clone(), theme::tool_name()),
                            Span::styled(format!(" {}", detail), theme::tool_detail()),
                        ]));
                    }
                    MessageBlock::Text(text) => {
                        let mut in_code = false;
                        let mut in_diff = false;
                        for line in text.lines() {
                            let (rendered, new_code, new_diff) = render_markdown_line(line, in_code, in_diff);
                            in_code = new_code;
                            in_diff = new_diff;
                            lines.push(rendered);
                        }
                    }
                }
            }
        } else if msg.role == "user" {
            for line in msg.content.lines() {
                let reordered = bidi::visual_reorder(line);
                lines.push(Line::from(Span::styled(format!(" {}", reordered), theme::user_msg())));
            }
        } else {
            let mut in_code = false;
            let mut in_diff = false;
            for line in msg.content.lines() {
                let (rendered, new_code, new_diff) = render_markdown_line(line, in_code, in_diff);
                in_code = new_code;
                in_diff = new_diff;
                lines.push(rendered);
            }
        }
        lines.push(Line::from(""));
    }

    if matches!(app.chat_state, crate::app::ChatState::Streaming) {
        if !app.chat_messages.last().is_some_and(|m| m.role == "assistant" && !m.content.is_empty()) {
            lines.push(Line::from(Span::styled(
                "◆ thinking...",
                theme::thinking(),
            )));
        }
    }

    let visible = area.height;
    let scroll = if lines.len() as u16 > visible {
        lines.len() as u16 - visible
    } else {
        0
    };

    let messages = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0));
    frame.render_widget(messages, area);
}

fn ghost_text(app: &App) -> String {
    if !app.input.starts_with('/') || app.input.is_empty() {
        return String::new();
    }

    if !app.arg_suggestions.is_empty() {
        if let Some(arg) = app.arg_suggestions.get(app.suggestion_cursor) {
            let space_idx = app.input.find(' ').unwrap_or(app.input.len());
            let typed_arg = &app.input[space_idx + 1..];
            if arg.starts_with(typed_arg) && arg.len() > typed_arg.len() {
                return arg[typed_arg.len()..].to_string();
            }
        }
        return String::new();
    }

    if let Some(&cmd_idx) = app.suggestions.get(app.suggestion_cursor) {
        let cmd = &COMMANDS[cmd_idx];
        if cmd.name.len() > app.input.len() {
            return cmd.name[app.input.len()..].to_string();
        }
    }

    String::new()
}

fn render_input(frame: &mut Frame, app: &App, area: Rect) {
    let ghost = ghost_text(app);

    let mut spans = vec![
        Span::styled(" › ", theme::accent_bold()),
    ];

    if app.input.is_empty() && matches!(app.chat_state, crate::app::ChatState::Idle) {
        spans.push(Span::styled("Type a message or / for commands...", theme::muted()));
    } else {
        spans.push(Span::raw(&app.input));
        if !ghost.is_empty() {
            spans.push(Span::styled(ghost, theme::muted()));
        }
    }

    let cwd = std::env::current_dir()
        .map(|p| {
            let home = dirs::home_dir().unwrap_or_default();
            if let Ok(rel) = p.strip_prefix(&home) {
                format!("~/{}", rel.display())
            } else {
                p.display().to_string()
            }
        })
        .unwrap_or_default();
    let title = format!(" {} ", cwd);

    let input = Paragraph::new(Line::from(spans))
    .block(
        Block::default()
            .borders(Borders::ALL)
            .title(title)
            .title_style(theme::dim())
            .border_style(theme::accent()),
    );
    frame.render_widget(input, area);

    let cursor_x = area.x + 4 + app.input[..app.input_cursor].chars().count() as u16;
    let cursor_y = area.y + 1;
    frame.set_cursor_position((cursor_x, cursor_y));
}

fn render_suggestions(frame: &mut Frame, app: &App, input_area: Rect) {
    let max_visible: usize = 8;

    let (items, title) = if !app.arg_suggestions.is_empty() {
        let total = app.arg_suggestions.len();
        let visible = total.min(max_visible);
        let scroll_offset = if app.suggestion_cursor >= visible {
            app.suggestion_cursor - visible + 1
        } else {
            0
        };
        let items: Vec<Line> = app.arg_suggestions
            .iter()
            .enumerate()
            .skip(scroll_offset)
            .take(visible)
            .map(|(i, arg)| {
                let style = if i == app.suggestion_cursor {
                    theme::accent_bold().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::styled(format!(" {}", arg), style)
            })
            .collect();
        let title = if total > visible {
            format!(" Options ({}/{}) ", app.suggestion_cursor + 1, total)
        } else {
            " Options ".to_string()
        };
        (items, title)
    } else {
        let total = app.suggestions.len();
        let visible = total.min(max_visible);
        let scroll_offset = if app.suggestion_cursor >= visible {
            app.suggestion_cursor - visible + 1
        } else {
            0
        };
        let items: Vec<Line> = app.suggestions
            .iter()
            .enumerate()
            .skip(scroll_offset)
            .take(visible)
            .map(|(i, &cmd_idx)| {
                let cmd = &COMMANDS[cmd_idx];
                let style = if i == app.suggestion_cursor {
                    theme::accent_bold().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::styled(format!(" {:16} {}", cmd.name, cmd.description), style)
            })
            .collect();
        let title = if total > visible {
            format!(" Commands ({}/{}) ", app.suggestion_cursor + 1, total)
        } else {
            " Commands ".to_string()
        };
        (items, title)
    };

    if items.is_empty() {
        return;
    }

    let popup_height = items.len() as u16 + 2;
    let popup_width = 46;

    let popup_area = Rect {
        x: input_area.x + 3,
        y: input_area.y.saturating_sub(popup_height),
        width: popup_width.min(input_area.width - 4),
        height: popup_height,
    };

    frame.render_widget(Clear, popup_area);

    let popup = Paragraph::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(theme::accent())
            .title(title),
    );
    frame.render_widget(popup, popup_area);
}
