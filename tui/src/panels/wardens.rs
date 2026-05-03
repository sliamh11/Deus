use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, List, ListItem, Paragraph};

use crate::app::App;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let layout = Layout::vertical([Constraint::Min(0), Constraint::Length(6)]).split(area);

    let items: Vec<ListItem> = app
        .wardens
        .iter()
        .enumerate()
        .map(|(i, w)| {
            let icon = if w.enabled { "●" } else { "○" };
            let color = if w.enabled {
                theme::good_color()
            } else {
                theme::bad_color()
            };
            let cursor_str = if i == app.cursor { "▸ " } else { "  " };
            let style = if i == app.cursor {
                theme::bold()
            } else {
                theme::dim()
            };
            ListItem::new(Line::from(vec![
                Span::raw(cursor_str.to_string()),
                Span::styled(format!("{} ", icon), Style::default().fg(color)),
                Span::styled(format!("{:24} {}", w.name, w.warden_type), style),
            ]))
        })
        .collect();

    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Wardens ")
            .border_style(theme::border()),
    );
    frame.render_widget(list, layout[0]);

    if let Some(selected) = app.wardens.get(app.cursor) {
        let detail = vec![
            Line::from(vec![
                Span::styled("  Type:         ", theme::dim()),
                Span::styled(&selected.warden_type, theme::accent()),
            ]),
            Line::from(vec![
                Span::styled("  Triggers:     ", theme::dim()),
                Span::raw(&selected.triggers),
            ]),
            Line::from(vec![
                Span::styled("  Instructions: ", theme::dim()),
                Span::raw(selected.custom_instructions.as_deref().unwrap_or("(none)")),
            ]),
        ];
        let detail_widget = Paragraph::new(detail).block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(theme::border()),
        );
        frame.render_widget(detail_widget, layout[1]);
    }
}
