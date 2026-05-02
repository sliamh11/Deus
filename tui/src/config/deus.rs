use crate::platform;
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;

pub fn read_key(key: &str) -> Option<String> {
    let path = platform::config_file();
    let content = fs::read_to_string(path).ok()?;
    let map: serde_json::Map<String, Value> = serde_json::from_str(&content).ok()?;
    match map.get(key)? {
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

pub fn load() -> Vec<(String, String)> {
    let path = platform::config_file();

    let content = match fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };

    let map: BTreeMap<String, Value> = match serde_json::from_str(&content) {
        Ok(m) => m,
        Err(_) => return Vec::new(),
    };

    map.into_iter()
        .filter_map(|(k, v)| {
            let display = match &v {
                Value::String(s) => s.clone(),
                Value::Bool(b) => b.to_string(),
                Value::Number(n) => n.to_string(),
                Value::Null => return None,
                _ => serde_json::to_string(&v).unwrap_or_default(),
            };
            Some((k, display))
        })
        .collect()
}
