use std::collections::HashMap;
use std::collections::HashSet;
use std::io::{BufRead, BufReader, Read as _};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use crate::config;
use crate::config::permissions::PermissionsConfig;
use crate::platform;

#[allow(dead_code)]
static NEXT_SESSION_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct SessionId(pub u64);

impl SessionId {
    pub const MAIN: Self = Self(0);

    #[allow(dead_code)]
    pub fn next() -> Self {
        Self(NEXT_SESSION_ID.fetch_add(1, Ordering::Relaxed))
    }
}

impl std::fmt::Display for SessionId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Tab {
    Chat,
    Wardens,
    Services,
    Channels,
    Config,
    Status,
}

impl Tab {
    pub fn label(self) -> &'static str {
        match self {
            Tab::Chat => "Chat",
            Tab::Wardens => "Wardens",
            Tab::Services => "Services",
            Tab::Channels => "Channels",
            Tab::Config => "Config",
            Tab::Status => "Status",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum SubagentStatus {
    Running,
    Completed,
}

#[derive(Clone)]
pub enum MessageBlock {
    Text(String),
    Thinking(String),
    ToolUse {
        #[allow(dead_code)]
        id: String,
        tool: String,
        detail: String,
    },
    SubagentBlock {
        id: String,
        subagent_type: String,
        description: String,
        status: SubagentStatus,
        output_preview: Option<String>,
        is_warden: bool,
    },
}

#[derive(Clone)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
    pub blocks: Vec<MessageBlock>,
}

impl ChatMessage {
    pub fn simple(role: &str, content: &str) -> Self {
        Self {
            role: role.to_string(),
            content: content.to_string(),
            blocks: vec![MessageBlock::Text(content.to_string())],
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ChatState {
    Idle,
    Streaming,
}

pub struct Session {
    #[allow(dead_code)]
    pub id: SessionId,
    pub chat_messages: Vec<ChatMessage>,
    pub chat_state: ChatState,
    pub stream_rx: Option<mpsc::Receiver<backend::StreamChunk>>,
    pub turn_count: u32,
    pub model: String,
    pub effort: String,
    pub token_count: u32,
    pub cost_usd: f64,
    pub turn_start: Option<Instant>,
    pub last_turn_duration: Option<Duration>,
    pub last_thinking_summary: Option<String>,
    pub permissions: PermissionsConfig,
    pub active_subagent_ids: HashSet<String>,
    pub scroll_offset: u16,
    pub scroll_pinned: bool,
    pub chat_dirty: bool,
    pub chat_version: u64,
    pub run_mode: backend::RunMode,
}

impl Session {
    fn new_main(model: String, effort: String, permissions: PermissionsConfig) -> Self {
        Self {
            id: SessionId::MAIN,
            chat_messages: vec![ChatMessage::simple(
                "system",
                "Welcome to Deus. Type a message or / for commands.",
            )],
            chat_state: ChatState::Idle,
            stream_rx: None,
            turn_count: 0,
            model,
            effort,
            token_count: 0,
            cost_usd: 0.0,
            turn_start: None,
            last_turn_duration: None,
            last_thinking_summary: None,
            permissions,
            active_subagent_ids: HashSet::new(),
            scroll_offset: 0,
            scroll_pinned: true,
            chat_dirty: true,
            chat_version: 0,
            run_mode: backend::RunMode::default(),
        }
    }

    pub fn mark_chat_changed(&mut self) {
        self.chat_version = self.chat_version.wrapping_add(1);
        self.chat_dirty = true;
    }
}

pub struct CommandDef {
    pub name: &'static str,
    pub description: &'static str,
    pub args: &'static [&'static str],
}

use crate::backend::{self, ChunkKind, RunConfig};
pub use crate::backend::{
    backend_labels, model_backend_name as model_backend, model_display, model_ids,
    models_for_backend,
};

pub const EFFORT_LEVELS: &[&str] = &["low", "medium", "high", "xhigh", "max"];

pub const COMMANDS: &[CommandDef] = &[
    CommandDef {
        name: "/wardens",
        description: "Quality gates",
        args: &["enable", "disable", "reset"],
    },
    CommandDef {
        name: "/services",
        description: "Service health",
        args: &["refresh"],
    },
    CommandDef {
        name: "/channels",
        description: "Channel status",
        args: &[],
    },
    CommandDef {
        name: "/config",
        description: "Configuration",
        args: &["backend", "vault"],
    },
    CommandDef {
        name: "/status",
        description: "System dashboard",
        args: &["refresh"],
    },
    CommandDef {
        name: "/model",
        description: "Switch model",
        args: &["claude", "codex"],
    },
    CommandDef {
        name: "/effort",
        description: "Reasoning effort",
        args: &["low", "medium", "high", "xhigh", "max"],
    },
    CommandDef {
        name: "/compress",
        description: "Save to vault",
        args: &[],
    },
    CommandDef {
        name: "/checkpoint",
        description: "Mid-session save",
        args: &[],
    },
    CommandDef {
        name: "/compact",
        description: "Compact context",
        args: &[],
    },
    CommandDef {
        name: "/resume",
        description: "Load recent work",
        args: &[],
    },
    CommandDef {
        name: "/history",
        description: "Past sessions",
        args: &["today", "yesterday", "week"],
    },
    CommandDef {
        name: "/init",
        description: "Init CLAUDE.md",
        args: &[],
    },
    CommandDef {
        name: "/help",
        description: "Show commands",
        args: &[],
    },
    CommandDef {
        name: "/permissions",
        description: "Tool permissions",
        args: &["mode", "allow", "deny", "remove", "reset"],
    },
    CommandDef {
        name: "/clear",
        description: "Clear chat",
        args: &[],
    },
    CommandDef {
        name: "/quit",
        description: "Exit",
        args: &[],
    },
];

pub struct App {
    // Session management
    pub sessions: HashMap<SessionId, Session>,
    pub active_session: SessionId,

    // Global UI state
    pub tab: Tab,
    pub cursor: usize,
    pub input: String,
    pub input_cursor: usize,
    pub input_history: Vec<String>,
    pub history_index: Option<usize>,
    pub input_draft: String,
    pub kill_ring: String,
    pub suggestions: Vec<usize>,
    pub arg_suggestions: Vec<String>,
    pub suggestion_cursor: usize,
    pub show_tools: bool,
    pub esc_pending: Option<Instant>,
    pub queued_messages: Vec<String>,
    pub session_start: Instant,

    // Dashboard data
    pub git_branch: String,
    pub wardens: Vec<config::wardens::WardenEntry>,
    pub services: Vec<config::healthcheck::ServiceEntry>,
    pub channels: Vec<config::channels::ChannelEntry>,
    pub deus_config: Vec<(String, String)>,
    pub system_context_file: Option<PathBuf>,
    pub mode: String,
}

// StreamChunk and parsing delegated to backend trait — see backend/mod.rs

fn detect_git_branch() -> String {
    Command::new("git")
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                Some(String::from_utf8_lossy(&o.stdout).trim().to_string())
            } else {
                None
            }
        })
        .unwrap_or_default()
}

