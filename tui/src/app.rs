use std::io::{BufRead, BufReader, Read as _};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use crate::config;



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

#[derive(Clone)]
pub enum MessageBlock {
    Text(String),
    Thinking(String),
    ToolUse { tool: String, detail: String },
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

pub enum ChatState {
    Idle,
    Streaming,
}

pub struct CommandDef {
    pub name: &'static str,
    pub description: &'static str,
    pub args: &'static [&'static str],
}

pub struct ModelDef {
    pub id: &'static str,
    pub display: &'static str,
    pub backend: &'static str,
    pub context: &'static str,
}

pub const MODEL_REGISTRY: &[ModelDef] = &[
    ModelDef { id: "sonnet", display: "Sonnet 4.6", backend: "claude", context: "1M" },
    ModelDef { id: "opus", display: "Opus 4.6", backend: "claude", context: "1M" },
    ModelDef { id: "haiku", display: "Haiku 4.5", backend: "claude", context: "200K" },
    ModelDef { id: "o3", display: "o3", backend: "codex", context: "200K" },
    ModelDef { id: "o4-mini", display: "o4-mini", backend: "codex", context: "200K" },
    ModelDef { id: "codex-mini", display: "Codex Mini", backend: "codex", context: "200K" },
];

pub fn model_display(id: &str) -> String {
    MODEL_REGISTRY.iter()
        .find(|m| m.id == id)
        .map(|m| m.display.to_string())
        .unwrap_or_else(|| id.to_string())
}

pub fn model_backend(id: &str) -> &'static str {
    MODEL_REGISTRY.iter()
        .find(|m| m.id == id)
        .map(|m| m.backend)
        .unwrap_or("claude")
}

pub fn model_ids() -> Vec<&'static str> {
    MODEL_REGISTRY.iter().map(|m| m.id).collect()
}

pub fn model_arg_labels() -> Vec<String> {
    MODEL_REGISTRY.iter()
        .map(|m| format!("{} ({}, {})", m.id, m.display, m.context))
        .collect()
}

pub const COMMANDS: &[CommandDef] = &[
    CommandDef { name: "/wardens", description: "Quality gates", args: &["enable", "disable", "reset"] },
    CommandDef { name: "/services", description: "Service health", args: &["refresh"] },
    CommandDef { name: "/channels", description: "Channel status", args: &[] },
    CommandDef { name: "/config", description: "Configuration", args: &["backend", "vault"] },
    CommandDef { name: "/status", description: "System dashboard", args: &["refresh"] },
    CommandDef { name: "/model", description: "Switch model", args: &[] },
    CommandDef { name: "/compress", description: "Save to vault", args: &[] },
    CommandDef { name: "/checkpoint", description: "Mid-session save", args: &[] },
    CommandDef { name: "/compact", description: "Compact context", args: &[] },
    CommandDef { name: "/resume", description: "Load recent work", args: &[] },
    CommandDef { name: "/history", description: "Past sessions", args: &["today", "yesterday", "week"] },
    CommandDef { name: "/init", description: "Init CLAUDE.md", args: &[] },
    CommandDef { name: "/help", description: "Show commands", args: &[] },
    CommandDef { name: "/clear", description: "Clear chat", args: &[] },
    CommandDef { name: "/quit", description: "Exit", args: &[] },
];

pub struct App {
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
    pub chat_messages: Vec<ChatMessage>,
    pub chat_state: ChatState,
    pub stream_rx: Option<mpsc::Receiver<StreamChunk>>,
    pub turn_count: u32,
    pub model: String,
    pub token_count: u32,
    pub cost_usd: f64,
    pub session_start: Instant,
    pub turn_start: Option<Instant>,
    pub last_turn_duration: Option<Duration>,
    pub last_thinking_summary: Option<String>,
    pub show_tools: bool,
    pub scroll_offset: u16,
    pub scroll_pinned: bool,
    pub esc_pending: Option<Instant>,
    pub queued_messages: Vec<String>,
    pub git_branch: String,
    pub wardens: Vec<config::wardens::WardenEntry>,
    pub services: Vec<config::healthcheck::ServiceEntry>,
    pub channels: Vec<config::channels::ChannelEntry>,
    pub deus_config: Vec<(String, String)>,
}

