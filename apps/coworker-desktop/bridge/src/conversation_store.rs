use std::{collections::HashSet, path::Path, sync::Mutex};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    actor::{ActorConversation, ActorMessage, ActorStreamEvent, publish_actor_stream_event},
    coworker::CoworkerRegistration,
    desktop_protocol::{ActorId, DeliveryAck, DesktopEnvelopeV1},
    error::{BridgeError, Result},
};

const DEFAULT_CONVERSATION_TITLE_MAX_CHARS: usize = 60;
const OUTBOX_INITIAL_RETRY_GRACE_SECONDS: i64 = 35;

pub(crate) fn default_conversation_title(content: &str) -> Option<String> {
    let collapsed = content.split_whitespace().collect::<Vec<_>>().join(" ");
    let length = collapsed.chars().count();
    if length == 0 {
        None
    } else if length <= DEFAULT_CONVERSATION_TITLE_MAX_CHARS {
        Some(collapsed)
    } else {
        let prefix: String = collapsed
            .chars()
            .take(DEFAULT_CONVERSATION_TITLE_MAX_CHARS - 1)
            .collect();
        Some(format!("{prefix}…"))
    }
}

#[derive(Debug, Clone)]
pub struct PendingDelivery {
    pub message_id: String,
    pub coworker_id: String,
    pub envelope: DesktopEnvelopeV1,
    pub attempts: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalRequest {
    pub request_id: String,
    pub actor_id: ActorId,
    pub conversation_id: String,
    pub coworker_id: String,
    pub owner_id: String,
    pub tool_name: String,
    pub input: Value,
    pub status: String,
    pub response: Option<Value>,
    pub expires_at: DateTime<Utc>,
    /// Links the SQLite approval row to an in-memory oneshot wait for the Codex
    /// actor. Claude actor rows leave this `None` since their resolution is
    /// polled directly from SQLite by the MCP sidecar.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub server_request_id: Option<String>,
}

/// Structured result returned when the Desktop UI resolves an approval.
/// `ok: false` with a `reason` replaces the former hard error that was
/// surfaced to the user as an unhelpful toast.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolveApprovalResult {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

pub struct ConversationStore {
    connection: Mutex<Connection>,
    active_inbox: Mutex<HashSet<String>>,
}

impl ConversationStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        if let Some(parent) = path.as_ref().parent() {
            std::fs::create_dir_all(parent)?;
        }
        let connection = Connection::open(path).map_err(sql_error)?;
        connection
            .execute_batch(
                "PRAGMA journal_mode=WAL;
                 PRAGMA foreign_keys=ON;
                 CREATE TABLE IF NOT EXISTS inbox (
                    message_id TEXT PRIMARY KEY,
                    ack_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing'
                 );
                 CREATE TABLE IF NOT EXISTS outbox (
                    message_id TEXT PRIMARY KEY,
                    coworker_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT
                 );
                 CREATE TABLE IF NOT EXISTS conversations (
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    coworker_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    writable INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, conversation_id)
                 );
                 CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    author_kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                 );
                 CREATE TABLE IF NOT EXISTS leases (
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, conversation_id)
                 );
                 CREATE TABLE IF NOT EXISTS registrations (
                    coworker_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    registration_id TEXT NOT NULL,
                    participant_id TEXT NOT NULL,
                    PRIMARY KEY(coworker_id, actor_id)
                 );
                 CREATE TABLE IF NOT EXISTS actor_runs (
                    run_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT,
                    coworker_id TEXT
                 );
                 CREATE TABLE IF NOT EXISTS approval_requests (
                    request_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    coworker_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    response_json TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                 );
                 CREATE TABLE IF NOT EXISTS conversation_modes (
                    actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    PRIMARY KEY(actor_id, conversation_id)
                 );",
            )
            .map_err(sql_error)?;
        let _ = connection.execute("ALTER TABLE actor_runs ADD COLUMN sidecar_token TEXT", []);
        let _ = connection.execute("ALTER TABLE actor_runs ADD COLUMN coworker_id TEXT", []);
        let _ = connection.execute(
            "ALTER TABLE inbox ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
            [],
        );
        // Links Codex actor approval rows (which resolve through an in-memory
        // oneshot) back to the bridge's pending server request map. Existing
        // Claude rows leave the column NULL.
        let _ = connection.execute(
            "ALTER TABLE approval_requests ADD COLUMN server_request_id TEXT",
            [],
        );
        // A Desktop-owned actor can finish creating its native session before
        // the outer request persists the messages. Recover that ownership after
        // an app restart so an interrupted Coworker -> actor turn remains
        // continuable instead of falling back to read-only native history.
        connection
            .execute(
                "INSERT OR IGNORE INTO conversations(
                    actor_id, conversation_id, coworker_id, writable, updated_at
                 )
                 SELECT actor_id, conversation_id, '', 1, ?1 FROM actor_runs
                 WHERE conversation_id IS NOT NULL AND conversation_id <> ''",
                [Utc::now().to_rfc3339()],
            )
            .map_err(sql_error)?;
        Ok(Self {
            connection: Mutex::new(connection),
            active_inbox: Mutex::new(HashSet::new()),
        })
    }

    pub fn remember_inbox(&self, envelope: &DesktopEnvelopeV1) -> Result<DeliveryAck> {
        envelope.validate()?;
        if self
            .active_inbox
            .lock()
            .expect("conversation store poisoned")
            .contains(&envelope.message_id)
        {
            return Ok(DeliveryAck {
                message_id: envelope.message_id.clone(),
                accepted: true,
                duplicate: true,
            });
        }
        let connection = self.connection.lock().expect("conversation store poisoned");
        if let Some((ack_json, status)) = connection
            .query_row(
                "SELECT ack_json, status FROM inbox WHERE message_id=?1",
                [&envelope.message_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(sql_error)?
            && status == "completed"
        {
            let mut ack: DeliveryAck = serde_json::from_str(&ack_json)?;
            ack.duplicate = true;
            return Ok(ack);
        }
        let ack = DeliveryAck {
            message_id: envelope.message_id.clone(),
            accepted: true,
            duplicate: false,
        };
        connection
            .execute(
                "INSERT INTO inbox(message_id, ack_json, received_at, status)
                 VALUES (?1, ?2, ?3, 'processing')
                 ON CONFLICT(message_id) DO UPDATE SET
                    ack_json=excluded.ack_json,
                    received_at=excluded.received_at,
                    status='processing'",
                params![
                    envelope.message_id,
                    serde_json::to_string(&ack)?,
                    Utc::now().to_rfc3339()
                ],
            )
            .map_err(sql_error)?;
        self.active_inbox
            .lock()
            .expect("conversation store poisoned")
            .insert(envelope.message_id.clone());
        Ok(ack)
    }

    pub fn complete_inbox(&self, message_id: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE inbox SET status='completed' WHERE message_id=?1",
                [message_id],
            )
            .map_err(sql_error)?;
        self.active_inbox
            .lock()
            .expect("conversation store poisoned")
            .remove(message_id);
        Ok(())
    }

    pub fn forget_inbox(&self, message_id: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute("DELETE FROM inbox WHERE message_id=?1", [message_id])
            .map_err(sql_error)?;
        self.active_inbox
            .lock()
            .expect("conversation store poisoned")
            .remove(message_id);
        Ok(())
    }

    pub fn enqueue(&self, coworker_id: &str, envelope: &DesktopEnvelopeV1) -> Result<()> {
        envelope.validate()?;
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "INSERT OR IGNORE INTO outbox(message_id, coworker_id, envelope_json, next_attempt_at)
                 VALUES (?1, ?2, ?3, ?4)",
                params![
                    envelope.message_id,
                    coworker_id,
                    serde_json::to_string(envelope)?,
                    (Utc::now()
                        + chrono::Duration::seconds(OUTBOX_INITIAL_RETRY_GRACE_SECONDS))
                    .to_rfc3339()
                ],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn acknowledge(&self, message_id: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE outbox SET state='acked', last_error=NULL WHERE message_id=?1",
                [message_id],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn mark_dead_letter(&self, message_id: &str, error: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE outbox SET state='dead_letter', last_error=?2 WHERE message_id=?1",
                params![message_id, error],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn schedule_retry(&self, message_id: &str, error: &str) -> Result<()> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let attempts = connection
            .query_row(
                "SELECT attempts FROM outbox WHERE message_id=?1",
                [message_id],
                |row| row.get::<_, u32>(0),
            )
            .optional()
            .map_err(sql_error)?
            .unwrap_or(0)
            .saturating_add(1);
        let delay_seconds = (1_u64 << attempts.min(6)).min(60);
        let next_attempt = Utc::now() + chrono::Duration::seconds(delay_seconds as i64);
        connection
            .execute(
                "UPDATE outbox SET state='pending', attempts=?2, next_attempt_at=?3, last_error=?4
                 WHERE message_id=?1",
                params![message_id, attempts, next_attempt.to_rfc3339(), error],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn pending_deliveries(&self, limit: usize) -> Result<Vec<PendingDelivery>> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let mut statement = connection
            .prepare(
                "SELECT message_id, coworker_id, envelope_json, attempts FROM outbox
                 WHERE state='pending' AND next_attempt_at<=?1
                 ORDER BY next_attempt_at ASC LIMIT ?2",
            )
            .map_err(sql_error)?;
        let rows = statement
            .query_map(
                params![Utc::now().to_rfc3339(), limit.max(1) as i64],
                |row| {
                    let envelope_json: String = row.get(2)?;
                    let envelope = serde_json::from_str(&envelope_json).map_err(|error| {
                        rusqlite::Error::FromSqlConversionFailure(
                            envelope_json.len(),
                            rusqlite::types::Type::Text,
                            Box::new(error),
                        )
                    })?;
                    Ok(PendingDelivery {
                        message_id: row.get(0)?,
                        coworker_id: row.get(1)?,
                        envelope,
                        attempts: row.get(3)?,
                    })
                },
            )
            .map_err(sql_error)?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .map_err(sql_error)
    }

    #[cfg(test)]
    fn outbox_state(&self, message_id: &str) -> Result<Option<(String, u32)>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT state, attempts FROM outbox WHERE message_id=?1",
                [message_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(sql_error)
    }

    pub fn append_message(
        &self,
        id: &str,
        actor: ActorId,
        conversation_id: &str,
        coworker_id: &str,
        author_kind: &str,
        content: &str,
        metadata: &Value,
    ) -> Result<()> {
        self.append_message_at(
            id,
            actor,
            conversation_id,
            coworker_id,
            author_kind,
            content,
            metadata,
            Utc::now(),
        )
    }

    pub fn append_message_at(
        &self,
        id: &str,
        actor: ActorId,
        conversation_id: &str,
        coworker_id: &str,
        author_kind: &str,
        content: &str,
        metadata: &Value,
        created_at: DateTime<Utc>,
    ) -> Result<()> {
        let created_at = created_at.to_rfc3339();
        let initial_title =
            if actor == ActorId::Local && matches!(author_kind, "local" | "coworker") {
                default_conversation_title(content).unwrap_or_default()
            } else {
                String::new()
            };
        let connection = self.connection.lock().expect("conversation store poisoned");
        connection
            .execute(
                "INSERT INTO conversations(actor_id, conversation_id, coworker_id, title, writable, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 1, ?5)
                 ON CONFLICT(actor_id, conversation_id) DO UPDATE SET
                   coworker_id=CASE
                     WHEN excluded.coworker_id <> '' THEN excluded.coworker_id
                     ELSE conversations.coworker_id
                   END,
                   title=CASE
                     WHEN conversations.title = '' AND excluded.title <> '' THEN excluded.title
                     ELSE conversations.title
                   END,
                   writable=1,
                   updated_at=excluded.updated_at",
                params![actor.as_str(), conversation_id, coworker_id, initial_title, created_at],
            )
            .map_err(sql_error)?;
        connection
            .execute(
                "INSERT OR IGNORE INTO messages(id, actor_id, conversation_id, author_kind, content, created_at, metadata_json)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![
                    id,
                    actor.as_str(),
                    conversation_id,
                    author_kind,
                    content,
                    created_at,
                    serde_json::to_string(metadata)?
                ],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn acquire_lease(
        &self,
        actor: ActorId,
        conversation_id: &str,
        owner_id: &str,
        expires_at: DateTime<Utc>,
    ) -> Result<bool> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let existing: Option<(String, String)> = connection
            .query_row(
                "SELECT owner_id, expires_at FROM leases WHERE actor_id=?1 AND conversation_id=?2",
                params![actor.as_str(), conversation_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(sql_error)?;
        if let Some((owner, expiry)) = existing
            && owner != owner_id
            && DateTime::parse_from_rfc3339(&expiry)
                .map(|value| value.with_timezone(&Utc) > Utc::now())
                .unwrap_or(false)
        {
            return Ok(false);
        }
        connection
            .execute(
                "INSERT INTO leases(actor_id, conversation_id, owner_id, expires_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(actor_id, conversation_id) DO UPDATE SET owner_id=excluded.owner_id, expires_at=excluded.expires_at",
                params![actor.as_str(), conversation_id, owner_id, expires_at.to_rfc3339()],
            )
            .map_err(sql_error)?;
        Ok(true)
    }

    pub fn save_registration(
        &self,
        coworker_id: &str,
        actor: ActorId,
        registration: &CoworkerRegistration,
    ) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "INSERT INTO registrations(coworker_id, actor_id, registration_id, participant_id)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(coworker_id, actor_id) DO UPDATE SET
                   registration_id=excluded.registration_id, participant_id=excluded.participant_id",
                params![
                    coworker_id,
                    actor.as_str(),
                    registration.registration_id,
                    registration.participant_id
                ],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn registration(
        &self,
        coworker_id: &str,
        actor: ActorId,
    ) -> Result<Option<CoworkerRegistration>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT registration_id, participant_id FROM registrations WHERE coworker_id=?1 AND actor_id=?2",
                params![coworker_id, actor.as_str()],
                |row| {
                    Ok(CoworkerRegistration {
                        registration_id: row.get(0)?,
                        participant_id: row.get(1)?,
                    })
                },
            )
            .optional()
            .map_err(sql_error)
    }

    pub fn remove_registration(&self, coworker_id: &str, actor: ActorId) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "DELETE FROM registrations WHERE coworker_id=?1 AND actor_id=?2",
                params![coworker_id, actor.as_str()],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn list_local_conversations(&self, limit: usize) -> Result<Vec<ActorConversation>> {
        self.list_stored_conversations(ActorId::Local, limit)
    }

    pub fn list_stored_conversations(
        &self,
        actor: ActorId,
        limit: usize,
    ) -> Result<Vec<ActorConversation>> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let mut statement = connection
            .prepare(
                "SELECT c.conversation_id, c.title, c.writable, c.updated_at,
                        (SELECT m.content FROM messages m
                         WHERE m.actor_id=c.actor_id
                           AND m.conversation_id=c.conversation_id
                           AND m.author_kind IN ('local', 'coworker')
                           AND trim(m.content) <> ''
                         ORDER BY m.created_at ASC, m.rowid ASC LIMIT 1)
                 FROM conversations c
                 WHERE c.actor_id=?1 ORDER BY c.updated_at DESC LIMIT ?2",
            )
            .map_err(sql_error)?;
        let rows = statement
            .query_map(params![actor.as_str(), limit.max(1) as i64], |row| {
                let title: String = row.get(1)?;
                let conversation_id: String = row.get(0)?;
                let first_human_message: Option<String> = row.get(4)?;
                Ok(ActorConversation {
                    actor_id: actor,
                    title: if title.is_empty() {
                        first_human_message
                            .as_deref()
                            .and_then(default_conversation_title)
                            .unwrap_or_else(|| "搭档会话".to_owned())
                    } else {
                        title
                    },
                    conversation_id,
                    project_id: None,
                    project_name: None,
                    project_path: None,
                    writable: row.get::<_, i64>(2)? != 0,
                    updated_at: Some(row.get(3)?),
                    mode: None,
                })
            })
            .map_err(sql_error)?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .map_err(sql_error)
    }

    pub fn list_messages(
        &self,
        actor: ActorId,
        conversation_id: &str,
        limit: usize,
    ) -> Result<Vec<ActorMessage>> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let mut statement = connection
            .prepare(
                "SELECT id, author_kind, content, created_at, metadata_json FROM messages
                 WHERE actor_id=?1 AND conversation_id=?2 ORDER BY created_at DESC LIMIT ?3",
            )
            .map_err(sql_error)?;
        let mut messages = statement
            .query_map(
                params![actor.as_str(), conversation_id, limit.max(1) as i64],
                |row| {
                    let metadata_json: String = row.get(4)?;
                    Ok(ActorMessage {
                        id: row.get(0)?,
                        actor_id: actor,
                        conversation_id: conversation_id.to_owned(),
                        author_kind: row.get(1)?,
                        content: row.get(2)?,
                        created_at: row.get(3)?,
                        metadata: serde_json::from_str(&metadata_json).unwrap_or(Value::Null),
                    })
                },
            )
            .map_err(sql_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(sql_error)?;
        messages.reverse();
        Ok(messages)
    }

    pub fn rename_conversation(
        &self,
        actor: ActorId,
        conversation_id: &str,
        title: &str,
    ) -> Result<()> {
        let changed = self
            .connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE conversations SET title=?3 WHERE actor_id=?1 AND conversation_id=?2",
                params![actor.as_str(), conversation_id, title],
            )
            .map_err(sql_error)?;
        if changed == 0 {
            return Err(BridgeError::message("conversation not found"));
        }
        Ok(())
    }

    pub fn set_actor_run(
        &self,
        run_id: &str,
        actor: ActorId,
        conversation_id: Option<&str>,
    ) -> Result<()> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        connection
            .execute(
                "INSERT INTO actor_runs(run_id, actor_id, conversation_id) VALUES (?1, ?2, ?3)
                 ON CONFLICT(run_id) DO UPDATE SET conversation_id=excluded.conversation_id",
                params![run_id, actor.as_str(), conversation_id],
            )
            .map_err(sql_error)?;
        if let Some(conversation_id) = conversation_id.filter(|value| !value.is_empty()) {
            connection
                .execute(
                    "INSERT OR IGNORE INTO conversations(
                        actor_id, conversation_id, coworker_id, writable, updated_at
                     ) VALUES (?1, ?2, '', 1, ?3)",
                    params![actor.as_str(), conversation_id, Utc::now().to_rfc3339()],
                )
                .map_err(sql_error)?;
        }
        Ok(())
    }

    pub fn set_actor_run_token(&self, run_id: &str, token: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE actor_runs SET sidecar_token=?2 WHERE run_id=?1",
                params![run_id, token],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn set_actor_run_coworker_id(&self, run_id: &str, coworker_id: Option<&str>) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE actor_runs SET coworker_id=?2 WHERE run_id=?1",
                params![run_id, coworker_id.filter(|value| !value.trim().is_empty())],
            )
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn consume_actor_run_token(&self, run_id: &str, token: &str) -> Result<bool> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let changed = connection
            .execute(
                "UPDATE actor_runs SET sidecar_token=NULL WHERE run_id=?1 AND sidecar_token=?2",
                params![run_id, token],
            )
            .map_err(sql_error)?;
        Ok(changed == 1)
    }

    pub fn actor_run_conversation(&self, run_id: &str) -> Result<Option<String>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT conversation_id FROM actor_runs WHERE run_id=?1",
                [run_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(sql_error)
            .map(Option::flatten)
    }

    pub fn actor_run_coworker_id(&self, run_id: &str) -> Result<Option<String>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT coworker_id FROM actor_runs WHERE run_id=?1",
                [run_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(sql_error)
            .map(Option::flatten)
    }

    pub fn remove_actor_run(&self, run_id: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute("DELETE FROM actor_runs WHERE run_id=?1", [run_id])
            .map_err(sql_error)?;
        Ok(())
    }

    pub fn create_approval(&self, request: &ApprovalRequest) -> Result<()> {
        self.create_pending_request(request, "desktop.approval.requested")
    }

    pub fn create_user_input(&self, request: &ApprovalRequest) -> Result<()> {
        self.create_pending_request(request, "desktop.user_input.requested")
    }

    fn create_pending_request(&self, request: &ApprovalRequest, event_type: &str) -> Result<()> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "INSERT INTO approval_requests(
                    request_id, actor_id, conversation_id, coworker_id, owner_id,
                    tool_name, input_json, status, response_json, expires_at, created_at,
                    server_request_id
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
                params![
                    request.request_id,
                    request.actor_id.as_str(),
                    request.conversation_id,
                    request.coworker_id,
                    request.owner_id,
                    request.tool_name,
                    serde_json::to_string(&request.input)?,
                    request.status,
                    request
                        .response
                        .as_ref()
                        .map(serde_json::to_string)
                        .transpose()?,
                    request.expires_at.to_rfc3339(),
                    Utc::now().to_rfc3339(),
                    request.server_request_id,
                ],
            )
            .map_err(sql_error)?;
        publish_actor_stream_event(ActorStreamEvent {
            actor_id: request.actor_id,
            conversation_id: request.conversation_id.clone(),
            message_id: None,
            event: json!({
                "type": event_type,
                "request_id": request.request_id,
                "actor_id": request.actor_id.as_str(),
                "tool_name": request.tool_name,
                "input": request.input,
                "status": "pending",
            }),
        });
        Ok(())
    }

    pub fn approval(&self, request_id: &str) -> Result<Option<ApprovalRequest>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT actor_id, conversation_id, coworker_id, owner_id, tool_name,
                        input_json, status, response_json, expires_at, server_request_id
                 FROM approval_requests WHERE request_id=?1",
                [request_id],
                |row| approval_from_row(request_id, row),
            )
            .optional()
            .map_err(sql_error)
    }

    pub fn pending_approvals(&self) -> Result<Vec<ApprovalRequest>> {
        let connection = self.connection.lock().expect("conversation store poisoned");
        let mut statement = connection
            .prepare(
                "SELECT request_id, actor_id, conversation_id, coworker_id, owner_id,
                        tool_name, input_json, status, response_json, expires_at, server_request_id
                 FROM approval_requests
                 WHERE status='pending'
                   AND NOT (actor_id='claude' AND tool_name='AskUserQuestion')
                   AND expires_at>?1
                 ORDER BY created_at ASC",
            )
            .map_err(sql_error)?;
        let rows = statement
            .query_map([Utc::now().to_rfc3339()], |row| {
                let request_id: String = row.get(0)?;
                approval_from_row_offset(&request_id, row, 1)
            })
            .map_err(sql_error)?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .map_err(sql_error)
    }

    pub fn resolve_approval(
        &self,
        request_id: &str,
        actor: ActorId,
        conversation_id: &str,
        coworker_id: &str,
        owner_id: &str,
        response: &Value,
    ) -> Result<bool> {
        let changed = self
            .connection
            .lock()
            .expect("conversation store poisoned")
            .execute(
                "UPDATE approval_requests SET status='resolved', response_json=?6
                 WHERE request_id=?1 AND actor_id=?2 AND conversation_id=?3
                   AND coworker_id=?4 AND owner_id=?5 AND status='pending' AND expires_at>?7",
                params![
                    request_id,
                    actor.as_str(),
                    conversation_id,
                    coworker_id,
                    owner_id,
                    serde_json::to_string(response)?,
                    Utc::now().to_rfc3339(),
                ],
            )
            .map_err(sql_error)?;
        Ok(changed == 1)
    }

    pub fn expire_approval(&self, request_id: &str) -> Result<()> {
        self.connection.lock().expect("conversation store poisoned").execute(
            "UPDATE approval_requests SET status='expired' WHERE request_id=?1 AND status='pending'",
            [request_id],
        ).map_err(sql_error)?;
        Ok(())
    }

    pub fn set_conversation_mode(
        &self,
        actor: ActorId,
        conversation_id: &str,
        mode: &str,
    ) -> Result<()> {
        self.connection.lock().expect("conversation store poisoned").execute(
            "INSERT INTO conversation_modes(actor_id, conversation_id, mode) VALUES (?1, ?2, ?3)
             ON CONFLICT(actor_id, conversation_id) DO UPDATE SET mode=excluded.mode",
            params![actor.as_str(), conversation_id, mode],
        ).map_err(sql_error)?;
        Ok(())
    }

    pub fn conversation_mode(
        &self,
        actor: ActorId,
        conversation_id: &str,
    ) -> Result<Option<String>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT mode FROM conversation_modes WHERE actor_id=?1 AND conversation_id=?2",
                params![actor.as_str(), conversation_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(sql_error)
    }

    pub fn conversation_is_writable(&self, actor: ActorId, conversation_id: &str) -> Result<bool> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT writable FROM conversations WHERE actor_id=?1 AND conversation_id=?2",
                params![actor.as_str(), conversation_id],
                |row| Ok(row.get::<_, i64>(0)? != 0),
            )
            .optional()
            .map(|value| value.unwrap_or(false))
            .map_err(sql_error)
    }

    pub fn conversation_coworker_id(
        &self,
        actor: ActorId,
        conversation_id: &str,
    ) -> Result<Option<String>> {
        self.connection
            .lock()
            .expect("conversation store poisoned")
            .query_row(
                "SELECT coworker_id FROM conversations WHERE actor_id=?1 AND conversation_id=?2",
                params![actor.as_str(), conversation_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(sql_error)
    }
}

fn approval_from_row(
    request_id: &str,
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<ApprovalRequest> {
    approval_from_row_offset(request_id, row, 0)
}

fn approval_from_row_offset(
    request_id: &str,
    row: &rusqlite::Row<'_>,
    offset: usize,
) -> rusqlite::Result<ApprovalRequest> {
    let actor: String = row.get(offset)?;
    let input_json: String = row.get(offset + 5)?;
    let response_json: Option<String> = row.get(offset + 7)?;
    let expires_at: String = row.get(offset + 8)?;
    let server_request_id: Option<String> = row.get(offset + 9).unwrap_or(None);
    Ok(ApprovalRequest {
        request_id: request_id.to_owned(),
        actor_id: match actor.as_str() {
            "local" => ActorId::Local,
            "codex" => ActorId::Codex,
            "claude" => ActorId::Claude,
            _ => return Err(rusqlite::Error::InvalidQuery),
        },
        conversation_id: row.get(offset + 1)?,
        coworker_id: row.get(offset + 2)?,
        owner_id: row.get(offset + 3)?,
        tool_name: row.get(offset + 4)?,
        input: serde_json::from_str(&input_json).unwrap_or(Value::Null),
        status: row.get(offset + 6)?,
        response: response_json.and_then(|value| serde_json::from_str(&value).ok()),
        expires_at: DateTime::parse_from_rfc3339(&expires_at)
            .map(|value| value.with_timezone(&Utc))
            .map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(
                    expires_at.len(),
                    rusqlite::types::Type::Text,
                    Box::new(error),
                )
            })?,
        server_request_id,
    })
}

