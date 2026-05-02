use std::path::PathBuf;

pub fn home_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"))
}

pub fn config_dir() -> PathBuf {
    home_dir().join(".config").join("deus")
}

pub fn config_file() -> PathBuf {
    config_dir().join("config.json")
}

pub fn current_dir() -> PathBuf {
    std::env::current_dir().unwrap_or_default()
}

pub fn current_exe() -> PathBuf {
    std::env::current_exe().unwrap_or_default()
}

pub fn env_var(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

pub fn env_flag(key: &str) -> bool {
    env_var(key).map(|v| v == "true").unwrap_or(false)
}

pub fn expand_tilde(path: &str) -> PathBuf {
    if path.starts_with('~') {
        home_dir().join(&path[2..])
    } else {
        PathBuf::from(path)
    }
}

pub fn display_path(path: &std::path::Path) -> String {
    let home = home_dir();
    if let Ok(rel) = path.strip_prefix(&home) {
        format!("~/{}", rel.display())
    } else {
        path.display().to_string()
    }
}

pub fn is_macos() -> bool {
    cfg!(target_os = "macos")
}

pub fn is_linux() -> bool {
    cfg!(target_os = "linux")
}

pub fn is_windows() -> bool {
    cfg!(target_os = "windows")
}
