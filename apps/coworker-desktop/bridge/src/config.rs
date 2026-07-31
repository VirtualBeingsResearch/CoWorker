use std::{
    collections::HashMap,
    env, fs,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tracing::warn;

use crate::claude::ClaudeConfig;
use crate::error::{BridgeError, Result};

pub const DEFAULT_DESKTOP_CONFIG_PATH: &str = "coworker_desktop.json";
pub const DEFAULT_COWORKER_ID: &str = "cw_default";
pub const DEFAULT_LOGS_DIR: &str = "data/logs";
pub const DEFAULT_SERVICE_NAME: &str = "coworker_desktop";
pub const DEFAULT_STATE_PATH: &str = "data/coworker_desktop_state.json";
pub const DEFAULT_ATTACHMENT_STORE_DIR: &str = "data/coworker_desktop_attachments";
pub const DEFAULT_SESSION_OVERLAY_DIR: &str = "data/coworker_desktop_sessions";
pub const DEFAULT_DESKTOP_STORAGE_DIR: &str = "data/coworker_desktop";
pub const DEFAULT_INTERRUPTED_CONTINUE_MESSAGE: &str =
    "请从刚才中断的位置自动继续，完成原任务；如果已经完成，请简要说明当前结果。";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DefaultCodexNames {
    pub codex_id: String,
    pub display_name: String,
    pub used_user_name: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BridgeCoworker {
    pub coworker_id: String,
    pub display_name: String,
    pub base_url: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BridgeConfig {
    pub codex_id: String,
    pub display_name: String,
    pub coworkers: Vec<BridgeCoworker>,
    pub command: String,
    pub args: Vec<String>,
    pub snapshot_thread_limit: usize,
    pub snapshot_scan_thread_limit: usize,
    pub snapshot_interval_seconds: u64,
    pub reconnect_seconds: u64,
    pub state_path: Option<String>,
    pub codex_home_dir: String,
    pub session_overlay_dir: String,
    pub service_name: String,
    pub snapshot_source_kinds: Vec<String>,
    pub permissions_mode: String,
    pub approvals_reviewer: String,
    pub approval_timeout_seconds: u64,
    pub auto_continue_interrupted_turns: bool,
    pub auto_continue_interrupted_max_attempts: usize,
    pub auto_continue_interrupted_message: String,
    pub attachment_store_dir: String,
    pub attachment_max_bytes: u64,
    pub attachment_max_count: usize,
    pub logs_dir: String,
    pub log_level: String,
    pub file_log_level: String,
    pub chat_workspaces_dir: String,
}

impl BridgeConfig {
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let text = fs::read_to_string(path)?;
        let data: Value = serde_json::from_str(&text)?;
        let mut config = Self::from_value(data)?;
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            config.resolve_relative_paths(parent);
        }
        Ok(config)
    }

    pub fn from_value(data: Value) -> Result<Self> {
        let data = merge_codex_actor_config(&data)?;
        let obj = data
            .as_object()
            .ok_or_else(|| BridgeError::Config("bridge config must be a JSON object".into()))?;
        let codex_id = required_string(obj.get("codex_id"), "codex_id")?;
        let coworkers = parse_coworkers(obj)?;
        Ok(Self {
            display_name: string_or(obj.get("display_name"), &codex_id),
            codex_id,
            coworkers,
            command: string_or(obj.get("command"), "codex"),
            args: string_vec_or(obj.get("args"), &["app-server"]),
            snapshot_thread_limit: usize_or(obj.get("snapshot_thread_limit"), 20),
            snapshot_scan_thread_limit: usize_or(obj.get("snapshot_scan_thread_limit"), 200),
            snapshot_interval_seconds: u64_or(obj.get("snapshot_interval_seconds"), 300),
            reconnect_seconds: u64_or(obj.get("reconnect_seconds"), 5),
            state_path: optional_string_or(obj.get("state_path"), DEFAULT_STATE_PATH),
            codex_home_dir: optional_trimmed_string(obj.get("codex_home_dir"))
                .or_else(default_codex_home_dir)
                .unwrap_or_else(|| ".codex".to_owned()),
            session_overlay_dir: string_or(
                obj.get("session_overlay_dir"),
                DEFAULT_SESSION_OVERLAY_DIR,
            ),
            service_name: string_or(obj.get("service_name"), DEFAULT_SERVICE_NAME),
            snapshot_source_kinds: string_vec_or(
                obj.get("snapshot_source_kinds"),
                &["cli", "vscode", "appServer"],
            ),
            permissions_mode: normalize_permissions_mode(&string_or(
                obj.get("permissions_mode"),
                "read-only",
            )),
            approvals_reviewer: normalize_approvals_reviewer(&string_or(
                obj.get("approvals_reviewer"),
                "none",
            )),
            approval_timeout_seconds: u64_or(obj.get("approval_timeout_seconds"), 300),
            auto_continue_interrupted_turns: bool_or(
                obj.get("auto_continue_interrupted_turns"),
                true,
            ),
            auto_continue_interrupted_max_attempts: usize_or(
                obj.get("auto_continue_interrupted_max_attempts"),
                3,
            ),
            auto_continue_interrupted_message: string_or(
                obj.get("auto_continue_interrupted_message"),
                DEFAULT_INTERRUPTED_CONTINUE_MESSAGE,
            ),
            attachment_store_dir: string_or(
                obj.get("attachment_store_dir"),
                DEFAULT_ATTACHMENT_STORE_DIR,
            ),
            attachment_max_bytes: u64_or(obj.get("attachment_max_bytes"), 20 * 1024 * 1024),
            attachment_max_count: usize_or(obj.get("attachment_max_count"), 5),
            logs_dir: string_or(obj.get("logs_dir"), DEFAULT_LOGS_DIR),
            log_level: string_or(obj.get("log_level"), "INFO"),
            file_log_level: string_or(obj.get("file_log_level"), "INFO"),
            chat_workspaces_dir: optional_trimmed_string(obj.get("chat_workspaces_dir"))
                .or_else(default_chat_workspaces_dir)
                .unwrap_or_else(|| "data/codex_chats".to_owned()),
        })
    }

    fn resolve_relative_paths(&mut self, base_dir: &Path) {
        if let Some(state_path) = self.state_path.as_mut() {
            resolve_relative_path_string(state_path, base_dir);
        }
        resolve_relative_path_string(&mut self.codex_home_dir, base_dir);
        resolve_relative_path_string(&mut self.session_overlay_dir, base_dir);
        resolve_relative_path_string(&mut self.attachment_store_dir, base_dir);
        resolve_relative_path_string(&mut self.logs_dir, base_dir);
        resolve_relative_path_string(&mut self.chat_workspaces_dir, base_dir);
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopSecurityConfig {
    pub development_mode: bool,
    pub bearer_tokens: HashMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopConfig {
    pub schema_version: u32,
    pub desktop_id: String,
    pub display_name: String,
    pub storage_dir: PathBuf,
    pub local_enabled: bool,
    pub codex_enabled: bool,
    pub codex: BridgeConfig,
    pub claude: ClaudeConfig,
    pub security: DesktopSecurityConfig,
}

impl DesktopConfig {
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let data: Value = serde_json::from_str(&fs::read_to_string(path)?)?;
        let mut config = Self::from_value(data)?;
        if let Some(parent) = path.parent().filter(|value| !value.as_os_str().is_empty()) {
            config.codex.resolve_relative_paths(parent);
            if config.storage_dir.is_relative() {
                config.storage_dir = parent.join(&config.storage_dir);
            }
        }
        config.claude.storage_dir = config.storage_dir.clone();
        config.claude.desktop_config_path = Some(path.to_path_buf());
        Ok(config)
    }

    pub fn from_value(data: Value) -> Result<Self> {
        let obj = data
            .as_object()
            .ok_or_else(|| BridgeError::Config("desktop config must be a JSON object".into()))?;
        let schema_version = obj
            .get("schema_version")
            .and_then(Value::as_u64)
            .unwrap_or_default() as u32;
        if schema_version != 2 {
            return Err(BridgeError::Config(format!(
                "desktop config schema_version must be 2, got {schema_version}"
            )));
        }
        if !obj
            .get("coworkers")
            .and_then(Value::as_array)
            .is_some_and(|items| !items.is_empty())
        {
            return Err(BridgeError::Config(
                "coworkers must be a non-empty array".into(),
            ));
        }
        let codex = BridgeConfig::from_value(data.clone())?;
        let desktop_id = optional_trimmed_string(obj.get("desktop_id"))
            .unwrap_or_else(|| codex.codex_id.clone());
        let display_name = string_or(obj.get("display_name"), &desktop_id);
        let actors = obj.get("actors").and_then(Value::as_object);
        let local = actors
            .and_then(|value| value.get("local"))
            .and_then(Value::as_object);
        let codex_actor = actors
            .and_then(|value| value.get("codex"))
            .and_then(Value::as_object);
        let claude_actor = actors
            .and_then(|value| value.get("claude"))
            .and_then(Value::as_object);
        let coworkers = obj
            .get("coworkers")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let bearer_tokens = coworkers
            .iter()
            .filter_map(|item| {
                let mapping = item.as_object()?;
                let coworker_id = mapping.get("coworker_id")?.as_str()?.to_owned();
                let token = mapping.get("bearer_token")?.as_str()?.trim().to_owned();
                (!token.is_empty()).then_some((coworker_id, token))
            })
            .collect();
        let security = obj.get("security").and_then(Value::as_object);
        let development_mode = security
            .and_then(|value| value.get("development_mode"))
            .or_else(|| obj.get("development_mode"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let claude_home = claude_actor
            .and_then(|value| value.get("home_dir"))
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| ClaudeConfig::default().home_dir);
        let config = Self {
            schema_version,
            desktop_id,
            display_name,
            storage_dir: PathBuf::from(string_or(
                obj.get("storage_dir"),
                DEFAULT_DESKTOP_STORAGE_DIR,
            )),
            local_enabled: local
                .and_then(|value| value.get("enabled"))
                .and_then(Value::as_bool)
                .unwrap_or(true),
            codex_enabled: codex_actor
                .and_then(|value| value.get("enabled"))
                .and_then(Value::as_bool)
                .unwrap_or(true),
            claude: ClaudeConfig {
                enabled: claude_actor
                    .and_then(|value| value.get("enabled"))
                    .and_then(Value::as_bool)
                    .unwrap_or(true),
                command: claude_actor
                    .and_then(|value| value.get("command"))
                    .and_then(Value::as_str)
                    .unwrap_or("claude")
                    .to_owned(),
                args: claude_actor
                    .and_then(|value| value.get("args"))
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_owned)
                            .collect()
                    })
                    .unwrap_or_default(),
                permissions_mode: codex.permissions_mode.clone(),
                home_dir: claude_home,
                storage_dir: PathBuf::from(string_or(
                    obj.get("storage_dir"),
                    DEFAULT_DESKTOP_STORAGE_DIR,
                )),
                desktop_config_path: None,
            },
            codex,
            security: DesktopSecurityConfig {
                development_mode,
                bearer_tokens,
            },
        };
        if !config.security.development_mode {
            for coworker in &config.codex.coworkers {
                if !coworker.base_url.starts_with("https://")
                    && crate::relay_transport::relay_endpoint(&coworker.base_url).is_none()
                {
                    return Err(BridgeError::Config(format!(
                        "production Coworker {} must use an https:// URL",
                        coworker.coworker_id
                    )));
                }
                if !config
                    .security
                    .bearer_tokens
                    .contains_key(&coworker.coworker_id)
                {
                    return Err(BridgeError::Config(format!(
                        "production Coworker {} requires bearer_token",
                        coworker.coworker_id
                    )));
                }
            }
        }
        Ok(config)
    }
}

fn merge_codex_actor_config(data: &Value) -> Result<Value> {
    let Some(root) = data.as_object() else {
        return Ok(data.clone());
    };
    let Some(codex) = root
        .get("actors")
        .and_then(Value::as_object)
        .and_then(|actors| actors.get("codex"))
        .and_then(Value::as_object)
    else {
        return Ok(data.clone());
    };
    let mut merged = root.clone();
    for (key, value) in codex {
        if key != "enabled" {
            merged.insert(key.clone(), value.clone());
        }
    }
    if !merged.contains_key("codex_id")
        && let Some(desktop_id) = root.get("desktop_id")
    {
        merged.insert("codex_id".to_owned(), desktop_id.clone());
    }
    Ok(Value::Object(merged))
}

fn resolve_relative_path_string(value: &mut String, base_dir: &Path) {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return;
    }
    let path = Path::new(trimmed);
    if path.is_absolute() {
        if trimmed != value {
            *value = trimmed.to_owned();
        }
        return;
    }
    *value = base_dir.join(path).to_string_lossy().into_owned();
}

