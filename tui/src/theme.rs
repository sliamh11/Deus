// Changes to theme.json require restarting the TUI.
use std::sync::OnceLock;

use ratatui::style::{Color, Modifier, Style};
use serde::Deserialize;

use crate::platform;

// Brand colors — not themable, used for logo and brand-locked UI elements.
pub const EMBER: Color = Color::Rgb(0xE8, 0x72, 0x3A);
pub const FLAME: Color = Color::Rgb(0xF4, 0xA2, 0x61);
pub const DEEP_TEAL: Color = Color::Rgb(0x1B, 0x7A, 0x6E);
pub const OCEAN: Color = Color::Rgb(0x2E, 0xC4, 0xB6);

const DEFAULT_ACCENT: Color = OCEAN;
const DEFAULT_GOOD: Color = Color::Rgb(0x4E, 0xC9, 0x90);
const DEFAULT_WARN: Color = Color::Rgb(0xF4, 0xA2, 0x61);
const DEFAULT_BAD: Color = Color::Rgb(0xE8, 0x5D, 0x5D);
const DEFAULT_TEXT: Color = Color::White;
const DEFAULT_TEXT_DIM: Color = Color::Rgb(0x6C, 0x6C, 0x8A);
const DEFAULT_BORDER: Color = Color::Rgb(0x3A, 0x3A, 0x5A);
const DEFAULT_AGENT: Color = Color::Rgb(0x9B, 0x59, 0xB6);
const DEFAULT_SHIELD: Color = Color::Rgb(0xD4, 0xAA, 0x00);

#[derive(Deserialize)]
#[serde(default)]
struct ThemeConfig {
    accent: String,
    good: String,
    warn: String,
    bad: String,
    text: String,
    text_dim: String,
    border: String,
    agent: String,
    shield: String,
}

impl Default for ThemeConfig {
    fn default() -> Self {
        Self {
            accent: "#2EC4B6".to_string(),
            good: "#4EC990".to_string(),
            warn: "#F4A261".to_string(),
            bad: "#E85D5D".to_string(),
            text: "#FFFFFF".to_string(),
            text_dim: "#6C6C8A".to_string(),
            border: "#3A3A5A".to_string(),
            agent: "#9B59B6".to_string(),
            shield: "#D4AA00".to_string(),
        }
    }
}

impl ThemeConfig {
    fn load() -> Self {
        let path = platform::config_dir().join("theme.json");
        std::fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }
}

fn parse_hex(hex: &str) -> Option<Color> {
    let hex = hex.trim_start_matches('#');
    if hex.len() != 6 {
        return None;
    }
    let r = u8::from_str_radix(&hex[0..2], 16).ok()?;
    let g = u8::from_str_radix(&hex[2..4], 16).ok()?;
    let b = u8::from_str_radix(&hex[4..6], 16).ok()?;
    Some(Color::Rgb(r, g, b))
}

struct ParsedTheme {
    accent: Color,
    good: Color,
    warn: Color,
    bad: Color,
    text: Color,
    text_dim: Color,
    border: Color,
    agent: Color,
    shield: Color,
}

impl ParsedTheme {
    fn from_config(cfg: &ThemeConfig) -> Self {
        Self {
            accent: parse_hex(&cfg.accent).unwrap_or(DEFAULT_ACCENT),
            good: parse_hex(&cfg.good).unwrap_or(DEFAULT_GOOD),
            warn: parse_hex(&cfg.warn).unwrap_or(DEFAULT_WARN),
            bad: parse_hex(&cfg.bad).unwrap_or(DEFAULT_BAD),
            text: parse_hex(&cfg.text).unwrap_or(DEFAULT_TEXT),
            text_dim: parse_hex(&cfg.text_dim).unwrap_or(DEFAULT_TEXT_DIM),
            border: parse_hex(&cfg.border).unwrap_or(DEFAULT_BORDER),
            agent: parse_hex(&cfg.agent).unwrap_or(DEFAULT_AGENT),
            shield: parse_hex(&cfg.shield).unwrap_or(DEFAULT_SHIELD),
        }
    }
}

static THEME: OnceLock<ParsedTheme> = OnceLock::new();

fn t() -> &'static ParsedTheme {
    THEME.get_or_init(|| ParsedTheme::from_config(&ThemeConfig::load()))
}

