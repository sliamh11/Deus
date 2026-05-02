pub mod channels;
pub mod deus;
pub mod healthcheck;
pub mod wardens;

use std::path::PathBuf;

pub fn repo_root() -> PathBuf {
    let exe = std::env::current_exe().unwrap_or_default();
    // Binary at tui/target/{debug,release}/deus-tui → repo is 3 levels up
    // Also handle running from repo root via `cargo run`
    let mut dir = exe.parent().unwrap_or(std::path::Path::new(".")).to_path_buf();
    for _ in 0..5 {
        if dir.join(".claude").join("wardens").exists() {
            return dir;
        }
        if !dir.pop() {
            break;
        }
    }
    // Fallback: try CWD
    let cwd = std::env::current_dir().unwrap_or_default();
    for ancestor in cwd.ancestors() {
        if ancestor.join(".claude").join("wardens").exists() {
            return ancestor.to_path_buf();
        }
    }
    cwd
}
