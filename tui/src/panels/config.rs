use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, List, ListItem};

use crate::app::App;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let items: Vec<ListItem> = app
        .deus_config
        .iter()
        .enumerate()
        .map(|(i, (key, value))| {
            let cursor = if i == app.cursor { "▸ " } else { "  " };
            ListItem::new(Line::from(vec![
                Span::raw(cursor),
                Span::styled(format!("{:22}", key), theme::accent()),
                Span::raw(value),
            ]))
        })
        .collect();

    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Config ")
            .border_style(theme::border()),
    );
    frame.render_widget(list, area);
}
