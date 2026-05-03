// Generated from: python3 scripts/companion_to_braille.py assets/brand-production/logo/logo-transparent-256.png --size mini --mode braille
use ratatui::prelude::*;

use crate::theme;

pub fn logo_lines() -> Vec<Line<'static>> {
    let fl = Style::default().fg(theme::FLAME);
    let em = Style::default().fg(theme::EMBER);
    let oc = Style::default().fg(theme::OCEAN);
    let dt = Style::default().fg(theme::DEEP_TEAL);

    vec![
        Line::from(""),
        // Line 1:    ⢀⡀  ⢀⡀
        Line::from(vec![
            Span::raw("       "),
            Span::styled("⢀", fl),
            Span::styled("⡀", fl),
            Span::raw("  "),
            Span::styled("⢀", oc),
            Span::styled("⡀", fl),
        ]),
        // Line 2:   ⣴⡿⢿⣷⣾⣿⢿⣆
        Line::from(vec![
            Span::raw("      "),
            Span::styled("⣴", fl),
            Span::styled("⡿", fl),
            Span::styled("⢿", fl),
            Span::styled("⣷", fl),
            Span::styled("⣾", fl),
            Span::styled("⣿", oc),
            Span::styled("⢿", oc),
            Span::styled("⣆", oc),
        ]),
        // Line 3:   ⢿⣆⣨⣿⣿⣅⣰⣿
        Line::from(vec![
            Span::raw("      "),
            Span::styled("⢿", em),
            Span::styled("⣆", em),
            Span::styled("⣨", em),
            Span::styled("⣿", fl),
            Span::styled("⣿", fl),
            Span::styled("⣅", fl),
            Span::styled("⣰", dt),
            Span::styled("⣿", oc),
        ]),
        // Line 4:   ⠈⠻⠟⠋⠙⠿⠟⠁
        Line::from(vec![
            Span::raw("      "),
            Span::styled("⠈", em),
            Span::styled("⠻", fl),
            Span::styled("⠟", em),
            Span::styled("⠋", fl),
            Span::styled("⠙", fl),
            Span::styled("⠿", dt),
            Span::styled("⠟", oc),
            Span::styled("⠁", dt),
        ]),
        Line::from(""),
        Line::from(vec![
            Span::raw("      "),
            Span::styled("D  E  U  S", theme::accent_bold()),
        ]),
        Line::from(vec![
            Span::raw("      "),
            Span::styled(concat!("v", env!("CARGO_PKG_VERSION")), theme::dim()),
        ]),
        Line::from(""),
    ]
}
