use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

use crate::app::{App, ChatState, SessionState, Tab};
use crate::panels;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App) {
    let area = frame.area();

    match app.tab {
        Tab::Chat => {
            let layout = Layout::vertical([Constraint::Min(0), Constraint::Length(1)]).split(area);

            panels::chat::render(frame, app, layout[0]);
            render_status_bar(frame, app, layout[1]);
        }
        _ => {
            let layout = Layout::vertical([
                Constraint::Length(3),
                Constraint::Min(0),
                Constraint::Length(1),
            ])
            .split(area);

            render_panel_header(frame, app, layout[0]);
            match app.tab {
                Tab::Wardens => panels::wardens::render(frame, app, layout[1]),
                Tab::Services => panels::services::render(frame, app, layout[1]),
                Tab::Channels => panels::channels::render(frame, app, layout[1]),
                Tab::Config => panels::config::render(frame, app, layout[1]),
                Tab::Status => panels::status::render(frame, app, layout[1]),
                Tab::Chat => unreachable!(),
            }
            render_panel_footer(frame, app, layout[2]);
        }
    }

    if app.show_session_picker {
        render_session_picker(frame, app, area);
    }

    if app.show_rewind_picker {
        render_rewind_picker(frame, app, area);
    }
}

fn render_status_bar(frame: &mut Frame, app: &App, area: Rect) {
    let mut left: Vec<Span> = Vec::new();
    let mut right: Vec<Span> = Vec::new();

    let session = app.active();
    let state_indicator = match session.chat_state {
        crate::app::ChatState::Idle => Span::styled("●", theme::good()),
        crate::app::ChatState::Streaming => Span::styled("●", theme::warn()),
    };
    left.push(Span::raw(" "));
    left.push(state_indicator);
    left.push(Span::styled(
        format!(" {} ", crate::app::model_display(&session.model)),
        theme::accent(),
    ));

    if session.permissions.mode != "default" {
        left.push(Span::styled("│ ", theme::muted()));
        let perm_style = if session.permissions.is_bypass() {
            theme::warn()
        } else {
            theme::dim()
        };
        let display_mode = match session.permissions.mode.as_str() {
            "bypassPermissions" => "bypass",
            "acceptEdits" => "auto-edit",
            m => m,
        };
        left.push(Span::styled(format!(" {}", display_mode), perm_style));
        left.push(Span::raw(" "));
    }

    if app.esc_pending_active() {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled("Press Esc again to quit ", theme::warn()));
    } else if let Some(hint) = chat_activity_hint(app) {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled(hint, theme::thinking()));
    }

    let bg_count = app.background_session_count();
    if bg_count > 0 {
        left.push(Span::styled("│ ", theme::muted()));
        let streaming = app.streaming_background_count();
        let label = if streaming > 0 {
            format!("{} agents ({} running) ", bg_count, streaming)
        } else {
            format!("{} agents ", bg_count)
        };
        left.push(Span::styled(label, theme::dim()));
    }

    if app.has_pending_permission() {
        left.push(Span::styled("│ ", theme::muted()));
        let count = app.active().pending_permissions.len();
        left.push(Span::styled(
            format!("{} pending ", count),
            Style::default()
                .fg(theme::warn_color())
                .add_modifier(Modifier::BOLD),
        ));
    }

    if app.mode != "home" {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled("ext project ", theme::warn()));
    }

    if !app.git_branch.is_empty() {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled(format!(" {}", app.git_branch), theme::dim()));
    }

    // Right side: cost + turn duration + remaining
    let session = app.active();
    if let Some(dur) = app.turn_duration_display() {
        right.push(Span::styled(format!("{} ", dur), theme::dim()));
        right.push(Span::styled("│ ", theme::muted()));
    }

    if session.cost_usd > 0.0 {
        right.push(Span::styled(
            format!("${:.2} ", session.cost_usd),
            theme::dim(),
        ));
        right.push(Span::styled("│ ", theme::muted()));
    } else if crate::app::model_backend(&session.model) == "codex" {
        right.push(Span::styled("cost: n/a ", theme::dim()));
        right.push(Span::styled("│ ", theme::muted()));
    }

    if !app.queued_messages.is_empty() {
        right.push(Span::styled(
            format!("{} queued ", app.queued_messages.len()),
            theme::warn(),
        ));
        right.push(Span::styled("│ ", theme::muted()));
    }

    {
        let ctx_total = crate::backend::model_context_tokens(&session.model);
        let ctx_used = session.token_count as u64;
        let pct = (ctx_used * 100)
            .checked_div(ctx_total)
            .unwrap_or(0)
            .min(100) as u16;
        let filled = (pct as usize * 10 / 100).min(10);
        let bar: String = format!(
            "[{}{}] {}%",
            "|".repeat(filled),
            ".".repeat(10 - filled),
            pct
        );
        let bar_color = if pct < 50 {
            theme::good_color()
        } else if pct < 75 {
            theme::warn_color()
        } else {
            theme::bad_color()
        };
        right.push(Span::styled(bar, Style::default().fg(bar_color)));
        right.push(Span::raw(" "));
    }

    // Calculate padding
    let left_len: usize = left.iter().map(|s| s.content.chars().count()).sum();
    let right_len: usize = right.iter().map(|s| s.content.chars().count()).sum();
    let pad = (area.width as usize).saturating_sub(left_len + right_len);

    let mut spans = left;
    spans.push(Span::raw(" ".repeat(pad)));
    spans.extend(right);

    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn chat_activity_hint(app: &App) -> Option<String> {
    let session = app.active();
    if matches!(session.chat_state, crate::app::ChatState::Streaming) {
        let tool_hint = if app.show_tools {
            "thinking... Ctrl+O hide"
        } else {
            "thinking... Ctrl+O show"
        };
        let subagent_hint = if !session.active_subagent_ids.is_empty() {
            " | Ctrl+B: parallel agent"
        } else {
            ""
        };
        Some(format!("{}{}", tool_hint, subagent_hint))
    } else {
        None
    }
}