pub fn read_config_value(path: impl AsRef<Path>) -> Result<Value> {
    let text = fs::read_to_string(path)?;
    Ok(serde_json::from_str(&text)?)
}

pub fn write_config_value(path: impl AsRef<Path>, data: &Value) -> Result<BridgeConfig> {
    DesktopConfig::from_value(data.clone())?;
    let config = BridgeConfig::from_value(data.clone())?;
    if let Some(parent) = path.as_ref().parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, serde_json::to_string_pretty(data)?)?;
    Ok(config)
}

pub fn default_config_value(codex_id: &str, base_url: &str) -> Value {
    let display_name = default_display_name_for_codex_id(codex_id);
    default_config_value_with_display_name(codex_id, &display_name, base_url)
}

pub fn default_config_value_with_display_name(
    codex_id: &str,
    display_name: &str,
    base_url: &str,
) -> Value {
    json!({
        "schema_version": 2,
        "desktop_id": codex_id,
        "codex_id": codex_id,
        "display_name": display_name,
        "storage_dir": DEFAULT_DESKTOP_STORAGE_DIR,
        // Production checks are the default.  Local HTTP requires an explicit
        // opt-in in the generated config.
        "security": {"development_mode": false},
        "actors": {
            "local": {"enabled": true},
            "codex": {"enabled": true, "command": "codex", "args": ["app-server"]},
            "claude": {"enabled": true, "command": "claude", "args": []}
        },
        "state_path": DEFAULT_STATE_PATH,
        "codex_home_dir": default_codex_home_dir().unwrap_or_else(|| ".codex".to_owned()),
        "session_overlay_dir": DEFAULT_SESSION_OVERLAY_DIR,
        "logs_dir": DEFAULT_LOGS_DIR,
        "log_level": "INFO",
        "file_log_level": "INFO",
        "desktop_update_url": "",
        "chat_workspaces_dir": default_chat_workspaces_dir().unwrap_or_else(|| "data/codex_chats".to_owned()),
        "snapshot_thread_limit": 20,
        "snapshot_scan_thread_limit": 200,
        "snapshot_interval_seconds": 30,
        "reconnect_seconds": 3,
        "permissions_mode": "read-only",
        "approvals_reviewer": "none",
        "approval_timeout_seconds": 300,
        "auto_continue_interrupted_turns": true,
        "auto_continue_interrupted_max_attempts": 3,
        "auto_continue_interrupted_message": DEFAULT_INTERRUPTED_CONTINUE_MESSAGE,
        "attachment_store_dir": DEFAULT_ATTACHMENT_STORE_DIR,
        "attachment_max_bytes": 20 * 1024 * 1024,
        "attachment_max_count": 5,
        "coworkers": [{
            "coworker_id": DEFAULT_COWORKER_ID,
            "display_name": "搭档",
            "base_url": base_url.trim_end_matches('/'),
            "enabled": true,
        }],
    })
}

