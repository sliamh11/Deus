use super::{Backend, ChunkKind, ModelDef, RunConfig, SUBAGENT_TOOLS_CODEX, StreamChunk};
use std::process::{Command, Stdio};

pub struct CodexBackend;

static MODELS: &[ModelDef] = &[
    ModelDef {
        id: "gpt-5.5",
        display: "GPT-5.5",
        context: "1M",
    },
    ModelDef {
        id: "gpt-5.4",
        display: "GPT-5.4",
        context: "1M",
    },
    ModelDef {
        id: "gpt-5.4-mini",
        display: "GPT-5.4 Mini",
        context: "1M",
    },
    ModelDef {
        id: "o3",
        display: "o3",
        context: "200K",
    },
    ModelDef {
        id: "o4-mini",
        display: "o4-mini",
        context: "200K",
    },
];

impl Backend for CodexBackend {
    fn name(&self) -> &'static str {
        "codex"
    }
    fn display_name(&self) -> &'static str {
        "OpenAI (GPT/o-series)"
    }
    fn models(&self) -> &'static [ModelDef] {
        MODELS
    }

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
        if config.permissions.is_bypass() {
            cmd.arg("--dangerously-bypass-approvals-and-sandbox");
        }
        cmd.arg(&message);
        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
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
            "item.completed" => v.get("item").map(parse_output_item).unwrap_or_default(),
            "response.output_item.done" => v.get("item").map(parse_output_item).unwrap_or_default(),
            "response.output_text.delta" => v
                .get("delta")
                .and_then(|d| d.as_str())
                .map(|delta| {
                    vec![StreamChunk {
                        kind: ChunkKind::Text(delta.to_string()),
                    }]
                })
                .unwrap_or_default(),
            "response.output_text.done" => v
                .get("text")
                .and_then(|t| t.as_str())
                .map(|text| {
                    vec![StreamChunk {
                        kind: ChunkKind::Text(text.to_string()),
                    }]
                })
                .unwrap_or_default(),
            "response.reasoning_text.done" => v
                .get("text")
                .and_then(|t| t.as_str())
                .map(|text| {
                    vec![StreamChunk {
                        kind: ChunkKind::Thinking(text.to_string()),
                    }]
                })
                .unwrap_or_default(),
            "response.reasoning_summary_text.delta" => v
                .get("delta")
                .and_then(|d| d.as_str())
                .map(|delta| {
                    vec![StreamChunk {
                        kind: ChunkKind::Thinking(delta.to_string()),
                    }]
                })
                .unwrap_or_default(),
            "response.reasoning_summary_text.done" => v
                .get("text")
                .and_then(|t| t.as_str())
                .map(|text| {
                    vec![StreamChunk {
                        kind: ChunkKind::Thinking(text.to_string()),
                    }]
                })
                .unwrap_or_default(),
            "response.function_call_arguments.done" => {
                let id = v
                    .get("call_id")
                    .and_then(|i| i.as_str())
                    .unwrap_or("")
                    .to_string();
                let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                let args = v.get("arguments").and_then(|a| a.as_str()).unwrap_or("");
                handle_function_call(name, &id, args)
            }
            "function_call" => {
                let id = v
                    .get("id")
                    .and_then(|i| i.as_str())
                    .unwrap_or("")
                    .to_string();
                let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                let args = v.get("arguments").and_then(|a| a.as_str()).unwrap_or("");
                handle_function_call(name, &id, args)
            }
            "function_call_output" => {
                let id = v
                    .get("call_id")
                    .and_then(|i| i.as_str())
                    .unwrap_or("")
                    .to_string();
                let output = v.get("output").and_then(|o| o.as_str()).unwrap_or("");
                let preview: String = output.chars().take(200).collect();
                vec![StreamChunk {
                    kind: ChunkKind::ToolResult {
                        id,
                        content_preview: preview,
                    },
                }]
            }
            "turn.completed" => {
                let usage = v.get("usage");
                let input = usage
                    .and_then(|u| u.get("input_tokens"))
                    .and_then(|t| t.as_u64())
                    .unwrap_or(0);
                let output = usage
                    .and_then(|u| u.get("output_tokens"))
                    .and_then(|t| t.as_u64())
                    .unwrap_or(0);
                vec![StreamChunk {
                    kind: ChunkKind::CostUpdate {
                        cost_usd: 0.0,
                        input_tokens: input,
                        output_tokens: output,
                    },
                }]
            }
            "turn.failed" => {
                let msg = v
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("unknown error");
                vec![StreamChunk {
                    kind: ChunkKind::Error(msg.to_string()),
                }]
            }
            _ => Vec::new(),
        }
    }
}

