use std::{collections::HashMap, path::Path, sync::Arc};

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use serde_json::{Value, json};
use tokio::{
    sync::{Mutex, mpsc, oneshot},
    time::{Duration, Instant, sleep},
};
use tracing::{info, warn};
use uuid::Uuid;

use crate::{
    actor::{
        ActorAdapter, ActorConversation, ActorConversationPage, ActorHealth, ActorMessageInput,
        ActorMessagePage, ActorOutboundRequest, ActorStreamEvent, publish_actor_stream_event,
    },
    bridge::AbortOnDrop,
    config::{BridgeCoworker, DesktopConfig},
    conversation_store::{ConversationStore, default_conversation_title},
    coworker::{CoworkerHttpClient, CoworkerRegistration},
    desktop_protocol::{ActorId, DesktopEnvelopeV1, DesktopEventType},
    error::{BridgeError, Result},
    ids::new_compact_id,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DeliveryFailure {
    Retry,
    DeadLetter,
}

fn actor_media_type(path: &str) -> &'static str {
    match Path::new(path)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "pdf" => "application/pdf",
        "json" => "application/json",
        "md" => "text/markdown",
        "txt" | "log" => "text/plain",
        _ => "application/octet-stream",
    }
}

fn actor_attachment_metadata(paths: &[String]) -> Value {
    Value::Array(paths.iter().map(|path| json!({
        "filename": Path::new(path).file_name().and_then(|value| value.to_str()).unwrap_or("attachment"),
        "media_type": actor_media_type(path),
        "path": path,
        "downloadable": true,
    })).collect())
}

fn sanitize_attachment_component(value: &str) -> String {
    let leaf = value
        .rsplit(['/', '\\'])
        .next()
        .filter(|value| !value.is_empty())
        .unwrap_or(value);
    let mut sanitized = String::with_capacity(leaf.len());
    for ch in leaf.chars() {
        if ch.is_control() || matches!(ch, '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*') {
            sanitized.push('_');
        } else {
            sanitized.push(ch);
        }
    }
    let sanitized = sanitized.trim_matches(['.', ' ']).trim().to_owned();
    if sanitized.is_empty() {
        "attachment".to_owned()
    } else {
        sanitized
    }
}

fn save_incoming_attachments(
    storage_dir: &Path,
    raw: Option<&Value>,
    request_id: &str,
    max_count: usize,
    max_bytes: u64,
) -> Result<(Vec<String>, Value)> {
    let Some(raw) = raw else {
        return Ok((Vec::new(), Value::Array(Vec::new())));
    };
    let items = raw
        .as_array()
        .ok_or_else(|| BridgeError::message("attachments must be an array"))?;
    if items.len() > max_count {
        return Err(BridgeError::message(format!(
            "attachments count exceeds limit: {} > {max_count}",
            items.len()
        )));
    }

    let mut decoded = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        let object = item.as_object().ok_or_else(|| {
            BridgeError::message(format!("attachments[{index}] must be an object"))
        })?;
        let filename = object
            .get("filename")
            .and_then(Value::as_str)
            .map(sanitize_attachment_component)
            .unwrap_or_else(|| format!("attachment_{}", index + 1));
        let media_type = object
            .get("media_type")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| actor_media_type(&filename).to_owned());
        let data = object.get("data").and_then(Value::as_str).ok_or_else(|| {
            BridgeError::message(format!("attachments[{index}].data is required"))
        })?;
        let bytes = BASE64.decode(data).map_err(|error| {
            BridgeError::message(format!(
                "attachments[{index}].data is invalid base64: {error}"
            ))
        })?;
        if bytes.len() as u64 > max_bytes {
            return Err(BridgeError::message(format!(
                "attachment {filename} exceeds size limit: {} > {max_bytes}",
                bytes.len()
            )));
        }
        decoded.push((filename, media_type, bytes));
    }

    if decoded.is_empty() {
        return Ok((Vec::new(), Value::Array(Vec::new())));
    }
    let directory = storage_dir
        .join("attachments")
        .join("incoming")
        .join(sanitize_attachment_component(request_id));
    std::fs::create_dir_all(&directory)?;
    let mut paths = Vec::with_capacity(decoded.len());
    let mut metadata = Vec::with_capacity(decoded.len());
    for (index, (filename, media_type, bytes)) in decoded.into_iter().enumerate() {
        let target = directory.join(format!("{:02}_{filename}", index + 1));
        std::fs::write(&target, &bytes)?;
        let path = target.to_string_lossy().into_owned();
        paths.push(path.clone());
        metadata.push(json!({
            "filename": filename,
            "media_type": media_type,
            "size": bytes.len(),
            "path": path,
            "downloadable": true,
        }));
    }
    Ok((paths, Value::Array(metadata)))
}

