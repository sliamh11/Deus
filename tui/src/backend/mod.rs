pub mod claude;
pub mod codex;

use std::process::Command;

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
    ToolUse { tool: String, detail: String },
    ToolResult,
    CostUpdate { cost_usd: f64, input_tokens: u64, output_tokens: u64 },
    Done,
    Error(String),
}

pub struct RunConfig {
    pub model: String,
    pub message: String,
    pub effort: String,
    pub is_continuation: bool,
}

pub trait Backend: Send + Sync {
    fn name(&self) -> &'static str;
    fn display_name(&self) -> &'static str;
    fn models(&self) -> &'static [ModelDef];
    fn build_command(&self, config: &RunConfig) -> Command;
    fn parse_line(&self, line: &str) -> Option<StreamChunk>;
}

pub fn all_backends() -> Vec<Box<dyn Backend>> {
    vec![
        Box::new(claude::ClaudeBackend),
        Box::new(codex::CodexBackend),
    ]
}

pub fn find_backend(model_id: &str) -> Option<Box<dyn Backend>> {
    for b in all_backends() {
        if b.models().iter().any(|m| m.id == model_id) {
            return Some(b);
        }
    }
    None
}

pub fn backend_for(model_id: &str) -> Box<dyn Backend> {
    find_backend(model_id).unwrap_or_else(|| Box::new(claude::ClaudeBackend))
}

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
            return b.models().iter()
                .map(|m| format!("{} — {} ({})", m.id, m.display, m.context))
                .collect();
        }
    }
    Vec::new()
}

pub fn backend_labels() -> Vec<String> {
    all_backends().iter()
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
