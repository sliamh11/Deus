use std::process::{Command, Stdio};
use super::{Backend, ModelDef, RunConfig, StreamChunk, ChunkKind, SUBAGENT_TOOLS_CODEX};

pub struct CodexBackend;

static MODELS: &[ModelDef] = &[
    ModelDef { id: "gpt-5.5", display: "GPT-5.5", context: "1M" },
    ModelDef { id: "gpt-5.4", display: "GPT-5.4", context: "1M" },
    ModelDef { id: "gpt-5.4-mini", display: "GPT-5.4 Mini", context: "1M" },
    ModelDef { id: "o3", display: "o3", context: "200K" },
    ModelDef { id: "o4-mini", display: "o4-mini", context: "200K" },
];

impl Backend for CodexBackend {
    fn name(&self) -> &'static str { "codex" }
    fn display_name(&self) -> &'static str { "OpenAI (GPT/o-series)" }
    fn models(&self) -> &'static [ModelDef] { MODELS }

    fn build_command(&self, config: &RunConfig) -> Command {
        let effort_cfg = format!("model_reasoning_effort=\"{}\"", config.effort);

        let message = if let Some(ref ctx_file) = config.system_context_file {
            if let Ok(ctx) = std::fs::read_to_string(ctx_file) {
                if !ctx.is_empty() {
                    format!("{}\n\nUSER REQUEST:\n{}", ctx, config.message)
                } else {
                    config.message.clone()
                }
            } else {
                config.message.clone()
            }
        } else {
            config.message.clone()
        };

        let mut cmd = Command::new("codex");
        cmd.args(["exec", "--json", "-m", &config.model, "-c", &effort_cfg]);
        if config.bypass_permissions {
            cmd.arg("--dangerously-bypass-approvals-and-sandbox");
        }
        cmd.arg(&message);
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        cmd
    }

    fn parse_line(&self, line: &str) -> Vec<StreamChunk> {
        let v: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        let event_type = match v.get("type").and_then(|t| t.as_str()) {
            Some(t) => t,
            None => return Vec::new(),
        };

        match event_type {
            "item.completed" => {
                if let Some(text) = v.get("item").and_then(|i| i.get("text")).and_then(|t| t.as_str()) {
                    vec![StreamChunk { kind: ChunkKind::Text(text.to_string()) }]
                } else {
                    Vec::new()
                }
            }
            "function_call" => {
                let id = v.get("id").and_then(|i| i.as_str()).unwrap_or("").to_string();
                let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                let args = v.get("arguments").and_then(|a| a.as_str()).unwrap_or("");
                let detail: String = args.chars().take(80).collect();

                if SUBAGENT_TOOLS_CODEX.contains(&name) {
                    let parsed = serde_json::from_str::<serde_json::Value>(args).ok();
                    let subagent_type = parsed.as_ref()
                        .and_then(|a| a.get("type").and_then(|t| t.as_str()).map(String::from))
                        .unwrap_or_else(|| name.to_string());
                    let description = parsed.as_ref()
                        .and_then(|a| a.get("description").and_then(|d| d.as_str()).map(String::from))
                        .unwrap_or_default();
                    vec![StreamChunk {
                        kind: ChunkKind::SubagentStart { id, subagent_type, description },
                    }]
                } else {
                    vec![StreamChunk {
                        kind: ChunkKind::ToolUse { id, tool: name.to_string(), detail },
                    }]
                }
            }
            "function_call_output" => {
                let id = v.get("call_id").and_then(|i| i.as_str()).unwrap_or("").to_string();
                let output = v.get("output").and_then(|o| o.as_str()).unwrap_or("");
                let preview: String = output.chars().take(200).collect();
                vec![StreamChunk {
                    kind: ChunkKind::ToolResult { id, content_preview: preview },
                }]
            }
            "turn.completed" => {
                let usage = v.get("usage");
                let input = usage.and_then(|u| u.get("input_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                let output = usage.and_then(|u| u.get("output_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                vec![StreamChunk { kind: ChunkKind::CostUpdate { cost_usd: 0.0, input_tokens: input, output_tokens: output } }]
            }
            "turn.failed" => {
                let msg = v.get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("unknown error");
                vec![StreamChunk { kind: ChunkKind::Error(msg.to_string()) }]
            }
            _ => Vec::new(),
        }
    }
}