pub const SPINNER_FRAMES: &[&str] = &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

impl App {
    pub fn active(&self) -> &Session {
        self.sessions.get(&self.active_session).unwrap()
    }

    pub fn active_mut(&mut self) -> &mut Session {
        self.sessions.get_mut(&self.active_session).unwrap()
    }

    pub fn mark_chat_changed(&mut self) {
        self.active_mut().mark_chat_changed();
    }

    pub fn spinner_frame(&self) -> &'static str {
        let elapsed = self
            .active()
            .turn_start
            .map(|t| t.elapsed().as_millis())
            .unwrap_or(0);
        let idx = (elapsed / 80) as usize % SPINNER_FRAMES.len();
        SPINNER_FRAMES[idx]
    }

    pub fn new() -> Self {
        let ctx_file = platform::env_var("DEUS_TUI_CONTEXT_FILE").map(PathBuf::from);
        let mode = platform::env_var("DEUS_TUI_MODE").unwrap_or_else(|| "home".to_string());
        let backend = platform::env_var("DEUS_TUI_BACKEND").unwrap_or_else(|| "claude".to_string());
        let fallback_model = if backend == "codex" {
            "gpt-5.4"
        } else {
            "sonnet"
        };
        let default_model =
            config::deus::read_key("default_model").unwrap_or_else(|| fallback_model.to_string());
        let mut permissions = PermissionsConfig::load();
        if platform::env_flag("DEUS_TUI_BYPASS") {
            permissions.mode = "bypassPermissions".to_string();
        }

        let main_id = SessionId::MAIN;
        let main_session = Session::new_main(default_model, "high".to_string(), permissions);
        let mut sessions = HashMap::new();
        sessions.insert(main_id, main_session);

        let mut app = Self {
            sessions,
            active_session: main_id,

            tab: Tab::Chat,
            cursor: 0,
            input: String::new(),
            input_cursor: 0,
            input_history: Vec::new(),
            history_index: None,
            input_draft: String::new(),
            kill_ring: String::new(),
            suggestions: Vec::new(),
            arg_suggestions: Vec::new(),
            suggestion_cursor: 0,
            show_tools: false,
            esc_pending: None,
            queued_messages: Vec::new(),
            session_start: Instant::now(),

            git_branch: detect_git_branch(),
            wardens: Vec::new(),
            services: Vec::new(),
            channels: Vec::new(),
            deus_config: Vec::new(),
            system_context_file: ctx_file,
            mode,
        };
        app.refresh();

        if let Some(prompt) = platform::env_var("DEUS_TUI_INITIAL_PROMPT")
            && !prompt.is_empty()
        {
            app.dispatch_message(prompt);
        }

        app
    }

    pub fn refresh(&mut self) {
        self.wardens = config::wardens::load();
        self.services = config::healthcheck::load();
        self.channels = config::channels::load();
        self.deus_config = config::deus::load();
    }

    pub fn next_item(&mut self) {
        let max = self.item_count();
        if max > 0 && self.cursor < max - 1 {
            self.cursor += 1;
        }
    }

    pub fn prev_item(&mut self) {
        if self.cursor > 0 {
            self.cursor -= 1;
        }
    }

    pub fn toggle_item(&mut self) {
        if self.tab == Tab::Wardens && self.cursor < self.wardens.len() {
            self.wardens[self.cursor].enabled = !self.wardens[self.cursor].enabled;
            config::wardens::save(&self.wardens);
        }
    }

    pub fn send_message(&mut self) {
        let msg = self.input.trim().to_string();
        if msg.is_empty() {
            return;
        }

        self.input_history.push(msg.clone());
        self.history_index = None;

        let handled = self.handle_command(&msg);
        if handled {
            self.input.clear();
            self.input_cursor = 0;
            self.suggestions.clear();
            self.arg_suggestions.clear();
            self.mark_chat_changed();
            return;
        }

        if matches!(self.active().chat_state, ChatState::Streaming) {
            self.queued_messages.push(msg);
            self.active_mut()
                .chat_messages
                .push(ChatMessage::simple("system", "(queued)"));
            self.input.clear();
            self.input_cursor = 0;
            self.suggestions.clear();
            self.scroll_to_bottom();
            return;
        }

        self.input.clear();
        self.input_cursor = 0;
        self.suggestions.clear();
        self.arg_suggestions.clear();
        self.dispatch_message(msg);
    }

    fn dispatch_message(&mut self, msg: String) {
        let ctx_file = self.system_context_file.clone();
        let session = self.active_mut();
        session
            .chat_messages
            .push(ChatMessage::simple("user", &msg));
        session.chat_state = ChatState::Streaming;
        session.turn_start = Some(Instant::now());
        session.last_thinking_summary = None;
        session.mark_chat_changed();
        session.scroll_offset = 0;
        session.scroll_pinned = true;

        let (tx, rx) = mpsc::channel();
        session.stream_rx = Some(rx);
        let config = RunConfig {
            model: session.model.clone(),
            message: msg,
            effort: session.effort.clone(),
            is_continuation: session.turn_count > 0,
            system_context_file: if session.turn_count == 0 {
                ctx_file
            } else {
                None
            },
            permissions: session.permissions.clone(),
            run_mode: session.run_mode.clone(),
        };
        session.turn_count += 1;
        let be = backend::backend_for(&config.model);

        thread::spawn(move || {
            let child = be.build_command(&config).spawn();

            match child {
                Ok(mut process) => {
                    let stderr_handle = process.stderr.take().map(|stderr| {
                        let tx2 = tx.clone();
                        thread::spawn(move || {
                            let mut buf = String::new();
                            let mut reader = BufReader::new(stderr);
                            let _ = reader.read_to_string(&mut buf);
                            if !buf.trim().is_empty() {
                                let _ = tx2.send(backend::StreamChunk {
                                    kind: ChunkKind::Error(buf.trim().to_string()),
                                });
                            }
                        })
                    });
                    if let Some(stdout) = process.stdout.take() {
                        let reader = BufReader::new(stdout);
                        for line in reader.lines() {
                            match line {
                                Ok(text) => {
                                    for chunk in be.parse_line(&text) {
                                        let _ = tx.send(chunk);
                                    }
                                }
                                Err(e) => {
                                    let _ = tx.send(backend::StreamChunk {
                                        kind: ChunkKind::Error(e.to_string()),
                                    });
                                    break;
                                }
                            }
                        }
                    }
                    let _ = process.wait();
                    if let Some(h) = stderr_handle {
                        let _ = h.join();
                    }
                    let _ = tx.send(backend::StreamChunk {
                        kind: ChunkKind::Done,
                    });
                }
                Err(e) => {
                    let _ = tx.send(backend::StreamChunk {
                        kind: ChunkKind::Error(format!("Failed to launch: {}", e)),
                    });
                    let _ = tx.send(backend::StreamChunk {
                        kind: ChunkKind::Done,
                    });
                }
            }
        });

        self.active_mut().chat_messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: String::new(),
            blocks: Vec::new(),
        });
    }

    fn handle_command(&mut self, msg: &str) -> bool {
        let parts: Vec<&str> = msg.splitn(2, ' ').collect();
        let cmd = parts[0];
        let arg = parts.get(1).unwrap_or(&"").trim();

        match cmd {
            "/wardens" if arg.is_empty() => {
                self.tab = Tab::Wardens;
                self.cursor = 0;
                true
            }
            "/wardens" => false,
            "/services" => {
                self.tab = Tab::Services;
                self.cursor = 0;
                true
            }
            "/channels" => {
                self.tab = Tab::Channels;
                self.cursor = 0;
                true
            }
            "/config" => {
                self.tab = Tab::Config;
                self.cursor = 0;
                true
            }
            "/status" => {
                self.tab = Tab::Status;
                self.cursor = 0;
                true
            }
            "/clear" => {
                let session = self.active_mut();
                session.chat_messages.clear();
                session.turn_count = 0;
                session.cost_usd = 0.0;
                session.token_count = 0;
                session.last_turn_duration = None;
                session.last_thinking_summary = None;
                session.active_subagent_ids.clear();
                self.scroll_to_bottom();
                true
            }
            "/quit" => {
                std::process::exit(0);
            }
            "/model" => {
                let ids = model_ids();
                let (current_model, current_effort, current_perm_mode) = {
                    let s = self.active();
                    (
                        s.model.clone(),
                        s.effort.clone(),
                        s.permissions.mode.clone(),
                    )
                };

                if arg.is_empty() {
                    let claude_models = models_for_backend("claude");
                    let codex_models = models_for_backend("codex");
                    self.active_mut().chat_messages.push(ChatMessage::simple("system", &format!(
                        "Current: {} [{}] | Effort: {}\n\n  Claude (Anthropic)\n{}\n\n  Codex (OpenAI)\n{}\n\nUsage: /model <id>  or  /model claude  /model codex",
                        model_display(&current_model), model_backend(&current_model), current_effort,
                        claude_models.iter().map(|m| format!("    {}", m)).collect::<Vec<_>>().join("\n"),
                        codex_models.iter().map(|m| format!("    {}", m)).collect::<Vec<_>>().join("\n"),
                    )));
                } else if arg == "claude" {
                    let models = models_for_backend("claude");
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Claude models:\n{}",
                            models
                                .iter()
                                .map(|m| format!("  {}", m))
                                .collect::<Vec<_>>()
                                .join("\n"),
                        ),
                    ));
                } else if arg == "codex" {
                    let models = models_for_backend("codex");
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Codex models:\n{}",
                            models
                                .iter()
                                .map(|m| format!("  {}", m))
                                .collect::<Vec<_>>()
                                .join("\n"),
                        ),
                    ));
                } else if ids.contains(&arg) {
                    let prev_backend = model_backend(&current_model);
                    let new_backend = model_backend(arg);
                    let session = self.active_mut();
                    session.model = arg.to_string();
                    if prev_backend != new_backend {
                        session.turn_count = 0;
                        session.chat_messages.push(ChatMessage::simple("system", &format!(
                            "⚠ Switched to {} [{}]. Backend changed from {} → {} — conversation history reset.",
                            model_display(&session.model), new_backend, prev_backend, new_backend
                        )));
                        if !PermissionsConfig::supports_mode(new_backend, &current_perm_mode) {
                            let available = PermissionsConfig::available_modes(new_backend);
                            session.chat_messages.push(ChatMessage::simple("system", &format!(
                                "⚠ Permission mode '{}' is not supported by {}. Available: {}. Use /permissions mode <mode> to change.",
                                current_perm_mode, new_backend, available.join(", ")
                            )));
                        }
                    } else {
                        session.chat_messages.push(ChatMessage::simple(
                            "system",
                            &format!(
                                "Switched to {} [{}]",
                                model_display(&session.model),
                                new_backend
                            ),
                        ));
                    }
                } else {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Unknown model: {}. Try /model to see available models.",
                            arg
                        ),
                    ));
                }
                true
            }
            "/effort" => {
                if arg.is_empty() {
                    let effort = self.active().effort.clone();
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Current effort: {}. Available: {}",
                            effort,
                            EFFORT_LEVELS.join(", ")
                        ),
                    ));
                } else if EFFORT_LEVELS.contains(&arg) {
                    self.active_mut().effort = arg.to_string();
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Effort set to {}", arg),
                    ));
                } else {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Unknown effort: {}. Available: {}",
                            arg,
                            EFFORT_LEVELS.join(", ")
                        ),
                    ));
                }
                true
            }
            "/help" => {
                let help: String = COMMANDS
                    .iter()
                    .map(|c| format!("  {:16} {}", c.name, c.description))
                    .collect::<Vec<_>>()
                    .join("\n");
                self.active_mut().chat_messages.push(ChatMessage::simple("system", &format!("Available commands:\n\n{}\n\nKeyboard shortcuts:\n  Ctrl+L  Clear screen\n  Ctrl+U  Clear input line\n  Ctrl+K  Kill to end of line\n  Ctrl+Y  Yank (paste killed text)\n  Ctrl+A  Start of line\n  Ctrl+E  End of line\n  Ctrl+J  New line (multi-line input)\n  Ctrl+O  Toggle tool/thinking details\n  Ctrl+D  Exit\n  Alt+B   Word left\n  Alt+F   Word right\n  ↑/↓     Input history\n  PgUp/Dn Scroll chat\n  Mouse   Scroll chat (Shift+drag to select text)", help)));
                true
            }
            "/history" => {
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    "Session history browsing coming in Phase 2.",
                ));
                true
            }
            "/permissions" => {
                self.handle_permissions_command(arg);
                true
            }
            "/init" | "/compress" | "/checkpoint" | "/compact" | "/resume" => false,
            _ => false,
        }
    }

    fn handle_permissions_command(&mut self, arg: &str) {
        let parts: Vec<&str> = arg.splitn(2, ' ').collect();
        let sub = parts[0];
        let value = parts.get(1).unwrap_or(&"").trim();

        match sub {
            "" => {
                let (backend, available, allowed, disallowed, perm_mode) = {
                    let s = self.active();
                    let be = model_backend(&s.model);
                    let avail = PermissionsConfig::available_modes(be);
                    let al = if s.permissions.allowed_tools.is_empty() {
                        "(none)".to_string()
                    } else {
                        s.permissions.allowed_tools.join(", ")
                    };
                    let dis = if s.permissions.disallowed_tools.is_empty() {
                        "(none)".to_string()
                    } else {
                        s.permissions.disallowed_tools.join(", ")
                    };
                    let pm = s.permissions.mode.clone();
                    (be, avail, al, dis, pm)
                };
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    &format!(
                        "Permission mode: {} [{}]\nAvailable modes: {}\n\nAllowed tools: {}\nDisallowed tools: {}\n\nUsage:\n  /permissions mode <mode>\n  /permissions allow <tool>\n  /permissions deny <tool>\n  /permissions remove <tool>\n  /permissions reset",
                        perm_mode,
                        backend,
                        available.join(", "),
                        allowed,
                        disallowed,
                    ),
                ));
            }
            "mode" if value.is_empty() => {
                let (backend, available, perm_mode) = {
                    let s = self.active();
                    let be = model_backend(&s.model);
                    let avail = PermissionsConfig::available_modes(be);
                    let pm = s.permissions.mode.clone();
                    (be, avail, pm)
                };
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    &format!(
                        "Current: {}. Available for {}: {}",
                        perm_mode,
                        backend,
                        available.join(", ")
                    ),
                ));
            }
            "mode" => {
                let backend = model_backend(&self.active().model);
                if !PermissionsConfig::supports_mode(backend, value) {
                    let available = PermissionsConfig::available_modes(backend);
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!(
                            "Mode '{}' is not supported by {}. Available: {}",
                            value,
                            backend,
                            available.join(", ")
                        ),
                    ));
                } else if self.active_mut().permissions.set_mode(value) {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Permission mode set to: {}", value),
                    ));
                } else {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Unknown mode: {}", value),
                    ));
                }
            }
            "allow" if !value.is_empty() => {
                self.active_mut().permissions.add_allowed(value);
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    &format!("Allowed: {}", value),
                ));
            }
            "deny" if !value.is_empty() => {
                self.active_mut().permissions.add_disallowed(value);
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    &format!("Disallowed: {}", value),
                ));
            }
            "remove" if !value.is_empty() => {
                if self.active_mut().permissions.remove(value) {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Removed: {}", value),
                    ));
                } else {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Not found: {}", value),
                    ));
                }
            }
            "reset" => {
                self.active_mut().permissions.reset();
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    "Permissions reset to defaults.",
                ));
            }
            _ => {
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    "Usage: /permissions [mode|allow|deny|remove|reset] [value]",
                ));
            }
        }
    }

    fn is_warden_name(&self, name: &str) -> bool {
        self.wardens.iter().any(|w| w.name == name)
    }

    pub fn poll_response(&mut self) {
        let session = self.active_mut();
        if let Some(rx) = &session.stream_rx {
            let mut had_chunks = false;
            let mut chunks: Vec<backend::StreamChunk> = Vec::new();
            while let Ok(chunk) = rx.try_recv() {
                had_chunks = true;
                chunks.push(chunk);
            }
            for chunk in chunks {
                match chunk.kind {
                    ChunkKind::Text(text) => {
                        let session = self.active_mut();
                        if let Some(last) = session.chat_messages.last_mut()
                            && last.role == "assistant"
                        {
                            if !last.content.is_empty() {
                                last.content.push('\n');
                            }
                            last.content.push_str(&text);
                            last.blocks.push(MessageBlock::Text(text));
                        }
                    }
                    ChunkKind::Thinking(text) => {
                        let summary: String = text.chars().take(100).collect();
                        let session = self.active_mut();
                        session.last_thinking_summary = Some(if summary.len() < text.len() {
                            format!("{}...", summary)
                        } else {
                            summary
                        });
                        if let Some(last) = session.chat_messages.last_mut()
                            && last.role == "assistant"
                        {
                            last.blocks.push(MessageBlock::Thinking(text));
                        }
                    }
                    ChunkKind::ToolUse { id, tool, detail } => {
                        let session = self.active_mut();
                        if let Some(last) = session.chat_messages.last_mut()
                            && last.role == "assistant"
                        {
                            let line = format!("[{}] {}", tool, detail);
                            if !last.content.is_empty() {
                                last.content.push('\n');
                            }
                            last.content.push_str(&line);
                            last.blocks.push(MessageBlock::ToolUse { id, tool, detail });
                        }
                    }
                    ChunkKind::SubagentStart {
                        id,
                        subagent_type,
                        description,
                    } => {
                        let is_warden = self.is_warden_name(&subagent_type);
                        let session = self.active_mut();
                        session.active_subagent_ids.insert(id.clone());
                        if let Some(last) = session.chat_messages.last_mut()
                            && last.role == "assistant"
                        {
                            last.blocks.push(MessageBlock::SubagentBlock {
                                id,
                                subagent_type,
                                description,
                                status: SubagentStatus::Running,
                                output_preview: None,
                                is_warden,
                            });
                        }
                    }
                    ChunkKind::ToolResult {
                        id,
                        content_preview,
                    } => {
                        let session = self.active_mut();
                        if session.active_subagent_ids.remove(&id)
                            && let Some(last) = session.chat_messages.last_mut()
                        {
                            for block in &mut last.blocks {
                                if let MessageBlock::SubagentBlock {
                                    id: bid,
                                    status,
                                    output_preview: preview,
                                    ..
                                } = block
                                    && *bid == id
                                {
                                    *status = SubagentStatus::Completed;
                                    *preview = Some(content_preview.clone());
                                    break;
                                }
                            }
                        }
                    }
                    ChunkKind::CostUpdate {
                        cost_usd,
                        input_tokens,
                        output_tokens,
                    } => {
                        let session = self.active_mut();
                        session.cost_usd = cost_usd;
                        session.token_count = (input_tokens + output_tokens) as u32;
                    }
                    ChunkKind::PermissionDenials(denials) => {
                        let denied_list: Vec<String> = denials
                            .iter()
                            .map(|d| {
                                if d.tool_input_preview.is_empty() {
                                    d.tool_name.clone()
                                } else {
                                    format!("{}({})", d.tool_name, d.tool_input_preview)
                                }
                            })
                            .collect();
                        self.active_mut()
                            .chat_messages
                            .push(ChatMessage::simple(
                                "system",
                                &format!(
                                    "Permission denied for: {}\nUse /permissions allow <tool> to approve.",
                                    denied_list.join(", ")
                                ),
                            ));
                    }
                    ChunkKind::Done => {
                        let session = self.active_mut();
                        session.chat_state = ChatState::Idle;
                        session.stream_rx = None;
                        session.last_turn_duration = session.turn_start.map(|t| t.elapsed());
                        session.turn_start = None;
                        session.active_subagent_ids.clear();
                        session.mark_chat_changed();
                        if !self.queued_messages.is_empty() {
                            let next = self.queued_messages.remove(0);
                            self.dispatch_message(next);
                        }
                        return;
                    }
                    ChunkKind::Error(e) => {
                        let session = self.active_mut();
                        if let Some(last) = session.chat_messages.last_mut()
                            && last.role == "assistant"
                        {
                            last.content.push_str(&format!("\n[Error: {}]", e));
                        }
                    }
                }
            }
            if had_chunks {
                self.mark_chat_changed();
            }
        }
    }

    pub fn update_suggestions(&mut self) {
        self.arg_suggestions.clear();

        if !self.input.starts_with('/') {
            self.suggestions.clear();
            self.suggestion_cursor = 0;
            return;
        }

        if let Some(space_idx) = self.input.find(' ') {
            let cmd_part = &self.input[..space_idx];
            let arg_part = &self.input[space_idx + 1..];

            if let Some(cmd) = COMMANDS.iter().find(|c| c.name == cmd_part) {
                if cmd.name == "/model" {
                    if arg_part.is_empty() {
                        self.arg_suggestions = backend_labels();
                    } else if arg_part == "claude" || arg_part.starts_with("claude ") {
                        self.arg_suggestions = models_for_backend("claude");
                    } else if arg_part == "codex" || arg_part.starts_with("codex ") {
                        self.arg_suggestions = models_for_backend("codex");
                    } else {
                        let mut all: Vec<String> = Vec::new();
                        for b in backend::all_backends() {
                            for m in b.models() {
                                let label = format!("{} ({})", m.display, m.context);
                                if m.id.starts_with(arg_part)
                                    || m.display
                                        .to_lowercase()
                                        .starts_with(&arg_part.to_lowercase())
                                {
                                    all.push(label);
                                }
                            }
                        }
                        self.arg_suggestions = all;
                    }
                } else if !cmd.args.is_empty() {
                    self.arg_suggestions = cmd
                        .args
                        .iter()
                        .filter(|a| a.starts_with(arg_part))
                        .map(|a| a.to_string())
                        .collect();
                }
                if self.suggestion_cursor >= self.arg_suggestions.len() {
                    self.suggestion_cursor = 0;
                }
            }
            self.suggestions.clear();
            return;
        }

        let prefix = &self.input;
        self.suggestions = COMMANDS
            .iter()
            .enumerate()
            .filter(|(_, cmd)| cmd.name.starts_with(prefix))
            .map(|(i, _)| i)
            .collect();
        if self.suggestion_cursor >= self.suggestions.len() {
            self.suggestion_cursor = 0;
        }
    }

    pub fn has_suggestions(&self) -> bool {
        (!self.suggestions.is_empty() || !self.arg_suggestions.is_empty())
            && self.input.starts_with('/')
    }

    pub fn dismiss_suggestions(&mut self) {
        self.suggestions.clear();
        self.arg_suggestions.clear();
        self.suggestion_cursor = 0;
    }

    pub fn cancel_response(&mut self) {
        let session = self.active_mut();
        session.chat_state = ChatState::Idle;
        session.stream_rx = None;
        if let Some(last) = session.chat_messages.last_mut() {
            if last.role == "assistant" && last.content.is_empty() {
                session.chat_messages.pop();
            } else if last.role == "assistant" {
                last.content.push_str("\n[cancelled]");
            }
        }
    }

    pub fn suggestion_is_exact_match(&self) -> bool {
        if !self.arg_suggestions.is_empty() {
            if let Some(arg) = self.arg_suggestions.get(self.suggestion_cursor)
                && let Some(space_idx) = self.input.find(' ')
            {
                let typed_arg = &self.input[space_idx + 1..];
                let arg_value = arg.split_whitespace().next().unwrap_or(arg);
                return typed_arg == arg_value;
            }
            return false;
        }
        if let Some(&cmd_idx) = self.suggestions.get(self.suggestion_cursor) {
            let cmd = &COMMANDS[cmd_idx];
            return self.input == cmd.name;
        }
        false
    }

    pub fn accept_suggestion(&mut self) {
        if !self.arg_suggestions.is_empty() {
            if let Some(arg) = self.arg_suggestions.get(self.suggestion_cursor) {
                let space_idx = self.input.find(' ').unwrap_or(self.input.len());
                let cmd_part = &self.input[..space_idx];
                let arg_value = if cmd_part == "/model" {
                    backend::model_id_from_suggestion(arg)
                        .unwrap_or_else(|| arg.split_whitespace().next().unwrap_or(arg).to_string())
                } else {
                    arg.split_whitespace().next().unwrap_or(arg).to_string()
                };
                self.input = format!("{} {}", cmd_part, arg_value);
                self.input_cursor = self.input.len();
                self.arg_suggestions.clear();
                self.suggestions.clear();
            }
            return;
        }
        if let Some(&cmd_idx) = self.suggestions.get(self.suggestion_cursor) {
            let cmd = &COMMANDS[cmd_idx];
            self.input = cmd.name.to_string();
            if !cmd.args.is_empty() {
                self.input.push(' ');
            }
            self.input_cursor = self.input.len();
            self.suggestions.clear();
            self.update_suggestions();
        }
    }

    pub fn next_suggestion(&mut self) {
        let total = self.suggestion_count();
        if total > 0 {
            self.suggestion_cursor = (self.suggestion_cursor + 1) % total;
        }
    }

    pub fn prev_suggestion(&mut self) {
        let total = self.suggestion_count();
        if total > 0 {
            self.suggestion_cursor = (self.suggestion_cursor + total - 1) % total;
        }
    }

    fn suggestion_count(&self) -> usize {
        if !self.arg_suggestions.is_empty() {
            self.arg_suggestions.len()
        } else {
            self.suggestions.len()
        }
    }

    pub fn input_char(&mut self, c: char) {
        self.input.insert(self.input_cursor, c);
        self.input_cursor += c.len_utf8();
        self.update_suggestions();
        self.history_index = None;
    }

    pub fn input_backspace(&mut self) {
        if self.input_cursor > 0 {
            let prev = self.input[..self.input_cursor]
                .char_indices()
                .next_back()
                .map(|(i, _)| i)
                .unwrap_or(0);
            self.input.drain(prev..self.input_cursor);
            self.input_cursor = prev;
            self.update_suggestions();
        }
    }

    pub fn input_delete_word(&mut self) {
        if self.input_cursor == 0 {
            return;
        }
        let before = &self.input[..self.input_cursor];
        let end = before.trim_end().len();
        let word_start = before[..end].rfind(' ').map(|i| i + 1).unwrap_or(0);
        self.input.drain(word_start..self.input_cursor);
        self.input_cursor = word_start;
        self.update_suggestions();
    }

    pub fn input_delete(&mut self) {
        if self.input_cursor < self.input.len() {
            let next = self.input[self.input_cursor..]
                .char_indices()
                .nth(1)
                .map(|(i, _)| self.input_cursor + i)
                .unwrap_or(self.input.len());
            self.input.drain(self.input_cursor..next);
            self.update_suggestions();
        }
    }

    pub fn input_left(&mut self) {
        if self.input_cursor > 0 {
            self.input_cursor = self.input[..self.input_cursor]
                .char_indices()
                .next_back()
                .map(|(i, _)| i)
                .unwrap_or(0);
        }
    }

    pub fn input_right(&mut self) {
        if self.input_cursor < self.input.len() {
            self.input_cursor = self.input[self.input_cursor..]
                .char_indices()
                .nth(1)
                .map(|(i, _)| self.input_cursor + i)
                .unwrap_or(self.input.len());
        }
    }

    pub fn input_home(&mut self) {
        self.input_cursor = 0;
    }

    pub fn input_end(&mut self) {
        self.input_cursor = self.input.len();
    }

    pub fn input_delete_current_line(&mut self) {
        let line_start = self.input[..self.input_cursor]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0);
        let line_end = self.input[self.input_cursor..]
            .find('\n')
            .map(|i| self.input_cursor + i)
            .unwrap_or(self.input.len());
        let drain_start = if line_start > 0 {
            line_start - 1
        } else {
            line_start
        };
        let drain_end = if line_end < self.input.len() && drain_start == line_start {
            line_end + 1
        } else {
            line_end
        };
        self.input.drain(drain_start..drain_end);
        self.input_cursor = drain_start.min(self.input.len());
        self.update_suggestions();
    }

    pub fn input_clear_line(&mut self) {
        self.input.clear();
        self.input_cursor = 0;
        self.update_suggestions();
    }

    pub fn history_prev(&mut self) {
        if self.input_history.is_empty() {
            return;
        }
        if self.history_index.is_none() {
            self.input_draft = self.input.clone();
        }
        let idx = match self.history_index {
            None => self.input_history.len() - 1,
            Some(i) if i > 0 => i - 1,
            Some(_) => return,
        };
        self.history_index = Some(idx);
        self.input = self.input_history[idx].clone();
        self.input_cursor = self.input.len();
        self.update_suggestions();
    }

    pub fn history_next(&mut self) {
        match self.history_index {
            None => return,
            Some(i) if i < self.input_history.len() - 1 => {
                self.history_index = Some(i + 1);
                self.input = self.input_history[i + 1].clone();
            }
            _ => {
                self.history_index = None;
                self.input = self.input_draft.clone();
                self.input_draft.clear();
            }
        }
        self.input_cursor = self.input.len();
        self.update_suggestions();
    }

    #[allow(dead_code)]
    pub fn session_duration(&self) -> String {
        let secs = self.session_start.elapsed().as_secs();
        if secs < 60 {
            format!("{}s", secs)
        } else if secs < 3600 {
            format!("{}m", secs / 60)
        } else {
            format!("{}h{}m", secs / 3600, (secs % 3600) / 60)
        }
    }

    pub fn toggle_tools(&mut self) {
        self.show_tools = !self.show_tools;
        self.mark_chat_changed();
    }

    pub fn scroll_up(&mut self, amount: u16) {
        let session = self.active_mut();
        session.scroll_offset = session.scroll_offset.saturating_add(amount);
        session.scroll_pinned = false;
    }

    pub fn scroll_down(&mut self, amount: u16) {
        let session = self.active_mut();
        session.scroll_offset = session.scroll_offset.saturating_sub(amount);
        if session.scroll_offset == 0 {
            session.scroll_pinned = true;
        }
    }

    pub fn scroll_to_bottom(&mut self) {
        let session = self.active_mut();
        session.scroll_offset = 0;
        session.scroll_pinned = true;
    }

    pub fn input_kill_to_end(&mut self) {
        self.kill_ring = self.input[self.input_cursor..].to_string();
        self.input.truncate(self.input_cursor);
        self.update_suggestions();
    }

    pub fn input_yank(&mut self) {
        if !self.kill_ring.is_empty() {
            self.input.insert_str(self.input_cursor, &self.kill_ring);
            self.input_cursor += self.kill_ring.len();
            self.update_suggestions();
        }
    }

    pub fn input_word_left(&mut self) {
        if self.input_cursor == 0 {
            return;
        }
        let before = &self.input[..self.input_cursor];
        let trimmed = before.trim_end();
        let pos = trimmed.rfind([' ', '\n']).map(|i| i + 1).unwrap_or(0);
        self.input_cursor = pos;
    }

    pub fn input_word_right(&mut self) {
        if self.input_cursor >= self.input.len() {
            return;
        }
        let after = &self.input[self.input_cursor..];
        let skip_word = after.find([' ', '\n']).unwrap_or(after.len());
        let skip_space = after[skip_word..]
            .find(|c: char| !matches!(c, ' ' | '\n'))
            .unwrap_or(after.len() - skip_word);
        self.input_cursor += skip_word + skip_space;
    }

    pub fn input_newline(&mut self) {
        self.input.insert(self.input_cursor, '\n');
        self.input_cursor += 1;
    }

    pub fn is_multiline(&self) -> bool {
        self.input.contains('\n')
    }

    pub fn input_cursor_line(&self) -> usize {
        self.input[..self.input_cursor].matches('\n').count()
    }

    pub fn input_line_count(&self) -> usize {
        self.input.matches('\n').count() + 1
    }

    pub fn input_line_up(&mut self) {
        let before = &self.input[..self.input_cursor];
        if let Some(nl) = before.rfind('\n') {
            let col = self.input_cursor - nl - 1;
            let prev_start = before[..nl].rfind('\n').map(|i| i + 1).unwrap_or(0);
            let prev_len = nl - prev_start;
            self.input_cursor = prev_start + col.min(prev_len);
        }
    }

    pub fn input_line_down(&mut self) {
        let after = &self.input[self.input_cursor..];
        if let Some(nl) = after.find('\n') {
            let line_start = self.input[..self.input_cursor]
                .rfind('\n')
                .map(|i| i + 1)
                .unwrap_or(0);
            let col = self.input_cursor - line_start;
            let next_start = self.input_cursor + nl + 1;
            let next_end = self.input[next_start..]
                .find('\n')
                .map(|i| next_start + i)
                .unwrap_or(self.input.len());
            let next_len = next_end - next_start;
            self.input_cursor = next_start + col.min(next_len);
        }
    }

    pub fn turn_duration_display(&self) -> Option<String> {
        let session = self.active();
        let dur = if matches!(session.chat_state, ChatState::Streaming) {
            session.turn_start.map(|t| t.elapsed())
        } else {
            session.last_turn_duration
        };
        dur.map(|d| {
            let secs = d.as_secs();
            if secs < 60 {
                format!("{}s", secs)
            } else {
                format!("{}m{}s", secs / 60, secs % 60)
            }
        })
    }

    fn item_count(&self) -> usize {
        match self.tab {
            Tab::Chat | Tab::Status => 0,
            Tab::Wardens => self.wardens.len(),
            Tab::Services => self.services.len(),
            Tab::Channels => self.channels.len(),
            Tab::Config => self.deus_config.len(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toggle_tools_invalidates_the_chat_cache() {
        let mut app = App::new();
        let before = app.active().chat_version;

        app.toggle_tools();

        assert!(app.show_tools);
        assert_ne!(app.active().chat_version, before);
    }
}