pub fn accent_color() -> Color {
    t().accent
}
pub fn good_color() -> Color {
    t().good
}
pub fn warn_color() -> Color {
    t().warn
}
pub fn bad_color() -> Color {
    t().bad
}
pub fn text_color() -> Color {
    t().text
}
pub fn text_dim_color() -> Color {
    t().text_dim
}
pub fn border_color() -> Color {
    t().border
}
pub fn agent_color() -> Color {
    t().agent
}
pub fn shield_color() -> Color {
    t().shield
}

// Style helpers — use cached color accessors.
pub fn accent() -> Style {
    Style::default().fg(accent_color())
}

pub fn accent_bold() -> Style {
    Style::default()
        .fg(accent_color())
        .add_modifier(Modifier::BOLD)
}

pub fn dim() -> Style {
    Style::default().fg(text_dim_color())
}

pub fn muted() -> Style {
    Style::default().fg(Color::DarkGray)
}

pub fn bold() -> Style {
    Style::default()
        .fg(text_color())
        .add_modifier(Modifier::BOLD)
}

pub fn good() -> Style {
    Style::default().fg(good_color())
}

pub fn warn() -> Style {
    Style::default().fg(warn_color())
}

pub fn bad() -> Style {
    Style::default().fg(bad_color())
}

pub fn border() -> Style {
    Style::default().fg(border_color())
}

pub fn user_msg() -> Style {
    Style::default()
        .fg(text_color())
        .add_modifier(Modifier::BOLD)
}

pub fn tool_name() -> Style {
    Style::default().fg(FLAME).add_modifier(Modifier::BOLD)
}

pub fn tool_detail() -> Style {
    Style::default().fg(text_dim_color())
}

pub fn thinking() -> Style {
    Style::default()
        .fg(text_dim_color())
        .add_modifier(Modifier::ITALIC)
}

pub fn code() -> Style {
    Style::default().fg(FLAME)
}

pub fn heading1() -> Style {
    Style::default()
        .fg(accent_color())
        .add_modifier(Modifier::BOLD)
}

pub fn heading2() -> Style {
    Style::default()
        .fg(text_color())
        .add_modifier(Modifier::BOLD)
}

pub fn bullet() -> Style {
    Style::default().fg(accent_color())
}

pub fn diff_add() -> Style {
    Style::default().fg(good_color())
}

pub fn diff_del() -> Style {
    Style::default().fg(bad_color())
}

pub fn diff_hunk() -> Style {
    Style::default().fg(accent_color())
}

pub fn agent_name() -> Style {
    Style::default()
        .fg(agent_color())
        .add_modifier(Modifier::BOLD)
}

pub fn agent_detail() -> Style {
    Style::default()
        .fg(text_dim_color())
        .add_modifier(Modifier::ITALIC)
}

pub fn warden_name() -> Style {
    Style::default()
        .fg(shield_color())
        .add_modifier(Modifier::BOLD)
}

pub fn verdict_ship() -> Style {
    Style::default()
        .fg(good_color())
        .add_modifier(Modifier::BOLD)
}

pub fn verdict_revise() -> Style {
    Style::default()
        .fg(warn_color())
        .add_modifier(Modifier::BOLD)
}

pub fn verdict_hold() -> Style {
    Style::default()
        .fg(bad_color())
        .add_modifier(Modifier::BOLD)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_hex_valid() {
        assert_eq!(parse_hex("#FF0000"), Some(Color::Rgb(255, 0, 0)));
        assert_eq!(parse_hex("00FF00"), Some(Color::Rgb(0, 255, 0)));
        assert_eq!(parse_hex("#2EC4B6"), Some(Color::Rgb(0x2E, 0xC4, 0xB6)));
    }

    #[test]
    fn parse_hex_invalid() {
        assert_eq!(parse_hex(""), None);
        assert_eq!(parse_hex("#FFF"), None);
        assert_eq!(parse_hex("ZZZZZZ"), None);
    }

    #[test]
    fn default_theme_parses() {
        let t = ThemeConfig::default();
        assert!(parse_hex(&t.accent).is_some());
        assert!(parse_hex(&t.good).is_some());
        assert!(parse_hex(&t.bad).is_some());
    }

    #[test]
    fn partial_json_uses_defaults() {
        let json = r##"{"accent": "#FF0000"}"##;
        let t: ThemeConfig = serde_json::from_str(json).unwrap();
        assert_eq!(t.accent, "#FF0000");
        assert_eq!(t.good, "#4EC990"); // default
    }

    #[test]
    fn invalid_json_falls_back() {
        let result: Result<ThemeConfig, _> = serde_json::from_str("not json");
        assert!(result.is_err());
    }
}
