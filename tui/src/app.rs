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
use crate::permission_bridge::{self, PermissionBridge};
use crate::platform;

static NEXT_SESSION_ID: AtomicU64 = AtomicU64::new(1);

pub const TRANSCRIPT_CAP: usize = 200;

pub struct EffortPolicy;

impl EffortPolicy {
    /// Centralized here (not per-backend) because task classification is a UX concern;
    /// backends own only the flag encoding in build_command.
    pub fn for_prompt(prompt: &str) -> &'static str {
        let words: Vec<String> = prompt
            .to_lowercase()
            .split_whitespace()
            .map(|w| w.to_string())
            .collect();
        const HIGH: &[&str] = &["review", "plan", "analyze", "audit", "design", "architect"];
        const LOW: &[&str] = &["find", "grep", "search", "list", "show", "check", "lookup"];
        if words.iter().any(|w| HIGH.contains(&w.as_str())) {
            return "high";
        }
        if words.iter().any(|w| LOW.contains(&w.as_str())) {
            return "low";
        }
        "medium"
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct SessionId(pub u64);

impl SessionId {
    pub const MAIN: Self = Self(0);

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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionState {
    Running,
    Completed,
    Failed,
}

pub struct AgentArgs {
    pub prompt: String,
    pub model: Option<String>,
    pub effort: Option<String>,
}

pub struct Session {
    #[allow(dead_code)]
    pub id: SessionId,
    pub label: String,
    pub session_state: SessionState,
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
    pub last_subagent_hint: Option<String>,
    pub had_error: bool,
    pub scroll_offset: u16,
    pub scroll_pinned: bool,
    pub chat_dirty: bool,
    pub chat_version: u64,
    pub run_mode: backend::RunMode,
    pub pending_permissions: Vec<permission_bridge::PermissionRequest>,
}

impl Session {
    fn new_main(model: String, effort: String, permissions: PermissionsConfig) -> Self {
        Self {
            id: SessionId::MAIN,
            label: "main".to_string(),
            session_state: SessionState::Running,
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
            last_subagent_hint: None,
            had_error: false,
            scroll_offset: 0,
            scroll_pinned: true,
            chat_dirty: true,
            chat_version: 0,
            run_mode: backend::RunMode::default(),
            pending_permissions: Vec::new(),
        }
    }

    pub fn trim_transcript(&mut self) -> usize {
        if self.id == SessionId::MAIN {
            return 0;
        }
        if matches!(self.chat_state, ChatState::Streaming) {
            return 0;
        }
        let len = self.chat_messages.len();
        if len <= TRANSCRIPT_CAP {
            return 0;
        }
        let to_remove = len - TRANSCRIPT_CAP;
        self.chat_messages.drain(..to_remove);
        self.mark_chat_changed();
        to_remove
    }