pub fn default_codex_names() -> DefaultCodexNames {
    default_codex_names_from_user_name(current_user_name().as_deref())
}

pub fn codex_names_for_user_name(user_name: &str) -> DefaultCodexNames {
    meaningful_codex_names_for_user_name(user_name).unwrap_or_else(generic_codex_names)
}

fn default_codex_names_from_user_name(user_name: Option<&str>) -> DefaultCodexNames {
    user_name
        .and_then(meaningful_codex_names_for_user_name)
        .unwrap_or_else(generic_codex_names)
}

fn generic_codex_names() -> DefaultCodexNames {
    DefaultCodexNames {
        codex_id: "codex-local".to_owned(),
        display_name: "Local Codex".to_owned(),
        used_user_name: false,
    }
}

fn meaningful_codex_names_for_user_name(user_name: &str) -> Option<DefaultCodexNames> {
    let user_name = user_name.trim();
    if user_name.is_empty() || !user_name_is_meaningful(user_name) {
        return None;
    }

    let user_slug = slugify_codex_id_component(user_name);
    Some(DefaultCodexNames {
        codex_id: if user_slug.is_empty() {
            "codex-local".to_owned()
        } else {
            format!("{user_slug}-codex-local")
        },
        display_name: format!("{user_name} Local Codex"),
        used_user_name: true,
    })
}