fn sql_error(error: rusqlite::Error) -> BridgeError {
    BridgeError::message(format!("desktop store error: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::desktop_protocol::DesktopEventType;
    use chrono::Duration;
    use serde_json::json;

    fn store() -> ConversationStore {
        ConversationStore::open(":memory:").unwrap()
    }

    #[test]
    fn inbox_deduplication_survives_repeated_delivery() {
        let store = store();
        let envelope = DesktopEnvelopeV1::new(DesktopEventType::ThreadEvent, json!({}));
        assert!(!store.remember_inbox(&envelope).unwrap().duplicate);
        assert!(store.remember_inbox(&envelope).unwrap().duplicate);
        store.complete_inbox(&envelope.message_id).unwrap();
        assert!(store.remember_inbox(&envelope).unwrap().duplicate);
        store.forget_inbox(&envelope.message_id).unwrap();
        assert!(!store.remember_inbox(&envelope).unwrap().duplicate);
    }

    #[test]
    fn interrupted_inbox_delivery_can_be_reclaimed_after_restart() {
        let root =
            std::env::temp_dir().join(format!("coworker-inbox-restart-{}", uuid::Uuid::new_v4()));
        let database = root.join("desktop.sqlite3");
        let envelope = DesktopEnvelopeV1::new(DesktopEventType::ThreadEvent, json!({}));
        {
            let store = ConversationStore::open(&database).unwrap();
            assert!(!store.remember_inbox(&envelope).unwrap().duplicate);
        }
        let store = ConversationStore::open(&database).unwrap();
        assert!(!store.remember_inbox(&envelope).unwrap().duplicate);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn lease_is_actor_and_owner_scoped() {
        let store = store();
        let expiry = Utc::now() + Duration::minutes(5);
        assert!(
            store
                .acquire_lease(ActorId::Claude, "s1", "desk-a", expiry)
                .unwrap()
        );
        assert!(
            !store
                .acquire_lease(ActorId::Claude, "s1", "desk-b", expiry)
                .unwrap()
        );
        assert!(
            store
                .acquire_lease(ActorId::Codex, "s1", "desk-b", expiry)
                .unwrap()
        );
    }

    #[test]
    fn outbox_retries_and_dead_letters_are_persistent() {
        let store = store();
        let envelope = DesktopEnvelopeV1::new(DesktopEventType::ThreadEvent, json!({}));
        store.enqueue("partner", &envelope).unwrap();
        assert!(store.pending_deliveries(10).unwrap().is_empty());
        store
            .schedule_retry(&envelope.message_id, "offline")
            .unwrap();
        assert_eq!(
            store.outbox_state(&envelope.message_id).unwrap(),
            Some(("pending".into(), 1))
        );
        store
            .mark_dead_letter(&envelope.message_id, "unauthorized")
            .unwrap();
        assert_eq!(
            store.outbox_state(&envelope.message_id).unwrap(),
            Some(("dead_letter".into(), 1))
        );
    }

    #[test]
    fn approval_resolution_requires_every_owner_dimension() {
        let store = store();
        let mut approval_events = crate::actor::subscribe_actor_stream_events();
        let request = ApprovalRequest {
            request_id: "request-1".into(),
            actor_id: ActorId::Claude,
            conversation_id: "session-1".into(),
            coworker_id: "partner-1".into(),
            owner_id: "desktop-1".into(),
            tool_name: "Bash".into(),
            input: json!({"command":"git status"}),
            status: "pending".into(),
            response: None,
            expires_at: Utc::now() + Duration::minutes(5),
            server_request_id: None,
        };
        store.create_approval(&request).unwrap();
        let approval_event = loop {
            let event = approval_events.try_recv().unwrap();
            if event.event["request_id"] == request.request_id {
                break event;
            }
        };
        assert_eq!(approval_event.event["type"], "desktop.approval.requested");
        assert!(
            !store
                .resolve_approval(
                    "request-1",
                    ActorId::Codex,
                    "session-1",
                    "partner-1",
                    "desktop-1",
                    &json!({"behavior":"allow"})
                )
                .unwrap()
        );
        assert!(
            !store
                .resolve_approval(
                    "request-1",
                    ActorId::Claude,
                    "session-1",
                    "partner-2",
                    "desktop-1",
                    &json!({"behavior":"allow"})
                )
                .unwrap()
        );
        assert!(
            store
                .resolve_approval(
                    "request-1",
                    ActorId::Claude,
                    "session-1",
                    "partner-1",
                    "desktop-1",
                    &json!({"behavior":"deny"})
                )
                .unwrap()
        );
    }

    #[test]
    fn claude_question_is_user_input_not_pending_approval() {
        let store = store();
        let mut events = crate::actor::subscribe_actor_stream_events();
        let request = ApprovalRequest {
            request_id: "question-1".into(),
            actor_id: ActorId::Claude,
            conversation_id: "question-store-session".into(),
            coworker_id: "partner-1".into(),
            owner_id: "desktop-1".into(),
            tool_name: "AskUserQuestion".into(),
            input: json!({"questions":[{"question":"Which approach?"}]}),
            status: "pending".into(),
            response: None,
            expires_at: Utc::now() + Duration::minutes(5),
            server_request_id: None,
        };

        store.create_user_input(&request).unwrap();

        let event = loop {
            let event = events.try_recv().unwrap();
            if event.event["request_id"] == request.request_id {
                break event;
            }
        };
        assert_eq!(event.event["type"], "desktop.user_input.requested");
        assert!(store.pending_approvals().unwrap().is_empty());
        assert!(store.approval("question-1").unwrap().is_some());
    }

    #[test]
    fn sidecar_token_is_one_time() {
        let store = store();
        store
            .set_actor_run("run-1", ActorId::Claude, Some("session-1"))
            .unwrap();
        assert!(
            store
                .conversation_is_writable(ActorId::Claude, "session-1")
                .unwrap()
        );
        store
            .set_actor_run_coworker_id("run-1", Some("partner-1"))
            .unwrap();
        assert_eq!(
            store.actor_run_coworker_id("run-1").unwrap().as_deref(),
            Some("partner-1")
        );
        store.set_actor_run_token("run-1", "secret").unwrap();
        assert!(store.consume_actor_run_token("run-1", "secret").unwrap());
        assert!(!store.consume_actor_run_token("run-1", "secret").unwrap());
    }

    #[test]
    fn reopening_store_recovers_interrupted_actor_run_as_writable() {
        let path = std::env::temp_dir().join(format!(
            "coworker-desktop-interrupted-run-{}.sqlite3",
            uuid::Uuid::new_v4()
        ));
        let store = ConversationStore::open(&path).unwrap();
        store
            .set_actor_run("run-1", ActorId::Claude, Some("session-interrupted"))
            .unwrap();
        store
            .connection
            .lock()
            .unwrap()
            .execute(
                "DELETE FROM conversations WHERE actor_id='claude' AND conversation_id='session-interrupted'",
                [],
            )
            .unwrap();
        drop(store);

        let recovered = ConversationStore::open(&path).unwrap();
        assert!(
            recovered
                .conversation_is_writable(ActorId::Claude, "session-interrupted")
                .unwrap()
        );
        drop(recovered);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn claimed_actor_conversation_records_coworker_on_first_message() {
        let store = store();
        store
            .set_actor_run("run-1", ActorId::Claude, Some("session-1"))
            .unwrap();
        store
            .append_message(
                "message-1",
                ActorId::Claude,
                "session-1",
                "partner-a",
                "coworker",
                "hello",
                &json!({}),
            )
            .unwrap();

        assert_eq!(
            store
                .conversation_coworker_id(ActorId::Claude, "session-1")
                .unwrap(),
            Some("partner-a".to_owned())
        );
    }

    #[test]
    fn local_conversation_keeps_its_original_coworker() {
        let store = store();
        store
            .append_message(
                "message-1",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "local",
                "hello",
                &json!({}),
            )
            .unwrap();

        assert_eq!(
            store
                .conversation_coworker_id(ActorId::Local, "local-thread-1")
                .unwrap(),
            Some("partner-a".to_owned())
        );
    }

    #[test]
    fn local_conversation_uses_the_first_sent_message_as_its_bounded_title() {
        let store = store();
        let first_message = format!("  {}\n trailing words  ", "你".repeat(70));
        store
            .append_message(
                "message-1",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "local",
                &first_message,
                &json!({}),
            )
            .unwrap();
        store
            .append_message(
                "message-2",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "local",
                "This later message must not replace the title",
                &json!({}),
            )
            .unwrap();

        let conversation = store.list_local_conversations(10).unwrap().remove(0);
        assert_eq!(conversation.title, format!("{}…", "你".repeat(59)));
        assert_eq!(conversation.title.chars().count(), 60);
    }

    #[test]
    fn local_conversation_uses_the_first_non_empty_human_message_as_title() {
        let store = store();
        store
            .append_message(
                "message-1",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "coworker",
                "Incoming message",
                &json!({}),
            )
            .unwrap();
        assert_eq!(
            store.list_local_conversations(10).unwrap()[0].title,
            "Incoming message"
        );
        store
            .append_message(
                "message-2",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "local",
                "  My first sent\nmessage  ",
                &json!({}),
            )
            .unwrap();
        assert_eq!(
            store.list_local_conversations(10).unwrap()[0].title,
            "Incoming message"
        );
        store
            .rename_conversation(ActorId::Local, "local-thread-1", "Custom title")
            .unwrap();
        store
            .append_message(
                "message-3",
                ActorId::Local,
                "local-thread-1",
                "partner-a",
                "local",
                "Another sent message",
                &json!({}),
            )
            .unwrap();

        let conversation = store.list_local_conversations(10).unwrap().remove(0);
        assert_eq!(conversation.title, "Custom title");
    }

    #[test]
    fn local_conversation_backfills_an_existing_blank_title_from_its_first_human_message() {
        let store = store();
        let connection = store.connection.lock().unwrap();
        connection
            .execute(
                "INSERT INTO conversations(actor_id, conversation_id, coworker_id, title, writable, updated_at)
                 VALUES ('local', 'legacy-local-thread', 'partner-a', '', 1, '2026-07-13T00:00:00Z')",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO messages(id, actor_id, conversation_id, author_kind, content, created_at, metadata_json)
                 VALUES ('legacy-message', 'local', 'legacy-local-thread', 'coworker', '  Legacy\nfirst message  ', '2026-07-13T00:00:00Z', '{}')",
                [],
            )
            .unwrap();
        drop(connection);

        let conversation = store.list_local_conversations(10).unwrap().remove(0);
        assert_eq!(conversation.title, "Legacy first message");
    }

    #[test]
    fn actor_conversation_can_be_renamed_and_change_mode() {
        let store = store();
        store
            .append_message(
                "message-1",
                ActorId::Claude,
                "claude-thread-1",
                "partner-a",
                "local",
                "hello",
                &json!({}),
            )
            .unwrap();
        store
            .rename_conversation(ActorId::Claude, "claude-thread-1", "New title")
            .unwrap();
        store
            .set_conversation_mode(ActorId::Claude, "claude-thread-1", "plan")
            .unwrap();

        let conversation = store
            .list_stored_conversations(ActorId::Claude, 10)
            .unwrap()
            .remove(0);
        assert_eq!(conversation.title, "New title");
        assert_eq!(
            store
                .conversation_mode(ActorId::Claude, "claude-thread-1")
                .unwrap()
                .as_deref(),
            Some("plan")
        );
    }
}
