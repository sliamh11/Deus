pub mod claude;
pub mod codex;

use std::path::PathBuf;
use std::process::Command;

use crate::config::permissions::PermissionsConfig;

pub struct ModelDef {
    pub id: &'static str,
    pub display: &'static str,
    pub context: &'static str,
}

pub struct StreamChunk {
    pub kind: ChunkKind,
}

pub enum ChunkKind {
    Text(String),
    Thinking(String),
    ToolUse {
        id: String,
        tool: String,
        detail: String,
    },
    ToolResult {
        id: String,
        content_preview: String,
    },
    SubagentStart {
        id: String,
        subagent_type: String,
        description: String,
    },
    CostUpdate {
        cost_usd: f64,
        input_tokens: u64,
        output_tokens: u64,
    },
    PermissionDenials(Vec<PermissionDenial>),
    Done,
    Error(String),
}

#[derive(Clone, Debug)]
pub struct PermissionDenial {
    pub tool_name: String,
    pub tool_input_preview: String,
}

/// How a backend session should be launched.
///
/// Enum makes illegal states unrepresentable: you can't accidentally set both
/// `resume` and `ephemeral` at the same time. Each variant carries only the
/// data relevant to that mode.
#[derive(Clone, Debug, PartialEq, Eq)]
#[allow(dead_code)]
pub enum RunMode {
    /// Normal session — optionally pinned to a backend-scoped UUID.
    /// `session_id` is None for the main session (backend picks its own).
    Normal { session_id: Option<String> },
    /// Resume an existing session by its backend-scoped ID.
    Resume { session_id: String },
    /// Lightweight sidechain — no persistence, minimal startup overhead.
    /// Claude: --bare --no-session-persistence. Codex: --ephemeral.
    Ephemeral,
}

impl Default for RunMode {
    fn default() -> Self {
        Self::Normal { session_id: None }
    }
}

pub struct RunConfig {
    pub model: String,
    pub message: String,
    pub effort: String,
    pub is_continuation: bool,
    pub system_context_file: Option<PathBuf>,
    pub permissions: PermissionsConfig,
    pub run_mode: RunMode,
    pub permissions_dir: Option<PathBuf>,
}

// Tool names that represent subagent/task spawning across providers.
// Claude uses PascalCase, Codex uses snake_case — both lists here.
pub const SUBAGENT_TOOLS_CLAUDE: &[&str] = &["Agent", "TaskCreate", "TaskUpdate"];
pub const SUBAGENT_TOOLS_CODEX: &[&str] = &["spawn_agent", "task_create", "task_update"];

pub trait Backend: Send + Sync {
    fn name(&self) -> &'static str;
    fn display_name(&self) -> &'static str;
    fn models(&self) -> &'static [ModelDef];
    fn build_command(&self, config: &RunConfig) -> Command;
    fn parse_line(&self, line: &str) -> Vec<StreamChunk>;
}

pub fn all_backends() -> Vec<Box<dyn Backend>> {
    vec![
        Box::new(claude::ClaudeBackend),
        Box::new(codex::CodexBackend),
    ]
}

pub fn find_backend(model_id: &str) -> Option<Box<dyn Backend>> {
    all_backends()
        .into_iter()
        .find(|b| b.models().iter().any(|m| m.id == model_id))
}

pub fn backend_for(model_id: &str) -> Box<dyn Backend> {
    find_backend(model_id).unwrap_or_else(|| Box::new(claude::ClaudeBackend))
}

#[allow(dead_code)]
pub fn all_models() -> Vec<(&'static str, &'static ModelDef)> {
    let mut out = Vec::new();
    for b in all_backends() {
        let name = b.name();
        for m in b.models() {
            out.push((name, m));
        }
    }
    out
}

pub fn parse_context_tokens(context: &str) -> u64 {
    if let Some(n) = context
        .strip_suffix('M')
        .and_then(|s| s.parse::<u64>().ok())
    {
        n * 1_000_000
    } else if let Some(n) = context
        .strip_suffix('K')
        .and_then(|s| s.parse::<u64>().ok())
    {
        n * 1_000
    } else {
        200_000
    }
}

pub fn model_context_tokens(id: &str) -> u64 {
    for b in all_backends() {
        if let Some(m) = b.models().iter().find(|m| m.id == id) {
            return parse_context_tokens(m.context);
        }
    }
    200_000
}

pub fn model_display(id: &str) -> String {
    for b in all_backends() {
        if let Some(m) = b.models().iter().find(|m| m.id == id) {
            return m.display.to_string();
        }
    }
    id.to_string()
}

pub fn model_backend_name(id: &str) -> &'static str {
    for b in all_backends() {
        if b.models().iter().any(|m| m.id == id) {
            return b.name();
        }
    }
    "claude"
}

pub fn models_for_backend(backend: &str) -> Vec<String> {
    for b in all_backends() {
        if b.name() == backend {
            return b
                .models()
                .iter()
                .map(|m| format!("{} ({})", m.display, m.context))
                .collect();
        }
    }
    Vec::new()
}

pub fn model_id_from_suggestion(suggestion: &str) -> Option<String> {
    for b in all_backends() {
        for m in b.models() {
            let label = format!("{} ({})", m.display, m.context);
            if label == suggestion {
                return Some(m.id.to_string());
            }
        }
    }
    None
}

pub fn backend_labels() -> Vec<String> {
    all_backends()
        .iter()
        .map(|b| format!("{} — {}", b.name(), b.display_name()))
        .collect()
}

pub fn model_ids() -> Vec<&'static str> {
    let mut out = Vec::new();
    for b in all_backends() {
        for m in b.models() {
            out.push(m.id);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_context_1m() {
        assert_eq!(parse_context_tokens("1M"), 1_000_000);
    }

    #[test]
    fn parse_context_200k() {
        assert_eq!(parse_context_tokens("200K"), 200_000);
    }

    #[test]
    fn parse_context_500k() {
        assert_eq!(parse_context_tokens("500K"), 500_000);
    }

    #[test]
    fn parse_context_fallback() {
        assert_eq!(parse_context_tokens("unknown"), 200_000);
    }

    #[test]
    fn model_context_known_model() {
        assert_eq!(model_context_tokens("opus"), 1_000_000);
        assert_eq!(model_context_tokens("sonnet"), 200_000);
    }

    #[test]
    fn model_context_unknown_model() {
        assert_eq!(model_context_tokens("nonexistent"), 200_000);
    }
}
