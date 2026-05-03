use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, List, ListItem};

use crate::app::App;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let items: Vec<ListItem> = app
        .services
        .iter()
        .enumerate()
        .map(|(i, svc)| {
            let (icon, color) = match svc.status.as_str() {
                "running" => ("●", theme::good_color()),
                "stale" => ("◐", theme::warn_color()),
                _ => ("○", theme::bad_color()),
            };
            let cursor = if i == app.cursor { "▸ " } else { "  " };
            ListItem::new(Line::from(vec![
                Span::raw(cursor),
                Span::styled(format!("{} ", icon), Style::default().fg(color)),
                Span::styled(
                    format!("{:42}", svc.description),
                    if i == app.cursor {
                        theme::bold()
                    } else {
                        Style::default()
                    },
                ),
                Span::styled(&svc.status, theme::dim()),
            ]))
        })
        .collect();

    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Services ")
            .border_style(theme::border()),
    );
    frame.render_widget(list, area);
}
