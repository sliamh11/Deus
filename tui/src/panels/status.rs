use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::app::App;
use crate::theme;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let mut lines: Vec<Line> = Vec::new();

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("  WARDENS", theme::bold())));
    lines.push(Line::from(""));
    for w in &app.wardens {
        let icon = if w.enabled { "●" } else { "○" };
        let color = if w.enabled {
            theme::good_color()
        } else {
            theme::bad_color()
        };
        lines.push(Line::from(vec![
            Span::styled(format!("  {} ", icon), Style::default().fg(color)),
            Span::raw(format!("{:24} {}", w.name, w.warden_type)),
        ]));
    }

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("  SERVICES", theme::bold())));
    lines.push(Line::from(""));
    for svc in &app.services {
        let (icon, color) = match svc.status.as_str() {
            "running" => ("●", theme::good_color()),
            "stale" => ("◐", theme::warn_color()),
            _ => ("○", theme::bad_color()),
        };
        lines.push(Line::from(vec![
            Span::styled(format!("  {} ", icon), Style::default().fg(color)),
            Span::raw(format!("{:36} {}", svc.description, svc.status)),
        ]));
    }

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("  CHANNELS", theme::bold())));
    lines.push(Line::from(""));
    for ch in &app.channels {
        let (icon, color) = if ch.configured {
            ("●", theme::good_color())
        } else {
            ("○", theme::bad_color())
        };
        let status = if ch.configured {
            "connected"
        } else {
            "not configured"
        };
        lines.push(Line::from(vec![
            Span::styled(format!("  {} ", icon), Style::default().fg(color)),
            Span::raw(format!("{:16} {}", ch.name, status)),
        ]));
    }

    let widget = Paragraph::new(lines).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" System Status ")
            .border_style(theme::border()),
    );
    frame.render_widget(widget, area);
}
