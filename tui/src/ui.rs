use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::app::{App, Tab};
use crate::panels;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App) {
    let area = frame.area();

    match app.tab {
        Tab::Chat => {
            let layout = Layout::vertical([
                Constraint::Min(0),
                Constraint::Length(1),
            ])
            .split(area);

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
}

fn render_status_bar(frame: &mut Frame, app: &App, area: Rect) {
    let mut left: Vec<Span> = Vec::new();
    let mut right: Vec<Span> = Vec::new();

    let state_indicator = match app.chat_state {
        crate::app::ChatState::Idle => Span::styled("●", theme::good()),
        crate::app::ChatState::Streaming => Span::styled("●", theme::warn()),
    };
    left.push(Span::raw(" "));
    left.push(state_indicator);
    left.push(Span::styled(
        format!(" {} ", crate::app::model_display(&app.model)),
        theme::accent(),
    ));

    if !app.git_branch.is_empty() {
        left.push(Span::styled("│ ", theme::muted()));
        left.push(Span::styled(format!(" {}", app.git_branch), theme::dim()));
    }

    // Right side: cost + turn duration + remaining
    if let Some(dur) = app.turn_duration_display() {
        right.push(Span::styled(format!("{} ", dur), theme::dim()));
        right.push(Span::styled("│ ", theme::muted()));
    }

    if app.cost_usd > 0.0 {
        right.push(Span::styled(format!("${:.2} ", app.cost_usd), theme::dim()));
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
    let remaining_pct = if elapsed_secs >= window_secs { 0 } else {
        ((window_secs - elapsed_secs) * 100 / window_secs) as u64
    };
    let remaining_color = if remaining_pct > 50 { theme::GOOD }
        else if remaining_pct > 20 { theme::WARN }
        else { theme::BAD };
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

fn render_panel_header(frame: &mut Frame, app: &App, area: Rect) {
    let title = format!(" ◇ deus › {} ", app.tab.label());
    let block = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .border_style(theme::accent());
    frame.render_widget(block, area);
}

fn render_panel_footer(frame: &mut Frame, app: &App, area: Rect) {
    let hints = match app.tab {
        Tab::Wardens => " ↑↓ move │ space toggle │ r refresh │ esc back to chat",
        _ => " ↑↓ move │ r refresh │ esc back to chat",
    };
    let footer = Paragraph::new(hints).style(theme::muted());
    frame.render_widget(footer, area);
}