fn content_with_actor_attachments(content: &str, paths: &[String]) -> String {
    if paths.is_empty() {
        return content.to_owned();
    }
    let manifest = paths
        .iter()
        .enumerate()
        .map(|(index, path)| {
            format!(
                "{}. {} saved_path={}",
                index + 1,
                Path::new(path)
                    .file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("attachment"),
                path
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    if content.trim().is_empty() {
        format!("[附件]\n{manifest}")
    } else {
        format!("{content}\n\n[附件]\n{manifest}")
    }
}

pub struct DesktopRouter {
    config: DesktopConfig,
    http: CoworkerHttpClient,
    store: Arc<ConversationStore>,
    adapters: HashMap<ActorId, Arc<dyn ActorAdapter>>,
    registrations: Mutex<HashMap<(String, ActorId), CoworkerRegistration>>,
    published_snapshots: Mutex<HashMap<(String, ActorId), PublishedActorSnapshot>>,
}

struct PublishedActorSnapshot {
    fingerprint: String,
    registration_id: String,
    sent_at: Instant,
}

struct InboxReservation {
    store: Arc<ConversationStore>,
    message_id: String,
    committed: bool,
}

impl InboxReservation {
    fn new(store: Arc<ConversationStore>, message_id: String) -> Self {
        Self {
            store,
            message_id,
            committed: false,
        }
    }

    fn commit(&mut self) -> Result<()> {
        self.store.complete_inbox(&self.message_id)?;
        self.committed = true;
        Ok(())
    }
}

impl Drop for InboxReservation {
    fn drop(&mut self) {
        if !self.committed
            && let Err(error) = self.store.forget_inbox(&self.message_id)
        {
            warn!(%error, message_id = %self.message_id, "Failed to release Desktop inbox reservation");
        }
    }
}

impl DesktopRouter {
    pub fn new(config: DesktopConfig, adapters: Vec<Arc<dyn ActorAdapter>>) -> Result<Self> {
        let store = ConversationStore::open(config.storage_dir.join("desktop.sqlite3"))?;
        Ok(Self {
            config,
            http: CoworkerHttpClient::new()?,
            store: Arc::new(store),
            adapters: adapters
                .into_iter()
                .map(|adapter| (adapter.actor_id(), adapter))
                .collect(),
            registrations: Mutex::new(HashMap::new()),
            published_snapshots: Mutex::new(HashMap::new()),
        })
    }

    pub fn config(&self) -> &DesktopConfig {
        &self.config
    }

    pub async fn actor_health(&self) -> Vec<ActorHealth> {
        let mut health = vec![ActorHealth {
            actor_id: ActorId::Local,
            available: self.config.local_enabled,
            message: if self.config.local_enabled {
                "Local chat is available".to_owned()
            } else {
                "Local chat is disabled".to_owned()
            },
        }];
        for actor in [ActorId::Codex, ActorId::Claude] {
            if let Some(adapter) = self.adapters.get(&actor) {
                health.push(adapter.health().await);
            } else {
                health.push(ActorHealth {
                    actor_id: actor,
                    available: false,
                    message: format!("{actor} adapter is unavailable"),
                });
            }
        }
        health
    }

    pub async fn sync_registrations(&self) -> Result<Vec<ActorHealth>> {
        let health = self.actor_health().await;
        let availability: HashMap<ActorId, bool> = health
            .iter()
            .map(|item| (item.actor_id, item.available))
            .collect();
        let scan_limit = self.config.codex.snapshot_scan_thread_limit.max(1);
        let per_project_limit = self.config.codex.snapshot_thread_limit.max(1);
        let mut snapshots = HashMap::new();
        for actor in ActorId::ALL {
            if availability.get(&actor).copied().unwrap_or(false) {
                snapshots.insert(
                    actor,
                    self.build_actor_snapshot(actor, scan_limit, per_project_limit)
                        .await,
                );
            }
        }

        let mut attempted = 0;
        let mut succeeded = 0;
        let mut last_error = None;
        for coworker in &self.config.codex.coworkers {
            for actor in ActorId::ALL {
                if availability.get(&actor).copied().unwrap_or(false) {
                    attempted += 1;
                    let result = self
                        .publish_actor_snapshot(
                            coworker,
                            actor,
                            snapshots.get(&actor).expect("available actor snapshot"),
                        )
                        .await;
                    match result {
                        Ok(()) => succeeded += 1,
                        Err(error) => {
                            warn!(coworker_id = %coworker.coworker_id, %actor, %error, "Failed to publish Desktop actor snapshot");
                            last_error = Some(error);
                        }
                    }
                } else if let Err(error) = self.unregister(coworker, actor).await {
                    warn!(coworker_id = %coworker.coworker_id, %actor, %error, "Failed to unregister unavailable Desktop actor");
                    last_error = Some(error);
                }
            }
        }
        if attempted > 0
            && succeeded == 0
            && let Some(error) = last_error
        {
            return Err(error);
        }
        Ok(health)
    }

    async fn build_actor_snapshot(
        &self,
        actor: ActorId,
        scan_limit: usize,
        per_project_limit: usize,
    ) -> Value {
        let page = match self.actor_conversation_snapshot(actor, scan_limit).await {
            Ok(page) => page,
            Err(error) => {
                warn!(%actor, %error, "Failed to list actor conversations for snapshot");
                ActorConversationPage {
                    conversations: Vec::new(),
                    complete: false,
                }
            }
        };
        let projects = build_actor_project_summary(
            &page.conversations,
            per_project_limit,
            page.complete,
            &self.config.codex.chat_workspaces_dir,
        );
        json!({
            "desktop_id": self.config.desktop_id,
            "display_name": self.config.display_name,
            "actor_id": actor,
            "available": true,
            "protocol_versions": [1],
            "required_skill": "coworker-desktop",
            "snapshot_policy": {
                "conversation_limit_per_project": per_project_limit,
                "scan_conversation_limit": scan_limit,
            },
            "conversation_scan": {
                "complete": page.complete,
                "matched_conversation_count": page.conversations.len(),
            },
            "projects": projects,
        })
    }

    async fn publish_actor_snapshot(
        &self,
        coworker: &BridgeCoworker,
        actor: ActorId,
        snapshot: &Value,
    ) -> Result<()> {
        let registration = self.ensure_registered(coworker, actor).await?;
        let key = (coworker.coworker_id.clone(), actor);
        let fingerprint = snapshot.to_string();
        let heartbeat_seconds = self
            .config
            .codex
            .snapshot_interval_seconds
            .saturating_mul(10)
            .clamp(30, 300);
        let unchanged = self
            .published_snapshots
            .lock()
            .await
            .get(&key)
            .is_some_and(|published| {
                published.fingerprint == fingerprint
                    && published.registration_id == registration.registration_id
                    && published.sent_at.elapsed() < Duration::from_secs(heartbeat_seconds)
            });
        if unchanged {
            return Ok(());
        }
        if let Err(error) = self
            .post_actor_event(
                &coworker.coworker_id,
                actor,
                &registration,
                DesktopEventType::ActorSnapshot,
                None,
                snapshot.clone(),
            )
            .await
        {
            self.published_snapshots.lock().await.remove(&key);
            self.registrations.lock().await.remove(&key);
            return Err(error);
        }
        self.published_snapshots.lock().await.insert(
            key,
            PublishedActorSnapshot {
                fingerprint,
                registration_id: registration.registration_id,
                sent_at: Instant::now(),
            },
        );
        Ok(())
    }

    async fn republish_actor_snapshot(&self, coworker_id: &str, actor: ActorId) -> Result<()> {
        self.published_snapshots
            .lock()
            .await
            .remove(&(coworker_id.to_owned(), actor));
        let coworker = self.coworker(coworker_id)?;
        let snapshot = self
            .build_actor_snapshot(
                actor,
                self.config.codex.snapshot_scan_thread_limit.max(1),
                self.config.codex.snapshot_thread_limit.max(1),
            )
            .await;
        self.publish_actor_snapshot(&coworker, actor, &snapshot)
            .await
    }

    pub async fn send_local_message(
        &self,
        coworker_id: &str,
        conversation_id: Option<&str>,
        content: &str,
        attachment_paths: &[String],
    ) -> Result<Value> {
        self.send_direct_actor_message(
            ActorId::Local,
            coworker_id,
            conversation_id,
            content,
            attachment_paths,
        )
        .await
    }

    pub async fn send_actor_coworker_message(
        &self,
        actor: ActorId,
        coworker_id: &str,
        conversation_id: Option<&str>,
        content: &str,
        attachment_paths: &[String],
    ) -> Result<Value> {
        self.send_direct_actor_message(
            actor,
            coworker_id,
            conversation_id,
            content,
            attachment_paths,
        )
        .await
    }

    async fn send_direct_actor_message(
        &self,
        actor: ActorId,
        coworker_id: &str,
        conversation_id: Option<&str>,
        content: &str,
        attachment_paths: &[String],
    ) -> Result<Value> {
        if actor == ActorId::Local && !self.config.local_enabled {
            return Err(BridgeError::message("Local chat actor is disabled"));
        }
        if actor == ActorId::Codex && conversation_id.is_none() {
            return Err(BridgeError::message(
                "An existing Codex conversation is required",
            ));
        }
        let conversation_id = conversation_id
            .filter(|value| !value.trim().is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| new_compact_id("local_"));
        let coworker = self.coworker(coworker_id)?;
        let registration = self.ensure_registered(&coworker, actor).await?;
        let attachments = self.desktop_attachment_payload(attachment_paths)?;
        let mut envelope = DesktopEnvelopeV1::new(
            DesktopEventType::ThreadEvent,
            json!({
                "message": content,
                "attachments": attachments,
            }),
        );
        envelope.conversation_id = Some(conversation_id.clone());
        self.store.enqueue(coworker_id, &envelope)?;
        let token = self.bearer_token(coworker_id);
        let queued = match self
            .http
            .post_desktop_envelope(
                &coworker,
                &registration.participant_id,
                &envelope,
                token,
                self.config.security.development_mode,
            )
            .await
        {
            Ok(ack) if ack.accepted => {
                self.store.acknowledge(&envelope.message_id)?;
                false
            }
            Ok(_) => {
                self.store
                    .mark_dead_letter(&envelope.message_id, "Coworker rejected desktop message")?;
                return Err(BridgeError::message("Coworker rejected desktop message"));
            }
            Err(error) => match delivery_failure(&error) {
                DeliveryFailure::Retry => {
                    self.store
                        .schedule_retry(&envelope.message_id, &error.to_string())?;
                    true
                }
                DeliveryFailure::DeadLetter => {
                    self.store
                        .mark_dead_letter(&envelope.message_id, &error.to_string())?;
                    return Err(error);
                }
            },
        };
        if actor == ActorId::Codex {
            let target_label = if coworker.display_name.trim().is_empty() {
                coworker_id
            } else {
                coworker.display_name.as_str()
            };
            let author_label = format!("本机 → {target_label}");
            self.adapter(actor)?
                .record_external_message(
                    &conversation_id,
                    ActorMessageInput {
                        message_id: Some(&envelope.message_id),
                        author_kind: "local",
                        author_id: Some(&self.config.desktop_id),
                        author_label: Some(&author_label),
                        coworker_id: Some(coworker_id),
                        content,
                        attachment_paths,
                        project_path: None,
                        mode: None,
                    },
                )
                .await?;
        } else {
            self.store.append_message(
                &envelope.message_id,
                actor,
                &conversation_id,
                coworker_id,
                "local",
                content,
                &json!({"attachments": actor_attachment_metadata(attachment_paths)}),
            )?;
        }
        Ok(json!({
            "actor_id": actor,
            "conversation_id": conversation_id,
            "message_id": envelope.message_id,
            "queued": queued,
        }))
    }

    pub async fn list_conversations(
        &self,
        limit: usize,
    ) -> Result<Vec<crate::actor::ActorConversation>> {
        let mut conversations = self.store.list_local_conversations(limit)?;
        for actor in [ActorId::Codex, ActorId::Claude] {
            if let Some(adapter) = self.adapters.get(&actor)
                && adapter.health().await.available
            {
                conversations.extend(self.actor_conversations(actor, limit).await?);
            }
        }
        for conversation in &mut conversations {
            conversation.mode = self
                .store
                .conversation_mode(conversation.actor_id, &conversation.conversation_id)?;
        }
        conversations.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
        conversations.truncate(limit);
        Ok(conversations)
    }

    pub async fn list_actor_conversations(
        &self,
        actor: ActorId,
        limit: usize,
    ) -> Result<Vec<crate::actor::ActorConversation>> {
        let mut conversations = self.actor_conversations(actor, limit).await?;
        for conversation in &mut conversations {
            conversation.mode = self
                .store
                .conversation_mode(actor, &conversation.conversation_id)?
                .or_else(|| conversation.mode.clone());
        }
        conversations.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
        conversations.truncate(limit.max(1));
        Ok(conversations)
    }

    pub async fn send_actor_message(
        &self,
        actor: ActorId,
        coworker_id: Option<&str>,
        conversation_id: Option<&str>,
        content: &str,
        project_path: Option<&str>,
        mode: Option<&str>,
        attachment_paths: &[String],
    ) -> Result<Value> {
        if actor == ActorId::Local {
            let stored_coworker_id = conversation_id
                .map(|id| self.store.conversation_coworker_id(actor, id))
                .transpose()?
                .flatten();
            let coworker_id = stored_coworker_id
                .as_deref()
                .or(coworker_id)
                .filter(|value| !value.trim().is_empty())
                .or_else(|| {
                    self.config
                        .codex
                        .coworkers
                        .first()
                        .map(|value| value.coworker_id.as_str())
                })
                .ok_or_else(|| BridgeError::message("A Coworker is required for local chat"))?;
            let response = self
                .send_local_message(coworker_id, conversation_id, content, attachment_paths)
                .await?;
            return Ok(response);
        }
        let adapter = self.adapter(actor)?;
        if !adapter.health().await.available {
            return Err(BridgeError::message(format!(
                "{actor} actor is unavailable"
            )));
        }
        if actor == ActorId::Claude
            && let Some(conversation_id) = conversation_id
            && !self
                .store
                .conversation_is_writable(actor, conversation_id)?
        {
            return Err(BridgeError::message(
                "Claude history not created by CoWorker Desktop is read-only",
            ));
        }
        let stored_mode = conversation_id
            .map(|id| self.store.conversation_mode(actor, id))
            .transpose()?
            .flatten();
        let actor_content = content_with_actor_attachments(content, attachment_paths);
        let message_id = Uuid::new_v4().to_string();
        let project_path = resolve_actor_project_path(
            actor,
            conversation_id,
            project_path,
            &self.config.codex.chat_workspaces_dir,
            &message_id,
        )?;
        let received_at = chrono::Utc::now();
        let message_metadata = json!({
            "attachments": actor_attachment_metadata(attachment_paths),
            "native_content": actor_content.clone(),
        });
        // Persist an input for an existing conversation before waiting for the
        // actor. Long Claude turns and concurrent Coworker input must not make
        // an already received message disappear until the turn completes.
        if actor == ActorId::Claude
            && let Some(conversation_id) = conversation_id
        {
            self.store.append_message_at(
                &message_id,
                actor,
                conversation_id,
                coworker_id.unwrap_or(""),
                "local",
                content,
                &message_metadata,
                received_at,
            )?;
        }
        let response = adapter
            .send_message(
                conversation_id,
                ActorMessageInput {
                    message_id: None,
                    author_kind: "local",
                    author_id: Some(if actor == ActorId::Codex {
                        &self.config.codex.codex_id
                    } else {
                        &self.config.desktop_id
                    }),
                    author_label: Some("本机"),
                    coworker_id,
                    content,
                    attachment_paths,
                    project_path: project_path.as_deref(),
                    mode: mode.or(stored_mode.as_deref()),
                },
            )
            .await?;
        if actor == ActorId::Claude {
            let next_id = response
                .get("conversation_id")
                .and_then(Value::as_str)
                .ok_or_else(|| BridgeError::message("Claude response missing conversation_id"))?;
            if let Some(mode) = mode {
                self.store.set_conversation_mode(actor, next_id, mode)?;
            }
            if conversation_id.is_none() {
                self.store.append_message_at(
                    &message_id,
                    actor,
                    next_id,
                    coworker_id.unwrap_or(""),
                    "local",
                    content,
                    &message_metadata,
                    received_at,
                )?;
            }
            if let Some(result) = response
                .get("result")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
            {
                self.store.append_message(
                    &Uuid::new_v4().to_string(),
                    actor,
                    next_id,
                    coworker_id.unwrap_or(""),
                    "assistant",
                    result,
                    &json!({"local_only": true}),
                )?;
            }
        }
        Ok(response)
    }

    fn desktop_attachment_payload(&self, paths: &[String]) -> Result<Value> {
        if paths.len() > self.config.codex.attachment_max_count {
            return Err(BridgeError::message("attachments count exceeds limit"));
        }
        let mut attachments = Vec::with_capacity(paths.len());
        for path in paths {
            let bytes = std::fs::read(path)?;
            if bytes.len() as u64 > self.config.codex.attachment_max_bytes {
                return Err(BridgeError::message(format!(
                    "attachment exceeds size limit: {path}"
                )));
            }
            attachments.push(json!({
                "filename": Path::new(path).file_name().and_then(|value| value.to_str()).unwrap_or("attachment"),
                "media_type": actor_media_type(path),
                "data": BASE64.encode(bytes),
            }));
        }
        Ok(Value::Array(attachments))
    }

    pub async fn load_messages(
        &self,
        actor: ActorId,
        conversation_id: &str,
        before_cursor: Option<&str>,
        page_size: usize,
    ) -> Result<ActorMessagePage> {
        if actor == ActorId::Codex {
            return self
                .adapter(actor)?
                .load_messages(conversation_id, before_cursor, page_size)
                .await;
        }
        let skip = before_cursor
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0);
        let target = skip.saturating_add(page_size.max(1));
        let stored = self
            .store
            .list_messages(actor, conversation_id, target.saturating_add(1))?;
        let messages = if actor == ActorId::Claude {
            crate::claude::merge_history_messages(
                &self.config.claude,
                conversation_id,
                stored,
                target.saturating_add(1),
            )
        } else {
            stored
        };
        let end = messages.len().saturating_sub(skip);
        let start = end.saturating_sub(page_size.max(1));
        Ok(ActorMessagePage {
            messages: messages[start..end].to_vec(),
            next_before_cursor: (start > 0).then(|| skip.saturating_add(end - start).to_string()),
        })
    }

    pub async fn set_actor_mode(
        &self,
        actor: ActorId,
        conversation_id: &str,
        mode: &str,
    ) -> Result<Value> {
        let valid = match actor {
            ActorId::Claude => matches!(
                mode,
                "default" | "acceptEdits" | "plan" | "bypassPermissions"
            ),
            ActorId::Codex => matches!(mode, "default" | "plan"),
            ActorId::Local => false,
        };
        if !valid {
            return Err(BridgeError::message(
                "unsupported conversation mode for actor",
            ));
        }
        if actor == ActorId::Codex {
            self.adapter(actor)?.set_mode(conversation_id, mode).await?;
        }
        self.store
            .set_conversation_mode(actor, conversation_id, mode)?;
        Ok(json!({"actor_id": actor, "conversation_id": conversation_id, "mode": mode}))
    }

    pub async fn rename_actor_conversation(
        &self,
        actor: ActorId,
        conversation_id: &str,
        title: &str,
    ) -> Result<Value> {
        let title = title.trim();
        if title.is_empty() {
            return Err(BridgeError::message("conversation title is required"));
        }
        if actor == ActorId::Codex {
            self.adapter(actor)?
                .rename_conversation(conversation_id, title)
                .await?;
        } else {
            self.store
                .rename_conversation(actor, conversation_id, title)?;
        }
        Ok(json!({"actor_id": actor, "conversation_id": conversation_id, "title": title}))
    }

    async fn actor_conversations(
        &self,
        actor: ActorId,
        limit: usize,
    ) -> Result<Vec<crate::actor::ActorConversation>> {
        Ok(self
            .actor_conversation_snapshot(actor, limit)
            .await?
            .conversations)
    }

    async fn actor_conversation_snapshot(
        &self,
        actor: ActorId,
        limit: usize,
    ) -> Result<ActorConversationPage> {
        let limit = limit.max(1);
        if actor == ActorId::Local {
            let mut conversations = self
                .store
                .list_stored_conversations(actor, limit.saturating_add(1))?;
            let complete = conversations.len() <= limit;
            conversations.truncate(limit);
            return Ok(ActorConversationPage {
                conversations,
                complete,
            });
        }
        let mut page = self.adapter(actor)?.conversation_snapshot(limit).await?;
        if actor == ActorId::Codex {
            page.conversations
                .sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
            page.conversations.truncate(limit);
            return Ok(page);
        }
        let owned = self
            .store
            .list_stored_conversations(actor, limit.saturating_add(1))?;
        let owned_complete = owned.len() <= limit;
        for conversation in owned {
            if let Some(existing) = page
                .conversations
                .iter_mut()
                .find(|item| item.conversation_id == conversation.conversation_id)
            {
                existing.writable = conversation.writable;
                existing.updated_at = conversation.updated_at;
                if conversation.title != "搭档会话" {
                    existing.title = conversation.title;
                }
                existing.mode = self
                    .store
                    .conversation_mode(actor, &existing.conversation_id)?;
            } else {
                let mut conversation = conversation;
                conversation.mode = self
                    .store
                    .conversation_mode(actor, &conversation.conversation_id)?;
                page.conversations.push(conversation);
            }
        }
        page.conversations
            .sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
        page.complete &= owned_complete && page.conversations.len() <= limit;
        page.conversations.truncate(limit);
        Ok(page)
    }

    pub fn pending_approvals(&self) -> Result<Vec<crate::conversation_store::ApprovalRequest>> {
        let result = self.store.pending_approvals();
        if let Ok(ref items) = result {
            info!(count = items.len(), "pending_approvals query");
        }
        result
    }

    pub async fn resolve_approval(
        &self,
        request_id: &str,
        actor: ActorId,
        conversation_id: &str,
        coworker_id: &str,
        response: &Value,
    ) -> Result<crate::conversation_store::ResolveApprovalResult> {
        use crate::conversation_store::ResolveApprovalResult;
        let is_user_input = actor == ActorId::Claude
            && self
                .store
                .approval(request_id)?
                .is_some_and(|request| request.tool_name == "AskUserQuestion");
        let changed_event_type = if is_user_input {
            "desktop.user_input.changed"
        } else {
            "desktop.approval.changed"
        };
        info!(
            %request_id,
            %actor,
            %conversation_id,
            %coworker_id,
            desktop_id = %self.config.desktop_id,
            "resolve_approval called"
        );
        if self.store.resolve_approval(
            request_id,
            actor,
            conversation_id,
            coworker_id,
            &self.config.desktop_id,
            response,
        )? {
            info!(%request_id, %actor, %changed_event_type, "resolve_approval: SQLite UPDATE succeeded");
            publish_actor_stream_event(ActorStreamEvent {
                actor_id: actor,
                conversation_id: conversation_id.to_owned(),
                message_id: None,
                event: json!({
                    "type": changed_event_type,
                    "request_id": request_id,
                    "actor_id": actor.as_str(),
                    "status": "resolved",
                    "resolver": "desktop",
                }),
            });

            // For Codex approvals, also forward the resolution to the bridge's
            // in-memory oneshot channel so `await_approval_response` unblocks
            // immediately instead of waiting for the timeout.  Codex bridge
            // expects `{ decision: "accept"/"decline" }` so we must translate
            // from the `behavior` format used by the Desktop UI.
            if actor == ActorId::Codex {
                let codex_response = if response.get("behavior") == Some(&json!("allow"))
                    || response.get("decision") == Some(&json!("accept"))
                    || response.get("decision") == Some(&json!("approved"))
                {
                    json!({"decision": "accept"})
                } else {
                    json!({"decision": "decline"})
                };
                if let Err(error) = self
                    .adapter(actor)?
                    .resolve_request(coworker_id, conversation_id, request_id, codex_response)
                    .await
                {
                    warn!(
                        %request_id,
                        %error,
                        "Failed to forward Codex approval resolution to bridge"
                    );
                }
            }

            Ok(ResolveApprovalResult {
                ok: true,
                reason: None,
            })
        } else {
            info!(%request_id, %actor, "resolve_approval: SQLite UPDATE matched 0 rows (already resolved or expired)");
            Ok(ResolveApprovalResult {
                ok: false,
                reason: Some("already_resolved".to_owned()),
            })
        }
    }

    pub async fn run_until_shutdown(
        self: Arc<Self>,
        mut shutdown: oneshot::Receiver<()>,
        mut outbound: mpsc::Receiver<ActorOutboundRequest>,
    ) -> Result<()> {
        loop {
            match self.sync_registrations().await {
                Ok(_) => break,
                Err(error) => {
                    warn!(%error, "Desktop registration failed; retrying without stopping available local actors");
                    tokio::select! {
                        _ = &mut shutdown => return Ok(()),
                        _ = sleep(Duration::from_secs(self.config.codex.reconnect_seconds.max(1))) => {}
                    }
                }
            }
        }
        let registrations = self.registrations.lock().await.clone();
        let mut tasks = Vec::new();
        for ((coworker_id, actor), registration) in registrations {
            let router = Arc::clone(&self);
            tasks.push(tokio::spawn(async move {
                router
                    .actor_stream_loop(coworker_id, actor, registration)
                    .await
            }));
        }
        let retry_router = Arc::clone(&self);
        tasks.push(tokio::spawn(async move {
            retry_router.outbox_retry_loop().await
        }));
        let snapshot_router = Arc::clone(&self);
        tasks.push(tokio::spawn(async move {
            snapshot_router.snapshot_refresh_loop().await
        }));
        let outbound_router = Arc::clone(&self);
        tasks.push(tokio::spawn(async move {
            while let Some(request) = outbound.recv().await {
                let result = outbound_router
                    .send_outbound(
                        request.actor_id,
                        &request.coworker_id,
                        request.conversation_id.as_deref(),
                        request.event_type,
                        request.payload,
                    )
                    .await;
                let _ = request.response.send(result);
            }
            Ok(())
        }));
        let _ = (&mut shutdown).await;
        for task in tasks {
            task.abort();
            let _ = task.await;
        }
        Ok(())
    }

    async fn send_outbound(
        &self,
        actor: ActorId,
        coworker_id: &str,
        conversation_id: Option<&str>,
        event_type: DesktopEventType,
        payload: Value,
    ) -> Result<()> {
        // Intercept Codex approval requests and mirror them into the SQLite
        // pending-approvals table so they appear in the Desktop UI alongside
        // Claude approvals.  The event is still forwarded to the Coworker so
        // the remote side can also review it.
        if actor == ActorId::Codex && event_type == DesktopEventType::ApprovalRequested {
            self.mirror_codex_approval(coworker_id, conversation_id, &payload);
        }
        let coworker = self.coworker(coworker_id)?;
        let registration = self.ensure_registered(&coworker, actor).await?;
        self.post_actor_event(
            coworker_id,
            actor,
            &registration,
            event_type,
            conversation_id,
            payload,
        )
        .await
    }

    /// Write a Codex approval request into the SQLite `approval_requests` table
    /// so the Desktop UI can display it.  The `method` field is mapped to
    /// `tool_name` (e.g. `item/commandExecution/requestApproval` →
    /// `commandExecution`) and `params` becomes the tool `input`.
    fn mirror_codex_approval(
        &self,
        coworker_id: &str,
        conversation_id: Option<&str>,
        payload: &Value,
    ) {
        let server_request_id = payload
            .get("server_request_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let method = payload
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        // Derive a short tool_name from the method path, e.g.
        // "item/commandExecution/requestApproval" → "commandExecution"
        let tool_name = method.split('/').nth(1).unwrap_or(method);
        let params = payload
            .get("params")
            .cloned()
            .unwrap_or(Value::Object(serde_json::Map::new()));
        let request_id = if server_request_id.is_empty() {
            new_compact_id("req_")
        } else {
            server_request_id.to_owned()
        };
        let timeout_seconds = self.config.codex.approval_timeout_seconds.max(1);
        let request = crate::conversation_store::ApprovalRequest {
            request_id: request_id.clone(),
            actor_id: ActorId::Codex,
            conversation_id: conversation_id.unwrap_or_default().to_owned(),
            coworker_id: coworker_id.to_owned(),
            owner_id: self.config.desktop_id.clone(),
            tool_name: tool_name.to_owned(),
            input: params,
            status: "pending".to_owned(),
            response: None,
            expires_at: chrono::Utc::now() + chrono::Duration::seconds(timeout_seconds as i64),
            server_request_id: Some(server_request_id.to_owned()),
        };
        if let Err(error) = self.store.create_approval(&request) {
            warn!(
                %request_id,
                %error,
                "Failed to mirror Codex approval request into SQLite"
            );
        }
    }

    async fn snapshot_refresh_loop(self: Arc<Self>) -> Result<()> {
        loop {
            sleep(Duration::from_secs(
                self.config.codex.snapshot_interval_seconds.max(1),
            ))
            .await;
            if let Err(error) = self.sync_registrations().await {
                warn!(%error, "Failed to refresh Desktop actor snapshots");
            }
        }
    }

    async fn outbox_retry_loop(self: Arc<Self>) -> Result<()> {
        loop {
            for delivery in self.store.pending_deliveries(100)? {
                if !self
                    .config
                    .codex
                    .coworkers
                    .iter()
                    .any(|coworker| coworker.coworker_id == delivery.coworker_id)
                {
                    continue;
                }
                let actor = delivery
                    .envelope
                    .payload
                    .get("actor_id")
                    .and_then(Value::as_str)
                    .and_then(parse_actor_id)
                    .unwrap_or(ActorId::Local);
                let result = async {
                    let coworker = self.coworker(&delivery.coworker_id)?;
                    let registration = self.ensure_registered(&coworker, actor).await?;
                    self.http
                        .post_desktop_envelope(
                            &coworker,
                            &registration.participant_id,
                            &delivery.envelope,
                            self.bearer_token(&delivery.coworker_id),
                            self.config.security.development_mode,
                        )
                        .await
                }
                .await;
                match result {
                    Ok(ack) if ack.accepted => self.store.acknowledge(&delivery.message_id)?,
                    Ok(_) => self.store.mark_dead_letter(
                        &delivery.message_id,
                        "Coworker rejected desktop message",
                    )?,
                    Err(error) => match delivery_failure(&error) {
                        DeliveryFailure::Retry => self
                            .store
                            .schedule_retry(&delivery.message_id, &error.to_string())?,
                        DeliveryFailure::DeadLetter => self
                            .store
                            .mark_dead_letter(&delivery.message_id, &error.to_string())?,
                    },
                }
            }
            sleep(Duration::from_secs(1)).await;
        }
    }

    /// Pump inbound Desktop messages from a stream. Regular messages (no
    /// `extra.operation`) are processed sequentially by a dedicated worker so
    /// per-conversation ordering and turn side effects are preserved. Control
    /// operations (`resolve_request`, `set_conversation_mode`,
    /// `list_conversations`, `check_desktop_update`) are dispatched
    /// concurrently so they never wait behind an in-flight turn.
    ///
    /// The split is what unblocks Claude approvals: a Claude turn blocks the
    /// regular worker while it polls SQLite for an approval, and the coworker's
    /// `resolve_request` for that very approval is a control operation that
    /// must be able to write the resolution *during* the turn instead of after
    /// the approval times out.
    async fn run_actor_message_stream(
        self: Arc<Self>,
        coworker_id: String,
        actor: ActorId,
        registration: CoworkerRegistration,
        mut rx: mpsc::Receiver<String>,
    ) {
        let (regular_tx, mut regular_rx) = mpsc::channel::<String>(128);
        let worker_router = Arc::clone(&self);
        let worker_coworker_id = coworker_id.clone();
        let worker_registration = registration.clone();
        let worker = tokio::spawn(async move {
            while let Some(message) = regular_rx.recv().await {
                worker_router
                    .handle_incoming_or_report(
                        &worker_coworker_id,
                        actor,
                        &worker_registration,
                        &message,
                    )
                    .await;
            }
        });

        while let Some(message) = rx.recv().await {
            if is_control_operation(&message) {
                let router = Arc::clone(&self);
                let coworker_id = coworker_id.clone();
                let registration = registration.clone();
                tokio::spawn(async move {
                    router
                        .handle_incoming_or_report(&coworker_id, actor, &registration, &message)
                        .await;
                });
            } else if regular_tx.send(message).await.is_err() {
                // Worker exited; stop draining the stream.
                break;
            }
        }
        drop(regular_tx);
        let _ = worker.await;
    }

    async fn handle_incoming_or_report(
        &self,
        coworker_id: &str,
        actor: ActorId,
        registration: &CoworkerRegistration,
        message: &str,
    ) {
        if let Err(error) = self
            .handle_incoming(coworker_id, actor, registration, message)
            .await
        {
            warn!(%error, %actor, %coworker_id, "Desktop actor message failed");
            let _ = self
                .post_actor_event(
                    coworker_id,
                    actor,
                    registration,
                    DesktopEventType::Error,
                    None,
                    json!({"message": error.to_string()}),
                )
                .await;
        }
    }

    async fn actor_stream_loop(
        self: Arc<Self>,
        coworker_id: String,
        actor: ActorId,
        registration: CoworkerRegistration,
    ) -> Result<()> {
        loop {
            let coworker = self.coworker(&coworker_id)?;
            let (tx, rx) = mpsc::channel(128);
            let (connected_tx, connected_rx) = oneshot::channel();
            let http = self.http.clone();
            let participant_id = registration.participant_id.clone();
            let token = self.bearer_token(&coworker_id).map(str::to_owned);
            let development_mode = self.config.security.development_mode;
            let stream = AbortOnDrop::new(tokio::spawn(async move {
                http.consume_desktop_sse_once(
                    coworker,
                    participant_id,
                    token.as_deref(),
                    development_mode,
                    tx,
                    connected_tx,
                )
                .await
            }));
            if connected_rx.await.is_ok()
                && let Err(error) = self.republish_actor_snapshot(&coworker_id, actor).await
            {
                warn!(%error, %actor, %coworker_id, "Failed to republish Desktop actor snapshot after SSE reconnect");
            }
            self.clone()
                .run_actor_message_stream(coworker_id.clone(), actor, registration.clone(), rx)
                .await;
            let _ = stream.join().await;
            sleep(Duration::from_secs(
                self.config.codex.reconnect_seconds.max(1),
            ))
            .await;
        }
    }

    async fn handle_incoming(
        &self,
        coworker_id: &str,
        actor: ActorId,
        registration: &CoworkerRegistration,
        message: &str,
    ) -> Result<()> {
        let command: Value = serde_json::from_str(message)?;
        let mapping = command
            .as_object()
            .ok_or_else(|| BridgeError::message("Desktop command must be an object"))?;
        let content = mapping
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let conversation_id = mapping
            .get("conversation_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        let extra = mapping.get("extra").and_then(Value::as_object);
        let bubble_metadata = extra
            .and_then(|value| value.get("bubble"))
            .filter(|value| value.is_object())
            .cloned();
        let request_id = extra
            .and_then(|value| value.get("request_id"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| new_compact_id("req_"));
        let incoming = DesktopEnvelopeV1 {
            protocol_version: 1,
            message_id: Uuid::parse_str(&request_id)
                .map(|value| value.to_string())
                .unwrap_or_else(|_| Uuid::new_v4().to_string()),
            request_id: Some(request_id.clone()),
            conversation_id: conversation_id.map(str::to_owned),
            created_at: chrono::Utc::now(),
            event_type: DesktopEventType::Command,
            payload: command.clone(),
        };
        let receipt = self.store.remember_inbox(&incoming)?;
        info!(
            %actor,
            %coworker_id,
            message_id = %incoming.message_id,
            %request_id,
            conversation_id = conversation_id.unwrap_or("<new>"),
            operation = extra
                .and_then(|value| value.get("operation"))
                .and_then(|value| value.as_str())
                .unwrap_or("message"),
            duplicate = receipt.duplicate,
            "Received Desktop actor message"
        );
        if receipt.duplicate {
            // The original result is already protected by the persistent
            // outbox. Emitting a new result here makes de-duplication visible
            // as a second message and gives it a different message_id.
            return Ok(());
        }
        // Receiving a command reserves its id for de-duplication while it is in
        // flight. Failed actor turns release it so an explicit retry with the
        // same request_id can run the command again.
        let mut inbox_reservation =
            InboxReservation::new(Arc::clone(&self.store), incoming.message_id.clone());

        match extra
            .and_then(|value| value.get("operation"))
            .and_then(Value::as_str)
        {
            Some("check_desktop_update") => {
                publish_actor_stream_event(ActorStreamEvent {
                    actor_id: actor,
                    conversation_id: String::new(),
                    message_id: Some(incoming.message_id.clone()),
                    event: json!({
                        "type": "desktop_update_check_requested",
                        "published_version": extra
                            .and_then(|value| value.get("published_version"))
                            .and_then(Value::as_str),
                    }),
                });
            }
            Some("list_conversations") => {
                let conversations = self.actor_conversations(actor, 200).await?;
                self.post_actor_event(
                    coworker_id,
                    actor,
                    registration,
                    DesktopEventType::CommandResult,
                    conversation_id,
                    json!({
                        "request_id": request_id,
                        "ok": true,
                        "conversations": conversations,
                    }),
                )
                .await?;
            }
            Some("set_conversation_mode") => {
                if actor == ActorId::Local {
                    return Err(BridgeError::message(
                        "local conversations do not have an AI mode",
                    ));
                }
                let conversation_id = conversation_id
                    .ok_or_else(|| BridgeError::message("conversation_id is required"))?;
                let mode = extra
                    .and_then(|value| value.get("mode"))
                    .and_then(Value::as_str)
                    .ok_or_else(|| BridgeError::message("mode is required"))?;
                let valid = match actor {
                    ActorId::Claude => matches!(
                        mode,
                        "default" | "acceptEdits" | "plan" | "bypassPermissions"
                    ),
                    ActorId::Codex => matches!(mode, "default" | "plan"),
                    ActorId::Local => false,
                };
                if !valid {
                    return Err(BridgeError::message(
                        "unsupported conversation mode for actor",
                    ));
                }
                if actor == ActorId::Codex {
                    self.adapter(actor)?.set_mode(conversation_id, mode).await?;
                }
                self.store
                    .set_conversation_mode(actor, conversation_id, mode)?;
                self.post_actor_event(
                    coworker_id,
                    actor,
                    registration,
                    DesktopEventType::CommandResult,
                    Some(conversation_id),
                    json!({
                        "request_id": request_id, "ok": true, "mode": mode,
                    }),
                )
                .await?;
            }
            Some("resolve_request") => {
                let approval_request_id = extra
                    .and_then(|value| {
                        value
                            .get("user_input_request_id")
                            .or_else(|| value.get("approval_request_id"))
                            .or_else(|| value.get("server_request_id"))
                            .or_else(|| value.get("request_id"))
                    })
                    .and_then(Value::as_str)
                    .ok_or_else(|| BridgeError::message("approval_request_id is required"))?;
                let stored_approval = self.store.approval(approval_request_id)?;
                let conversation_id = conversation_id
                    .or_else(|| {
                        stored_approval
                            .as_ref()
                            .filter(|request| {
                                request.actor_id == actor
                                    && request.coworker_id == coworker_id
                                    && request.owner_id == self.config.desktop_id
                            })
                            .map(|request| request.conversation_id.as_str())
                    })
                    .ok_or_else(|| BridgeError::message("conversation_id is required"))?;

                let is_user_input = actor == ActorId::Claude
                    && stored_approval
                        .as_ref()
                        .is_some_and(|request| request.tool_name == "AskUserQuestion");
                let (response, is_approved, resolution) = if is_user_input {
                    let decision = extra
                        .and_then(|value| value.get("decision"))
                        .and_then(Value::as_str);
                    if matches!(decision, Some("decline" | "denied")) {
                        (
                            json!({
                                "behavior": "deny",
                                "message": "Coworker declined to answer Claude's question"
                            }),
                            false,
                            "declined",
                        )
                    } else {
                        let answers = extra
                            .and_then(|value| value.get("answers"))
                            .and_then(Value::as_object)
                            .filter(|answers| {
                                !answers.is_empty() && answers.values().all(Value::is_string)
                            })
                            .ok_or_else(|| {
                                BridgeError::message(
                                    "answers must be a non-empty object of question-to-string entries",
                                )
                            })?;
                        let stored_request = stored_approval
                            .as_ref()
                            .expect("AskUserQuestion request was loaded");
                        let all_questions_answered = stored_request
                            .input
                            .get("questions")
                            .and_then(Value::as_array)
                            .is_some_and(|questions| {
                                !questions.is_empty()
                                    && questions.iter().all(|question| {
                                        question
                                            .get("question")
                                            .and_then(Value::as_str)
                                            .is_some_and(|text| answers.contains_key(text))
                                    })
                            });
                        if !all_questions_answered {
                            return Err(BridgeError::message(
                                "answers must include every AskUserQuestion question",
                            ));
                        }
                        let mut updated_input = stored_request.input.clone();
                        updated_input
                            .as_object_mut()
                            .ok_or_else(|| {
                                BridgeError::message("AskUserQuestion input must be an object")
                            })?
                            .insert("answers".to_owned(), Value::Object(answers.clone()));
                        (
                            json!({"behavior": "allow", "updatedInput": updated_input}),
                            true,
                            "answered",
                        )
                    }
                } else {
                    // Approval commands use one actor-neutral input contract;
                    // the bridge adapts it to each actor's response shape.
                    let decision = extra
                        .and_then(|value| value.get("decision"))
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BridgeError::message("decision is required (\"accept\" or \"decline\")")
                        })?;
                    let is_approved = matches!(decision, "accept" | "approved");
                    let response = if is_approved {
                        json!({"behavior": "allow", "decision": "accept"})
                    } else {
                        json!({"behavior": "deny", "decision": "decline"})
                    };
                    (response, is_approved, decision)
                };

                info!(
                    %actor,
                    %coworker_id,
                    %approval_request_id,
                    %conversation_id,
                    %resolution,
                    "resolve_request from Coworker"
                );
                if actor == ActorId::Codex {
                    // Codex bridge expects `{ decision: "accept"/"decline" }`.
                    let codex_response = if is_approved {
                        json!({"decision": "accept"})
                    } else {
                        json!({"decision": "decline"})
                    };
                    self.adapter(actor)?
                        .resolve_request(
                            coworker_id,
                            conversation_id,
                            approval_request_id,
                            codex_response,
                        )
                        .await?;
                    // Also mark the mirrored SQLite row as resolved so the
                    // Desktop UI clears the approval panel immediately.
                    let _ = self.store.resolve_approval(
                        approval_request_id,
                        actor,
                        conversation_id,
                        coworker_id,
                        &self.config.desktop_id,
                        &response,
                    );
                    // Notify the Desktop UI that the Coworker resolved this.
                    publish_actor_stream_event(ActorStreamEvent {
                        actor_id: actor,
                        conversation_id: conversation_id.to_owned(),
                        message_id: None,
                        event: json!({
                            "type": "desktop.approval.changed",
                            "request_id": approval_request_id,
                            "actor_id": actor.as_str(),
                            "status": "resolved",
                            "resolver": "coworker",
                        }),
                    });
                } else if actor == ActorId::Claude {
                    // Claude MCP sidecar polls SQLite for the callback response.
                    let _ = self
                        .resolve_approval(
                            approval_request_id,
                            actor,
                            conversation_id,
                            coworker_id,
                            &response,
                        )
                        .await?;
                } else {
                    return Err(BridgeError::message(
                        "local conversations do not have approval requests",
                    ));
                }
                self.post_actor_event(coworker_id, actor, registration, DesktopEventType::CommandResult, Some(conversation_id), json!({
                    "request_id": request_id, "approval_request_id": approval_request_id, "ok": true,
                })).await?;
            }
            Some(operation) => {
                return Err(BridgeError::message(format!(
                    "unsupported Desktop operation: {operation}"
                )));
            }
            None if actor == ActorId::Local => {
                let conversation_id = conversation_id
                    .map(str::to_owned)
                    .unwrap_or_else(|| new_compact_id("local_"));
                let (_, attachments) = save_incoming_attachments(
                    &self.config.storage_dir,
                    mapping.get("attachments"),
                    &request_id,
                    self.config.codex.attachment_max_count,
                    self.config.codex.attachment_max_bytes,
                )?;
                self.store.append_message(
                    &incoming.message_id,
                    actor,
                    &conversation_id,
                    coworker_id,
                    "coworker",
                    content,
                    &json!({
                        "participant_id": registration.participant_id,
                        "attachments": attachments,
                        "bubble": bubble_metadata.clone(),
                    }),
                )?;
                publish_actor_stream_event(ActorStreamEvent {
                    actor_id: actor,
                    conversation_id: conversation_id.clone(),
                    message_id: Some(incoming.message_id.clone()),
                    event: json!({"type": "conversation_updated"}),
                });
                self.post_actor_event(
                    coworker_id,
                    actor,
                    registration,
                    DesktopEventType::CommandResult,
                    Some(&conversation_id),
                    json!({"request_id": request_id, "ok": true}),
                )
                .await?;
            }
            None => {
                let (attachment_paths, attachments) = save_incoming_attachments(
                    &self.config.storage_dir,
                    mapping.get("attachments"),
                    &request_id,
                    self.config.codex.attachment_max_count,
                    self.config.codex.attachment_max_bytes,
                )?;
                let author_label = self.coworker(coworker_id)?.display_name.clone();
                let author_label = if author_label.trim().is_empty() {
                    "搭档"
                } else {
                    author_label.as_str()
                };
                let actor_content = content_with_actor_attachments(content, &attachment_paths);
                let actor_content = crate::desktop_protocol::actor_model_message(
                    "coworker",
                    Some(coworker_id),
                    Some(author_label),
                    &actor_content,
                )?;
                let message_metadata = json!({
                    "attachments": attachments,
                    "native_content": actor_content.clone(),
                    "bubble": bubble_metadata.clone(),
                });
                if actor == ActorId::Claude
                    && let Some(conversation_id) = conversation_id
                {
                    self.store.append_message_at(
                        &incoming.message_id,
                        actor,
                        conversation_id,
                        coworker_id,
                        "coworker",
                        content,
                        &message_metadata,
                        incoming.created_at,
                    )?;
                    publish_actor_stream_event(ActorStreamEvent {
                        actor_id: actor,
                        conversation_id: conversation_id.to_owned(),
                        message_id: Some(incoming.message_id.clone()),
                        event: json!({"type": "conversation_updated"}),
                    });
                }
                let stored_mode = conversation_id
                    .map(|id| self.store.conversation_mode(actor, id))
                    .transpose()?
                    .flatten();
                let requested_mode = extra
                    .and_then(|value| value.get("mode"))
                    .and_then(Value::as_str);
                let requested_project_path = extra
                    .and_then(|value| value.get("project_path").or_else(|| value.get("cwd")))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|value| !value.is_empty());
                let project_path = resolve_actor_project_path(
                    actor,
                    conversation_id,
                    requested_project_path,
                    &self.config.codex.chat_workspaces_dir,
                    &incoming.message_id,
                )?;
                let adapter = self.adapter(actor)?;
                let response = adapter
                    .send_message(
                        conversation_id,
                        ActorMessageInput {
                            message_id: Some(&incoming.message_id),
                            author_kind: "coworker",
                            author_id: Some(coworker_id),
                            author_label: Some(author_label),
                            coworker_id: Some(coworker_id),
                            content,
                            attachment_paths: &attachment_paths,
                            project_path: project_path.as_deref(),
                            mode: requested_mode.or(stored_mode.as_deref()),
                        },
                    )
                    .await?;
                let next_conversation_id = response
                    .get("conversation_id")
                    .or_else(|| response.get("thread_id"))
                    .and_then(Value::as_str)
                    .or(conversation_id)
                    .ok_or_else(|| {
                        BridgeError::message("Actor response missing conversation id")
                    })?;
                if actor == ActorId::Claude && conversation_id.is_none() {
                    self.store.append_message_at(
                        &incoming.message_id,
                        actor,
                        next_conversation_id,
                        coworker_id,
                        "coworker",
                        content,
                        &message_metadata,
                        incoming.created_at,
                    )?;
                    if let Some(title) = default_conversation_title(content) {
                        self.store
                            .rename_conversation(actor, next_conversation_id, &title)?;
                    }
                }
                if let Some(result) = response
                    .get("result")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    self.store.append_message(
                        &Uuid::new_v4().to_string(),
                        actor,
                        next_conversation_id,
                        coworker_id,
                        "assistant",
                        result,
                        &json!({"local_only": true}),
                    )?;
                }
                // A command acknowledgement is protocol control, not the AI's
                // final answer. The final remains local unless the actor calls
                // send_to_coworker explicitly.
                self.post_actor_event(
                    coworker_id,
                    actor,
                    registration,
                    DesktopEventType::CommandResult,
                    Some(next_conversation_id),
                    json!({"request_id": request_id, "ok": true}),
                )
                .await?;
            }
        }
        inbox_reservation.commit()?;
        Ok(())
    }

    async fn post_actor_event(
        &self,
        coworker_id: &str,
        actor: ActorId,
        registration: &CoworkerRegistration,
        event_type: DesktopEventType,
        conversation_id: Option<&str>,
        payload: Value,
    ) -> Result<()> {
        let coworker = self.coworker(coworker_id)?;
        let request_id = payload
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let mut envelope = DesktopEnvelopeV1::new(event_type, payload);
        envelope.request_id = request_id;
        envelope.conversation_id = conversation_id.map(str::to_owned);
        self.store.enqueue(coworker_id, &envelope)?;
        let result = self
            .http
            .post_desktop_envelope(
                &coworker,
                &registration.participant_id,
                &envelope,
                self.bearer_token(coworker_id),
                self.config.security.development_mode,
            )
            .await;
        match result {
            Ok(ack) if ack.accepted => self.store.acknowledge(&envelope.message_id)?,
            Ok(_) => self
                .store
                .mark_dead_letter(&envelope.message_id, "Coworker rejected desktop event")?,
            Err(error) => match delivery_failure(&error) {
                DeliveryFailure::Retry => {
                    self.store
                        .schedule_retry(&envelope.message_id, &error.to_string())?;
                    warn!(%actor, %coworker_id, %error, "Queued Desktop event for retry");
                }
                DeliveryFailure::DeadLetter => {
                    self.store
                        .mark_dead_letter(&envelope.message_id, &error.to_string())?;
                    return Err(error);
                }
            },
        }
        info!(%actor, %coworker_id, %event_type, "Published Desktop actor event");
        Ok(())
    }

    fn adapter(&self, actor: ActorId) -> Result<&Arc<dyn ActorAdapter>> {
        self.adapters
            .get(&actor)
            .ok_or_else(|| BridgeError::message(format!("{actor} actor is unavailable")))
    }

    async fn ensure_registered(
        &self,
        coworker: &BridgeCoworker,
        actor: ActorId,
    ) -> Result<CoworkerRegistration> {
        let key = (coworker.coworker_id.clone(), actor);
        if let Some(registration) = self.registrations.lock().await.get(&key).cloned() {
            return Ok(registration);
        }
        if let Some(registration) = self.store.registration(&coworker.coworker_id, actor)? {
            match self
                .http
                .list_desktop_registrations(
                    coworker,
                    self.bearer_token(&coworker.coworker_id),
                    self.config.security.development_mode,
                )
                .await
            {
                Ok(registrations)
                    if registrations.iter().any(|remote| {
                        remote.registration_id == registration.registration_id
                            && remote.participant_id == registration.participant_id
                    }) =>
                {
                    self.registrations
                        .lock()
                        .await
                        .insert(key, registration.clone());
                    info!(
                        coworker_id = %coworker.coworker_id,
                        %actor,
                        registration_id = %registration.registration_id,
                        participant_id = %registration.participant_id,
                        "Reusing persisted CoWorker Desktop actor registration"
                    );
                    return Ok(registration);
                }
                Ok(_) => {
                    warn!(
                        coworker_id = %coworker.coworker_id,
                        %actor,
                        registration_id = %registration.registration_id,
                        participant_id = %registration.participant_id,
                        "Persisted CoWorker Desktop actor registration is missing remotely; re-registering"
                    );
                    self.store
                        .remove_registration(&coworker.coworker_id, actor)?;
                }
                Err(error) => {
                    warn!(
                        coworker_id = %coworker.coworker_id,
                        %actor,
                        %error,
                        "Failed to validate persisted CoWorker Desktop actor registration; reusing cached participant"
                    );
                    self.registrations
                        .lock()
                        .await
                        .insert(key, registration.clone());
                    return Ok(registration);
                }
            }
        }
        let display_name = format!("{} {}", self.config.display_name, actor);
        let registration = self
            .http
            .register_desktop_participant(
                coworker,
                &self.config.desktop_id,
                actor,
                &display_name,
                self.bearer_token(&coworker.coworker_id),
                self.config.security.development_mode,
            )
            .await?;
        self.store
            .save_registration(&coworker.coworker_id, actor, &registration)?;
        self.registrations
            .lock()
            .await
            .insert(key, registration.clone());
        info!(coworker_id = %coworker.coworker_id, %actor, "Registered CoWorker Desktop actor");
        Ok(registration)
    }

    async fn unregister(&self, coworker: &BridgeCoworker, actor: ActorId) -> Result<()> {
        let key = (coworker.coworker_id.clone(), actor);
        let registration = self
            .registrations
            .lock()
            .await
            .remove(&key)
            .or(self.store.registration(&coworker.coworker_id, actor)?);
        let Some(registration) = registration else {
            return Ok(());
        };
        match self
            .http
            .delete_desktop_registration(
                coworker,
                &registration.registration_id,
                self.bearer_token(&coworker.coworker_id),
                self.config.security.development_mode,
            )
            .await
        {
            Ok(()) => {
                self.store
                    .remove_registration(&coworker.coworker_id, actor)?;
                info!(coworker_id = %coworker.coworker_id, %actor, "Unregistered unavailable Desktop actor");
            }
            Err(error) => {
                warn!(coworker_id = %coworker.coworker_id, %actor, %error, "Unable to unregister Desktop actor");
                return Err(error);
            }
        }
        Ok(())
    }

    fn coworker(&self, coworker_id: &str) -> Result<BridgeCoworker> {
        self.config
            .codex
            .coworkers
            .iter()
            .find(|item| item.coworker_id == coworker_id)
            .cloned()
            .ok_or_else(|| BridgeError::message(format!("Unknown Coworker: {coworker_id}")))
    }

    fn bearer_token(&self, coworker_id: &str) -> Option<&str> {
        self.config
            .security
            .bearer_tokens
            .get(coworker_id)
            .map(String::as_str)
    }
}

fn build_actor_project_summary(
    conversations: &[ActorConversation],
    per_project_limit: usize,
    source_complete: bool,
    chat_workspaces_dir: &str,
) -> Vec<Value> {
    let mut projects = Vec::new();
    let mut project_indexes = HashMap::new();
    let mut matched_counts = Vec::new();

    for conversation in conversations {
        let (key, project) = actor_conversation_project(conversation, chat_workspaces_dir);
        let index = *project_indexes.entry(key).or_insert_with(|| {
            projects.push(project);
            matched_counts.push(0);
            projects.len() - 1
        });
        matched_counts[index] += 1;
        let recent = projects[index]["recent_conversations"]
            .as_array_mut()
            .expect("recent_conversations array");
        if recent.len() < per_project_limit {
            recent.push(json!(conversation));
        }
    }

    for (index, project) in projects.iter_mut().enumerate() {
        let shown = project["recent_conversations"]
            .as_array()
            .map_or(0, Vec::len);
        let matched = matched_counts[index];
        project["matched_conversation_count"] = json!(matched);
        project["shown_conversation_count"] = json!(shown);
        project["truncated"] = json!(shown < matched);
        project["complete"] = json!(source_complete && shown == matched);
    }
    projects
}

fn actor_conversation_project(
    conversation: &ActorConversation,
    chat_workspaces_dir: &str,
) -> (String, Value) {
    let path = conversation
        .project_path
        .as_deref()
        .map(str::trim)
        .filter(|path| !path.is_empty());
    let project_id = conversation
        .project_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let project_name = conversation
        .project_name
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if path.is_some_and(|path| actor_path_is_within(path, chat_workspaces_dir))
        || (path.is_none() && project_id.is_none() && project_name.is_none())
    {
        return (
            "__conversation__".to_owned(),
            json!({
                "project_id": "no-project",
                "name": "对话",
                "scope": "conversation",
                "recent_conversations": [],
            }),
        );
    }

    let normalized_path = path.map(normalize_actor_path);
    let stable_id = project_id
        .map(str::to_owned)
        .or_else(|| normalized_path.as_deref().map(actor_path_key))
        .unwrap_or_else(|| project_name.unwrap_or("unknown").to_owned());
    let name = project_name
        .map(str::to_owned)
        .or_else(|| {
            normalized_path.as_deref().and_then(|path| {
                path.rsplit('/')
                    .find(|part| !part.is_empty())
                    .map(str::to_owned)
            })
        })
        .unwrap_or_else(|| stable_id.clone());
    let key = format!("project:{stable_id}");
    let mut project = json!({
        "project_id": stable_id,
        "name": name,
        "recent_conversations": [],
    });
    if let Some(path) = path {
        project["path"] = json!(path);
    }
    (key, project)
}

fn normalize_actor_path(path: &str) -> String {
    path.trim()
        .replace('\\', "/")
        .trim_end_matches('/')
        .to_owned()
}

fn actor_path_key(path: &str) -> String {
    if cfg!(windows) {
        path.to_lowercase()
    } else {
        path.to_owned()
    }
}

fn actor_path_is_within(path: &str, root: &str) -> bool {
    let path = actor_path_key(&normalize_actor_path(path));
    let root = actor_path_key(&normalize_actor_path(root));
    !root.is_empty() && (path == root || path.starts_with(&format!("{root}/")))
}

fn parse_actor_id(value: &str) -> Option<ActorId> {
    match value {
        "local" => Some(ActorId::Local),
        "codex" => Some(ActorId::Codex),
        "claude" => Some(ActorId::Claude),
        _ => None,
    }
}

/// Returns true for Desktop messages carrying an `extra.operation`. These
/// control operations (`resolve_request`, `set_conversation_mode`,
/// `list_conversations`, `check_desktop_update`) are dispatched concurrently
/// (see `run_actor_message_stream`) so an in-flight actor turn cannot starve
/// them. A message without `extra.operation` is a regular turn-triggering
/// message and stays sequential. A non-object `extra` is treated as absent,
/// matching `handle_incoming`.
fn is_control_operation(message: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(message) else {
        return false;
    };
    value
        .get("extra")
        .and_then(Value::as_object)
        .is_some_and(|extra| extra.get("operation").and_then(Value::as_str).is_some())
}

fn projectless_chat_workspace_dir(base_dir: &str, request_id: &str) -> Result<String> {
    let path = Path::new(base_dir)
        .join(chrono::Local::now().format("%Y-%m-%d").to_string())
        .join(format!("claude-{request_id}"));
    std::fs::create_dir_all(&path)?;
    Ok(std::fs::canonicalize(&path)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned())
}

fn resolve_actor_project_path(
    actor: ActorId,
    conversation_id: Option<&str>,
    project_path: Option<&str>,
    chat_workspaces_dir: &str,
    request_id: &str,
) -> Result<Option<String>> {
    if let Some(project_path) = project_path {
        return Ok(Some(project_path.to_owned()));
    }
    if actor == ActorId::Claude && conversation_id.is_none() {
        return projectless_chat_workspace_dir(chat_workspaces_dir, request_id).map(Some);
    }
    Ok(None)
}

fn delivery_failure(error: &BridgeError) -> DeliveryFailure {
    match error {
        BridgeError::Http(error) => match error.status().map(|status| status.as_u16()) {
            Some(408 | 429) => DeliveryFailure::Retry,
            Some(status) if status >= 500 => DeliveryFailure::Retry,
            Some(400 | 401 | 403 | 404 | 409 | 422) => DeliveryFailure::DeadLetter,
            Some(_) => DeliveryFailure::DeadLetter,
            None => DeliveryFailure::Retry,
        },
        _ => DeliveryFailure::DeadLetter,
    }
}

#[cfg(test)]
mod delivery_tests {
    use std::future::pending;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::Notify;

    use super::*;

    fn local_test_config_value(codex_id: &str, base_url: &str) -> Value {
        let mut value = crate::config::default_config_value(codex_id, base_url);
        value["security"]["development_mode"] = Value::Bool(true);
        value
    }

    #[test]
    fn actor_snapshot_groups_conversations_by_project() {
        let conversation = |id: &str, project_path: Option<&str>| ActorConversation {
            actor_id: ActorId::Codex,
            conversation_id: id.to_owned(),
            title: format!("Conversation {id}"),
            project_id: None,
            project_name: None,
            project_path: project_path.map(str::to_owned),
            writable: true,
            updated_at: Some("2026-07-14T00:00:00Z".to_owned()),
            mode: Some("default".to_owned()),
        };
        let projects = build_actor_project_summary(
            &[
                conversation("one", Some("D:\\Work\\App")),
                conversation("two", Some("D:/Work/App/")),
                conversation("chat", Some("D:\\Chats\\2026-07-14\\chat-1")),
            ],
            1,
            true,
            "D:\\Chats",
        );

        assert_eq!(projects.len(), 2);
        assert_eq!(projects[0]["name"], "App");
        assert_eq!(projects[0]["matched_conversation_count"], 2);
        assert_eq!(projects[0]["shown_conversation_count"], 1);
        assert_eq!(projects[0]["truncated"], true);
        assert_eq!(projects[0]["complete"], false);
        assert_eq!(projects[1]["project_id"], "no-project");
        assert_eq!(projects[1]["scope"], "conversation");
        assert!(projects[1].get("path").is_none());
    }

    #[test]
    fn actor_snapshot_prefers_native_project_identity_over_worktree_path() {
        let conversation = |id: &str, path: &str| ActorConversation {
            actor_id: ActorId::Codex,
            conversation_id: id.to_owned(),
            title: id.to_owned(),
            project_id: Some("native-project".to_owned()),
            project_name: Some("Native project".to_owned()),
            project_path: Some(path.to_owned()),
            writable: true,
            updated_at: None,
            mode: None,
        };
        let projects = build_actor_project_summary(
            &[
                conversation("one", "D:\\worktrees\\one"),
                conversation("two", "D:\\worktrees\\two"),
            ],
            20,
            true,
            "D:\\Chats",
        );

        assert_eq!(projects.len(), 1);
        assert_eq!(projects[0]["project_id"], "native-project");
        assert_eq!(projects[0]["name"], "Native project");
        assert_eq!(projects[0]["matched_conversation_count"], 2);
    }

    #[tokio::test]
    async fn snapshot_scan_is_shared_and_one_coworker_failure_does_not_block_another() {
        struct CountingAdapter(Arc<AtomicUsize>);

        #[async_trait::async_trait]
        impl ActorAdapter for CountingAdapter {
            fn actor_id(&self) -> ActorId {
                ActorId::Codex
            }

            async fn health(&self) -> ActorHealth {
                ActorHealth {
                    actor_id: ActorId::Codex,
                    available: true,
                    message: "ready".to_owned(),
                }
            }

            async fn list_conversations(&self, _limit: usize) -> Result<Vec<ActorConversation>> {
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(Vec::new())
            }

            async fn load_messages(
                &self,
                _conversation_id: &str,
                _before_cursor: Option<&str>,
                _page_size: usize,
            ) -> Result<ActorMessagePage> {
                unreachable!()
            }

            async fn send_message(
                &self,
                _conversation_id: Option<&str>,
                _input: ActorMessageInput<'_>,
            ) -> Result<Value> {
                unreachable!()
            }

            async fn interrupt(&self, _conversation_id: &str) -> Result<()> {
                unreachable!()
            }
        }

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let address = listener.local_addr().expect("test server address");
        let server = std::thread::spawn(move || {
            for body in [
                json!({
                    "registration_id": "registration-good",
                    "participant_id": "participant-good"
                }),
                json!({"message_id": "snapshot-ack", "accepted": true, "duplicate": false}),
                json!({"message_id": "reconnect-snapshot-ack", "accepted": true, "duplicate": false}),
            ] {
                let (mut socket, _) = listener.accept().expect("accept request");
                let mut request = [0_u8; 4096];
                let _ = socket.read(&mut request).expect("read request");
                let body = body.to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                socket
                    .write_all(response.as_bytes())
                    .expect("write response");
            }
        });
        let storage_dir =
            std::env::temp_dir().join(format!("coworker-desktop-snapshot-sync-{}", Uuid::new_v4()));
        let config = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desktop-test",
            "display_name": "Desktop Test",
            "storage_dir": storage_dir,
            "coworkers": [
                {"coworker_id": "offline", "base_url": "http://127.0.0.1:1"},
                {"coworker_id": "online", "base_url": format!("http://{address}")}
            ],
            "actors": {
                "local": {"enabled": false},
                "codex": {"enabled": true},
                "claude": {"enabled": false}
            },
            "security": {"development_mode": true}
        }))
        .expect("desktop config");
        let scans = Arc::new(AtomicUsize::new(0));
        let adapter: Arc<dyn ActorAdapter> = Arc::new(CountingAdapter(Arc::clone(&scans)));
        let router = DesktopRouter::new(config, vec![adapter]).expect("router");

        let snapshot = router.build_actor_snapshot(ActorId::Codex, 20, 10).await;
        assert!(snapshot.get("projects").is_some());
        assert!(snapshot.get("conversations").is_none());
        scans.store(0, Ordering::SeqCst);

        router
            .sync_registrations()
            .await
            .expect("online Coworker should still receive snapshot");
        assert_eq!(scans.load(Ordering::SeqCst), 1);

        router
            .sync_registrations()
            .await
            .expect("unchanged online snapshot should be skipped");
        assert_eq!(scans.load(Ordering::SeqCst), 2);
        router
            .republish_actor_snapshot("online", ActorId::Codex)
            .await
            .expect("reconnected actor should force a fresh snapshot");
        server.join().expect("test server");
        assert_eq!(scans.load(Ordering::SeqCst), 3);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[test]
    fn actor_ids_are_never_guessed_from_message_text() {
        assert_eq!(parse_actor_id("codex"), Some(ActorId::Codex));
        assert_eq!(parse_actor_id("please ask claude"), None);
    }

    #[test]
    fn actor_attachment_manifest_preserves_message_and_paths() {
        let content = content_with_actor_attachments(
            "review this",
            &[
                "C:\\work\\note.md".to_owned(),
                "C:\\work\\image.png".to_owned(),
            ],
        );
        assert!(content.starts_with("review this\n\n[附件]"));
        assert!(content.contains("note.md"));
        assert!(content.contains("image.png"));
    }

    #[test]
    fn projectless_claude_chat_gets_an_isolated_workspace() {
        let root =
            std::env::temp_dir().join(format!("coworker-desktop-claude-chat-{}", Uuid::new_v4()));

        let workspace =
            projectless_chat_workspace_dir(root.to_string_lossy().as_ref(), "request-1")
                .expect("create Claude chat workspace");

        assert!(Path::new(&workspace).starts_with(std::fs::canonicalize(&root).unwrap()));
        assert!(Path::new(&workspace).is_dir());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn new_projectless_claude_message_defaults_to_configured_workspace() {
        let root = std::env::temp_dir().join(format!(
            "coworker-desktop-claude-default-{}",
            Uuid::new_v4()
        ));

        let workspace = resolve_actor_project_path(
            ActorId::Claude,
            None,
            None,
            root.to_string_lossy().as_ref(),
            "request-1",
        )
        .expect("resolve Claude workspace")
        .expect("Claude workspace");

        assert!(Path::new(&workspace).starts_with(std::fs::canonicalize(&root).unwrap()));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn incoming_attachments_are_saved_with_download_metadata() {
        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-incoming-attachment-{}",
            Uuid::new_v4()
        ));
        let attachments = json!([{
            "filename": "../brief.txt",
            "media_type": "text/plain",
            "data": BASE64.encode(b"hello")
        }]);

        let (paths, metadata) =
            save_incoming_attachments(&storage_dir, Some(&attachments), "../request-1", 5, 1024)
                .expect("save incoming attachment");

        assert_eq!(paths.len(), 1);
        assert_eq!(
            std::fs::read(&paths[0]).expect("read saved attachment"),
            b"hello"
        );
        assert!(Path::new(&paths[0]).starts_with(&storage_dir));
        assert_eq!(metadata[0]["filename"], "brief.txt");
        assert_eq!(metadata[0]["media_type"], "text/plain");
        assert_eq!(metadata[0]["size"], 5);
        assert_eq!(metadata[0]["path"], paths[0]);
        assert_eq!(metadata[0]["downloadable"], true);
        assert!(content_with_actor_attachments("", &paths).starts_with("[附件]\n"));

        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn codex_request_resolution_does_not_require_owner_id() {
        struct ResolvingCodexAdapter(Arc<Mutex<Vec<Value>>>);

        #[async_trait::async_trait]
        impl ActorAdapter for ResolvingCodexAdapter {
            fn actor_id(&self) -> ActorId {
                ActorId::Codex
            }

            async fn health(&self) -> ActorHealth {
                unreachable!()
            }

            async fn list_conversations(&self, _limit: usize) -> Result<Vec<ActorConversation>> {
                unreachable!()
            }

            async fn load_messages(
                &self,
                _conversation_id: &str,
                _before_cursor: Option<&str>,
                _page_size: usize,
            ) -> Result<ActorMessagePage> {
                unreachable!()
            }

            async fn send_message(
                &self,
                _conversation_id: Option<&str>,
                _input: ActorMessageInput<'_>,
            ) -> Result<Value> {
                unreachable!()
            }

            async fn interrupt(&self, _conversation_id: &str) -> Result<()> {
                unreachable!()
            }

            async fn resolve_request(
                &self,
                coworker_id: &str,
                conversation_id: &str,
                request_id: &str,
                response: Value,
            ) -> Result<Value> {
                self.0.lock().await.push(json!({
                    "coworker_id": coworker_id,
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "response": response,
                }));
                Ok(json!({"ok": true}))
            }
        }

        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-codex-approval-{}",
            Uuid::new_v4()
        ));
        let mut config_value = local_test_config_value("desktop-test", "http://127.0.0.1:1");
        config_value["storage_dir"] = json!(storage_dir.to_string_lossy());
        let config = DesktopConfig::from_value(config_value).expect("desktop config");
        let resolved = Arc::new(Mutex::new(Vec::new()));
        let adapter: Arc<dyn ActorAdapter> = Arc::new(ResolvingCodexAdapter(Arc::clone(&resolved)));
        let router = DesktopRouter::new(config, vec![adapter]).expect("desktop router");
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:codex:cw_default:1".to_owned(),
        };

        router
            .handle_incoming(
                "cw_default",
                ActorId::Codex,
                &registration,
                &json!({
                    "conversation_id": "thread-1",
                    "extra": {
                        "operation": "resolve_request",
                        "request_id": Uuid::new_v4().to_string(),
                        "server_request_id": "approval-1",
                        "decision": "accept"
                    }
                })
                .to_string(),
            )
            .await
            .expect("resolve Codex request without owner_id");

        assert_eq!(
            resolved.lock().await.as_slice(),
            &[json!({
                "coworker_id": "cw_default",
                "conversation_id": "thread-1",
                "request_id": "approval-1",
                "response": {"decision": "accept"},
            })]
        );

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn claude_request_resolution_derives_conversation_from_approval() {
        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-claude-approval-{}",
            Uuid::new_v4()
        ));
        let mut config_value = local_test_config_value("desktop-test", "http://127.0.0.1:1");
        config_value["storage_dir"] = json!(storage_dir.to_string_lossy());
        let config = DesktopConfig::from_value(config_value).expect("desktop config");
        let router = DesktopRouter::new(config, Vec::new()).expect("desktop router");
        router
            .store
            .create_approval(&crate::conversation_store::ApprovalRequest {
                request_id: "approval-1".to_owned(),
                actor_id: ActorId::Claude,
                conversation_id: "session-1".to_owned(),
                coworker_id: "cw_default".to_owned(),
                owner_id: "desktop-test".to_owned(),
                tool_name: "Bash".to_owned(),
                input: json!({"command": "git status"}),
                status: "pending".to_owned(),
                response: None,
                expires_at: chrono::Utc::now() + chrono::Duration::minutes(5),
                server_request_id: None,
            })
            .expect("create approval");
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:claude:cw_default:1".to_owned(),
        };

        router
            .handle_incoming(
                "cw_default",
                ActorId::Claude,
                &registration,
                &json!({
                    "extra": {
                        "operation": "resolve_request",
                        "request_id": Uuid::new_v4().to_string(),
                        "server_request_id": "approval-1",
                        "decision": "accept"
                    }
                })
                .to_string(),
            )
            .await
            .expect("resolve Claude request without owner_id");

        let approval = router
            .store
            .approval("approval-1")
            .expect("load approval")
            .expect("approval exists");
        assert_eq!(approval.status, "resolved");
        assert_eq!(
            approval.response,
            Some(json!({"behavior": "allow", "decision": "accept"}))
        );

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn claude_question_resolution_returns_answers_as_updated_input() {
        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-claude-question-{}",
            Uuid::new_v4()
        ));
        let mut config_value = local_test_config_value("desktop-test", "http://127.0.0.1:1");
        config_value["storage_dir"] = json!(storage_dir.to_string_lossy());
        let config = DesktopConfig::from_value(config_value).expect("desktop config");
        let router = DesktopRouter::new(config, Vec::new()).expect("desktop router");
        let questions = json!([{
            "question": "Which database should we use?",
            "header": "Database",
            "options": [{"label": "SQLite", "description": "Local file"}],
            "multiSelect": false
        }]);
        router
            .store
            .create_user_input(&crate::conversation_store::ApprovalRequest {
                request_id: "question-1".to_owned(),
                actor_id: ActorId::Claude,
                conversation_id: "question-router-session".to_owned(),
                coworker_id: "cw_default".to_owned(),
                owner_id: "desktop-test".to_owned(),
                tool_name: "AskUserQuestion".to_owned(),
                input: json!({"questions": questions}),
                status: "pending".to_owned(),
                response: None,
                expires_at: chrono::Utc::now() + chrono::Duration::minutes(5),
                server_request_id: None,
            })
            .expect("create user input request");
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:claude:cw_default:1".to_owned(),
        };

        router
            .handle_incoming(
                "cw_default",
                ActorId::Claude,
                &registration,
                &json!({
                    "extra": {
                        "operation": "resolve_request",
                        "request_id": Uuid::new_v4().to_string(),
                        "user_input_request_id": "question-1",
                        "answers": {"Which database should we use?": "SQLite"}
                    }
                })
                .to_string(),
            )
            .await
            .expect("answer Claude question");

        let request = router
            .store
            .approval("question-1")
            .expect("load user input request")
            .expect("user input request exists");
        assert_eq!(request.status, "resolved");
        assert_eq!(
            request.response,
            Some(json!({
                "behavior": "allow",
                "updatedInput": {
                    "questions": questions,
                    "answers": {"Which database should we use?": "SQLite"}
                }
            }))
        );

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[test]
    fn failed_inbox_reservation_allows_explicit_retry() {
        let storage_dir =
            std::env::temp_dir().join(format!("coworker-desktop-inbox-retry-{}", Uuid::new_v4()));
        let store = Arc::new(
            ConversationStore::open(storage_dir.join("desktop.sqlite3"))
                .expect("open conversation store"),
        );
        let envelope = DesktopEnvelopeV1::new(DesktopEventType::Command, json!({}));

        assert!(!store.remember_inbox(&envelope).unwrap().duplicate);
        {
            let _reservation =
                InboxReservation::new(Arc::clone(&store), envelope.message_id.clone());
        }
        assert!(!store.remember_inbox(&envelope).unwrap().duplicate);

        {
            let mut reservation =
                InboxReservation::new(Arc::clone(&store), envelope.message_id.clone());
            reservation.commit().unwrap();
        }
        assert!(store.remember_inbox(&envelope).unwrap().duplicate);

        drop(store);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn actor_messages_inherit_stored_mode_and_honor_requested_mode() {
        struct BlockingClaudeAdapter {
            started: Arc<Notify>,
            release: Arc<Notify>,
            observed_modes: Arc<Mutex<Vec<Option<String>>>>,
            observed_project_paths: Arc<Mutex<Vec<Option<String>>>>,
        }

        #[async_trait::async_trait]
        impl ActorAdapter for BlockingClaudeAdapter {
            fn actor_id(&self) -> ActorId {
                ActorId::Claude
            }

            async fn health(&self) -> ActorHealth {
                ActorHealth {
                    actor_id: ActorId::Claude,
                    available: true,
                    message: "ready".to_owned(),
                }
            }

            async fn list_conversations(
                &self,
                _limit: usize,
            ) -> Result<Vec<crate::actor::ActorConversation>> {
                Ok(Vec::new())
            }

            async fn load_messages(
                &self,
                _conversation_id: &str,
                _before_cursor: Option<&str>,
                _page_size: usize,
            ) -> Result<ActorMessagePage> {
                Ok(ActorMessagePage {
                    messages: Vec::new(),
                    next_before_cursor: None,
                })
            }

            async fn send_message(
                &self,
                conversation_id: Option<&str>,
                input: ActorMessageInput<'_>,
            ) -> Result<Value> {
                self.observed_modes
                    .lock()
                    .await
                    .push(input.mode.map(str::to_owned));
                self.observed_project_paths
                    .lock()
                    .await
                    .push(input.project_path.map(str::to_owned));
                self.started.notify_one();
                self.release.notified().await;
                Ok(json!({
                    "conversation_id": conversation_id,
                    "result": "done",
                }))
            }

            async fn interrupt(&self, _conversation_id: &str) -> Result<()> {
                Ok(())
            }
        }

        let storage_dir =
            std::env::temp_dir().join(format!("coworker-desktop-input-order-{}", Uuid::new_v4()));
        let mut config_value = local_test_config_value("desktop-test", "http://127.0.0.1:1");
        config_value["storage_dir"] = json!(storage_dir.to_string_lossy());
        let config = DesktopConfig::from_value(config_value).expect("desktop config");
        let started = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let observed_modes = Arc::new(Mutex::new(Vec::new()));
        let observed_project_paths = Arc::new(Mutex::new(Vec::new()));
        let adapter: Arc<dyn ActorAdapter> = Arc::new(BlockingClaudeAdapter {
            started: Arc::clone(&started),
            release: Arc::clone(&release),
            observed_modes: Arc::clone(&observed_modes),
            observed_project_paths: Arc::clone(&observed_project_paths),
        });
        let router = Arc::new(DesktopRouter::new(config, vec![adapter]).expect("router"));
        router
            .store
            .set_actor_run("run-1", ActorId::Claude, Some("session-1"))
            .unwrap();

        let send_router = Arc::clone(&router);
        let send = tokio::spawn(async move {
            send_router
                .send_actor_message(
                    ActorId::Claude,
                    None,
                    Some("session-1"),
                    "hello",
                    None,
                    None,
                    &[],
                )
                .await
        });
        started.notified().await;

        let pending_messages = router
            .store
            .list_messages(ActorId::Claude, "session-1", 20)
            .unwrap();
        assert_eq!(pending_messages.len(), 1);
        assert_eq!(pending_messages[0].author_kind, "local");
        assert_eq!(pending_messages[0].content, "hello");

        release.notify_one();
        send.await.unwrap().unwrap();
        let completed_messages = router
            .store
            .list_messages(ActorId::Claude, "session-1", 20)
            .unwrap();
        assert_eq!(completed_messages.len(), 2);
        assert_eq!(completed_messages[1].author_kind, "assistant");
        assert_eq!(completed_messages[1].content, "done");

        router
            .store
            .set_conversation_mode(ActorId::Claude, "session-1", "plan")
            .unwrap();
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:claude:cw_default:1".to_owned(),
        };
        let mut events = crate::actor::subscribe_actor_stream_events();
        let incoming_router = Arc::clone(&router);
        let incoming = tokio::spawn(async move {
            incoming_router
                .handle_incoming(
                    "cw_default",
                    ActorId::Claude,
                    &registration,
                    &json!({
                        "message": "from coworker",
                        "conversation_id": "session-1",
                        "extra": {
                            "request_id": Uuid::new_v4().to_string(),
                            "project_path": "D:\\Projects\\chosen"
                        }
                    })
                    .to_string(),
                )
                .await
        });
        started.notified().await;

        assert_eq!(
            observed_modes
                .lock()
                .await
                .last()
                .cloned()
                .flatten()
                .as_deref(),
            Some("plan")
        );
        assert_eq!(
            observed_project_paths
                .lock()
                .await
                .last()
                .cloned()
                .flatten()
                .as_deref(),
            Some("D:\\Projects\\chosen")
        );
        let event = tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                let event = events.recv().await.expect("actor event");
                if event.actor_id == ActorId::Claude
                    && event.conversation_id == "session-1"
                    && event.event["type"] == "conversation_updated"
                {
                    break event;
                }
            }
        })
        .await
        .expect("Claude conversation event");
        assert_eq!(event.event["type"], "conversation_updated");

        incoming.abort();
        let _ = incoming.await;

        let registration = CoworkerRegistration {
            registration_id: "registration-2".to_owned(),
            participant_id: "coworker-desktop:desktop-test:claude:cw_default:2".to_owned(),
        };
        let incoming_router = Arc::clone(&router);
        let incoming = tokio::spawn(async move {
            incoming_router
                .handle_incoming(
                    "cw_default",
                    ActorId::Claude,
                    &registration,
                    &json!({
                        "message": "plan first",
                        "extra": {
                            "request_id": Uuid::new_v4().to_string(),
                            "mode": "plan",
                            "project_path": "D:\\Projects\\chosen"
                        }
                    })
                    .to_string(),
                )
                .await
        });
        started.notified().await;

        assert_eq!(
            observed_modes
                .lock()
                .await
                .last()
                .cloned()
                .flatten()
                .as_deref(),
            Some("plan")
        );

        incoming.abort();
        let _ = incoming.await;

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn dropping_abort_guard_cancels_nested_stream_task() {
        struct DropSignal(Option<oneshot::Sender<()>>);

        impl Drop for DropSignal {
            fn drop(&mut self) {
                if let Some(signal) = self.0.take() {
                    let _ = signal.send(());
                }
            }
        }

        let (started_tx, started_rx) = oneshot::channel();
        let (dropped_tx, dropped_rx) = oneshot::channel();
        let stream = AbortOnDrop::new(tokio::spawn(async move {
            let _drop_signal = DropSignal(Some(dropped_tx));
            let _ = started_tx.send(());
            pending::<()>().await;
        }));

        started_rx.await.expect("nested task started");
        drop(stream);

        tokio::time::timeout(Duration::from_secs(1), dropped_rx)
            .await
            .expect("nested task was not cancelled")
            .expect("drop signal sender disappeared");
    }

    #[tokio::test]
    async fn incoming_local_message_publishes_a_conversation_update() {
        let storage_dir =
            std::env::temp_dir().join(format!("coworker-desktop-local-event-{}", Uuid::new_v4()));
        let config = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desktop-test",
            "display_name": "Desktop Test",
            "storage_dir": storage_dir,
            "coworkers": [{
                "coworker_id": "cw_default",
                "base_url": "http://127.0.0.1:1",
                "display_name": "Coworker"
            }],
            "actors": {
                "local": {"enabled": true},
                "codex": {"enabled": false},
                "claude": {"enabled": false}
            },
            "security": {"development_mode": true}
        }))
        .expect("desktop config");
        let router = DesktopRouter::new(config, Vec::new()).expect("desktop router");
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:local:cw_default:1".to_owned(),
        };
        let request_id = Uuid::new_v4().to_string();
        let command = json!({
            "message": "hello from coworker",
            "conversation_id": "local-event-thread",
            "extra": {
                "request_id": request_id,
                "bubble": {
                    "id": "bbl_frontend",
                    "kind": "handoff",
                    "phase": "start",
                    "resumed": false
                }
            }
        })
        .to_string();
        let mut events = crate::actor::subscribe_actor_stream_events();

        router
            .handle_incoming("cw_default", ActorId::Local, &registration, &command)
            .await
            .expect("handle local message");
        router
            .handle_incoming("cw_default", ActorId::Local, &registration, &command)
            .await
            .expect("ignore duplicate local message");

        let connection = rusqlite::Connection::open(storage_dir.join("desktop.sqlite3"))
            .expect("open conversation store");
        let envelope_json: String = connection
            .query_row(
                "SELECT envelope_json FROM outbox ORDER BY rowid DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .expect("command result envelope");
        let result_envelope: DesktopEnvelopeV1 =
            serde_json::from_str(&envelope_json).expect("valid command result envelope");
        assert_eq!(
            result_envelope.request_id.as_deref(),
            Some(request_id.as_str())
        );
        let result_count: u32 = connection
            .query_row("SELECT COUNT(*) FROM outbox", [], |row| row.get(0))
            .expect("command result count");
        assert_eq!(result_count, 1);
        let metadata_json: String = connection
            .query_row(
                "SELECT metadata_json FROM messages WHERE conversation_id=?1 LIMIT 1",
                ["local-event-thread"],
                |row| row.get(0),
            )
            .expect("local message metadata");
        let metadata: Value =
            serde_json::from_str(&metadata_json).expect("valid local message metadata");
        assert_eq!(metadata["bubble"]["id"], "bbl_frontend");
        assert_eq!(metadata["bubble"]["phase"], "start");

        let event = tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                let event = events.recv().await.expect("actor event");
                if event.actor_id == ActorId::Local && event.conversation_id == "local-event-thread"
                {
                    break event;
                }
            }
        })
        .await
        .expect("local conversation event");
        assert_eq!(event.event["type"], "conversation_updated");

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn update_check_command_only_publishes_a_local_control_event() {
        let storage_dir =
            std::env::temp_dir().join(format!("coworker-desktop-update-push-{}", Uuid::new_v4()));
        let config = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desktop-test",
            "display_name": "Desktop Test",
            "storage_dir": storage_dir,
            "coworkers": [{
                "coworker_id": "cw_default",
                "base_url": "http://127.0.0.1:1",
                "display_name": "Coworker"
            }],
            "actors": {
                "local": {"enabled": true},
                "codex": {"enabled": false},
                "claude": {"enabled": false}
            },
            "security": {"development_mode": true}
        }))
        .expect("desktop config");
        let router = DesktopRouter::new(config, Vec::new()).expect("desktop router");
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:local:cw_default:1".to_owned(),
        };
        let request_id = Uuid::new_v4().to_string();
        let command = json!({
            "extra": {
                "operation": "check_desktop_update",
                "request_id": request_id,
                "published_version": "0.2.0"
            }
        })
        .to_string();
        let mut events = crate::actor::subscribe_actor_stream_events();

        router
            .handle_incoming("cw_default", ActorId::Local, &registration, &command)
            .await
            .expect("handle update check");
        router
            .handle_incoming("cw_default", ActorId::Local, &registration, &command)
            .await
            .expect("ignore duplicate update check");

        let event = tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                let event = events.recv().await.expect("actor event");
                if event.event["type"] == "desktop_update_check_requested"
                    && event.event["published_version"] == "0.2.0"
                {
                    break event;
                }
            }
        })
        .await
        .expect("update control event");
        assert_eq!(event.event["type"], "desktop_update_check_requested");
        assert_eq!(event.event["published_version"], "0.2.0");
        let connection = rusqlite::Connection::open(storage_dir.join("desktop.sqlite3"))
            .expect("open conversation store");
        let outbox_count: u32 = connection
            .query_row("SELECT COUNT(*) FROM outbox", [], |row| row.get(0))
            .expect("outbox count");
        assert_eq!(outbox_count, 0);

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[tokio::test]
    async fn ensure_registered_reuses_persisted_remote_registration_without_post() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let address = listener.local_addr().expect("test server address");
        let (request_tx, request_rx) = std::sync::mpsc::channel();
        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:local:cw_default:1".to_owned(),
        };
        let remote_registration = registration.clone();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().expect("accept registration request");
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("set request timeout");
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let read = socket.read(&mut buffer).expect("read registration request");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            request_tx
                .send(String::from_utf8_lossy(&request).into_owned())
                .expect("record registration request");
            let body = json!({"registrations": [remote_registration]}).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            socket
                .write_all(response.as_bytes())
                .expect("write registration response");
        });

        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-registration-reuse-{}",
            Uuid::new_v4()
        ));
        let config = DesktopConfig::from_value(json!({
            "schema_version": 2,
            "desktop_id": "desktop-test",
            "display_name": "Desktop Test",
            "storage_dir": storage_dir,
            "coworkers": [{
                "coworker_id": "cw_default",
                "base_url": format!("http://{address}"),
                "display_name": "Coworker"
            }],
            "actors": {
                "local": {"enabled": true},
                "codex": {"enabled": false},
                "claude": {"enabled": false}
            },
            "security": {"development_mode": true}
        }))
        .expect("desktop config");
        let coworker = config.codex.coworkers[0].clone();
        let router = DesktopRouter::new(config, Vec::new()).expect("desktop router");
        router
            .store
            .save_registration(&coworker.coworker_id, ActorId::Local, &registration)
            .expect("persist registration");

        let reused = router
            .ensure_registered(&coworker, ActorId::Local)
            .await
            .expect("reuse registration");

        assert_eq!(reused, registration);
        let request = request_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("registration lookup request");
        assert!(request.starts_with("GET /api/communicate/register "));
        server.join().expect("registration server");
        let _ = std::fs::remove_dir_all(storage_dir);
    }

    #[test]
    fn control_operations_carry_extra_operation() {
        // A regular turn-triggering message has no `extra.operation`.
        assert!(!is_control_operation(
            &json!({"message": "hi", "conversation_id": "s1"}).to_string()
        ));
        // An empty `extra` object is still a regular message.
        assert!(!is_control_operation(
            &json!({"message": "hi", "extra": {}}).to_string()
        ));
        // Any `extra.operation` marks a control operation.
        assert!(is_control_operation(
            &json!({"extra": {"operation": "resolve_request", "decision": "accept"}}).to_string()
        ));
        assert!(is_control_operation(
            &json!({"extra": {"operation": "list_conversations"}}).to_string()
        ));
        // Malformed payloads and non-object `extra` do not count, matching
        // `handle_incoming` which treats a non-object extra as absent.
        assert!(!is_control_operation("not json"));
        assert!(!is_control_operation(
            &json!({"extra": "operation"}).to_string()
        ));
    }

    #[tokio::test]
    async fn resolve_request_bypasses_an_in_flight_claude_turn() {
        // Regression for the Claude approval self-deadlock: a Claude turn
        // blocks `handle_incoming` while it polls SQLite for an approval, and
        // the coworker's `resolve_request` for that approval used to queue
        // behind the very turn it was supposed to unblock. Control operations
        // are now dispatched concurrently, so the resolve must land *during*
        // the turn instead of after the approval times out.
        struct BlockingClaudeAdapter {
            started: Arc<Notify>,
            release: Arc<Notify>,
        }

        #[async_trait::async_trait]
        impl ActorAdapter for BlockingClaudeAdapter {
            fn actor_id(&self) -> ActorId {
                ActorId::Claude
            }

            async fn health(&self) -> ActorHealth {
                ActorHealth {
                    actor_id: ActorId::Claude,
                    available: true,
                    message: "ready".to_owned(),
                }
            }

            async fn list_conversations(&self, _limit: usize) -> Result<Vec<ActorConversation>> {
                Ok(Vec::new())
            }

            async fn load_messages(
                &self,
                _conversation_id: &str,
                _before_cursor: Option<&str>,
                _page_size: usize,
            ) -> Result<ActorMessagePage> {
                Ok(ActorMessagePage {
                    messages: Vec::new(),
                    next_before_cursor: None,
                })
            }

            async fn send_message(
                &self,
                _conversation_id: Option<&str>,
                _input: ActorMessageInput<'_>,
            ) -> Result<Value> {
                self.started.notify_one();
                self.release.notified().await;
                Ok(json!({"conversation_id": "session-blocked", "result": "done"}))
            }

            async fn interrupt(&self, _conversation_id: &str) -> Result<()> {
                Ok(())
            }
        }

        let storage_dir = std::env::temp_dir().join(format!(
            "coworker-desktop-approval-bypass-{}",
            Uuid::new_v4()
        ));
        let mut config_value = local_test_config_value("desktop-test", "http://127.0.0.1:1");
        config_value["storage_dir"] = json!(storage_dir.to_string_lossy());
        let config = DesktopConfig::from_value(config_value).expect("desktop config");
        let started = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let adapter: Arc<dyn ActorAdapter> = Arc::new(BlockingClaudeAdapter {
            started: Arc::clone(&started),
            release: Arc::clone(&release),
        });
        let router = Arc::new(DesktopRouter::new(config, vec![adapter]).expect("router"));

        router
            .store
            .create_approval(&crate::conversation_store::ApprovalRequest {
                request_id: "approval-blocked".to_owned(),
                actor_id: ActorId::Claude,
                conversation_id: "session-blocked".to_owned(),
                coworker_id: "cw_default".to_owned(),
                owner_id: "desktop-test".to_owned(),
                tool_name: "Bash".to_owned(),
                input: json!({"command": "echo hi"}),
                status: "pending".to_owned(),
                response: None,
                expires_at: chrono::Utc::now() + chrono::Duration::minutes(5),
                server_request_id: None,
            })
            .unwrap();

        let registration = CoworkerRegistration {
            registration_id: "registration-1".to_owned(),
            participant_id: "coworker-desktop:desktop-test:claude:cw_default:1".to_owned(),
        };
        let (tx, rx) = mpsc::channel::<String>(16);
        let stream_router = Arc::clone(&router);
        let stream_registration = registration.clone();
        let message_loop = tokio::spawn(async move {
            stream_router
                .run_actor_message_stream(
                    "cw_default".to_owned(),
                    ActorId::Claude,
                    stream_registration,
                    rx,
                )
                .await
        });

        // 1. Start a regular message turn - it blocks inside the Claude adapter.
        tx.send(
            json!({"message": "do something", "conversation_id": "session-blocked"}).to_string(),
        )
        .await
        .unwrap();
        started.notified().await;

        // 2. While the turn is blocked, enqueue a resolve_request. It must be
        //    dispatched concurrently and resolve the approval *during* the turn.
        tx.send(
            json!({
                "conversation_id": "session-blocked",
                "extra": {
                    "operation": "resolve_request",
                    "request_id": Uuid::new_v4().to_string(),
                    "server_request_id": "approval-blocked",
                    "decision": "accept"
                }
            })
            .to_string(),
        )
        .await
        .unwrap();

        // 3. The resolve must land while the turn is still blocked (the turn is
        //    only released below). A sequential dispatch would leave it queued
        //    behind the turn and this would time out.
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(approval) = router.store.approval("approval-blocked").unwrap() {
                    if approval.status == "resolved" {
                        return approval;
                    }
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("resolve_request must bypass the in-flight Claude turn");

        release.notify_one();
        drop(tx);
        message_loop.await.unwrap();

        let approval = router
            .store
            .approval("approval-blocked")
            .unwrap()
            .expect("approval exists");
        assert_eq!(approval.status, "resolved");
        assert_eq!(
            approval.response,
            Some(json!({"behavior": "allow", "decision": "accept"}))
        );

        drop(router);
        let _ = std::fs::remove_dir_all(storage_dir);
    }
}