fn parse_output_item(item: &serde_json::Value) -> Vec<StreamChunk> {
    let item_type = item.get("type").and_then(|t| t.as_str()).unwrap_or("");

    match item_type {
        "message" => {
            let mut chunks = Vec::new();
            if let Some(content) = item.get("content").and_then(|c| c.as_array()) {
                for block in content {
                    let block_type = block.get("type").and_then(|t| t.as_str()).unwrap_or("");
                    match block_type {
                        "output_text" | "text" => {
                            if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                                chunks.push(StreamChunk {
                                    kind: ChunkKind::Text(text.to_string()),
                                });
                            }
                        }
                        "reasoning" | "thinking" => {
                            if let Some(text) = extract_reasoning_text(block) {
                                chunks.push(StreamChunk {
                                    kind: ChunkKind::Thinking(text),
                                });
                            }
                        }
                        _ => {}
                    }
                }
            } else if let Some(text) = item.get("text").and_then(|t| t.as_str()) {
                chunks.push(StreamChunk {
                    kind: ChunkKind::Text(text.to_string()),
                });
            }
            chunks
        }
        "reasoning" | "thinking" => extract_reasoning_text(item)
            .map(|text| {
                vec![StreamChunk {
                    kind: ChunkKind::Thinking(text),
                }]
            })
            .unwrap_or_default(),
        _ => item
            .get("text")
            .and_then(|t| t.as_str())
            .map(|text| {
                vec![StreamChunk {
                    kind: ChunkKind::Text(text.to_string()),
                }]
            })
            .unwrap_or_default(),
    }
}

fn extract_reasoning_text(value: &serde_json::Value) -> Option<String> {
    value
        .get("thinking")
        .and_then(|t| t.as_str())
        .or_else(|| value.get("text").and_then(|t| t.as_str()))
        .or_else(|| value.get("delta").and_then(|t| t.as_str()))
        .or_else(|| value.get("summary").and_then(|t| t.as_str()))
        .map(|s| s.to_string())
}

fn handle_function_call(name: &str, id: &str, args: &str) -> Vec<StreamChunk> {
    let detail: String = args.chars().take(80).collect();

    if SUBAGENT_TOOLS_CODEX.contains(&name) {
        let parsed = serde_json::from_str::<serde_json::Value>(args).ok();
        let subagent_type = parsed
            .as_ref()
            .and_then(|a| a.get("type").and_then(|t| t.as_str()).map(String::from))
            .unwrap_or_else(|| name.to_string());
        let description = parsed
            .as_ref()
            .and_then(|a| {
                a.get("description")
                    .and_then(|d| d.as_str())
                    .map(String::from)
            })
            .unwrap_or_default();
        vec![StreamChunk {
            kind: ChunkKind::SubagentStart {
                id: id.to_string(),
                subagent_type,
                description,
            },
        }]
    } else {
        vec![StreamChunk {
            kind: ChunkKind::ToolUse {
                id: id.to_string(),
                tool: name.to_string(),
                detail,
            },
        }]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(json: &str) -> Vec<ChunkKind> {
        CodexBackend
            .parse_line(json)
            .into_iter()
            .map(|c| c.kind)
            .collect()
    }

    #[test]
    fn parse_item_completed() {
        let json = r#"{"type":"item.completed","item":{"text":"hello world"}}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(&chunks[0], ChunkKind::Text(t) if t == "hello world"));
    }

    #[test]
    fn parse_reasoning_delta_event() {
        let json = r#"{"type":"response.reasoning_summary_text.delta","delta":"thinking..." }"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(&chunks[0], ChunkKind::Thinking(t) if t == "thinking..."));
    }

    #[test]
    fn parse_output_text_delta_event() {
        let json = r#"{"type":"response.output_text.delta","delta":"hello"}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(&chunks[0], ChunkKind::Text(t) if t == "hello"));
    }

    #[test]
    fn parse_turn_failed() {
        let json = r#"{"type":"turn.failed","error":{"message":"rate limited"}}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(&chunks[0], ChunkKind::Error(e) if e == "rate limited"));
    }

    #[test]
    fn parse_turn_completed() {
        let json = r#"{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":100}}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(
            &chunks[0],
            ChunkKind::CostUpdate {
                input_tokens: 50,
                output_tokens: 100,
                ..
            }
        ));
    }

    #[test]
    fn parse_function_call_subagent() {
        let json = r#"{"type":"function_call","id":"f1","name":"spawn_agent","arguments":"{\"type\":\"explore\",\"description\":\"search\"}"}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(
            matches!(&chunks[0], ChunkKind::SubagentStart { subagent_type, .. } if subagent_type == "explore")
        );
    }

    #[test]
    fn parse_function_call_regular() {
        let json = r#"{"type":"function_call","id":"f2","name":"read_file","arguments":"{\"path\":\"/tmp/x\"}"}"#;
        let chunks = parse(json);
        assert_eq!(chunks.len(), 1);
        assert!(matches!(&chunks[0], ChunkKind::ToolUse { tool, .. } if tool == "read_file"));
    }

    #[test]
    fn parse_invalid_json() {
        assert!(parse("garbage").is_empty());
    }
}
