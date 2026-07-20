pub mod actor;
pub mod app_server;
pub mod bridge;
pub mod claude;
pub mod codex_session;
pub mod command_resolver;
pub mod config;
pub mod conversation_store;
pub mod coworker;
pub mod desktop_protocol;
pub mod desktop_router;
pub mod error;
pub mod ids;
pub mod lock;
pub mod logging;
pub mod mcp_sidecar;
pub mod runtime;

pub type JsonMap = serde_json::Map<String, serde_json::Value>;