fn render_panel_header(frame: &mut Frame, app: &App, area: Rect) {
    let title = format!(" ◇ deus › {} ", app.tab.label());
    let block = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .border_style(theme::accent());
    frame.render_widget(block, area);
}

fn render_session_picker(frame: &mut Frame, app: &App, area: Rect) {
    let session_count = app.session_order.len();
    let height = (session_count as u16 + 4).min(area.height.saturating_sub(4));
    let width = 60u16.min(area.width.saturating_sub(4));
    let x = (area.width.saturating_sub(width)) / 2;
    let y = (area.height.saturating_sub(height)) / 2;
    let popup = Rect::new(x, y, width, height);

    frame.render_widget(Clear, popup);

    let mut lines: Vec<Line> = Vec::new();
    for (i, &id) in app.session_order.iter().enumerate() {
        let session = match app.sessions.get(&id) {
            Some(s) => s,
            None => continue,
        };
        let is_selected = i == app.picker_cursor;
        let is_active = id == app.active_session;
        let marker = if is_active { ">" } else { " " };

        let state_icon = match (&session.session_state, &session.chat_state) {
            (SessionState::Completed, _) => Span::styled("✓", theme::good()),
            (SessionState::Failed, _) => Span::styled("✗", theme::bad()),
            (_, ChatState::Streaming) => Span::styled("●", theme::warn()),
            _ => Span::styled("○", theme::dim()),
        };

        let label = Span::styled(
            format!(
                " {} {} ",
                session.label,
                crate::app::model_display(&session.model)
            ),
            if is_selected {
                Style::default().bg(theme::accent_color()).fg(Color::Black)
            } else {
                Style::default()
            },
        );

        let cost = if session.cost_usd > 0.0 {
            Span::styled(format!(" ${:.2}", session.cost_usd), theme::dim())
        } else {
            Span::raw("")
        };

        lines.push(Line::from(vec![
            Span::raw(marker),
            Span::raw(" "),
            state_icon,
            label,
            cost,
        ]));
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Sessions — ↑↓ enter d:dismiss esc ")
        .border_style(theme::accent());
    let picker = Paragraph::new(lines).block(block);
    frame.render_widget(picker, popup);
}

fn render_rewind_picker(frame: &mut Frame, app: &App, area: Rect) {
    let target_count = app.rewind_targets.len();
    let height = (target_count as u16 + 4).min(area.height.saturating_sub(4));
    let width = 70u16.min(area.width.saturating_sub(4));
    let x = (area.width.saturating_sub(width)) / 2;
    let y = (area.height.saturating_sub(height)) / 2;
    let popup = Rect::new(x, y, width, height);

    frame.render_widget(Clear, popup);

    let session = app.active();
    let inner_width = width.saturating_sub(4) as usize;
    let mut lines: Vec<Line> = Vec::new();
    for (i, &msg_idx) in app.rewind_targets.iter().enumerate() {
        let is_selected = i == app.rewind_cursor;
        let msg = match session.chat_messages.get(msg_idx) {
            Some(m) => m,
            None => continue,
        };
        let preview: String = msg
            .content
            .chars()
            .take(inner_width.saturating_sub(4))
            .map(|c| if c == '\n' { ' ' } else { c })
            .collect();

        let style = if is_selected {
            Style::default().bg(theme::accent_color()).fg(Color::Black)
        } else {
            Style::default()
        };

        lines.push(Line::from(vec![
            Span::styled(if is_selected { "> " } else { "  " }, style),
            Span::styled(preview, style),
        ]));
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Rewind — ↑↓ enter esc ")
        .border_style(theme::accent());
    let picker = Paragraph::new(lines).block(block);
    frame.render_widget(picker, popup);
}

fn render_panel_footer(frame: &mut Frame, app: &App, area: Rect) {
    let hints = match app.tab {
        Tab::Wardens => " ↑↓ move │ space toggle │ r refresh │ tab: next panel │ esc back to chat",
        _ => " ↑↓ move │ r refresh │ tab: next panel │ esc back to chat",
    };
    let footer = Paragraph::new(hints).style(theme::muted());
    frame.render_widget(footer, area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{App, ChatState};

    #[test]
    fn chat_activity_hint_shows_while_streaming() {
        let mut app = App::new();
        app.active_mut().chat_state = ChatState::Streaming;
        app.show_tools = false;

        let hint = chat_activity_hint(&app).expect("streaming hint");
        assert!(hint.contains("thinking..."));
        assert!(hint.contains("Ctrl+O show"));
    }

    #[test]
    fn chat_activity_hint_is_hidden_when_idle() {
        let app = App::new();
        assert!(chat_activity_hint(&app).is_none());
    }

    #[test]
    fn permission_overlay_renders_without_panic() {
        use crate::permission_bridge::PermissionRequest;
        use ratatui::{Terminal, backend::TestBackend};

        let mut app = App::new();
        app.active_mut()
            .pending_permissions
            .push(PermissionRequest {
                tool_use_id: "toolu_test".to_string(),
                tool_name: "Bash".to_string(),
                tool_input_preview: "rm -rf /tmp/test".to_string(),
                session_id: crate::app::SessionId::MAIN,
            });

        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(frame, &app)).unwrap();
    }

    #[test]
    fn permission_overlay_shows_multi_count() {
        use crate::permission_bridge::PermissionRequest;
        use ratatui::{Terminal, backend::TestBackend};

        let mut app = App::new();
        for i in 0..3 {
            app.active_mut()
                .pending_permissions
                .push(PermissionRequest {
                    tool_use_id: format!("t{}", i),
                    tool_name: "Bash".to_string(),
                    tool_input_preview: format!("cmd {}", i),
                    session_id: crate::app::SessionId::MAIN,
                });
        }

        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(frame, &app)).unwrap();
    }
}