fn user_name_is_meaningful(user_name: &str) -> bool {
    let slug = slugify_codex_id_component(user_name);
    if slug.is_empty() {
        return true;
    }
    if slug.chars().all(|ch| ch.is_ascii_digit()) {
        return false;
    }

    let generic_names = [
        "admin",
        "administrator",
        "default",
        "default-user",
        "defaultuser0",
        "guest",
        "local",
        "owner",
        "public",
        "root",
        "test",
        "user",
        "username",
        "wdagutilityaccount",
    ];
    if generic_names.contains(&slug.as_str()) {
        return false;
    }

    let machine_prefixes = ["desktop-", "laptop-", "pc-", "win-", "windows-"];
    !machine_prefixes.iter().any(|prefix| {
        slug.strip_prefix(prefix)
            .is_some_and(|rest| rest.len() >= 5 && rest.chars().any(|ch| ch.is_ascii_digit()))
    })
}

fn default_display_name_for_codex_id(codex_id: &str) -> String {
    let default_names = default_codex_names();
    if codex_id == default_names.codex_id {
        default_names.display_name
    } else {
        "Local Codex".to_owned()
    }
}

fn current_user_name() -> Option<String> {
    ["USERNAME", "USER", "LOGNAME"]
        .into_iter()
        .filter_map(|key| env::var(key).ok())
        .map(|value| value.trim().to_owned())
        .find(|value| !value.is_empty())
        .or_else(current_user_name_from_home)
}