pub enum StreamChunk {
    Text(String),
    Thinking(String),
    ToolUse { tool: String, status: String },
    ToolResult { tool: String },
    CostUpdate { cost_usd: f64, input_tokens: u64, output_tokens: u64 },
    Done,
    Error(String),
}

fn parse_stream_line(line: &str) -> Option<StreamChunk> {
    let v: serde_json::Value = serde_json::from_str(line).ok()?;
    let event_type = v.get("type")?.as_str()?;

    match event_type {
        "assistant" => {
            let content = v.get("message")?.get("content")?.as_array()?;
            for block in content {
                let block_type = block.get("type")?.as_str()?;
                match block_type {
                    "text" => {
                        let text = block.get("text")?.as_str()?;
                        return Some(StreamChunk::Text(text.to_string()));
                    }
                    "thinking" => {
                        let text = block.get("thinking")?.as_str()?;
                        return Some(StreamChunk::Thinking(text.to_string()));
                    }
                    "tool_use" => {
                        let name = block.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                        let input = block.get("input").map(|i| {
                            if let Some(cmd) = i.get("command").and_then(|c| c.as_str()) {
                                cmd.chars().take(60).collect::<String>()
                            } else if let Some(path) = i.get("file_path").and_then(|p| p.as_str()) {
                                path.to_string()
                            } else {
                                String::new()
                            }
                        }).unwrap_or_default();
                        return Some(StreamChunk::ToolUse { tool: name.to_string(), status: input });
                    }
                    _ => {}
                }
            }
            None
        }
        "user" => {
            let content = v.get("message")?.get("content")?.as_array()?;
            for block in content {
                if block.get("type")?.as_str()? == "tool_result" {
                    let tool_use_id = block.get("tool_use_id").and_then(|t| t.as_str()).unwrap_or("");
                    return Some(StreamChunk::ToolResult { tool: tool_use_id.to_string() });
                }
            }
            None
        }
        "result" => {
            let cost = v.get("total_cost_usd").and_then(|c| c.as_f64()).unwrap_or(0.0);
            let usage = v.get("usage");
            let input = usage.and_then(|u| u.get("input_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
            let output = usage.and_then(|u| u.get("output_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
            Some(StreamChunk::CostUpdate { cost_usd: cost, input_tokens: input, output_tokens: output })
        }
        // Codex JSONL events
        "item.completed" => {
            let text = v.get("item")?.get("text")?.as_str()?;
            Some(StreamChunk::Text(text.to_string()))
        }
        "turn.completed" => {
            let usage = v.get("usage");
            let input = usage.and_then(|u| u.get("input_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
            let output = usage.and_then(|u| u.get("output_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
            Some(StreamChunk::CostUpdate { cost_usd: 0.0, input_tokens: input, output_tokens: output })
        }
        "turn.failed" => {
            let msg = v.get("error").and_then(|e| e.get("message")).and_then(|m| m.as_str()).unwrap_or("unknown error");
            Some(StreamChunk::Error(msg.to_string()))
        }
        _ => None,
    }
}

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
    pub fn spinner_frame(&self) -> &'static str {
        let elapsed = self.turn_start.map(|t| t.elapsed().as_millis()).unwrap_or(0);
        let idx = (elapsed / 80) as usize % SPINNER_FRAMES.len();
        SPINNER_FRAMES[idx]
    }

    pub fn new() -> Self {
        let mut app = Self {
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
            chat_messages: vec![ChatMessage::simple("system", "Welcome to Deus. Type a message or / for commands.")],
            chat_state: ChatState::Idle,
            stream_rx: None,
            turn_count: 0,
            model: "sonnet".to_string(),
            token_count: 0,
            cost_usd: 0.0,
            session_start: Instant::now(),
            turn_start: None,
            last_turn_duration: None,
            last_thinking_summary: None,
            show_tools: false,
            scroll_offset: 0,
            scroll_pinned: true,
            esc_pending: None,
            queued_messages: Vec::new(),
            git_branch: detect_git_branch(),
            wardens: Vec::new(),
            services: Vec::new(),
            channels: Vec::new(),
            deus_config: Vec::new(),
        };
        app.refresh();
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

        // Handle commands
        let handled = self.handle_command(&msg);
        if handled {
            self.input.clear();
            self.input_cursor = 0;
            self.suggestions.clear();
            self.arg_suggestions.clear();
            return;
        }

        if matches!(self.chat_state, ChatState::Streaming) {
            self.queued_messages.push(msg);
            self.chat_messages.push(ChatMessage::simple("system", "(queued)"));
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
        self.chat_messages.push(ChatMessage::simple("user", &msg));
        self.chat_state = ChatState::Streaming;
        self.turn_start = Some(Instant::now());
        self.last_thinking_summary = None;
        self.scroll_to_bottom();

        let (tx, rx) = mpsc::channel();
        self.stream_rx = Some(rx);
        let model = self.model.clone();
        let backend = model_backend(&self.model).to_string();
        let is_continuation = self.turn_count > 0;
        self.turn_count += 1;

        thread::spawn(move || {
            let child = match backend.as_str() {
                "codex" => {
                    let mut args = vec!["exec".to_string(), "--json".to_string(),
                        "-m".to_string(), model.clone()];
                    args.push(msg.clone());
                    Command::new("codex")
                        .args(&args)
                        .stdin(Stdio::null())
                        .stdout(Stdio::piped())
                        .stderr(Stdio::piped())
                        .spawn()
                }
                _ => {
                    let mut args = vec!["-p", "--output-format", "stream-json", "--verbose",
                        "--model", &model];
                    if is_continuation {
                        args.push("--continue");
                    }
                    args.push(&msg);
                    Command::new("claude")
                        .args(&args)
                        .stdout(Stdio::piped())
                        .stderr(Stdio::piped())
                        .spawn()
                }
            };

            match child {
                Ok(mut process) => {
                    let stderr_handle = process.stderr.take().map(|stderr| {
                        let tx2 = tx.clone();
                        thread::spawn(move || {
                            let mut buf = String::new();
                            let mut reader = BufReader::new(stderr);
                            let _ = reader.read_to_string(&mut buf);
                            if !buf.trim().is_empty() {
                                let _ = tx2.send(StreamChunk::Error(buf.trim().to_string()));
                            }
                        })
                    });
                    if let Some(stdout) = process.stdout.take() {
                        let reader = BufReader::new(stdout);
                        for line in reader.lines() {
                            match line {
                                Ok(text) => {
                                    if let Some(chunk) = parse_stream_line(&text) {
                                        let _ = tx.send(chunk);
                                    }
                                }
                                Err(e) => { let _ = tx.send(StreamChunk::Error(e.to_string())); break; }
                            }
                        }
                    }
                    let _ = process.wait();
                    if let Some(h) = stderr_handle { let _ = h.join(); }
                    let _ = tx.send(StreamChunk::Done);
                }
                Err(e) => {
                    let _ = tx.send(StreamChunk::Error(format!("Failed to run claude: {}", e)));
                    let _ = tx.send(StreamChunk::Done);
                }
            }
        });

        self.chat_messages.push(ChatMessage {
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
            "/wardens" if arg.is_empty() => { self.tab = Tab::Wardens; self.cursor = 0; true }
            "/wardens" => {
                // /wardens enable|disable|reset <name> → pass to python CLI
                false
            }
            "/services" => { self.tab = Tab::Services; self.cursor = 0; true }
            "/channels" => { self.tab = Tab::Channels; self.cursor = 0; true }
            "/config" => { self.tab = Tab::Config; self.cursor = 0; true }
            "/status" => { self.tab = Tab::Status; self.cursor = 0; true }
            "/clear" => { self.chat_messages.clear(); true }
            "/quit" => { std::process::exit(0); }
            "/model" => {
                let ids = model_ids();
                if arg.is_empty() {
                    let list: Vec<String> = MODEL_REGISTRY.iter()
                        .map(|m| format!("{} ({})", m.display, m.context))
                        .collect();
                    self.chat_messages.push(ChatMessage::simple("system", &format!(
                        "Current: {} [{}]\nAvailable: {}",
                        model_display(&self.model), model_backend(&self.model), list.join(", ")
                    )));
                } else if ids.contains(&arg) {
                    let prev_backend = model_backend(&self.model);
                    self.model = arg.to_string();
                    let new_backend = model_backend(&self.model);
                    if prev_backend != new_backend {
                        self.turn_count = 0;
                    }
                    self.chat_messages.push(ChatMessage::simple("system", &format!(
                        "Switched to {} [{}]", model_display(&self.model), new_backend
                    )));
                } else {
                    let list: Vec<String> = MODEL_REGISTRY.iter()
                        .map(|m| format!("{} ({})", m.id, m.display))
                        .collect();
                    self.chat_messages.push(ChatMessage::simple("system", &format!(
                        "Unknown model: {}. Available: {}", arg, list.join(", ")
                    )));
                }
                true
            }
            "/help" => {
                let help: String = COMMANDS.iter()
                    .map(|c| format!("  {:16} {}", c.name, c.description))
                    .collect::<Vec<_>>()
                    .join("\n");
                self.chat_messages.push(ChatMessage::simple("system", &format!("Available commands:\n\n{}\n\nKeyboard shortcuts:\n  Ctrl+L  Clear screen\n  Ctrl+U  Clear input line\n  Ctrl+K  Kill to end of line\n  Ctrl+Y  Yank (paste killed text)\n  Ctrl+A  Start of line\n  Ctrl+E  End of line\n  Ctrl+J  New line (multi-line input)\n  Ctrl+O  Toggle tool/thinking details\n  Ctrl+D  Exit\n  Alt+B   Word left\n  Alt+F   Word right\n  ↑/↓     Input history\n  PgUp/Dn Scroll chat\n  Mouse   Scroll chat (Shift+drag to select text)", help)));
                true
            }
            "/history" => {
                self.chat_messages.push(ChatMessage::simple("system", "Session history browsing coming in Phase 2."));
                true
            }
            "/init" | "/compress" | "/checkpoint" | "/compact" | "/resume" => {
                false // Pass through to claude
            }
            _ => false,
        }
    }

    pub fn poll_response(&mut self) {
        if let Some(rx) = &self.stream_rx {
            while let Ok(chunk) = rx.try_recv() {
                match chunk {
                    StreamChunk::Text(text) => {
                        if let Some(last) = self.chat_messages.last_mut() {
                            if last.role == "assistant" {
                                if !last.content.is_empty() {
                                    last.content.push('\n');
                                }
                                last.content.push_str(&text);
                                last.blocks.push(MessageBlock::Text(text));
                            }
                        }
                    }
                    StreamChunk::Thinking(text) => {
                        let summary: String = text.chars().take(100).collect();
                        self.last_thinking_summary = Some(
                            if summary.len() < text.len() { format!("{}...", summary) } else { summary }
                        );
                        if let Some(last) = self.chat_messages.last_mut() {
                            if last.role == "assistant" {
                                last.blocks.push(MessageBlock::Thinking(text));
                            }
                        }
                    }
                    StreamChunk::ToolUse { tool, status } => {
                        if let Some(last) = self.chat_messages.last_mut() {
                            if last.role == "assistant" {
                                let line = format!("[{}] {}", tool, status);
                                if !last.content.is_empty() {
                                    last.content.push('\n');
                                }
                                last.content.push_str(&line);
                                last.blocks.push(MessageBlock::ToolUse { tool, detail: status });
                            }
                        }
                    }
                    StreamChunk::ToolResult { .. } => {}
                    StreamChunk::CostUpdate { cost_usd, input_tokens, output_tokens } => {
                        self.cost_usd = cost_usd;
                        self.token_count = (input_tokens + output_tokens) as u32;
                    }
                    StreamChunk::Done => {
                        self.chat_state = ChatState::Idle;
                        self.stream_rx = None;
                        self.last_turn_duration = self.turn_start.map(|t| t.elapsed());
                        self.turn_start = None;
                        if !self.queued_messages.is_empty() {
                            let next = self.queued_messages.remove(0);
                            self.dispatch_message(next);
                        }
                        return;
                    }
                    StreamChunk::Error(e) => {
                        if let Some(last) = self.chat_messages.last_mut() {
                            if last.role == "assistant" {
                                last.content.push_str(&format!("\n[Error: {}]", e));
                            }
                        }
                    }
                }
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

        // Check if we're completing an argument (input has a space after the command)
        if let Some(space_idx) = self.input.find(' ') {
            let cmd_part = &self.input[..space_idx];
            let arg_part = &self.input[space_idx + 1..];

            if let Some(cmd) = COMMANDS.iter().find(|c| c.name == cmd_part) {
                if cmd.name == "/model" {
                    self.arg_suggestions = model_arg_labels()
                        .into_iter()
                        .filter(|a| a.starts_with(arg_part))
                        .collect();
                } else if !cmd.args.is_empty() {
                    self.arg_suggestions = cmd.args
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

        // Command completion
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
        (!self.suggestions.is_empty() || !self.arg_suggestions.is_empty()) && self.input.starts_with('/')
    }

    pub fn dismiss_suggestions(&mut self) {
        self.suggestions.clear();
        self.arg_suggestions.clear();
        self.suggestion_cursor = 0;
    }

    pub fn cancel_response(&mut self) {
        self.chat_state = ChatState::Idle;
        self.stream_rx = None;
        if let Some(last) = self.chat_messages.last_mut() {
            if last.role == "assistant" && last.content.is_empty() {
                self.chat_messages.pop();
            } else if last.role == "assistant" {
                last.content.push_str("\n[cancelled]");
            }
        }
    }

    pub fn accept_suggestion(&mut self) {
        if !self.arg_suggestions.is_empty() {
            if let Some(arg) = self.arg_suggestions.get(self.suggestion_cursor) {
                let space_idx = self.input.find(' ').unwrap_or(self.input.len());
                let arg_value = arg.split_whitespace().next().unwrap_or(arg);
                self.input = format!("{} {}", &self.input[..space_idx], arg_value);
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
        let word_start = before[..end].rfind(' ')
            .map(|i| i + 1)
            .unwrap_or(0);
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
        let line_start = self.input[..self.input_cursor].rfind('\n').map(|i| i + 1).unwrap_or(0);
        let line_end = self.input[self.input_cursor..].find('\n')
            .map(|i| self.input_cursor + i)
            .unwrap_or(self.input.len());
        // Remove the line and the preceding newline if not the first line
        let drain_start = if line_start > 0 { line_start - 1 } else { line_start };
        let drain_end = if line_end < self.input.len() && drain_start == line_start { line_end + 1 } else { line_end };
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

    pub fn session_duration(&self) -> String {
        let secs = self.session_start.elapsed().as_secs();
        if secs < 60 { format!("{}s", secs) }
        else if secs < 3600 { format!("{}m", secs / 60) }
        else { format!("{}h{}m", secs / 3600, (secs % 3600) / 60) }
    }

    pub fn toggle_tools(&mut self) {
        self.show_tools = !self.show_tools;
    }

    pub fn scroll_up(&mut self, amount: u16) {
        self.scroll_offset = self.scroll_offset.saturating_add(amount);
        self.scroll_pinned = false;
    }

    pub fn scroll_down(&mut self, amount: u16) {
        self.scroll_offset = self.scroll_offset.saturating_sub(amount);
        if self.scroll_offset == 0 {
            self.scroll_pinned = true;
        }
    }

    pub fn scroll_to_bottom(&mut self) {
        self.scroll_offset = 0;
        self.scroll_pinned = true;
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
        if self.input_cursor == 0 { return; }
        let before = &self.input[..self.input_cursor];
        let trimmed = before.trim_end();
        let pos = trimmed.rfind(|c: char| c == ' ' || c == '\n')
            .map(|i| i + 1)
            .unwrap_or(0);
        self.input_cursor = pos;
    }

    pub fn input_word_right(&mut self) {
        if self.input_cursor >= self.input.len() { return; }
        let after = &self.input[self.input_cursor..];
        let skip_word = after.find(|c: char| c == ' ' || c == '\n').unwrap_or(after.len());
        let skip_space = after[skip_word..].find(|c: char| c != ' ' && c != '\n').unwrap_or(after.len() - skip_word);
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
            let line_start = self.input[..self.input_cursor].rfind('\n').map(|i| i + 1).unwrap_or(0);
            let col = self.input_cursor - line_start;
            let next_start = self.input_cursor + nl + 1;
            let next_end = self.input[next_start..].find('\n').map(|i| next_start + i).unwrap_or(self.input.len());
            let next_len = next_end - next_start;
            self.input_cursor = next_start + col.min(next_len);
        }
    }

    pub fn turn_duration_display(&self) -> Option<String> {
        let dur = if matches!(self.chat_state, ChatState::Streaming) {
            self.turn_start.map(|t| t.elapsed())
        } else {
            self.last_turn_duration
        };
        dur.map(|d| {
            let secs = d.as_secs();
            if secs < 60 { format!("{}s", secs) }
            else { format!("{}m{}s", secs / 60, secs % 60) }
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
