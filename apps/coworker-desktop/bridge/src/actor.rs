use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    bridge::CodexBridge,
    desktop_protocol::{ActorId, DesktopEventType},
    error::{BridgeError, Result},
};
use std::sync::{Arc, OnceLock};
use tokio::sync::{broadcast, oneshot};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActorHealth {
    pub actor_id: ActorId,
    pub available: bool,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActorConversation {
    pub actor_id: ActorId,
    pub conversation_id: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_name: Option<String>,
    pub project_path: Option<String>,
    pub writable: bool,
    pub updated_at: Option<String>,
    pub mode: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActorConversationPage {
    pub conversations: Vec<ActorConversation>,
    pub complete: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActorMessage {
    pub id: String,
    pub actor_id: ActorId,
    pub conversation_id: String,
    pub author_kind: String,
    pub content: String,
    pub created_at: String,
    pub metadata: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActorMessagePage {
    pub messages: Vec<ActorMessage>,
    pub next_before_cursor: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub struct ActorMessageInput<'a> {
    pub message_id: Option<&'a str>,
    pub author_kind: &'a str,
    pub author_id: Option<&'a str>,
    pub author_label: Option<&'a str>,
    pub coworker_id: Option<&'a str>,
    pub content: &'a str,
    pub attachment_paths: &'a [String],
    pub project_path: Option<&'a str>,
    pub mode: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActorStreamEvent {
    pub actor_id: ActorId,
    pub conversation_id: String,
    pub message_id: Option<String>,
    pub event: Value,
}

#[derive(Debug)]
pub struct ActorOutboundRequest {
    pub actor_id: ActorId,
    pub coworker_id: String,
    pub conversation_id: Option<String>,
    pub event_type: DesktopEventType,
    pub payload: Value,
    pub response: oneshot::Sender<Result<()>>,
}

static ACTOR_STREAM_EVENTS: OnceLock<broadcast::Sender<ActorStreamEvent>> = OnceLock::new();

pub fn subscribe_actor_stream_events() -> broadcast::Receiver<ActorStreamEvent> {
    actor_stream_event_sender().subscribe()
}

pub fn publish_actor_stream_event(event: ActorStreamEvent) {
    let _ = actor_stream_event_sender().send(event);
}

fn actor_stream_event_sender() -> &'static broadcast::Sender<ActorStreamEvent> {
    ACTOR_STREAM_EVENTS.get_or_init(|| {
        let (tx, _) = broadcast::channel(1024);
        tx
    })
}

#[async_trait::async_trait]
pub trait ActorAdapter: Send + Sync {
    fn actor_id(&self) -> ActorId;
    async fn health(&self) -> ActorHealth;
    async fn list_conversations(&self, limit: usize) -> Result<Vec<ActorConversation>>;
    async fn conversation_snapshot(&self, limit: usize) -> Result<ActorConversationPage> {
        let conversations = self.list_conversations(limit).await?;
        let complete = conversations.len() < limit;
        Ok(ActorConversationPage {
            conversations,
            complete,
        })
    }
    async fn load_messages(
        &self,
        conversation_id: &str,
        before_cursor: Option<&str>,
        page_size: usize,
    ) -> Result<ActorMessagePage>;
    async fn send_message(
        &self,
        conversation_id: Option<&str>,
        input: ActorMessageInput<'_>,
    ) -> Result<Value>;
    async fn interrupt(&self, conversation_id: &str) -> Result<()>;
    async fn set_mode(&self, _conversation_id: &str, _mode: &str) -> Result<Value> {
        Err(BridgeError::message(
            "actor does not support conversation modes",
        ))
    }
    async fn rename_conversation(&self, _conversation_id: &str, _title: &str) -> Result<Value> {
        Err(BridgeError::message(
            "actor does not support native conversation rename",
        ))
    }
    async fn record_external_message(
        &self,
        _conversation_id: &str,
        _input: ActorMessageInput<'_>,
    ) -> Result<()> {
        Ok(())
    }
    async fn resolve_request(
        &self,
        _coworker_id: &str,
        _conversation_id: &str,
        _request_id: &str,
        _response: Value,
    ) -> Result<Value> {
        Err(BridgeError::message(
            "actor does not support request resolution",
        ))
    }
}

pub struct CodexActorAdapter {
    bridge: Arc<CodexBridge>,
}

impl CodexActorAdapter {
    pub fn new(bridge: Arc<CodexBridge>) -> Self {
        Self { bridge }
    }
}

#[async_trait::async_trait]
impl ActorAdapter for CodexActorAdapter {
    fn actor_id(&self) -> ActorId {
        ActorId::Codex
    }

    async fn health(&self) -> ActorHealth {
        ActorHealth {
            actor_id: ActorId::Codex,
            available: true,
            message: "Codex app-server is available".to_owned(),
        }
    }

    async fn list_conversations(&self, limit: usize) -> Result<Vec<ActorConversation>> {
        let (sessions, _) = self.bridge.list_codex_session_snapshot(limit).await?;
        Ok(sessions.into_iter().map(session_to_actor).collect())
    }

    async fn conversation_snapshot(&self, limit: usize) -> Result<ActorConversationPage> {
        let (sessions, complete) = self.bridge.list_codex_session_snapshot(limit).await?;
        Ok(ActorConversationPage {
            conversations: sessions.into_iter().map(session_to_actor).collect(),
            complete,
        })
    }

    async fn load_messages(
        &self,
        conversation_id: &str,
        before_cursor: Option<&str>,
        page_size: usize,
    ) -> Result<ActorMessagePage> {
        let page = self
            .bridge
            .load_codex_messages(conversation_id, before_cursor, page_size)?;
        Ok(ActorMessagePage {
            messages: page
                .messages
                .into_iter()
                .map(|message| session_message_to_actor(conversation_id, message))
                .collect(),
            next_before_cursor: page.next_before_cursor,
        })
    }

    async fn send_message(
        &self,
        conversation_id: Option<&str>,
        input: ActorMessageInput<'_>,
    ) -> Result<Value> {
        self.bridge
            .send_actor_conversation_message(
                conversation_id.map(str::to_owned),
                input.content.to_owned(),
                input.attachment_paths.to_vec(),
                input.mode.map(str::to_owned),
                input.project_path.map(str::to_owned),
                input.message_id.map(str::to_owned),
                input.author_kind.to_owned(),
                input.author_id.map(str::to_owned),
                input.author_label.map(str::to_owned),
                input.coworker_id.map(str::to_owned),
            )
            .await
    }

    async fn interrupt(&self, _conversation_id: &str) -> Result<()> {
        Err(BridgeError::message(
            "Codex interruption is handled by the app-server request path",
        ))
    }

    async fn set_mode(&self, conversation_id: &str, mode: &str) -> Result<Value> {
        self.bridge
            .set_codex_conversation_mode(conversation_id, mode)
            .await
    }

    async fn rename_conversation(&self, conversation_id: &str, title: &str) -> Result<Value> {
        self.bridge
            .rename_codex_conversation(conversation_id, title)
            .await
    }

    async fn record_external_message(
        &self,
        conversation_id: &str,
        input: ActorMessageInput<'_>,
    ) -> Result<()> {
        self.bridge
            .record_actor_conversation_message(
                conversation_id,
                input.message_id,
                input.author_kind,
                input.author_id,
                input.author_label,
                input.content,
                input.attachment_paths,
            )
            .await
    }

    async fn resolve_request(
        &self,
        coworker_id: &str,
        conversation_id: &str,
        request_id: &str,
        response: Value,
    ) -> Result<Value> {
        self.bridge
            .resolve_server_request_for_desktop(coworker_id, conversation_id, request_id, response)
            .await
    }
}

fn session_to_actor(session: crate::codex_session::SessionSummary) -> ActorConversation {
    ActorConversation {
        actor_id: ActorId::Codex,
        conversation_id: session.thread_id,
        title: session.title,
        project_id: session.project_id,
        project_name: session.project_name,
        project_path: session.project_path,
        writable: session.can_continue,
        updated_at: Some(session.last_active_at),
        mode: session
            .pending_collaboration_mode
            .or(session.collaboration_mode),
    }
}

pub fn session_message_to_actor(
    conversation_id: &str,
    message: crate::codex_session::SessionMessage,
) -> ActorMessage {
    ActorMessage {
        id: message.id,
        actor_id: ActorId::Codex,
        conversation_id: conversation_id.to_owned(),
        author_kind: message.author_kind,
        content: message.text,
        created_at: message.timestamp,
        metadata: json!({
            "author_id": message.author_id,
            "author_label": message.author_label,
            "kind": message.kind,
            "attachments": message.attachments,
            "turn_id": message.turn_id,
            "item_id": message.item_id,
            "streaming": message.streaming,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_codex_history_uses_continuation_capability_for_writable_state() {
        let conversation = session_to_actor(crate::codex_session::SessionSummary {
            thread_id: "native_thread".to_owned(),
            title: "Native thread".to_owned(),
            project_id: None,
            project_name: None,
            project_path: None,
            status: "notLoaded".to_owned(),
            last_active_at: "2026-07-25T00:00:00Z".to_owned(),
            owned_by_bridge: false,
            can_continue: true,
            collaboration_mode: None,
            pending_collaboration_mode: None,
            source: Some("vscode".to_owned()),
            participants: vec!["codex".to_owned()],
        });

        assert!(conversation.writable);
    }
}