    pub fn completion_summary(&self) -> String {
        for msg in self.chat_messages.iter().rev() {
            if msg.role == "assistant" && !msg.content.is_empty() {
                let first_line = msg.content.lines().next().unwrap_or("");
                let preview: String = first_line.chars().take(120).collect();
                if preview.len() < first_line.len() {
                    return format!("{}...", preview);
                }
                return preview;
            }
        }
        "(no output)".to_string()
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
        name: "/agent",
        description: "Spawn background agent",
        args: &["--effort", "--model"],
    },
    CommandDef {
        name: "/sessions",
        description: "Session picker",
        args: &[],
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
    pub session_order: Vec<SessionId>,
    pub show_session_picker: bool,
    pub picker_cursor: usize,

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
    pub should_quit: bool,

    // Permission bridge (Claude-only, None for Codex/bypass)
    pub permission_bridge: Option<PermissionBridge>,

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

        let use_permission_bridge = backend != "codex" && !permissions.is_bypass();

        let main_id = SessionId::MAIN;
        let main_session = Session::new_main(default_model, "high".to_string(), permissions);
        let mut sessions = HashMap::new();
        sessions.insert(main_id, main_session);

        let permission_bridge = if use_permission_bridge {
            PermissionBridge::sweep_orphans();
            Some(PermissionBridge::new())
        } else {
            None
        };

        let mut app = Self {
            sessions,
            active_session: main_id,
            session_order: vec![main_id],
            show_session_picker: false,
            picker_cursor: 0,

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
            should_quit: false,

            permission_bridge,

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
        self.dispatch_message_for(self.active_session, msg);
    }

    fn dispatch_message_for(&mut self, session_id: SessionId, msg: String) {
        let ctx_file = self.system_context_file.clone();
        let session = self.sessions.get_mut(&session_id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("user", &msg));
        session.chat_state = ChatState::Streaming;
        session.turn_start = Some(Instant::now());
        session.last_thinking_summary = None;
        session.mark_chat_changed();
        session.scroll_offset = 0;
        session.scroll_pinned = true;

        let permissions_dir = if backend::model_backend_name(&session.model) == "claude"
            && !session.permissions.is_bypass()
        {
            self.permission_bridge
                .as_ref()
                .map(|pb| pb.create_session(session_id))
        } else {
            None
        };

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
            permissions_dir,
        };
        session.turn_count += 1;
        let be = backend::backend_for(&config.model);

        let is_background = session_id != SessionId::MAIN;
        thread::spawn(move || {
            let child = be.build_command(&config).spawn();

            match child {
                Ok(mut process) => {
                    let (kill_rx, cancel_tx) = if is_background {
                        let (kill_tx, kill_rx) = mpsc::channel::<()>();
                        let (cancel_tx, cancel_rx) = mpsc::channel::<()>();
                        let timeout_secs: u64 = platform::env_var("DEUS_AGENT_TIMEOUT_SECS")
                            .and_then(|s| s.parse().ok())
                            .unwrap_or(600);
                        thread::spawn(move || {
                            match cancel_rx.recv_timeout(Duration::from_secs(timeout_secs)) {
                                Ok(()) => {}
                                Err(_) => {
                                    let _ = kill_tx.send(());
                                }
                            }
                        });
                        (Some(kill_rx), Some(cancel_tx))
                    } else {
                        (None, None)
                    };

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
                    if let Some(h) = stderr_handle {
                        let _ = h.join();
                    }
                    if let Some(tx) = cancel_tx {
                        let _ = tx.send(());
                    }

                    let timed_out = kill_rx.as_ref().is_some_and(|rx| rx.try_recv().is_ok());
                    if timed_out {
                        let _ = process.kill();
                    }
                    let status = process.wait();
                    if timed_out {
                        let _ = tx.send(backend::StreamChunk {
                            kind: ChunkKind::Error("Agent timed out".to_string()),
                        });
                    } else if let Ok(s) = &status
                        && !s.success()
                    {
                        let code = s.code().unwrap_or(-1);
                        let _ = tx.send(backend::StreamChunk {
                            kind: ChunkKind::Error(format!("Process exited with code {}", code)),
                        });
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

        let session = self.sessions.get_mut(&session_id).unwrap();
        session.chat_messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: String::new(),
            blocks: Vec::new(),
        });
    }

    pub fn max_agents() -> usize {
        if let Some(val) =
            platform::env_var("DEUS_MAX_AGENTS").and_then(|s| s.parse::<usize>().ok())
        {
            return val.max(1);
        }
        if let Some(val) =
            config::deus::read_key("max_parallel_agents").and_then(|s| s.parse::<usize>().ok())
        {
            return val.max(1);
        }
        (thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4)
            / 2)
        .clamp(2, 8)
    }

    pub fn spawn_agent(
        &mut self,
        prompt: String,
        model: Option<String>,
        effort: Option<String>,
    ) -> Option<SessionId> {
        let limit = Self::max_agents();
        let active_agents = self
            .sessions
            .iter()
            .filter(|(id, s)| {
                **id != SessionId::MAIN && matches!(s.chat_state, ChatState::Streaming)
            })
            .count();
        if active_agents >= limit {
            self.active_mut().chat_messages.push(ChatMessage::simple(
                "system",
                &format!("At agent limit ({} streaming). Wait for one to finish or adjust DEUS_MAX_AGENTS.", limit),
            ));
            return None;
        }

        let id = SessionId::next();
        let model = model.unwrap_or_else(|| self.active().model.clone());
        let effort = effort.unwrap_or_else(|| EffortPolicy::for_prompt(&prompt).to_string());
        let preview: String = prompt.chars().take(50).collect();
        let label = if preview.len() < prompt.len() {
            format!("{}...", preview)
        } else {
            preview
        };

        let session = Session {
            id,
            label,
            session_state: SessionState::Running,
            chat_messages: Vec::new(),
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
            permissions: self.active().permissions.clone(),
            active_subagent_ids: HashSet::new(),
            last_subagent_hint: None,
            had_error: false,
            scroll_offset: 0,
            scroll_pinned: true,
            chat_dirty: true,
            chat_version: 0,
            run_mode: backend::RunMode::Ephemeral,
            pending_permissions: Vec::new(),
        };

        self.sessions.insert(id, session);
        self.session_order.push(id);
        self.dispatch_message_for(id, prompt);
        Some(id)
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
                self.should_quit = true;
                true
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
                self.active_mut().chat_messages.push(ChatMessage::simple("system", &format!("Available commands:\n\n{}\n\nKeyboard shortcuts:\n  Ctrl+B  Parallel agent / session picker\n  Ctrl+Shift+B  Session picker (direct)\n  Ctrl+L  Clear screen\n  Ctrl+U  Clear input line\n  Ctrl+K  Kill to end of line\n  Ctrl+Y  Yank (paste killed text)\n  Ctrl+A  Start of line\n  Ctrl+E  End of line\n  Ctrl+J  New line (multi-line input)\n  Ctrl+O  Toggle tool/thinking details\n  Ctrl+D  Exit\n  Alt+B   Word left\n  Alt+F   Word right\n  ↑/↓     Input history\n  PgUp/Dn Scroll chat\n  Mouse   Scroll chat (Shift+drag to select text)", help)));
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
            "/agent" if !arg.is_empty() => {
                let args = parse_agent_args(arg);
                if args.prompt.is_empty() {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        "Usage: /agent [--effort <level>] [--model <id>] <prompt>",
                    ));
                    return true;
                }
                let preview: String = args.prompt.chars().take(60).collect();
                let effort_label = args
                    .effort
                    .as_deref()
                    .unwrap_or_else(|| EffortPolicy::for_prompt(&args.prompt))
                    .to_string();
                if self
                    .spawn_agent(args.prompt, args.model, args.effort)
                    .is_some()
                {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        &format!("Agent spawned (effort: {}): {}", effort_label, preview),
                    ));
                }
                true
            }
            "/agent" => {
                self.active_mut().chat_messages.push(ChatMessage::simple(
                    "system",
                    "Usage: /agent [--effort <level>] [--model <id>] <prompt>",
                ));
                true
            }
            "/sessions" => {
                if self.session_order.len() <= 1 {
                    self.active_mut().chat_messages.push(ChatMessage::simple(
                        "system",
                        "No background sessions. Use /agent <prompt> to spawn one.",
                    ));
                } else {
                    self.show_session_picker = true;
                    self.picker_cursor = 0;
                }
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
        let ids: Vec<SessionId> = self.sessions.keys().copied().collect();
        for id in ids {
            self.poll_session(id);
        }
    }

