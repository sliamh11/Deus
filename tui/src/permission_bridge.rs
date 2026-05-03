use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use crate::app::SessionId;

const HOOK_SCRIPT: &str = include_str!("../hooks/permission-bridge.sh");
pub const HOOK_TIMEOUT_SECS: u64 = 120;
pub const ENV_PERMISSIONS_DIR: &str = "DEUS_TUI_PERMISSIONS_DIR";
pub const ENV_ALLOWED_TOOLS: &str = "DEUS_TUI_ALLOWED_TOOLS";
const TEMP_DIR_PREFIX: &str = "deus-tui-permissions-";
const HOOK_FILENAME: &str = "permission-bridge.sh";
const SETTINGS_FILENAME: &str = "settings.json";

fn is_safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

#[derive(Clone, Debug)]
pub struct PermissionRequest {
    pub tool_use_id: String,
    pub tool_name: String,
    pub tool_input_preview: String,
    pub session_id: SessionId,
}

pub struct PermissionBridge {
    base_dir: PathBuf,
}

impl PermissionBridge {
    pub fn new() -> Self {
        let base_dir =
            std::env::temp_dir().join(format!("{}{}", TEMP_DIR_PREFIX, std::process::id()));
        let _ = fs::create_dir_all(&base_dir);

        let hook_path = base_dir.join(HOOK_FILENAME);
        let _ = fs::write(&hook_path, HOOK_SCRIPT);
        let _ = fs::set_permissions(&hook_path, fs::Permissions::from_mode(0o755));

        Self { base_dir }
    }

    pub fn session_dir(&self, session_id: SessionId) -> PathBuf {
        self.base_dir.join(session_id.0.to_string())
    }

    pub fn create_session(&self, session_id: SessionId) -> PathBuf {
        let dir = self.session_dir(session_id);
        let _ = fs::create_dir_all(&dir);

        let settings = serde_json::json!({
            "hooks": {
                "PreToolUse": [{
                    "hooks": [{
                        "type": "command",
                        "command": self.base_dir.join(HOOK_FILENAME).to_string_lossy(),
                        "timeout": HOOK_TIMEOUT_SECS
                    }]
                }]
            }
        });
        let _ = fs::write(
            dir.join(SETTINGS_FILENAME),
            serde_json::to_string_pretty(&settings).unwrap_or_default(),
        );

        dir
    }

    pub fn poll(&self, session_id: SessionId) -> Vec<PermissionRequest> {
        let dir = self.session_dir(session_id);
        let entries = match fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => return Vec::new(),
        };

        let mut requests = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name_str = name.to_string_lossy();
            if !name_str.starts_with("request-") || !name_str.ends_with(".json") {
                continue;
            }

            let tool_id = name_str
                .strip_prefix("request-")
                .and_then(|s| s.strip_suffix(".json"))
                .unwrap_or("")
                .to_string();
            if !is_safe_id(&tool_id) {
                continue;
            }

            if dir.join(format!("response-{}.json", tool_id)).exists() {
                continue;
            }

            let content = match fs::read_to_string(entry.path()) {
                Ok(c) => c,
                Err(_) => continue,
            };
            let v: serde_json::Value = match serde_json::from_str(&content) {
                Ok(v) => v,
                Err(_) => continue,
            };

