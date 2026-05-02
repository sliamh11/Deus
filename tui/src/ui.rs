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
    let state_indicator = match app.chat_state {
        crate::app::ChatState::Idle => Span::styled("●", theme::good()),
        crate::app::ChatState::Streaming => Span::styled("◐", theme::warn()),
    };

    let elapsed_secs = app.session_start.elapsed().as_secs();
    let window_secs: u64 = 5 * 3600;
    let remaining_pct = if elapsed_secs >= window_secs { 0 } else {
        ((window_secs - elapsed_secs) * 100 / window_secs) as u64
    };
    let remaining_color = if remaining_pct > 50 { theme::GOOD }
        else if remaining_pct > 20 { theme::WARN }
        else { theme::BAD };

    let bar = Line::from(vec![
        Span::raw(" "),
        state_indicator,
        Span::styled(format!(" {} ", app.model), theme::accent()),
        Span::styled("│ ", theme::muted()),
        Span::styled(
            if app.cost_usd > 0.0 { format!("${:.2} ", app.cost_usd) } else { String::new() },
            theme::dim(),
        ),
        Span::styled(format!("{}% remaining", remaining_pct), Style::default().fg(remaining_color)),
    ]);
    frame.render_widget(Paragraph::new(bar), area);
}

fn render_panel_header(frame: &mut Frame, _app: &App, area: Rect) {
    let title = format!(" ◇ deus › {} ", _app.tab.label());
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