    fn poll_session(&mut self, session_id: SessionId) {
        // Poll permission bridge for new requests
        if let Some(ref pb) = self.permission_bridge {
            let new_requests = pb.poll(session_id);
            if let Some(session) = self.sessions.get_mut(&session_id) {
                for req in new_requests {
                    if !session
                        .pending_permissions
                        .iter()
                        .any(|p| p.tool_use_id == req.tool_use_id)
                    {
                        session.pending_permissions.push(req);
                        session.mark_chat_changed();
                    }
                }
            }
        }

        let session = match self.sessions.get(&session_id) {
            Some(s) => s,
            None => return,
        };
        if session.stream_rx.is_none() {
            return;
        }

        let mut chunks: Vec<backend::StreamChunk> = Vec::new();
        if let Some(rx) = &session.stream_rx {
            while let Ok(chunk) = rx.try_recv() {
                chunks.push(chunk);
            }
        }
        if chunks.is_empty() {
            return;
        }

        for chunk in chunks {
            match chunk.kind {
                ChunkKind::Text(text) => {
                    let session = self.sessions.get_mut(&session_id).unwrap();
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
                    session.active_subagent_ids.insert(id.clone());
                    if !description.is_empty() {
                        session.last_subagent_hint = Some(description.clone());
                    }
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
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
                    self.sessions
                        .get_mut(&session_id)
                        .unwrap()
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
                    let session = self.sessions.get_mut(&session_id).unwrap();
                    session.chat_state = ChatState::Idle;
                    session.stream_rx = None;
                    session.last_turn_duration = session.turn_start.map(|t| t.elapsed());
                    session.turn_start = None;
                    session.active_subagent_ids.clear();
                    session.last_subagent_hint = None;
                    session.pending_permissions.clear();
                    session.mark_chat_changed();
                    if let Some(ref pb) = self.permission_bridge {
                        pb.cleanup_session(session_id);
                    }
                    if session_id != SessionId::MAIN {
                        session.session_state = if session.had_error {
                            SessionState::Failed
                        } else {
                            SessionState::Completed
                        };
                        session.trim_transcript();
                        let label = session.label.clone();
                        let summary = session.completion_summary();
                        let prefix = if session.had_error {
                            "Agent failed"
                        } else {
                            "Agent completed"
                        };
                        if let Some(main) = self.sessions.get_mut(&SessionId::MAIN) {
                            main.chat_messages.push(ChatMessage::simple(
                                "system",
                                &format!("[{}: {}] {}", prefix, label, summary),
                            ));
                            main.mark_chat_changed();
                        }
                        if self.active_session == session_id {
                            self.active_session = SessionId::MAIN;
                        }
                        self.sessions.remove(&session_id);
                        self.session_order.retain(|&sid| sid != session_id);
                        if self.picker_cursor >= self.session_order.len() && self.picker_cursor > 0
                        {
                            self.picker_cursor -= 1;
                        }
                        if self.session_order.len() <= 1 {
                            self.show_session_picker = false;
                        }
                    }
                    if session_id == self.active_session && !self.queued_messages.is_empty() {
                        let next = self.queued_messages.remove(0);
                        self.dispatch_message(next);
                    }
                    return;
                }
                ChunkKind::Error(e) => {
                    let session = self.sessions.get_mut(&session_id).unwrap();
                    session.had_error = true;
                    if let Some(last) = session.chat_messages.last_mut()
                        && last.role == "assistant"
                    {
                        last.content.push_str(&format!("\n[Error: {}]", e));
                    }
                }
            }
        }
        if session_id == self.active_session {
            self.mark_chat_changed();
        } else if let Some(s) = self.sessions.get_mut(&session_id) {
            s.mark_chat_changed();
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

    pub fn switch_to_session(&mut self, id: SessionId) {
        if self.sessions.contains_key(&id) {
            self.active_session = id;
            self.show_session_picker = false;
            self.mark_chat_changed();
        }
    }

    pub fn picker_next(&mut self) {
        if self.picker_cursor + 1 < self.session_order.len() {
            self.picker_cursor += 1;
        }
    }

    pub fn picker_prev(&mut self) {
        if self.picker_cursor > 0 {
            self.picker_cursor -= 1;
        }
    }

    pub fn picker_select(&mut self) {
        if let Some(&id) = self.session_order.get(self.picker_cursor) {
            self.switch_to_session(id);
        }
    }

    pub fn dismiss_session(&mut self) {
        if let Some(&id) = self.session_order.get(self.picker_cursor) {
            if id == SessionId::MAIN {
                return;
            }
            if let Some(session) = self.sessions.get(&id)
                && session.session_state == SessionState::Running
            {
                return;
            }
            self.sessions.remove(&id);
            self.session_order.retain(|&sid| sid != id);
            if self.picker_cursor >= self.session_order.len() && self.picker_cursor > 0 {
                self.picker_cursor -= 1;
            }
            if self.session_order.len() <= 1 {
                self.show_session_picker = false;
            }
        }
    }

    pub fn background_session_count(&self) -> usize {
        self.session_order.len().saturating_sub(1)
    }

    pub fn streaming_background_count(&self) -> usize {
        self.sessions
            .iter()
            .filter(|(id, s)| {
                **id != self.active_session && matches!(s.chat_state, ChatState::Streaming)
            })
            .count()
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

    pub fn has_pending_permission(&self) -> bool {
        !self.active().pending_permissions.is_empty()
    }

    pub fn approve_first_pending(&mut self) {
        let session_id = self.active_session;
        if let Some(ref pb) = self.permission_bridge
            && let Some(session) = self.sessions.get_mut(&session_id)
            && let Some(req) = session.pending_permissions.first()
        {
            pb.respond(req, "allow", "User approved via TUI");
            let tool = req.tool_name.clone();
            let preview = req.tool_input_preview.clone();
            session.pending_permissions.remove(0);
            session.chat_messages.push(ChatMessage::simple(
                "system",
                &format!("Approved: {}({})", tool, preview),
            ));
            session.mark_chat_changed();
        }
    }

    pub fn deny_first_pending(&mut self) {
        let session_id = self.active_session;
        if let Some(ref pb) = self.permission_bridge
            && let Some(session) = self.sessions.get_mut(&session_id)
            && let Some(req) = session.pending_permissions.first()
        {
            pb.respond(req, "deny", "User denied via TUI");
            let tool = req.tool_name.clone();
            session.pending_permissions.remove(0);
            session
                .chat_messages
                .push(ChatMessage::simple("system", &format!("Denied: {}", tool)));
            session.mark_chat_changed();
        }
    }

    pub fn always_allow_first_pending(&mut self) {
        let session_id = self.active_session;
        if let Some(ref pb) = self.permission_bridge
            && let Some(session) = self.sessions.get_mut(&session_id)
            && let Some(req) = session.pending_permissions.first()
        {
            pb.respond(req, "allow", "User always-allowed via TUI");
            let tool = req.tool_name.clone();
            session.pending_permissions.remove(0);
            session.permissions.add_allowed(&tool);
            session.chat_messages.push(ChatMessage::simple(
                "system",
                &format!("Always allowed: {}", tool),
            ));
            session.mark_chat_changed();
        }
    }

    pub fn cleanup_permissions(&self) {
        if let Some(ref pb) = self.permission_bridge {
            pb.cleanup();
        }
    }
}

pub fn parse_agent_args(args: &str) -> AgentArgs {
    let mut effort: Option<String> = None;
    let mut model: Option<String> = None;
    let mut prompt_parts: Vec<&str> = Vec::new();
    let tokens: Vec<&str> = args.split_whitespace().collect();
    let ids = model_ids();
    let mut i = 0;
    while i < tokens.len() {
        match tokens[i] {
            "--effort" if i + 1 < tokens.len() && EFFORT_LEVELS.contains(&tokens[i + 1]) => {
                effort = Some(tokens[i + 1].to_string());
                i += 2;
            }
            "--model" if i + 1 < tokens.len() && ids.contains(&tokens[i + 1]) => {
                model = Some(tokens[i + 1].to_string());
                i += 2;
            }
            _ => {
                prompt_parts.extend_from_slice(&tokens[i..]);
                break;
            }
        }
    }
    AgentArgs {
        prompt: prompt_parts.join(" "),
        model,
        effort,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn complete_session(app: &mut App, id: SessionId) {
        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "done"));
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();
        app.poll_response();
    }

    #[test]
    fn toggle_tools_invalidates_the_chat_cache() {
        let mut app = App::new();
        let before = app.active().chat_version;

        app.toggle_tools();

        assert!(app.show_tools);
        assert_ne!(app.active().chat_version, before);
    }

    #[test]
    fn spawn_agent_creates_background_session() {
        let mut app = App::new();
        assert_eq!(app.session_order.len(), 1);
        assert_eq!(app.background_session_count(), 0);

        let id = app
            .spawn_agent("test task".to_string(), None, None)
            .unwrap();

        assert_eq!(app.session_order.len(), 2);
        assert_eq!(app.background_session_count(), 1);
        assert_ne!(id, SessionId::MAIN);
        assert_eq!(app.active_session, SessionId::MAIN);

        let session = app.sessions.get(&id).unwrap();
        assert_eq!(session.run_mode, backend::RunMode::Ephemeral);
        assert_eq!(session.label, "test task");
    }

    #[test]
    fn spawn_agent_truncates_long_prompts() {
        let mut app = App::new();
        let long_prompt = "a".repeat(100);
        let id = app.spawn_agent(long_prompt, None, None).unwrap();

        let session = app.sessions.get(&id).unwrap();
        assert!(session.label.len() <= 54);
        assert!(session.label.ends_with("..."));
    }

    #[test]
    fn spawn_agent_inherits_model() {
        let mut app = App::new();
        let main_model = app.active().model.clone();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();

        assert_eq!(app.sessions.get(&id).unwrap().model, main_model);
    }

    #[test]
    fn spawn_agent_accepts_custom_model() {
        let mut app = App::new();
        let id = app
            .spawn_agent("test".to_string(), Some("haiku".to_string()), None)
            .unwrap();

        assert_eq!(app.sessions.get(&id).unwrap().model, "haiku");
    }

    #[test]
    fn session_picker_navigation() {
        let mut app = App::new();
        app.spawn_agent("task 1".to_string(), None, None).unwrap();
        app.spawn_agent("task 2".to_string(), None, None).unwrap();

        app.show_session_picker = true;
        app.picker_cursor = 0;

        app.picker_next();
        assert_eq!(app.picker_cursor, 1);

        app.picker_next();
        assert_eq!(app.picker_cursor, 2);

        app.picker_next();
        assert_eq!(app.picker_cursor, 2);

        app.picker_prev();
        assert_eq!(app.picker_cursor, 1);
    }

    #[test]
    fn session_picker_select_switches_active() {
        let mut app = App::new();
        let id = app.spawn_agent("task".to_string(), None, None).unwrap();

        app.show_session_picker = true;
        app.picker_cursor = 1;
        app.picker_select();

        assert_eq!(app.active_session, id);
        assert!(!app.show_session_picker);
    }

    #[test]
    fn dismiss_only_removes_completed_sessions() {
        let mut app = App::new();
        let id = app.spawn_agent("task".to_string(), None, None).unwrap();
        assert_eq!(app.session_order.len(), 2);

        app.picker_cursor = 1;
        app.dismiss_session();
        assert_eq!(app.session_order.len(), 2);

        app.sessions.get_mut(&id).unwrap().session_state = SessionState::Completed;
        app.dismiss_session();
        assert_eq!(app.session_order.len(), 1);
        assert!(!app.sessions.contains_key(&id));
    }

    #[test]
    fn cannot_dismiss_main_session() {
        let mut app = App::new();
        app.spawn_agent("task".to_string(), None, None).unwrap();

        app.picker_cursor = 0;
        app.dismiss_session();
        assert!(app.sessions.contains_key(&SessionId::MAIN));
    }

    #[test]
    fn switched_to_session_still_becomes_completed() {
        let mut app = App::new();
        let id = app.spawn_agent("task".to_string(), None, None).unwrap();

        app.switch_to_session(id);
        assert_eq!(app.active_session, id);

        {
            let session = app.sessions.get_mut(&id).unwrap();
            session.chat_state = ChatState::Idle;
            session.stream_rx = None;
            session.session_state = SessionState::Completed;
        }

        app.switch_to_session(SessionId::MAIN);
        app.picker_cursor = 1;
        app.dismiss_session();
        assert!(!app.sessions.contains_key(&id));
    }

    #[test]
    fn agent_command_spawns_session() {
        let mut app = App::new();
        let handled = app.handle_command("/agent do something");
        assert!(handled);
        assert_eq!(app.session_order.len(), 2);
    }

    #[test]
    fn agent_command_empty_shows_usage() {
        let mut app = App::new();
        let handled = app.handle_command("/agent");
        assert!(handled);
        assert_eq!(app.session_order.len(), 1);
    }

    #[test]
    fn sessions_command_shows_picker_when_agents_exist() {
        let mut app = App::new();
        app.spawn_agent("task".to_string(), None, None).unwrap();

        let handled = app.handle_command("/sessions");
        assert!(handled);
        assert!(app.show_session_picker);
    }

    #[test]
    fn sessions_command_shows_hint_when_no_agents() {
        let mut app = App::new();
        let handled = app.handle_command("/sessions");
        assert!(handled);
        assert!(!app.show_session_picker);
    }

    // --- 5a: Bounded transcripts ---

    #[test]
    fn trim_transcript_removes_old_messages() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session.chat_state = ChatState::Idle;
        let pre_existing = session.chat_messages.len();
        for i in 0..(TRANSCRIPT_CAP + 50) {
            session
                .chat_messages
                .push(ChatMessage::simple("assistant", &format!("msg {}", i)));
        }
        let total = pre_existing + TRANSCRIPT_CAP + 50;
        let expected_removed = total - TRANSCRIPT_CAP;
        let removed = session.trim_transcript();
        assert_eq!(removed, expected_removed);
        assert_eq!(session.chat_messages.len(), TRANSCRIPT_CAP);
    }

    #[test]
    fn trim_transcript_noop_for_main() {
        let mut app = App::new();
        let session = app.sessions.get_mut(&SessionId::MAIN).unwrap();
        session.chat_state = ChatState::Idle;
        for i in 0..(TRANSCRIPT_CAP + 50) {
            session
                .chat_messages
                .push(ChatMessage::simple("assistant", &format!("msg {}", i)));
        }
        let removed = session.trim_transcript();
        assert_eq!(removed, 0);
    }

    #[test]
    fn trim_transcript_noop_while_streaming() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        for _ in 0..(TRANSCRIPT_CAP + 50) {
            session
                .chat_messages
                .push(ChatMessage::simple("assistant", "x"));
        }
        let removed = session.trim_transcript();
        assert_eq!(removed, 0);
    }