            requests.push(PermissionRequest {
                tool_use_id: v
                    .get("tool_use_id")
                    .and_then(|s| s.as_str())
                    .unwrap_or(&tool_id)
                    .to_string(),
                tool_name: v
                    .get("tool_name")
                    .and_then(|s| s.as_str())
                    .unwrap_or("")
                    .to_string(),
                tool_input_preview: v
                    .get("tool_input_preview")
                    .and_then(|s| s.as_str())
                    .unwrap_or("")
                    .to_string(),
                session_id,
            });
        }
        requests
    }

    pub fn respond(&self, req: &PermissionRequest, decision: &str, reason: &str) {
        if !is_safe_id(&req.tool_use_id) {
            return;
        }
        let dir = self.session_dir(req.session_id);
        let resp = serde_json::json!({
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        });
        let path = dir.join(format!("response-{}.json", req.tool_use_id));
        let tmp = dir.join(format!("response-{}.json.tmp", req.tool_use_id));
        let _ = fs::write(&tmp, serde_json::to_string(&resp).unwrap_or_default());
        let _ = fs::rename(&tmp, &path);
    }

    pub fn cleanup_session(&self, session_id: SessionId) {
        let _ = fs::remove_dir_all(self.session_dir(session_id));
    }

    pub fn cleanup(&self) {
        let _ = fs::remove_dir_all(&self.base_dir);
    }

    /// Remove orphaned permission dirs from previous TUI crashes.
    pub fn sweep_orphans() {
        let tmp = std::env::temp_dir();
        let entries = match fs::read_dir(&tmp) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name_str = name.to_string_lossy();
            if let Some(pid_str) = name_str.strip_prefix(TEMP_DIR_PREFIX)
                && let Ok(pid) = pid_str.parse::<u32>()
                && !process_alive(pid)
            {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

fn process_alive(pid: u32) -> bool {
    // kill(pid, 0) checks existence without sending a signal
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn test_bridge() -> PermissionBridge {
        let id = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let base_dir =
            std::env::temp_dir().join(format!("deus-tui-perms-test-{}-{}", std::process::id(), id));
        let _ = fs::remove_dir_all(&base_dir);
        let _ = fs::create_dir_all(&base_dir);

        let hook_path = base_dir.join(HOOK_FILENAME);
        let _ = fs::write(&hook_path, HOOK_SCRIPT);
        let _ = fs::set_permissions(&hook_path, fs::Permissions::from_mode(0o755));

        PermissionBridge { base_dir }
    }

    #[test]
    fn hook_script_materialized() {
        let bridge = test_bridge();
        let path = bridge.base_dir.join(HOOK_FILENAME);
        assert!(path.exists());
        let meta = fs::metadata(&path).unwrap();
        assert!(meta.permissions().mode() & 0o111 != 0);
        bridge.cleanup();
    }

    #[test]
    fn create_session_writes_settings() {
        let bridge = test_bridge();
        let sid = SessionId(42);
        let dir = bridge.create_session(sid);
        assert!(dir.join(SETTINGS_FILENAME).exists());

        let content = fs::read_to_string(dir.join(SETTINGS_FILENAME)).unwrap();
        let v: serde_json::Value = serde_json::from_str(&content).unwrap();
        assert!(v["hooks"]["PreToolUse"].is_array());
        bridge.cleanup();
    }

    #[test]
    fn poll_returns_nothing_when_empty() {
        let bridge = test_bridge();
        let sid = SessionId(1);
        bridge.create_session(sid);
        assert!(bridge.poll(sid).is_empty());
        bridge.cleanup();
    }

    #[test]
    fn roundtrip_request_response() {
        let bridge = test_bridge();
        let sid = SessionId(1);
        let dir = bridge.create_session(sid);

        // Simulate hook writing a request
        let req_json = r#"{"tool_use_id":"t1","tool_name":"Bash","tool_input_preview":"ls -la","timestamp":1234}"#;
        fs::write(dir.join("request-t1.json"), req_json).unwrap();

        let requests = bridge.poll(sid);
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].tool_use_id, "t1");
        assert_eq!(requests[0].tool_name, "Bash");
        assert_eq!(requests[0].tool_input_preview, "ls -la");

        // Respond
        bridge.respond(&requests[0], "allow", "user approved");

        let resp_path = dir.join("response-t1.json");
        assert!(resp_path.exists());
        let resp: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(resp_path).unwrap()).unwrap();
        assert_eq!(resp["permissionDecision"], "allow");

        // After responding, poll should not return the same request
        assert!(bridge.poll(sid).is_empty());

        bridge.cleanup();
    }

    #[test]
    fn poll_skips_already_responded() {
        let bridge = test_bridge();
        let sid = SessionId(1);
        let dir = bridge.create_session(sid);

        fs::write(
            dir.join("request-t2.json"),
            r#"{"tool_use_id":"t2","tool_name":"Write"}"#,
        )
        .unwrap();
        fs::write(
            dir.join("response-t2.json"),
            r#"{"permissionDecision":"deny"}"#,
        )
        .unwrap();

        assert!(bridge.poll(sid).is_empty());
        bridge.cleanup();
    }

    #[test]
    fn cleanup_session_removes_dir() {
        let bridge = test_bridge();
        let sid = SessionId(5);
        let dir = bridge.create_session(sid);
        assert!(dir.exists());

        bridge.cleanup_session(sid);
        assert!(!dir.exists());
        bridge.cleanup();
    }

    #[test]
    fn cleanup_removes_everything() {
        let bridge = test_bridge();
        bridge.create_session(SessionId(1));
        bridge.create_session(SessionId(2));

        bridge.cleanup();
        assert!(!bridge.base_dir.exists());
    }

    #[test]
    fn respond_atomic_write() {
        let bridge = test_bridge();
        let sid = SessionId(1);
        bridge.create_session(sid);

        let req = PermissionRequest {
            tool_use_id: "atomic-test".to_string(),
            tool_name: "Bash".to_string(),
            tool_input_preview: "echo test".to_string(),
            session_id: sid,
        };
        bridge.respond(&req, "allow", "test");

        let dir = bridge.session_dir(sid);
        // .tmp file should not exist (renamed away)
        assert!(!dir.join("response-atomic-test.json.tmp").exists());
        assert!(dir.join("response-atomic-test.json").exists());
        bridge.cleanup();
    }

    #[test]
    fn respond_rejects_unsafe_id() {
        let bridge = test_bridge();
        let sid = SessionId(1);
        bridge.create_session(sid);

        let req = PermissionRequest {
            tool_use_id: "../escape".to_string(),
            tool_name: "Bash".to_string(),
            tool_input_preview: "".to_string(),
            session_id: sid,
        };
        bridge.respond(&req, "allow", "test");

        // Should not have created any response file
        let dir = bridge.session_dir(sid);
        let files: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .filter(|e| e.file_name().to_string_lossy().starts_with("response-"))
            .collect();
        assert!(files.is_empty());
        bridge.cleanup();
    }

    #[test]
    fn is_safe_id_validates_correctly() {
        assert!(is_safe_id("toolu_01abc"));
        assert!(is_safe_id("t1"));
        assert!(is_safe_id("a-b-c"));
        assert!(!is_safe_id(""));
        assert!(!is_safe_id("../etc"));
        assert!(!is_safe_id("foo/bar"));
        assert!(!is_safe_id("a b"));
        assert!(!is_safe_id(&"x".repeat(200)));
    }
}
