mod app;
mod backend;
mod bidi;
mod config;
mod panels;
mod platform;
mod theme;
mod ui;
mod widgets;

use std::io::{self, IsTerminal};
use std::time::Duration;

use crossterm::event::{
    self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    Event, KeyCode, KeyEventKind, KeyModifiers, KeyboardEnhancementFlags, MouseEvent,
    MouseEventKind, PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::prelude::*;

use app::{App, Tab};

fn main() -> io::Result<()> {
    if !io::stdout().is_terminal() {
        print_static();
        return Ok(());
    }

    terminal::enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(
        stdout,
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture,
        PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
    )?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new();

    loop {
        app.poll_response();
        terminal.draw(|frame| ui::render(frame, &app))?;

        if event::poll(Duration::from_millis(50))? {
            let ev = event::read()?;
            match ev {
                Event::Paste(ref text) if app.tab == Tab::Chat => {
                    for c in text.chars() {
                        if c == '\n' || c == '\r' {
                            app.input_newline();
                        } else {
                            app.input_char(c);
                        }
                    }
                }
                Event::Mouse(mouse)
                    if app.tab == Tab::Chat && handle_chat_mouse_event(&mut app, mouse) =>
                {
                    continue;
                }
                Event::Key(key) => {
                    if key.kind != KeyEventKind::Press {
                        continue;
                    }

                    // Any non-Esc key clears the pending Esc
                    if key.code != KeyCode::Esc {
                        app.esc_pending = None;
                    }

                    // Global Ctrl shortcuts
                    if key.modifiers.contains(KeyModifiers::CONTROL) {
                        match key.code {
                            KeyCode::Char('c') => {
                                if matches!(app.chat_state, crate::app::ChatState::Streaming) {
                                    app.cancel_response();
                                } else {
                                    break;
                                }
                                continue;
                            }
                            KeyCode::Char('d') => break,
                            _ => {}
                        }
                    }

                    if app.tab == Tab::Chat {
                        if key.modifiers.contains(KeyModifiers::SUPER) {
                            match key.code {
                                KeyCode::Backspace | KeyCode::Delete => {
                                    app.input_delete_current_line()
                                }
                                _ => {}
                            }
                            continue;
                        }
                        if key.modifiers.contains(KeyModifiers::CONTROL) {
                            match key.code {
                                KeyCode::Char('l') => {
                                    app.chat_messages.clear();
                                    app.scroll_to_bottom();
                                }
                                KeyCode::Char('u') => app.input_clear_line(),
                                KeyCode::Char('a') => app.input_home(),
                                KeyCode::Char('e') => app.input_end(),
                                KeyCode::Char('w') => app.input_delete_word(),
                                KeyCode::Char('k') => app.input_kill_to_end(),
                                KeyCode::Char('y') => app.input_yank(),
                                KeyCode::Char('o') => app.toggle_tools(),
                                KeyCode::Char('j') => app.input_newline(),
                                _ => {}
                            }
                            continue;
                        }
                        if key.modifiers.contains(KeyModifiers::ALT) {
                            match key.code {
                                KeyCode::Backspace => app.input_delete_word(),
                                KeyCode::Char('b') => app.input_word_left(),
                                KeyCode::Char('f') => app.input_word_right(),
                                _ => {}
                            }
                            continue;
                        }
                        match key.code {
                            KeyCode::Esc => {
                                if app.has_suggestions() {
                                    app.dismiss_suggestions();
                                } else if matches!(app.chat_state, crate::app::ChatState::Streaming)
                                {
                                    app.cancel_response();
                                } else if let Some(first) = app.esc_pending {
                                    if first.elapsed().as_millis() < 500 {
                                        break;
                                    }
                                    app.esc_pending = Some(std::time::Instant::now());
                                } else {
                                    app.esc_pending = Some(std::time::Instant::now());
                                }
                            }
                            KeyCode::Tab if app.has_suggestions() => {
                                app.accept_suggestion();
                            }
                            KeyCode::Enter => {
                                if key.modifiers.contains(KeyModifiers::SHIFT) {
                                    app.input_newline();
                                } else if app.has_suggestions() {
                                    app.accept_suggestion();
                                } else {
                                    app.send_message();
                                }
                            }
                            KeyCode::Up => {
                                if app.has_suggestions() {
                                    app.prev_suggestion();
                                } else if app.is_multiline() && app.input_cursor_line() > 0 {
                                    app.input_line_up();
                                } else {
                                    app.history_prev();
                                }
                            }
                            KeyCode::Down => {
                                if app.has_suggestions() {
                                    app.next_suggestion();
                                } else if app.is_multiline()
                                    && app.input_cursor_line() < app.input_line_count() - 1
                                {
                                    app.input_line_down();
                                } else {
                                    app.history_next();
                                }
                            }
                            KeyCode::PageUp => app.scroll_up(10),
                            KeyCode::PageDown => app.scroll_down(10),
                            KeyCode::Backspace => app.input_backspace(),
                            KeyCode::Delete => app.input_delete(),
                            KeyCode::Left => app.input_left(),
                            KeyCode::Right => app.input_right(),
                            KeyCode::Home => app.input_home(),
                            KeyCode::End => app.input_end(),
                            KeyCode::Char(c) => app.input_char(c),
                            _ => {}
                        }
                    } else {
                        match key.code {
                            KeyCode::Esc | KeyCode::Char('q') => {
                                app.tab = Tab::Chat;
                                app.cursor = 0;
                            }
                            KeyCode::Up | KeyCode::Char('k') => app.prev_item(),
                            KeyCode::Down | KeyCode::Char('j') => app.next_item(),
                            KeyCode::Char(' ') | KeyCode::Enter => app.toggle_item(),
                            KeyCode::Char('r') => app.refresh(),
                            _ => {}
                        }
                    }
                }
                _ => {}
            }
        }
    }

    terminal::disable_raw_mode()?;
    execute!(
        io::stdout(),
        PopKeyboardEnhancementFlags,
        LeaveAlternateScreen,
        DisableBracketedPaste,
        DisableMouseCapture
    )?;

    if let Some(ctx_file) = platform::env_var("DEUS_TUI_CONTEXT_FILE") {
        let _ = std::fs::remove_file(ctx_file);
    }

    Ok(())
}

fn print_static() {
    let wardens = config::wardens::load();
    let services = config::healthcheck::load();
    let channels = config::channels::load();
    let deus_cfg = config::deus::load();

    let w = 72;
    let top = format!("╭{}╮", "─".repeat(w - 2));
    let bot = format!("╰{}╯", "─".repeat(w - 2));
    let sep = format!("├{}┤", "─".repeat(w - 2));
    let line = |s: &str| {
        let visible_len = s.chars().count();
        let pad = (w - 4).saturating_sub(visible_len);
        format!("│ {}{} │", s, " ".repeat(pad))
    };
    let header = |s: &str| {
        let pad = w - 4 - s.len();
        let left = pad / 2;
        let right = pad - left;
        format!("│{}{}{}│", " ".repeat(left + 1), s, " ".repeat(right + 1))
    };

    println!();
    println!("{}", top);
    println!("{}", header("◆ D E U S"));
    println!("{}", sep);

    println!("{}", line(""));
    println!("{}", line("  WARDENS"));
    println!("{}", line(""));
    for entry in &wardens {
        let icon = if entry.enabled { "✓" } else { "✗" };
        let row = format!("  {} {:24} {}", icon, entry.name, entry.warden_type);
        println!("{}", line(&row));
    }

    println!("{}", line(""));
    println!("{}", sep);
    println!("{}", line(""));
    println!("{}", line("  SERVICES"));
    println!("{}", line(""));
    for svc in &services {
        let icon = match svc.status.as_str() {
            "running" => "✓",
            "stale" => "~",
            _ => "✗",
        };
        let row = format!("  {} {:40} {}", icon, svc.description, svc.status);
        println!("{}", line(&row));
    }

    println!("{}", line(""));
    println!("{}", sep);
    println!("{}", line(""));
    println!("{}", line("  CHANNELS"));
    println!("{}", line(""));
    for ch in &channels {
        let icon = if ch.configured { "✓" } else { "✗" };
        let status = if ch.configured {
            "connected"
        } else {
            "not configured"
        };
        let row = format!("  {} {:16} {}", icon, ch.name, status);
        println!("{}", line(&row));
    }

    println!("{}", line(""));
    println!("{}", sep);
    println!("{}", line(""));
    println!("{}", line("  CONFIG"));
    println!("{}", line(""));
    for (key, value) in &deus_cfg {
        let row = format!("  {:22} {}", key, value);
        println!("{}", line(&row));
    }
    println!("{}", line(""));
    println!("{}", bot);
    println!();
}

fn handle_chat_mouse_event(app: &mut App, mouse: MouseEvent) -> bool {
    match mouse.kind {
        MouseEventKind::ScrollUp => {
            app.scroll_up(3);
            true
        }
        MouseEventKind::ScrollDown => {
            app.scroll_down(3);
            true
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mouse_wheel_scrolls_chat_instead_of_input_history() {
        let mut app = App::new();
        app.input_history = vec!["first".to_string(), "second".to_string()];
        app.history_index = Some(1);

        let handled = handle_chat_mouse_event(
            &mut app,
            MouseEvent {
                kind: MouseEventKind::ScrollUp,
                column: 0,
                row: 0,
                modifiers: KeyModifiers::NONE,
            },
        );

        assert!(handled);
        assert_eq!(app.history_index, Some(1));
        assert_eq!(app.scroll_offset, 3);
        assert!(!app.scroll_pinned);
    }
}