    #[test]
    fn trim_transcript_bumps_version() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session.chat_state = ChatState::Idle;
        for _ in 0..(TRANSCRIPT_CAP + 10) {
            session
                .chat_messages
                .push(ChatMessage::simple("assistant", "x"));
        }
        let version_before = session.chat_version;
        session.trim_transcript();
        assert_ne!(session.chat_version, version_before);
    }

    // --- 5b: Dynamic effort + flag parsing ---

    #[test]
    fn parse_agent_args_plain_prompt() {
        let args = parse_agent_args("do something useful");
        assert_eq!(args.prompt, "do something useful");
        assert_eq!(args.model, None);
        assert_eq!(args.effort, None);
    }

    #[test]
    fn parse_agent_args_with_effort() {
        let args = parse_agent_args("--effort low find the bug");
        assert_eq!(args.prompt, "find the bug");
        assert_eq!(args.model, None);
        assert_eq!(args.effort, Some("low".to_string()));
    }

    #[test]
    fn parse_agent_args_with_model() {
        let args = parse_agent_args("--model haiku summarize this");
        assert_eq!(args.prompt, "summarize this");
        assert_eq!(args.model, Some("haiku".to_string()));
        assert_eq!(args.effort, None);
    }

    #[test]
    fn parse_agent_args_with_both_flags() {
        let args = parse_agent_args("--effort medium --model haiku do it");
        assert_eq!(args.prompt, "do it");
        assert_eq!(args.model, Some("haiku".to_string()));
        assert_eq!(args.effort, Some("medium".to_string()));
    }

