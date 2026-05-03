use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, List, ListItem};

use crate::app::App;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let items: Vec<ListItem> = app
        .channels
        .iter()
        .enumerate()
        .map(|(i, ch)| {
            let (icon, color, status) = if ch.configured {
                ("●", theme::good_color(), "connected")
            } else {
                ("○", theme::bad_color(), "not configured")
            };
            let cursor = if i == app.cursor { "▸ " } else { "  " };
            ListItem::new(Line::from(vec![
                Span::raw(cursor),
                Span::styled(format!("{} ", icon), Style::default().fg(color)),
                Span::styled(
                    format!("{:16}", ch.name),
                    if i == app.cursor {
                        theme::bold()
                    } else {
                        Style::default()
                    },
                ),
                Span::styled(status, theme::dim()),
            ]))
        })
        .collect();

    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Channels ")
            .border_style(theme::border()),
    );
    frame.render_widget(list, area);
}
