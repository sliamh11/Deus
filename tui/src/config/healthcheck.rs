use serde::Deserialize;
use std::fs;
use std::process::Command;
use std::time::SystemTime;
use crate::platform;

#[derive(Clone)]
pub struct ServiceEntry {
    pub description: String,
    pub status: String,
}

#[derive(Deserialize)]
struct Job {
    label: String,
    description: String,
    check: String,
    heartbeat_path: Option<String>,
    max_staleness_sec: Option<u64>,
}

#[derive(Deserialize)]
struct HealthcheckFile {
    jobs: Vec<Job>,
}

fn check_launchctl(label: &str) -> &'static str {
    if !platform::is_macos() {
        return "unsupported";
    }
    let uid = unsafe { libc::getuid() };
    let target = format!("gui/{}/{}", uid, label);
    let output = Command::new("launchctl")
        .args(["print", &target])
        .output();
    match output {
        Ok(o) if o.status.success() => {
            let stdout = String::from_utf8_lossy(&o.stdout);
            if stdout.contains("state = running") { "running" } else { "stopped" }
        }
        _ => "stopped",
    }
}

fn check_heartbeat(path: &str, max_staleness: u64) -> &'static str {
    let expanded = platform::expand_tilde(path);

    let meta = match fs::metadata(&expanded) {
        Ok(m) => m,
        Err(_) => return "stopped",
    };

    let modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
    let age = SystemTime::now()
        .duration_since(modified)
        .map(|d| d.as_secs())
        .unwrap_or(u64::MAX);

    if age <= max_staleness { "running" } else { "stale" }
}

pub fn load() -> Vec<ServiceEntry> {
    let config_dir = platform::config_dir().join("healthcheck.json");

    let content = match fs::read_to_string(&config_dir) {
        Ok(c) => c,
        Err(_) => return vec![ServiceEntry {
            description: "healthcheck.json not found".to_string(),
            status: "unknown".to_string(),
        }],
    };

    let file: HealthcheckFile = match serde_json::from_str(&content) {
        Ok(f) => f,
        Err(_) => return vec![ServiceEntry {
            description: "invalid healthcheck.json".to_string(),
            status: "unknown".to_string(),
        }],
    };

    file.jobs
        .iter()
        .map(|job| {
            let status = match job.check.as_str() {
                "loaded_and_running" => check_launchctl(&job.label).to_string(),
                "heartbeat" => {
                    let path = job.heartbeat_path.as_deref().unwrap_or("");
                    check_heartbeat(path, job.max_staleness_sec.unwrap_or(300)).to_string()
                }
                "heartbeat_glob" => {
                    let path = job.heartbeat_path.as_deref().unwrap_or("");
                    check_heartbeat(path, job.max_staleness_sec.unwrap_or(300)).to_string()
                }
                _ => "unknown".to_string(),
            };
            ServiceEntry { description: job.description.clone(), status }
        })
        .collect()
}