    #[test]
    fn parse_agent_args_invalid_effort_becomes_prompt() {
        let args = parse_agent_args("--effort bogus find files");
        assert_eq!(args.prompt, "--effort bogus find files");
        assert_eq!(args.effort, None);
    }

    #[test]
    fn parse_agent_args_unknown_flag_becomes_prompt() {
        let args = parse_agent_args("--something that is not a flag");
        assert_eq!(args.prompt, "--something that is not a flag");
    }

    #[test]
    fn parse_agent_args_empty_returns_empty_prompt() {
        let args = parse_agent_args("");
        assert_eq!(args.prompt, "");
    }

    #[test]
    fn spawn_agent_effort_from_policy() {
        let cases = [
            ("review the code", "high"),
            ("find the config file", "low"),
            ("summarize this", "medium"),
        ];
        for (prompt, expected) in cases {
            let mut app = App::new();
            let id = app.spawn_agent(prompt.to_string(), None, None).unwrap();
            assert_eq!(
                app.sessions.get(&id).unwrap().effort,
                expected,
                "prompt: {}",
                prompt
            );
        }
    }

    #[test]
    fn spawn_agent_accepts_custom_effort() {
        let mut app = App::new();
        let id = app
            .spawn_agent("test".to_string(), None, Some("max".to_string()))
            .unwrap();
        assert_eq!(app.sessions.get(&id).unwrap().effort, "max");
    }

