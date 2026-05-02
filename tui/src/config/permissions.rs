use crate::platform;
use serde::{Deserialize, Serialize};
use std::fs;

pub const MODES_CLAUDE: &[&str] = &[
    "default",
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "dontAsk",
    "plan",
];

pub const MODES_CODEX: &[&str] = &["default", "bypassPermissions"];

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PermissionsConfig {
    pub mode: String,
    pub allowed_tools: Vec<String>,
    pub disallowed_tools: Vec<String>,
}

impl Default for PermissionsConfig {
    fn default() -> Self {
        Self {
            mode: "default".to_string(),
            allowed_tools: Vec::new(),
            disallowed_tools: Vec::new(),
        }
    }
}

impl PermissionsConfig {
    pub fn load() -> Self {
        let path = platform::config_dir().join("tui-permissions.json");
        fs::read_to_string(path)
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) {
        let path = platform::config_dir().join("tui-permissions.json");
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(json) = serde_json::to_string_pretty(self) {
            let _ = fs::write(path, json);
        }
    }

    pub fn set_mode(&mut self, mode: &str) -> bool {
        if MODES_CLAUDE.contains(&mode) {
            self.mode = mode.to_string();
            self.save();
            true
        } else {
            false
        }
    }

    pub fn add_allowed(&mut self, pattern: &str) {
        let p = pattern.to_string();
        if !self.allowed_tools.contains(&p) {
            self.allowed_tools.push(p);
            self.save();
        }
    }

    pub fn add_disallowed(&mut self, pattern: &str) {
        let p = pattern.to_string();
        if !self.disallowed_tools.contains(&p) {
            self.disallowed_tools.push(p);
            self.save();
        }
    }

    pub fn remove(&mut self, pattern: &str) -> bool {
        let before = self.allowed_tools.len() + self.disallowed_tools.len();
        self.allowed_tools.retain(|t| t != pattern);
        self.disallowed_tools.retain(|t| t != pattern);
        let changed = (self.allowed_tools.len() + self.disallowed_tools.len()) != before;
        if changed {
            self.save();
        }
        changed
    }

    pub fn reset(&mut self) {
        *self = Self::default();
        self.save();
    }

    pub fn is_bypass(&self) -> bool {
        self.mode == "bypassPermissions"
    }

    pub fn supports_mode(backend: &str, mode: &str) -> bool {
        match backend {
            "codex" => MODES_CODEX.contains(&mode),
            _ => MODES_CLAUDE.contains(&mode),
        }
    }

    pub fn available_modes(backend: &str) -> &'static [&'static str] {
        match backend {
            "codex" => MODES_CODEX,
            _ => MODES_CLAUDE,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config() {
        let cfg = PermissionsConfig::default();
        assert_eq!(cfg.mode, "default");
        assert!(cfg.allowed_tools.is_empty());
        assert!(cfg.disallowed_tools.is_empty());
    }

    #[test]
    fn set_valid_mode() {
        let mut cfg = PermissionsConfig::default();
        assert!(cfg.set_mode("acceptEdits"));
        assert_eq!(cfg.mode, "acceptEdits");
    }

    #[test]
    fn set_invalid_mode() {
        let mut cfg = PermissionsConfig::default();
        assert!(!cfg.set_mode("invalid"));
        assert_eq!(cfg.mode, "default");
    }

    #[test]
    fn add_and_remove() {
        let mut cfg = PermissionsConfig::default();
        cfg.allowed_tools.push("Read".to_string());
        cfg.allowed_tools.push("Edit".to_string());
        assert_eq!(cfg.allowed_tools.len(), 2);

        cfg.allowed_tools.retain(|t| t != "Read");
        assert_eq!(cfg.allowed_tools.len(), 1);
        assert_eq!(cfg.allowed_tools[0], "Edit");
    }

    #[test]
    fn is_bypass() {
        let mut cfg = PermissionsConfig::default();
        assert!(!cfg.is_bypass());
        cfg.mode = "bypassPermissions".to_string();
        assert!(cfg.is_bypass());
    }

    #[test]
    fn codex_supports_fewer_modes() {
        assert!(PermissionsConfig::supports_mode("codex", "default"));
        assert!(PermissionsConfig::supports_mode(
            "codex",
            "bypassPermissions"
        ));
        assert!(!PermissionsConfig::supports_mode("codex", "acceptEdits"));
    }

    #[test]
    fn claude_supports_all_modes() {
        for mode in MODES_CLAUDE {
            assert!(PermissionsConfig::supports_mode("claude", mode));
        }
    }

    #[test]
    fn roundtrip_serialize() {
        let mut cfg = PermissionsConfig::default();
        cfg.mode = "auto".to_string();
        cfg.allowed_tools = vec!["Bash(git *)".to_string(), "Read".to_string()];
        cfg.disallowed_tools = vec!["Write".to_string()];

        let json = serde_json::to_string(&cfg).unwrap();
        let loaded: PermissionsConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(loaded.mode, "auto");
        assert_eq!(loaded.allowed_tools.len(), 2);
        assert_eq!(loaded.disallowed_tools.len(), 1);
    }
}
