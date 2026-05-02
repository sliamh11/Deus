use ratatui::style::{Color, Modifier, Style};

pub const EMBER: Color = Color::Rgb(0xE8, 0x72, 0x3A);
pub const FLAME: Color = Color::Rgb(0xF4, 0xA2, 0x61);
pub const DEEP_TEAL: Color = Color::Rgb(0x1B, 0x7A, 0x6E);
pub const OCEAN: Color = Color::Rgb(0x2E, 0xC4, 0xB6);
pub const SHADOW: Color = Color::Rgb(0xC4, 0x5A, 0x2A);
pub const NIGHT: Color = Color::Rgb(0x1A, 0x1A, 0x2E);

pub const SURFACE: Color = Color::Reset;
pub const TEXT: Color = Color::White;
pub const TEXT_DIM: Color = Color::Rgb(0x6C, 0x6C, 0x8A);
pub const TEXT_MUTED: Color = Color::DarkGray;
pub const BORDER: Color = Color::Rgb(0x3A, 0x3A, 0x5A);

pub const GOOD: Color = Color::Rgb(0x4E, 0xC9, 0x90);
pub const WARN: Color = Color::Rgb(0xF4, 0xA2, 0x61);
pub const BAD: Color = Color::Rgb(0xE8, 0x5D, 0x5D);

pub const ACCENT: Color = OCEAN;
pub const ACCENT_ALT: Color = EMBER;
pub const PROMPT: Color = OCEAN;

pub fn accent() -> Style {
    Style::default().fg(ACCENT)
}

pub fn accent_bold() -> Style {
    Style::default().fg(ACCENT).add_modifier(Modifier::BOLD)
}

pub fn dim() -> Style {
    Style::default().fg(TEXT_DIM)
}

pub fn muted() -> Style {
    Style::default().fg(TEXT_MUTED)
}

pub fn bold() -> Style {
    Style::default().fg(Color::Rgb(0xFF, 0xFF, 0xFF)).add_modifier(Modifier::BOLD)
}

pub fn good() -> Style {
    Style::default().fg(GOOD)
}

pub fn warn() -> Style {
    Style::default().fg(WARN)
}

pub fn bad() -> Style {
    Style::default().fg(BAD)
}

pub fn border() -> Style {
    Style::default().fg(BORDER)
}

pub fn user_msg() -> Style {
    Style::default().fg(Color::Rgb(0xFF, 0xFF, 0xFF)).add_modifier(Modifier::BOLD)
}

pub fn tool_name() -> Style {
    Style::default().fg(FLAME).add_modifier(Modifier::BOLD)
}

pub fn tool_detail() -> Style {
    Style::default().fg(TEXT_DIM)
}

pub fn thinking() -> Style {
    Style::default().fg(TEXT_DIM).add_modifier(Modifier::ITALIC)
}

pub fn code() -> Style {
    Style::default().fg(FLAME)
}

pub fn heading1() -> Style {
    Style::default().fg(OCEAN).add_modifier(Modifier::BOLD)
}

pub fn heading2() -> Style {
    Style::default().fg(TEXT).add_modifier(Modifier::BOLD)
}

pub fn bullet() -> Style {
    Style::default().fg(OCEAN)
}

pub fn diff_add() -> Style {
    Style::default().fg(GOOD)
}

pub fn diff_del() -> Style {
    Style::default().fg(BAD)
}

pub fn diff_hunk() -> Style {
    Style::default().fg(OCEAN)
}
