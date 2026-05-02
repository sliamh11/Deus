use std::fs;

use super::repo_root;
use crate::platform;

#[derive(Clone)]
pub struct ChannelEntry {
    pub name: String,
    pub configured: bool,
}

fn env_has(key: &str) -> bool {
    if platform::env_var(key).is_some() {
        return true;
    }
    let env_path = repo_root().join(".env");
    let content = match fs::read_to_string(&env_path) {
        Ok(c) => c,
        Err(_) => return false,
    };
    let prefix = format!("{}=", key);
    content.lines().any(|line| line.starts_with(&prefix) && line.len() > prefix.len())
}

pub fn load() -> Vec<ChannelEntry> {
    let root = repo_root();
    vec![
        ChannelEntry {
            name: "WhatsApp".to_string(),
            configured: root.join("store").join("auth").join("creds.json").exists(),
        },
        ChannelEntry {
            name: "Telegram".to_string(),
            configured: env_has("TELEGRAM_BOT_TOKEN"),
        },
        ChannelEntry {
            name: "Discord".to_string(),
            configured: env_has("DISCORD_BOT_TOKEN"),
        },
        ChannelEntry {
            name: "Slack".to_string(),
            configured: env_has("SLACK_BOT_TOKEN"),
        },
        ChannelEntry {
            name: "Gmail".to_string(),
            configured: env_has("GMAIL_CLIENT_ID"),
        },
        ChannelEntry {
            name: "X (Twitter)".to_string(),
            configured: env_has("X_API_KEY"),
        },
    ]
}
