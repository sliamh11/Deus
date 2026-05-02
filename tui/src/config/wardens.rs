use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use super::repo_root;

#[derive(Clone)]
pub struct WardenEntry {
    pub name: String,
    pub enabled: bool,
    pub warden_type: String,
    pub triggers: String,
    pub custom_instructions: Option<String>,
}

const TYPES: &[(&str, &str)] = &[
    ("plan-reviewer", "Validator (blocking)"),
    ("code-reviewer", "Validator (blocking)"),
    ("threat-modeler", "Validator (warning)"),
    ("architecture-snapshot", "Generator"),
    ("session-retrospective", "Generator"),
    ("data-quality", "Validator (manual)"),
];

fn config_path() -> PathBuf {
    repo_root().join(".claude").join("wardens").join("config.json")
}

fn triggers_label(val: &Value, name: &str) -> String {
    if name == "session-retrospective" {
        let threshold = val.get("auto_threshold").and_then(|v| v.as_u64()).unwrap_or(20);
        return format!("auto (threshold: {} sessions), manual", threshold);
    }
    match val.get("tools").and_then(|v| v.as_array()) {
        Some(tools) => tools
            .iter()
            .filter_map(|t| t.as_str())
            .collect::<Vec<_>>()
            .join(", "),
        None => "manual".to_string(),
    }
}

pub fn load() -> Vec<WardenEntry> {
    let path = config_path();
    let content = match fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    let map: BTreeMap<String, Value> = match serde_json::from_str(&content) {
        Ok(m) => m,
        Err(_) => return Vec::new(),
    };

    map.iter()
        .map(|(name, val)| {
            let enabled = val.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true);
            let warden_type = TYPES
                .iter()
                .find(|(n, _)| *n == name.as_str())
                .map(|(_, t)| t.to_string())
                .unwrap_or_default();
            let triggers = triggers_label(val, name);
            let custom_instructions = val
                .get("custom_instructions")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());

            WardenEntry { name: name.clone(), enabled, warden_type, triggers, custom_instructions }
        })
        .collect()
}

pub fn save(entries: &[WardenEntry]) {
    let path = config_path();
    let content = match fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return,
    };
    let mut map: BTreeMap<String, Value> = match serde_json::from_str(&content) {
        Ok(m) => m,
        Err(_) => return,
    };

    for entry in entries {
        if let Some(val) = map.get_mut(&entry.name) {
            if let Some(obj) = val.as_object_mut() {
                obj.insert("enabled".to_string(), Value::Bool(entry.enabled));
            }
        }
    }

    let json = serde_json::to_string_pretty(&map).unwrap_or_default();
    let _ = fs::write(&path, format!("{}\n", json));
}
