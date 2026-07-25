use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum BridgeError {
    #[error("{0}")]
    Message(String),
    #[error("config error: {0}")]
    Config(String),
    #[error("startup error: {0}")]
    Startup(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("http error: HTTP status {status} {reason}: {detail}")]
    HttpStatus {
        status: u16,
        reason: String,
        detail: String,
    },
    #[error("app-server request failed: {0}")]
    AppServer(String),
    #[error("duplicate Coworker SSE participant")]
    DuplicateSseParticipant,
    #[error("missing bridge config: {0}")]
    MissingConfig(PathBuf),
}

impl BridgeError {
    pub fn message(message: impl Into<String>) -> Self {
        Self::Message(message.into())
    }

    pub fn startup(message: impl Into<String>) -> Self {
        Self::Startup(message.into())
    }

    pub fn http_status(status: reqwest::StatusCode, detail: impl Into<String>) -> Self {
        Self::HttpStatus {
            status: status.as_u16(),
            reason: status.canonical_reason().unwrap_or("Unknown").to_owned(),
            detail: detail.into(),
        }
    }

    pub fn http_status_code(&self) -> Option<u16> {
        match self {
            Self::Http(error) => error.status().map(|status| status.as_u16()),
            Self::HttpStatus { status, .. } => Some(*status),
            _ => None,
        }
    }

    pub fn http_detail(&self) -> Option<&str> {
        match self {
            Self::HttpStatus { detail, .. } => Some(detail),
            _ => None,
        }
    }
}

pub type Result<T> = std::result::Result<T, BridgeError>;
