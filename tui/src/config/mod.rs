pub mod channels;
pub mod deus;
pub mod healthcheck;
pub mod permissions;
pub mod wardens;

use crate::platform;
use std::path::PathBuf;

pub fn repo_root() -> PathBuf {
    let exe = platform::current_exe();
    let mut dir = exe
        .parent()
        .unwrap_or(std::path::Path::new("."))
        .to_path_buf();
    for _ in 0..5 {
        if dir.join(".claude").join("wardens").exists() {
            return dir;
        }
        if !dir.pop() {
            break;
        }
    }
    let cwd = platform::current_dir();
    for ancestor in cwd.ancestors() {
        if ancestor.join(".claude").join("wardens").exists() {
            return ancestor.to_path_buf();
        }
    }
    cwd
}