fn current_user_name_from_home() -> Option<String> {
    env::var("USERPROFILE")
        .ok()
        .or_else(|| env::var("HOME").ok())
        .and_then(|home| {
            Path::new(home.trim())
                .file_name()
                .map(|value| value.to_string_lossy().trim().to_owned())
        })
        .filter(|value| !value.is_empty())
}

fn slugify_codex_id_component(value: &str) -> String {
    let mut slug = String::new();
    let mut last_was_separator = false;
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch.to_ascii_lowercase());
            last_was_separator = false;
        } else if !last_was_separator && !slug.is_empty() {
            slug.push('-');
            last_was_separator = true;
        }
    }
    slug.trim_matches('-').to_owned()
}

fn parse_coworkers(obj: &serde_json::Map<String, Value>) -> Result<Vec<BridgeCoworker>> {
    let default_items = vec![json!({
        "coworker_id": DEFAULT_COWORKER_ID,
        "display_name": "搭档",
        "base_url": "http://localhost:8000",
    })];
    let items = obj
        .get("coworkers")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .unwrap_or(&default_items);
    let mut coworkers = Vec::with_capacity(items.len());
    let mut ids = std::collections::HashSet::new();
    for item in items {
        let item = item
            .as_object()
            .ok_or_else(|| BridgeError::Config("coworkers entries must be JSON objects".into()))?;
        let coworker_id = required_string(item.get("coworker_id"), "coworker_id")?;
        if !ids.insert(coworker_id.clone()) {
            return Err(BridgeError::Config(
                "coworker_id values must be unique".into(),
            ));
        }
        if item.get("enabled").and_then(Value::as_bool) == Some(false) {
            continue;
        }
        coworkers.push(BridgeCoworker {
            display_name: string_or(item.get("display_name"), &coworker_id),
            base_url: trim_base_url(&string_or(item.get("base_url"), "http://localhost:8000")),
            coworker_id,
        });
    }

    if coworkers.is_empty() {
        return Err(BridgeError::Config(
            "at least one coworker must be enabled".into(),
        ));
    }
    Ok(coworkers)
}

fn required_string(value: Option<&Value>, name: &str) -> Result<String> {
    match value.and_then(Value::as_str).filter(|s| !s.is_empty()) {
        Some(value) => Ok(value.to_owned()),
        None => Err(BridgeError::Config(format!("{name} is required"))),
    }
}

fn string_or(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(Value::String(value)) if !value.is_empty() => value.clone(),
        Some(Value::Number(value)) if value.as_f64().is_some_and(|v| v != 0.0) => value.to_string(),
        Some(Value::Bool(true)) => "True".to_owned(),
        _ => default.to_owned(),
    }
}

fn optional_string_or(value: Option<&Value>, default: &str) -> Option<String> {
    match value {
        Some(Value::Null) => None,
        Some(Value::String(value)) => Some(value.to_owned()),
        Some(Value::Number(value)) => Some(value.to_string()),
        Some(Value::Bool(value)) => Some(if *value { "True" } else { "False" }.to_owned()),
        Some(_) | None => Some(default.to_owned()),
    }
}

fn optional_trimmed_string(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(value)) => {
            let trimmed = value.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_owned())
        }
        Some(Value::Number(value)) => Some(value.to_string()),
        Some(Value::Bool(value)) => Some(if *value { "True" } else { "False" }.to_owned()),
        Some(Value::Null) | Some(_) | None => None,
    }
}

fn default_chat_workspaces_dir() -> Option<String> {
    env::var("USERPROFILE")
        .ok()
        .or_else(|| env::var("HOME").ok())
        .map(|home| home.trim().to_owned())
        .filter(|home| !home.is_empty())
        .map(|home| Path::new(&home).join("Documents").join("Codex"))
        .map(|path| path.to_string_lossy().into_owned())
        .filter(|value| !value.is_empty())
}

fn default_codex_home_dir() -> Option<String> {
    env::var("CODEX_HOME")
        .ok()
        .map(|home| home.trim().to_owned())
        .filter(|home| !home.is_empty())
        .or_else(|| {
            env::var("USERPROFILE")
                .ok()
                .or_else(|| env::var("HOME").ok())
                .map(|home| home.trim().to_owned())
                .filter(|home| !home.is_empty())
                .map(|home| {
                    Path::new(&home)
                        .join(".codex")
                        .to_string_lossy()
                        .into_owned()
                })
        })
}

fn string_vec_or(value: Option<&Value>, default: &[&str]) -> Vec<String> {
    match value.and_then(Value::as_array) {
        Some(items) => items.iter().map(config_value_to_string).collect(),
        None => default.iter().map(|s| (*s).to_owned()).collect(),
    }
}

fn usize_or(value: Option<&Value>, default: usize) -> usize {
    u64_config_or(value, default as u64)
        .try_into()
        .unwrap_or(default)
}

