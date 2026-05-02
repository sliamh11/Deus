use std::process::{Command, Stdio};
use super::{Backend, ModelDef, RunConfig, StreamChunk, ChunkKind};

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
        let mut cmd = Command::new("codex");
        cmd.args(["exec", "--json", "-m", &config.model, "-c", &effort_cfg, &config.message]);
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        cmd
    }

    fn parse_line(&self, line: &str) -> Option<StreamChunk> {
        let v: serde_json::Value = serde_json::from_str(line).ok()?;
        let event_type = v.get("type")?.as_str()?;

        match event_type {
            "item.completed" => {
                let text = v.get("item")?.get("text")?.as_str()?;
                Some(StreamChunk { kind: ChunkKind::Text(text.to_string()) })
            }
            "turn.completed" => {
                let usage = v.get("usage");
                let input = usage.and_then(|u| u.get("input_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                let output = usage.and_then(|u| u.get("output_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                Some(StreamChunk { kind: ChunkKind::CostUpdate { cost_usd: 0.0, input_tokens: input, output_tokens: output } })
            }
            "turn.failed" => {
                let msg = v.get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("unknown error");
                Some(StreamChunk { kind: ChunkKind::Error(msg.to_string()) })
            }
            _ => None,
        }
    }
}