    // --- 5c: Completion summaries ---

    #[test]
    fn completion_summary_extracts_last_assistant_message() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session.chat_messages.push(ChatMessage::simple(
            "assistant",
            "I found 3 bugs in the code.\nHere are the details...",
        ));
        let summary = session.completion_summary();
        assert_eq!(summary, "I found 3 bugs in the code.");
    }

    #[test]
    fn completion_summary_returns_no_output_when_empty() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get(&id).unwrap();
        let summary = session.completion_summary();
        assert_eq!(summary, "(no output)");
    }

    #[test]
    fn completion_summary_truncates_long_lines() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", &"x".repeat(200)));
        let summary = session.completion_summary();
        assert!(summary.len() <= 124);
        assert!(summary.ends_with("..."));
    }

    #[test]
    fn completion_injects_notification_into_main() {
        let mut app = App::new();
        let id = app
            .spawn_agent("find bugs".to_string(), None, None)
            .unwrap();

        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "Found 2 issues"));
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();
        session.stream_rx = Some(rx);

        app.poll_response();

        let main = app.sessions.get(&SessionId::MAIN).unwrap();
        let last_system = main
            .chat_messages
            .iter()
            .rev()
            .find(|m| m.role == "system" && m.content.contains("Agent completed"));
        assert!(last_system.is_some());
        assert!(last_system.unwrap().content.contains("Found 2 issues"));
    }

    // --- 5d: Hint-driven spawn ---

    #[test]
    fn subagent_start_sets_hint() {
        let mut app = App::new();
        let (tx, rx) = std::sync::mpsc::channel();
        app.active_mut().stream_rx = Some(rx);
        app.active_mut().chat_state = ChatState::Streaming;
        app.active_mut().chat_messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: String::new(),
            blocks: Vec::new(),
        });
        tx.send(backend::StreamChunk {
            kind: ChunkKind::SubagentStart {
                id: "a1".to_string(),
                subagent_type: "Explore".to_string(),
                description: "search for config files".to_string(),
            },
        })
        .unwrap();
        app.poll_response();

        assert_eq!(
            app.active().last_subagent_hint.as_deref(),
            Some("search for config files")
        );
    }

    #[test]
    fn done_clears_subagent_hint() {
        let mut app = App::new();
        app.active_mut().last_subagent_hint = Some("test hint".to_string());
        let (tx, rx) = std::sync::mpsc::channel();
        app.active_mut().stream_rx = Some(rx);
        app.active_mut().chat_state = ChatState::Streaming;
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();
        app.poll_response();

        assert!(app.active().last_subagent_hint.is_none());
    }

    // --- EffortPolicy ---

    #[test]
    fn effort_policy_review_is_high() {
        assert_eq!(EffortPolicy::for_prompt("review the code"), "high");
        assert_eq!(EffortPolicy::for_prompt("plan the migration"), "high");
        assert_eq!(EffortPolicy::for_prompt("analyze performance"), "high");
    }

    #[test]
    fn effort_policy_find_is_low() {
        assert_eq!(EffortPolicy::for_prompt("find the config file"), "low");
        assert_eq!(EffortPolicy::for_prompt("grep for TODO"), "low");
        assert_eq!(EffortPolicy::for_prompt("search for imports"), "low");
    }

    #[test]
    fn effort_policy_general_is_medium() {
        assert_eq!(EffortPolicy::for_prompt("summarize this"), "medium");
        assert_eq!(EffortPolicy::for_prompt("refactor the module"), "medium");
    }

    #[test]
    fn effort_policy_case_insensitive() {
        assert_eq!(EffortPolicy::for_prompt("REVIEW the CODE"), "high");
        assert_eq!(EffortPolicy::for_prompt("Find Files"), "low");
    }

    #[test]
    fn spawn_agent_effort_explicit_overrides_policy() {
        let mut app = App::new();
        let id = app
            .spawn_agent("review the code".to_string(), None, Some("low".to_string()))
            .unwrap();
        assert_eq!(app.sessions.get(&id).unwrap().effort, "low");
    }

    // --- Health monitoring ---

    #[test]
    fn error_chunk_sets_had_error() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session.chat_messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: String::new(),
            blocks: Vec::new(),
        });
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Error("something failed".to_string()),
        })
        .unwrap();
        app.poll_response();

        assert!(app.sessions.get(&id).unwrap().had_error);
    }

    #[test]
    fn done_after_error_notifies_main_as_failed() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session.chat_messages.push(ChatMessage {
            role: "assistant".to_string(),
            content: String::new(),
            blocks: Vec::new(),
        });
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Error("crash".to_string()),
        })
        .unwrap();
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();
        app.poll_response();

        // Session is auto-GC'd — verify via main notification
        assert!(!app.sessions.contains_key(&id));
        let main = app.sessions.get(&SessionId::MAIN).unwrap();
        let has_failed = main
            .chat_messages
            .iter()
            .any(|m| m.content.contains("Agent failed"));
        assert!(has_failed);
    }

    // --- Session auto-GC ---

    #[test]
    fn auto_gc_removes_completed_session() {
        let mut app = App::new();
        let id = app.spawn_agent("test gc".to_string(), None, None).unwrap();
        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "done"));
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();

        app.poll_response();

        assert!(!app.sessions.contains_key(&id));
        assert_eq!(app.session_order.len(), 1);
    }

    #[test]
    fn auto_gc_resets_active_session_to_main() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        app.switch_to_session(id);
        assert_eq!(app.active_session, id);

        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "done"));
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();

        app.poll_response();

        assert_eq!(app.active_session, SessionId::MAIN);
        assert!(!app.sessions.contains_key(&id));
    }

    #[test]
    fn auto_gc_closes_picker_when_no_sessions_remain() {
        let mut app = App::new();
        let id = app.spawn_agent("test".to_string(), None, None).unwrap();
        app.show_session_picker = true;
        app.picker_cursor = 1;

        let session = app.sessions.get_mut(&id).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "done"));
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();

        app.poll_response();

        assert!(!app.show_session_picker);
        assert_eq!(app.picker_cursor, 0);
    }

    // --- Concurrent session limit ---

    #[test]
    fn max_agents_returns_positive() {
        assert!(App::max_agents() >= 1);
    }

    #[test]
    fn spawn_agent_rejects_at_limit() {
        let mut app = App::new();
        let limit = App::max_agents();
        for i in 0..limit {
            let result = app.spawn_agent(format!("task {}", i), None, None);
            assert!(result.is_some(), "spawn {} should succeed", i);
        }

        let rejected = app.spawn_agent("one too many".to_string(), None, None);
        assert!(rejected.is_none());
    }

    #[test]
    fn spawn_agent_allows_after_gc() {
        let mut app = App::new();
        let limit = App::max_agents();
        let mut ids = Vec::new();
        for i in 0..limit {
            ids.push(app.spawn_agent(format!("task {}", i), None, None).unwrap());
        }
        assert!(app.spawn_agent("blocked".to_string(), None, None).is_none());

        // Complete one agent — auto-GC frees the slot
        let first = ids[0];
        let session = app.sessions.get_mut(&first).unwrap();
        session
            .chat_messages
            .push(ChatMessage::simple("assistant", "done"));
        let (tx, rx) = std::sync::mpsc::channel();
        session.stream_rx = Some(rx);
        tx.send(backend::StreamChunk {
            kind: ChunkKind::Done,
        })
        .unwrap();
        app.poll_response();

        let after_gc = app.spawn_agent("now allowed".to_string(), None, None);
        assert!(after_gc.is_some());
    }
}
