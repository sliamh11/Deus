use std::process::{Command, Stdio};
use super::{Backend, ModelDef, RunConfig, StreamChunk, ChunkKind};

pub struct ClaudeBackend;

static MODELS: &[ModelDef] = &[
    ModelDef { id: "opus-4-7", display: "Opus 4.7", context: "200K" },
    ModelDef { id: "opus", display: "Opus 4.6", context: "1M" },
    ModelDef { id: "opus-200k", display: "Opus 4.6", context: "200K" },
    ModelDef { id: "sonnet", display: "Sonnet 4.6", context: "200K" },
    ModelDef { id: "haiku", display: "Haiku 4.5", context: "200K" },
];

impl Backend for ClaudeBackend {
    fn name(&self) -> &'static str { "claude" }
    fn display_name(&self) -> &'static str { "Anthropic (Claude)" }
    fn models(&self) -> &'static [ModelDef] { MODELS }

    fn build_command(&self, config: &RunConfig) -> Command {
        let mut cmd = Command::new("claude");
        cmd.args(["-p", "--output-format", "stream-json", "--verbose",
            "--model", &config.model, "--effort", &config.effort]);
        if config.is_continuation {
            cmd.arg("--continue");
        }
        cmd.arg(&config.message);
        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
        cmd
    }

    fn parse_line(&self, line: &str) -> Option<StreamChunk> {
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
                            return Some(StreamChunk { kind: ChunkKind::Text(text.to_string()) });
                        }
                        "thinking" => {
                            let text = block.get("thinking")?.as_str()?;
                            return Some(StreamChunk { kind: ChunkKind::Thinking(text.to_string()) });
                        }
                        "tool_use" => {
                            let name = block.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                            let input = block.get("input").map(|i| {
                                if let Some(cmd) = i.get("command").and_then(|c| c.as_str()) {
                                    cmd.chars().take(80).collect::<String>()
                                } else if let Some(path) = i.get("file_path").and_then(|p| p.as_str()) {
                                    path.to_string()
                                } else {
                                    String::new()
                                }
                            }).unwrap_or_default();
                            return Some(StreamChunk { kind: ChunkKind::ToolUse { tool: name.to_string(), detail: input } });
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
                        return Some(StreamChunk { kind: ChunkKind::ToolResult });
                    }
                }
                None
            }
            "result" => {
                let cost = v.get("total_cost_usd").and_then(|c| c.as_f64()).unwrap_or(0.0);
                let usage = v.get("usage");
                let input = usage.and_then(|u| u.get("input_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                let output = usage.and_then(|u| u.get("output_tokens")).and_then(|t| t.as_u64()).unwrap_or(0);
                Some(StreamChunk { kind: ChunkKind::CostUpdate { cost_usd: cost, input_tokens: input, output_tokens: output } })
            }
            _ => None,
        }
    }
}