fn u64_or(value: Option<&Value>, default: u64) -> u64 {
    u64_config_or(value, default)
}

fn bool_or(value: Option<&Value>, default: bool) -> bool {
    match value {
        None | Some(Value::Null) => default,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64().is_some_and(|value| value != 0.0),
        Some(Value::String(value)) => match value.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => !value.is_empty(),
        },
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

fn trim_base_url(value: &str) -> String {
    value.trim_end_matches('/').to_owned()
}

fn u64_config_or(value: Option<&Value>, default: u64) -> u64 {
    match value {
        Some(Value::Number(value)) => value
            .as_u64()
            .or_else(|| value.as_i64().and_then(|value| u64::try_from(value).ok()))
            .or_else(|| {
                value.as_f64().and_then(|value| {
                    value
                        .is_finite()
                        .then_some(value)
                        .filter(|value| *value >= 0.0)
                        .map(|value| value as u64)
                })
            })
            .unwrap_or(default),
        Some(Value::String(value)) => value.trim().parse().unwrap_or(default),
        Some(Value::Bool(true)) => 1,
        Some(Value::Bool(false)) | Some(Value::Null) | Some(_) | None => default,
    }
}

fn config_value_to_string(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Null => "None".to_owned(),
        value => value.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn internal_codex_config_uses_default_coworker() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
        }))
        .expect("config parses");

        assert_eq!(cfg.coworkers.len(), 1);
        assert_eq!(cfg.coworkers[0].coworker_id, "cw_default");
        assert_eq!(cfg.coworkers[0].display_name, "搭档");
        assert_eq!(cfg.coworkers[0].base_url, "http://localhost:8000");
    }

    #[test]
    fn multi_coworker_config_parses_array() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "coworkers": [
                {
                    "coworker_id": "cw_01",
                    "display_name": "搭档A",
                    "base_url": "http://a/"
                },
                {
                    "coworker_id": "cw_02",
                    "display_name": "搭档B",
                    "base_url": "http://b"
                }
            ]
        }))
        .expect("config parses");

        assert_eq!(cfg.coworkers[0].coworker_id, "cw_01");
        assert_eq!(cfg.coworkers[0].base_url, "http://a");
        assert_eq!(cfg.coworkers[1].coworker_id, "cw_02");
    }

    #[test]
    fn coworker_enabled_defaults_true_and_disabled_profiles_stay_out_of_runtime() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "coworkers": [
                {"coworker_id": "enabled", "base_url": "http://enabled"},
                {"coworker_id": "disabled", "base_url": "http://disabled", "enabled": false}
            ]
        }))
        .expect("config parses");

        assert_eq!(cfg.coworkers.len(), 1);
        assert_eq!(cfg.coworkers[0].coworker_id, "enabled");
    }

    #[test]
    fn config_requires_one_enabled_coworker() {
        let error = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "coworkers": [
                {"coworker_id": "disabled", "base_url": "http://disabled", "enabled": false}
            ]
        }))
        .expect_err("all-disabled config must fail");

        assert!(
            error
                .to_string()
                .contains("at least one coworker must be enabled")
        );
    }

    #[test]
    fn config_parses_auto_continue_options() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "auto_continue_interrupted_turns": "false",
            "auto_continue_interrupted_max_attempts": 9,
            "auto_continue_interrupted_message": "继续",
        }))
        .expect("config parses");

        assert!(!cfg.auto_continue_interrupted_turns);
        assert_eq!(cfg.auto_continue_interrupted_max_attempts, 9);
        assert_eq!(cfg.auto_continue_interrupted_message, "继续");
    }

    #[test]
    fn config_accepts_python_style_coercible_values() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "display_name": 123,
            "args": ["app-server", 42],
            "snapshot_thread_limit": "7",
            "snapshot_scan_thread_limit": "77",
            "snapshot_interval_seconds": "30",
            "reconnect_seconds": "2",
            "state_path": "",
            "service_name": 456,
            "snapshot_source_kinds": ["cli", 9, true],
            "auto_continue_interrupted_turns": 0,
            "auto_continue_interrupted_max_attempts": "4",
        }))
        .expect("config parses");

        assert_eq!(cfg.display_name, "123");
        assert_eq!(cfg.args, vec!["app-server", "42"]);
        assert_eq!(cfg.snapshot_thread_limit, 7);
        assert_eq!(cfg.snapshot_scan_thread_limit, 77);
        assert_eq!(cfg.snapshot_interval_seconds, 30);
        assert_eq!(cfg.reconnect_seconds, 2);
        assert_eq!(cfg.state_path.as_deref(), Some(""));
        assert_eq!(cfg.service_name, "456");
        assert_eq!(cfg.snapshot_source_kinds, vec!["cli", "9", "True"]);
        assert!(!cfg.auto_continue_interrupted_turns);
        assert_eq!(cfg.auto_continue_interrupted_max_attempts, 4);
    }

    #[test]
    fn config_parses_approval_options() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "permissions_mode": "workspace-write",
            "approvals_reviewer": "coworker",
            "approval_timeout_seconds": 12,
        }))
        .expect("config parses");

        assert_eq!(cfg.permissions_mode, "workspace-write");
        assert_eq!(cfg.approvals_reviewer, "coworker");
        assert_eq!(cfg.approval_timeout_seconds, 12);
    }

    #[test]
    fn config_parses_chat_workspaces_dir() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "chat_workspaces_dir": " D:\\Codex\\Chats ",
        }))
        .expect("config parses");

        assert_eq!(cfg.chat_workspaces_dir, "D:\\Codex\\Chats");
    }

    #[test]
    fn config_file_resolves_relative_data_paths_from_config_dir() {
        let base = std::env::temp_dir().join(format!(
            "coworker-desktop-config-test-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&base).expect("temp config dir");
        let path = base.join("coworker_desktop.json");
        std::fs::write(
            &path,
            serde_json::to_string(&json!({
                "codex_id": "codex-local",
                "state_path": "data/state.json",
                "codex_home_dir": "codex-home",
                "session_overlay_dir": "data/sessions",
                "logs_dir": "data/logs",
                "attachment_store_dir": "data/attachments",
                "chat_workspaces_dir": "data/chats",
            }))
            .expect("config json"),
        )
        .expect("write config");

        let cfg = BridgeConfig::from_file(&path).expect("config parses");

        assert_eq!(
            cfg.state_path.as_deref(),
            Some(base.join("data/state.json").to_string_lossy().as_ref())
        );
        assert_eq!(cfg.logs_dir, base.join("data/logs").to_string_lossy());
        assert_eq!(
            cfg.codex_home_dir,
            base.join("codex-home").to_string_lossy()
        );
        assert_eq!(
            cfg.session_overlay_dir,
            base.join("data/sessions").to_string_lossy()
        );
        assert_eq!(
            cfg.attachment_store_dir,
            base.join("data/attachments").to_string_lossy()
        );
        assert_eq!(
            cfg.chat_workspaces_dir,
            base.join("data/chats").to_string_lossy()
        );
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn desktop_config_file_resolves_nested_codex_paths_from_config_dir() {
        let directory = tempfile::tempdir().expect("temp config dir");
        let path = directory.path().join("coworker_desktop.json");
        std::fs::write(
            &path,
            serde_json::to_string(&json!({
                "schema_version": 2,
                "desktop_id": "desktop-local",
                "codex_id": "codex-local",
                "coworkers": [{"coworker_id":"cw-1","base_url":"http://localhost:8000"}],
                "storage_dir": "data/coworker_desktop",
                "logs_dir": "data/logs",
                "state_path": "data/state.json",
                "permissions_mode": "workspace-write",
                "security": {"development_mode": true},
                "actors": {"codex": {"enabled": true}},
            }))
            .expect("config json"),
        )
        .expect("write config");

        let config = DesktopConfig::from_file(&path).expect("desktop config parses");

        assert_eq!(
            config.codex.logs_dir,
            directory.path().join("data/logs").to_string_lossy()
        );
        assert_eq!(
            config.codex.state_path.as_deref(),
            Some(
                directory
                    .path()
                    .join("data/state.json")
                    .to_string_lossy()
                    .as_ref()
            )
        );
        assert_eq!(config.claude.permissions_mode, "workspace-write");
    }

    #[test]
    fn default_config_value_is_valid() {
        let cfg = BridgeConfig::from_value(default_config_value(
            "codex-local",
            "http://localhost:8000/",
        ))
        .expect("default config parses");

        assert_eq!(cfg.codex_id, "codex-local");
        assert_eq!(cfg.coworkers[0].base_url, "http://localhost:8000");
        assert_eq!(cfg.snapshot_thread_limit, 20);
        assert_eq!(cfg.snapshot_scan_thread_limit, 200);
        assert!(!cfg.chat_workspaces_dir.is_empty());
        assert!(!cfg.codex_home_dir.is_empty());
        assert_eq!(cfg.session_overlay_dir, DEFAULT_SESSION_OVERLAY_DIR);
        assert_eq!(cfg.log_level, "INFO");
        assert_eq!(cfg.file_log_level, "INFO");
    }

    #[test]
    fn generated_desktop_config_defaults_to_production_security() {
        let value = default_config_value("codex-local", "https://coworker.example.test");

        assert_eq!(
            value["security"]["development_mode"],
            serde_json::Value::Bool(false)
        );
    }

    #[test]
    fn explicit_log_levels_override_info_defaults() {
        let cfg = BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "log_level": "DEBUG",
            "file_log_level": "TRACE",
        }))
        .expect("config parses");

        assert_eq!(cfg.log_level, "DEBUG");
        assert_eq!(cfg.file_log_level, "TRACE");
    }

    #[test]
    fn default_codex_names_use_current_user_name_when_available() {
        let names = default_codex_names_from_user_name(Some("ExampleUser"));

        assert_eq!(names.codex_id, "exampleuser-codex-local");
        assert_eq!(names.display_name, "ExampleUser Local Codex");
        assert!(names.used_user_name);
    }

    #[test]
    fn default_codex_names_slugify_user_name_for_id() {
        let names = default_codex_names_from_user_name(Some("  Alice.Work  "));

        assert_eq!(names.codex_id, "alice-work-codex-local");
        assert_eq!(names.display_name, "Alice.Work Local Codex");
        assert!(names.used_user_name);
    }

    #[test]
    fn default_codex_names_keep_non_ascii_name_for_display_only() {
        let names = default_codex_names_from_user_name(Some("小明"));

        assert_eq!(names.codex_id, "codex-local");
        assert_eq!(names.display_name, "小明 Local Codex");
        assert!(names.used_user_name);
    }

    #[test]
    fn default_codex_names_ignore_generic_user_names() {
        for user_name in ["user", "Admin", "12345", "defaultuser0"] {
            let names = default_codex_names_from_user_name(Some(user_name));

            assert_eq!(names.codex_id, "codex-local");
            assert_eq!(names.display_name, "Local Codex");
            assert!(!names.used_user_name);
        }
    }

    #[test]
    fn default_codex_names_ignore_machine_like_user_names() {
        let names = default_codex_names_from_user_name(Some("DESKTOP-A1B2C3D"));

        assert_eq!(names.codex_id, "codex-local");
        assert_eq!(names.display_name, "Local Codex");
        assert!(!names.used_user_name);
    }

    #[test]
    fn production_desktop_config_requires_https_and_bearer() {
        let insecure = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desk-1",
            "coworkers": [{"coworker_id":"cw-1","base_url":"http://localhost:8000"}],
            "security": {"development_mode": false},
            "actors": {"codex": {"enabled": false}}
        }));
        assert!(insecure.is_err());

        let secure = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desk-1",
            "coworkers": [{
                "coworker_id":"cw-1",
                "base_url":"https://coworker.example",
                "bearer_token":"secret"
            }],
            "security": {"development_mode": false},
            "actors": {"codex": {"enabled": false}}
        }))
        .expect("secure production config");
        assert!(!secure.security.development_mode);
    }

    #[test]
    fn production_security_ignores_disabled_coworkers() {
        let config = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desk-1",
            "coworkers": [
                {
                    "coworker_id": "enabled",
                    "base_url": "https://coworker.example",
                    "bearer_token": "secret"
                },
                {
                    "coworker_id": "disabled",
                    "base_url": "http://localhost:8000",
                    "enabled": false
                }
            ],
            "security": {"development_mode": false},
            "actors": {"codex": {"enabled": false}}
        }))
        .expect("disabled coworker does not participate in transport validation");

        assert_eq!(config.codex.coworkers.len(), 1);
        assert_eq!(config.codex.coworkers[0].coworker_id, "enabled");
    }

    #[test]
    fn desktop_config_rejects_schema_v1() {
        let error = DesktopConfig::from_value(json!({
            "codex_id":"old-codex",
            "coworkers":[{"coworker_id":"cw-1","base_url":"http://localhost:8000"}]
        }))
        .expect_err("schema v1 must be rejected");
        assert!(error.to_string().contains("schema_version must be 2"));
    }
}

fn normalize_permissions_mode(value: &str) -> String {
    match value.trim().to_ascii_lowercase().as_str() {
        "read-only" | "readonly" | "read_only" => "read-only".to_owned(),
        "workspace-write" | "workspace_write" => "workspace-write".to_owned(),
        "danger-full-access" | "danger_full_access" => "danger-full-access".to_owned(),
        other => {
            warn!(
                permissions_mode = other,
                "unknown permissions_mode; falling back to read-only"
            );
            "read-only".to_owned()
        }
    }
}

fn normalize_approvals_reviewer(value: &str) -> String {
    match value.trim().to_ascii_lowercase().as_str() {
        "none" => "none".to_owned(),
        "coworker" => "coworker".to_owned(),
        other => {
            warn!(
                approvals_reviewer = other,
                "unknown approvals_reviewer; falling back to none"
            );
            "none".to_owned()
        }
    }
}
