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

pub fn spawn_editor(content: &str) -> Result<String, String> {
    let editor = env_var("VISUAL")
        .or_else(|| env_var("EDITOR"))
        .unwrap_or_else(|| {
            if cfg!(target_os = "windows") {
                "notepad".to_string()
            } else {
                "vim".to_string()
            }
        });

    let tmp_dir = std::env::temp_dir();
    let tmp_path = tmp_dir.join(format!("deus-editor-{}.txt", std::process::id()));

    std::fs::write(&tmp_path, content).map_err(|e| format!("Failed to write temp file: {}", e))?;

    let status = std::process::Command::new(&editor)
        .arg(&tmp_path)
        .stdin(std::process::Stdio::inherit())
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::inherit())
        .status()
        .map_err(|e| format!("Failed to launch {}: {}", editor, e))?;

    if !status.success() {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(format!("Editor exited with {}", status));
    }

    let result =
        std::fs::read_to_string(&tmp_path).map_err(|e| format!("Failed to read back: {}", e))?;
    let _ = std::fs::remove_file(&tmp_path);
    Ok(result)
}

pub fn is_macos() -> bool {
    cfg!(target_os = "macos")
}

#[allow(dead_code)]
pub fn is_linux() -> bool {
    cfg!(target_os = "linux")
}

#[allow(dead_code)]
pub fn is_windows() -> bool {
    cfg!(target_os = "windows")
}
