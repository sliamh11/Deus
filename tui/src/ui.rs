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

    if app.has_pending_permission() {
        render_permission_prompt(frame, app, area);
    } else if app.show_session_picker {
        render_session_picker(frame, app, area);
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
        left.push(Span::styled(
            format!(" {}", session.permissions.mode),
            perm_style,
        ));
        left.push(Span::raw(" "));
    }

    if let Some(hint) = chat_activity_hint(app) {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled(hint, theme::thinking()));
    }

    let bg_count = app.background_session_count();
    if bg_count > 0 {
        left.push(Span::styled("│ ", theme::muted()));
        let streaming = app.streaming_background_count();
        let label = if streaming > 0 {
            format!("{} bg ({} active) ", bg_count, streaming)
        } else {
            format!("{} bg ", bg_count)
        };
        left.push(Span::styled(label, theme::dim()));
    }

    if app.has_pending_permission() {
        left.push(Span::styled("│ ", theme::muted()));
        let count = app.active().pending_permissions.len();
        left.push(Span::styled(
            format!("PERM({}) ", count),
            Style::default()
                .fg(theme::WARN)
                .add_modifier(Modifier::BOLD),
        ));
    }

    if app.mode != "home" {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled("EXT ", theme::warn()));
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
    }

    if !app.queued_messages.is_empty() {
        right.push(Span::styled(
            format!("{} queued ", app.queued_messages.len()),
            theme::warn(),
        ));
        right.push(Span::styled("│ ", theme::muted()));
    }

    let elapsed_secs = app.session_start.elapsed().as_secs();
    let window_secs: u64 = 5 * 3600;
    let remaining_pct = if elapsed_secs >= window_secs {
        0
    } else {
        (window_secs - elapsed_secs) * 100 / window_secs
    };
    let remaining_color = if remaining_pct > 50 {
        theme::GOOD
    } else if remaining_pct > 20 {
        theme::WARN
    } else {
        theme::BAD
    };
    right.push(Span::styled(
        format!("{}%", remaining_pct),
        Style::default().fg(remaining_color),
    ));
    right.push(Span::raw(" "));

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
                &session.label,
                crate::app::model_display(&session.model)
            ),
            if is_selected {
                Style::default().bg(theme::ACCENT).fg(Color::Black)
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

fn render_permission_prompt(frame: &mut Frame, app: &App, area: Rect) {
    let session = app.active();
    let req = match session.pending_permissions.first() {
        Some(r) => r,
        None => return,
    };

    let mut lines: Vec<Line> = Vec::new();
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::styled("  Tool: ", theme::dim()),
        Span::styled(&req.tool_name, theme::tool_name()),
    ]));

    if !req.tool_input_preview.is_empty() {
        let preview: String = req.tool_input_preview.chars().take(50).collect();
        let display = if preview.len() < req.tool_input_preview.len() {
            format!("{}...", preview)
        } else {
            preview
        };
        lines.push(Line::from(vec![
            Span::styled("  Input: ", theme::dim()),
            Span::styled(display, theme::tool_detail()),
        ]));
    }

    if session.id != crate::app::SessionId::MAIN {
        lines.push(Line::from(vec![
            Span::styled("  Session: ", theme::dim()),
            Span::styled(&session.label, theme::agent_detail()),
        ]));
    }

    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::styled("  Y", theme::accent_bold()),
        Span::styled(" allow  ", theme::dim()),
        Span::styled("N", theme::accent_bold()),
        Span::styled(" deny  ", theme::dim()),
        Span::styled("A", theme::accent_bold()),
        Span::styled(" always  ", theme::dim()),
        Span::styled("Esc", theme::accent_bold()),
        Span::styled(" deny", theme::dim()),
    ]));
    lines.push(Line::from(""));

    let height = lines.len() as u16 + 2;
    let width = 56u16.min(area.width.saturating_sub(4));
    let x = (area.width.saturating_sub(width)) / 2;
    let y = (area.height.saturating_sub(height)) / 2;
    let popup = Rect::new(x, y, width, height);

    frame.render_widget(Clear, popup);

    let pending_count = session.pending_permissions.len();
    let title = if pending_count > 1 {
        format!(" Permission Required ({} pending) ", pending_count)
    } else {
        " Permission Required ".to_string()
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .border_style(theme::warn());
    let prompt = Paragraph::new(lines).block(block);
    frame.render_widget(prompt, popup);
}

fn render_panel_footer(frame: &mut Frame, app: &App, area: Rect) {
    let hints = match app.tab {
        Tab::Wardens => " ↑↓ move │ space toggle │ r refresh │ esc back to chat",
        _ => " ↑↓ move │ r refresh │ esc back to chat",
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
