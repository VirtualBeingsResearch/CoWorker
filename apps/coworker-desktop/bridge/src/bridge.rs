use std::{
    collections::{HashMap, HashSet},
    fs,
    hash::{Hash, Hasher},
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64_STANDARD};
use chrono::Local;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use tokio::{
    sync::{Mutex, mpsc, oneshot, watch},
    task::JoinHandle,
    time::{Duration, sleep},
};
use tracing::{debug, info, warn};

use crate::{
    actor::ActorOutboundRequest,
    app_server::{AppServerRequest, CodexAppServerClient},
    codex_session as session,
    codex_session::{
        BRIDGE_THREAD_SOURCE, RuntimeSessionState, SessionAttachment, SessionMessagePage,
        SessionSummary,
    },
    config::{BridgeConfig, BridgeCoworker, DEFAULT_COWORKER_ID},
    conversation_store::default_conversation_title,
    coworker::CoworkerMessageAttachment,
    desktop_protocol::{ActorId, DesktopEventType, actor_model_message},
    error::{BridgeError, Result},
};

#[async_trait::async_trait]
pub trait CodexClient: Send + Sync {
    async fn request(&self, method: &str, params: Value) -> Result<Value>;
}

#[async_trait::async_trait]
impl CodexClient for CodexAppServerClient {
    async fn request(&self, method: &str, params: Value) -> Result<Value> {
        CodexAppServerClient::request(self, method, params).await
    }
}

const SEND_TO_COWORKER_TOOL: &str = "send_to_coworker";
const BRIDGE_TASK_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);
const LIST_COWORKERS_TOOL: &str = "list_coworkers";
const COWORKER_TOOL_CALL_TYPE: &str = "coworker_tool_call";
const NOT_LOADED_THREAD_STATUS: &str = "notLoaded";

#[derive(Debug, Clone, PartialEq, Eq)]
struct SavedAttachment {
    filename: String,
    media_type: String,
    saved_path: PathBuf,
    size: u64,
}

/// Aborts a nested stream task if its owner exits before joining it.
pub(crate) struct AbortOnDrop<T> {
    handle: Option<JoinHandle<T>>,
}

impl<T> AbortOnDrop<T> {
    pub(crate) fn new(handle: JoinHandle<T>) -> Self {
        Self {
            handle: Some(handle),
        }
    }

    pub(crate) async fn join(mut self) -> std::result::Result<T, tokio::task::JoinError> {
        self.handle.take().expect("join handle present").await
    }
}

impl<T> Drop for AbortOnDrop<T> {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            handle.abort();
        }
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct PersistedState {
    #[serde(default)]
    bootstrapped_thread_ids: Vec<String>,
    #[serde(default)]
    coworker_started_thread_ids: Vec<String>,
    #[serde(default)]
    bridge_started_thread_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pending_collaboration_modes: HashMap<String, String>,
}

#[derive(Default)]
struct BridgeState {
    thread_status: HashMap<String, String>,
    thread_active_turn: HashMap<String, String>,
    thread_last_coworker: HashMap<String, String>,
    thread_active_flags: HashMap<String, Vec<String>>,
    thread_last_error: HashMap<String, String>,
    thread_auto_compact_turn: HashMap<String, String>,
    thread_pending_requests: HashMap<String, HashMap<String, Value>>,
    thread_collaboration_mode: HashMap<String, String>,
    thread_model: HashMap<String, String>,
    thread_auto_continue_attempts: HashMap<String, usize>,
    auto_continued_turn_ids: HashSet<(String, String)>,
    pending_server_request_futures: HashMap<String, oneshot::Sender<Value>>,
    pending_server_request_coworker: HashMap<String, String>,
    collaboration_modes_by_name: Option<HashMap<String, Value>>,
    default_model: Option<String>,
    bootstrapped_thread_ids: HashSet<String>,
    coworker_started_thread_ids: HashSet<String>,
    bridge_started_thread_ids: HashSet<String>,
    thread_pending_collaboration_mode: HashMap<String, String>,
    handled_text_tool_calls: HashSet<String>,
}

pub struct CodexBridge {
    config: BridgeConfig,
    client: Arc<dyn CodexClient>,
    coworkers: HashMap<String, BridgeCoworker>,
    outbound: mpsc::Sender<ActorOutboundRequest>,
    state: Mutex<BridgeState>,
}

impl CodexBridge {
    pub fn new(
        config: BridgeConfig,
        client: CodexAppServerClient,
        outbound: mpsc::Sender<ActorOutboundRequest>,
    ) -> Result<Arc<Self>> {
        Self::new_with_client(config, Arc::new(client), outbound)
    }

    pub fn new_with_client(
        config: BridgeConfig,
        client: Arc<dyn CodexClient>,
        outbound: mpsc::Sender<ActorOutboundRequest>,
    ) -> Result<Arc<Self>> {
        let persisted = load_state(config.state_path.as_ref().map(PathBuf::from));
        let state = BridgeState {
            bootstrapped_thread_ids: persisted.bootstrapped_thread_ids.into_iter().collect(),
            coworker_started_thread_ids: persisted
                .coworker_started_thread_ids
                .into_iter()
                .collect(),
            bridge_started_thread_ids: persisted.bridge_started_thread_ids.into_iter().collect(),
            thread_pending_collaboration_mode: persisted.pending_collaboration_modes,
            ..BridgeState::default()
        };
        let coworkers = config
            .coworkers
            .iter()
            .cloned()
            .map(|c| (c.coworker_id.clone(), c))
            .collect();
        Ok(Arc::new(Self {
            config,
            client,
            coworkers,
            outbound,
            state: Mutex::new(state),
        }))
    }

    /// Services Codex app-server requests and notifications for the Desktop actor.
    pub async fn run_app_server_until_shutdown(
        self: Arc<Self>,
        mut notifications: mpsc::Receiver<Value>,
        mut server_requests: mpsc::Receiver<AppServerRequest>,
        mut shutdown: oneshot::Receiver<()>,
    ) -> Result<()> {
        info!(codex_id = %self.config.codex_id, "Starting Codex app-server actor");

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let notification_bridge = self.clone();
        let mut notification_shutdown = shutdown_rx.clone();
        let notification_task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    Some(msg) = notifications.recv() => {
                        notification_bridge.handle_notification_async(msg).await;
                    }
                    _ = notification_shutdown.changed() => break,
                    else => break,
                }
            }
        });

        let request_bridge = self.clone();
        let mut request_shutdown = shutdown_rx;
        let request_task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    Some(request) = server_requests.recv() => {
                        let result = request_bridge
                            .handle_app_server_request(&request.method, request.params, request.id)
                            .await
                            .map_err(|error| error.to_string());
                        let _ = request.response.send(result);
                    }
                    _ = request_shutdown.changed() => break,
                    else => break,
                }
            }
        });

        let _ = (&mut shutdown).await;
        info!("Codex app-server actor shutdown requested");
        let _ = shutdown_tx.send(true);
        self.resolve_pending_server_requests_for_shutdown().await;
        join_or_abort_task("notification loop", notification_task).await;
        join_or_abort_task("app-server request loop", request_task).await;
        Ok(())
    }

    async fn fetch_threads(&self, query: ThreadListQuery) -> Result<ThreadFetchResult> {
        let mut collected = Vec::new();
        let mut cursor: Option<String> = None;
        let page_limit = query.limit.unwrap_or(200).max(1);
        let complete;
        loop {
            let response = self
                .client
                .request(
                    "thread/list",
                    build_thread_list_params(
                        &self.config,
                        Some(page_limit),
                        cursor.as_deref(),
                        query.filter_source_kinds,
                    ),
                )
                .await?;
            let threads = response
                .get("data")
                .and_then(Value::as_array)
                .ok_or_else(|| BridgeError::message("thread/list response missing data"))?;
            collected.extend(
                threads
                    .iter()
                    .filter(|thread| thread_matches_query(thread, &query))
                    .cloned(),
            );
            let next_cursor = response
                .get("nextCursor")
                .or_else(|| response.get("next_cursor"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned);
            if let Some(limit) = query.limit
                && collected.len() >= limit
            {
                collected.truncate(limit);
                complete = next_cursor.is_none();
                break;
            }
            if !query.paginate || next_cursor.is_none() {
                complete = next_cursor.is_none();
                break;
            }
            cursor = next_cursor;
        }
        Ok(ThreadFetchResult {
            threads: collected,
            complete,
        })
    }

    async fn post_message_to_coworker(
        &self,
        coworker_id: &str,
        thread_id: &str,
        message: &str,
        attachments: &[CoworkerMessageAttachment],
    ) -> Result<()> {
        self.coworker(Some(coworker_id))?;
        let (response, result) = oneshot::channel();
        self.outbound
            .send(ActorOutboundRequest {
                actor_id: ActorId::Codex,
                coworker_id: coworker_id.to_owned(),
                conversation_id: Some(thread_id.to_owned()),
                event_type: DesktopEventType::ThreadEvent,
                payload: json!({
                    "message": message,
                    "attachments": attachments,
                }),
                response,
            })
            .await
            .map_err(|_| BridgeError::message("Desktop router outbound channel is closed"))?;
        result
            .await
            .map_err(|_| BridgeError::message("Desktop router dropped outbound response"))?
    }

    async fn fetch_threads_for_sessions(&self, limit: usize) -> ThreadFetchResult {
        self.fetch_threads(ThreadListQuery {
            limit: Some(limit.max(1)),
            paginate: true,
            filter_source_kinds: false,
        })
        .await
        .unwrap_or_else(|error| {
            warn!(%error, "Failed to fetch Codex threads; falling back to local session metadata");
            ThreadFetchResult {
                threads: Vec::new(),
                complete: false,
            }
        })
    }

    pub async fn list_codex_conversations(&self, limit: usize) -> Result<Vec<SessionSummary>> {
        Ok(self.list_codex_session_snapshot(limit).await?.0)
    }

    pub(crate) async fn list_codex_session_snapshot(
        &self,
        limit: usize,
    ) -> Result<(Vec<SessionSummary>, bool)> {
        let limit = limit.max(1);
        let fetch = self.fetch_threads_for_sessions(limit).await;
        let mut conversations = session::list_sessions(
            &self.config,
            &fetch.threads,
            self.runtime_session_state().await,
            limit.saturating_add(1),
        )?;
        let complete = fetch.complete && conversations.len() <= limit;
        conversations.truncate(limit);
        Ok((conversations, complete))
    }

    pub fn load_codex_messages(
        &self,
        thread_id: &str,
        before_cursor: Option<&str>,
        page_size: usize,
    ) -> Result<SessionMessagePage> {
        session::load_session_messages(&self.config, thread_id, before_cursor, page_size)
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn send_actor_conversation_message(
        &self,
        thread_id: Option<String>,
        content: String,
        attachment_paths: Vec<String>,
        collaboration_mode: Option<String>,
        project_path: Option<String>,
        message_id: Option<String>,
        author_kind: String,
        author_id: Option<String>,
        author_label: Option<String>,
        coworker_id: Option<String>,
    ) -> Result<Value> {
        let request_id = format!("ui-{}", current_millis());
        let mode_value = collaboration_mode.as_ref().map(|mode| json!(mode));
        let requested_mode = self
            .validated_collaboration_mode(mode_value.as_ref(), &request_id)
            .await?;
        let project_path = project_path.filter(|value| !value.trim().is_empty());
        let (thread_id, created) = match thread_id.filter(|value| !value.trim().is_empty()) {
            Some(thread_id) => {
                self.ensure_session_continuable(&thread_id).await?;
                (thread_id, false)
            }
            None => (
                self.create_bridge_session_thread(&request_id, project_path.as_deref())
                    .await?,
                true,
            ),
        };
        let (attachments, overlay_attachments) =
            self.save_ui_attachments(&thread_id, &attachment_paths)?;
        if content.trim().is_empty() && attachments.is_empty() {
            return Err(BridgeError::message("content or attachments are required"));
        }
        if requested_mode.is_some() && self.thread_requires_steer(&thread_id).await {
            return Err(BridgeError::message(
                "collaboration_mode cannot be applied while the thread requires turn/steer",
            ));
        }
        let initial_title = if created && author_kind == "coworker" {
            default_conversation_title(&content)
        } else {
            None
        };
        let mut overlay = session::overlay_record(
            &author_kind,
            author_id.clone(),
            author_label
                .as_deref()
                .unwrap_or(if author_kind == "coworker" {
                    "搭档"
                } else {
                    "本机"
                }),
            "message",
            content.clone(),
            overlay_attachments,
        );
        if let Some(message_id) = message_id.filter(|value| !value.trim().is_empty()) {
            overlay.id = format!("overlay-{message_id}");
        }
        session::append_overlay_message(&self.config, &thread_id, overlay)?;
        let content = content_with_attachments(&content, &attachments);
        let content = actor_model_message(
            &author_kind,
            author_id.as_deref(),
            author_label.as_deref(),
            &content,
        )?;
        if let Some(coworker_id) = coworker_id.as_deref()
            && self.coworkers.contains_key(coworker_id)
        {
            self.state
                .lock()
                .await
                .thread_last_coworker
                .insert(thread_id.clone(), coworker_id.to_owned());
        }
        let applied_mode = if requested_mode.is_some() {
            requested_mode.clone()
        } else if !self.thread_requires_steer(&thread_id).await {
            self.state
                .lock()
                .await
                .thread_pending_collaboration_mode
                .get(&thread_id)
                .cloned()
        } else {
            None
        };
        let response = self
            .start_or_steer_turn(&thread_id, &content, applied_mode.as_deref())
            .await?;
        {
            let mut state = self.state.lock().await;
            remember_turn_from_response(&mut state, &thread_id, &response);
            state.thread_auto_continue_attempts.remove(&thread_id);
            if created {
                state.bridge_started_thread_ids.insert(thread_id.clone());
            }
            self.save_state_locked(&state);
        }
        if let Some(title) = initial_title {
            self.try_set_initial_coworker_title(&thread_id, &title)
                .await;
        }
        let mut result = json!({
            "type": "desktop.command.result",
            "request_id": request_id,
            "codex_id": self.config.codex_id,
            "ok": true,
            "thread_id": thread_id.clone(),
            "conversation_id": thread_id,
            "created": created,
        });
        if !attachments.is_empty() {
            result["attachments"] = attachment_metadata(&attachments);
        }
        if let Some(mode) = applied_mode {
            result["collaboration_mode"] = json!(mode);
            result["applies_on"] = json!("current_turn_start");
        }
        Ok(result)
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn record_actor_conversation_message(
        &self,
        thread_id: &str,
        message_id: Option<&str>,
        author_kind: &str,
        author_id: Option<&str>,
        author_label: Option<&str>,
        content: &str,
        attachment_paths: &[String],
    ) -> Result<()> {
        self.ensure_session_continuable(thread_id).await?;
        let (_, overlay_attachments) = self.save_ui_attachments(thread_id, attachment_paths)?;
        let mut overlay = session::overlay_record(
            author_kind,
            author_id.map(str::to_owned),
            author_label.unwrap_or("本机"),
            "message",
            content.to_owned(),
            overlay_attachments,
        );
        if let Some(message_id) = message_id.filter(|value| !value.trim().is_empty()) {
            overlay.id = format!("overlay-{message_id}");
        }
        session::append_overlay_message(&self.config, thread_id, overlay)?;
        Ok(())
    }

    pub async fn set_codex_conversation_mode(&self, thread_id: &str, mode: &str) -> Result<Value> {
        self.ensure_session_continuable(thread_id).await?;
        let request_id = format!("ui-mode-{}", current_millis());
        let mode_value = json!(mode);
        let requested_mode = self
            .validated_collaboration_mode(Some(&mode_value), &request_id)
            .await?
            .ok_or_else(|| BridgeError::message("collaboration_mode is required"))?;
        let mut state = self.state.lock().await;
        state
            .thread_pending_collaboration_mode
            .insert(thread_id.to_owned(), requested_mode.clone());
        self.save_state_locked(&state);
        Ok(json!({
            "type": "desktop.command.result",
            "request_id": request_id,
            "codex_id": self.config.codex_id,
            "ok": true,
            "thread_id": thread_id,
            "conversation_id": thread_id,
            "collaboration_mode": requested_mode,
            "applies_on": "next_turn_start",
        }))
    }

    pub async fn resolve_server_request_for_desktop(
        &self,
        coworker_id: &str,
        thread_id: &str,
        server_request_id: &str,
        response: Value,
    ) -> Result<Value> {
        {
            let state = self.state.lock().await;
            let belongs_to_thread = state
                .thread_pending_requests
                .get(thread_id)
                .is_some_and(|requests| requests.contains_key(server_request_id));
            if !belongs_to_thread {
                return Err(BridgeError::message(
                    "server request does not belong to the supplied Codex conversation",
                ));
            }
        }
        let mut command = Map::new();
        command.insert("server_request_id".into(), json!(server_request_id));
        command.insert("response".into(), response);
        self.handle_server_request_resolve(
            &command,
            coworker_id,
            &format!("desktop-resolve-{}", current_millis()),
        )
        .await
    }

    pub async fn rename_codex_conversation(&self, thread_id: &str, title: &str) -> Result<Value> {
        self.ensure_session_continuable(thread_id).await?;
        let title = title.split_whitespace().collect::<Vec<_>>().join(" ");
        if title.is_empty() {
            return Err(BridgeError::message("session title cannot be empty"));
        }
        let request_id = format!("ui-rename-{}", current_millis());
        self.client
            .request(
                "thread/name/set",
                json!({
                    "threadId": thread_id,
                    "name": title,
                }),
            )
            .await?;
        session::publish_session_event(session::SessionEvent {
            thread_id: thread_id.to_owned(),
            event_type: "session-updated".to_owned(),
            message: None,
        });
        Ok(json!({
            "type": "desktop.command.result",
            "request_id": request_id,
            "codex_id": self.config.codex_id,
            "ok": true,
            "thread_id": thread_id,
            "conversation_id": thread_id,
            "title": title,
        }))
    }

    async fn try_set_initial_coworker_title(&self, thread_id: &str, title: &str) {
        if let Err(error) = self.rename_codex_conversation(thread_id, title).await {
            warn!(thread_id, %error, "Failed to set initial Coworker conversation title");
        }
    }

    pub fn copy_codex_attachment(
        &self,
        source_path: &str,
        destination_path: &str,
    ) -> Result<SessionAttachment> {
        session::copy_attachment_to_path(source_path, destination_path)
    }

    async fn create_bridge_session_thread(
        &self,
        request_id: &str,
        project_path: Option<&str>,
    ) -> Result<String> {
        let chat_path = if project_path.is_none() {
            Some(self.projectless_chat_workspace_dir(request_id)?)
        } else {
            None
        };
        let mut start_params = self.thread_start_params();
        start_params["cwd"] = json!(project_path.or(chat_path.as_deref()));
        let response = self.client.request("thread/start", start_params).await?;
        let thread_id = response
            .pointer("/thread/id")
            .and_then(Value::as_str)
            .ok_or_else(|| BridgeError::message("thread/start response missing thread.id"))?
            .to_owned();
        let mut state = self.state.lock().await;
        remember_thread_from_response(&mut state, &thread_id, &response, Some("idle"));
        state.bridge_started_thread_ids.insert(thread_id.clone());
        // developerInstructions + dynamicTools already provide the Desktop
        // context, so the first visible turn stays free of bootstrap text.
        state.bootstrapped_thread_ids.insert(thread_id.clone());
        self.save_state_locked(&state);
        Ok(thread_id)
    }

    fn thread_start_params(&self) -> Value {
        json!({
            "threadSource": BRIDGE_THREAD_SOURCE,
            "serviceName": self.config.service_name,
            "developerInstructions": self.developer_instructions(),
            "dynamicTools": self.dynamic_tool_specs(),
            "sandbox": self.config.permissions_mode,
            "approvalPolicy": if self.config.permissions_mode == "danger-full-access" {
                "never"
            } else {
                "on-request"
            },
            "approvalsReviewer": "user",
        })
    }

    async fn ensure_session_continuable(&self, thread_id: &str) -> Result<()> {
        let runtime = self.runtime_session_state().await;
        if session::is_thread_owned(&self.config, thread_id, &runtime) {
            return Ok(());
        }
        if self
            .state
            .lock()
            .await
            .thread_status
            .contains_key(thread_id)
        {
            return Ok(());
        }
        if !session::has_thread_history(&self.config, thread_id)? {
            debug!(
                thread_id,
                "Validating a Codex thread before its local history is available"
            );
        }
        // Resume validates native Codex App/CLI history before any overlay is
        // written. Unknown or stale ids are rejected by app-server.
        self.resume_thread(thread_id).await
    }

    async fn runtime_session_state(&self) -> RuntimeSessionState {
        let state = self.state.lock().await;
        let mut owned_thread_ids = state.coworker_started_thread_ids.clone();
        owned_thread_ids.extend(state.bridge_started_thread_ids.iter().cloned());
        RuntimeSessionState {
            owned_thread_ids,
            thread_status: state.thread_status.clone(),
            thread_collaboration_mode: state.thread_collaboration_mode.clone(),
            thread_pending_collaboration_mode: state.thread_pending_collaboration_mode.clone(),
        }
    }

    fn save_ui_attachments(
        &self,
        thread_id: &str,
        paths: &[String],
    ) -> Result<(Vec<SavedAttachment>, Vec<SessionAttachment>)> {
        if paths.len() > self.config.attachment_max_count {
            return Err(BridgeError::message(format!(
                "attachments count exceeds limit: {} > {}",
                paths.len(),
                self.config.attachment_max_count
            )));
        }
        let mut saved = Vec::new();
        let mut overlay = Vec::new();
        for path in paths {
            let attachment = session::save_session_attachment(&self.config, thread_id, path)?;
            let saved_path = attachment
                .path
                .as_ref()
                .map(PathBuf::from)
                .ok_or_else(|| BridgeError::message("attachment save did not return a path"))?;
            saved.push(SavedAttachment {
                filename: attachment.filename.clone(),
                media_type: attachment.media_type.clone(),
                size: attachment.size.unwrap_or_default(),
                saved_path,
            });
            overlay.push(attachment);
        }
        Ok((saved, overlay))
    }

    fn encode_local_attachments(
        &self,
        value: Option<&Value>,
    ) -> Result<Vec<CoworkerMessageAttachment>> {
        let Some(value) = value else {
            return Ok(Vec::new());
        };
        let items = value
            .as_array()
            .ok_or_else(|| BridgeError::message("attachments must be an array"))?;
        if items.len() > self.config.attachment_max_count {
            return Err(BridgeError::message(format!(
                "attachments count exceeds limit: {} > {}",
                items.len(),
                self.config.attachment_max_count
            )));
        }
        let mut encoded = Vec::with_capacity(items.len());
        for (index, item) in items.iter().enumerate() {
            let obj = item
                .as_object()
                .ok_or_else(|| BridgeError::message("attachments entries must be objects"))?;
            let path = string_field(obj, "path")
                .filter(|value| !value.is_empty())
                .ok_or_else(|| BridgeError::message("attachments[].path is required"))?;
            let path = Path::new(&path);
            let bytes = fs::read(path)?;
            if bytes.len() as u64 > self.config.attachment_max_bytes {
                return Err(BridgeError::message(format!(
                    "attachments[{index}] exceeds size limit: {} > {} bytes",
                    bytes.len(),
                    self.config.attachment_max_bytes
                )));
            }
            let filename = string_field(obj, "filename")
                .filter(|value| !value.is_empty())
                .or_else(|| {
                    path.file_name()
                        .and_then(|value| value.to_str())
                        .map(str::to_owned)
                })
                .map(|value| sanitize_file_component(&value))
                .unwrap_or_else(|| format!("attachment_{}", index + 1));
            let media_type = string_field(obj, "media_type")
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| guess_media_type(&filename).to_owned());
            encoded.push(CoworkerMessageAttachment {
                filename,
                media_type,
                data: BASE64_STANDARD.encode(bytes),
            });
        }
        Ok(encoded)
    }

    fn projectless_chat_workspace_dir(&self, request_id: &str) -> Result<String> {
        let day = Local::now().format("%Y-%m-%d").to_string();
        let request_component = sanitize_file_component(request_id);
        let path = PathBuf::from(&self.config.chat_workspaces_dir)
            .join(day)
            .join(request_component);
        fs::create_dir_all(&path)?;
        let path = fs::canonicalize(&path).unwrap_or(path);
        Ok(path.to_string_lossy().into_owned())
    }

    async fn handle_server_request_resolve(
        &self,
        command: &Map<String, Value>,
        coworker_id: &str,
        request_id: &str,
    ) -> Result<Value> {
        let server_request_id = self.resolve_server_request_id(command, coworker_id).await?;
        let payload = if let Some(response) = command.get("response").and_then(Value::as_object) {
            Value::Object(response.clone())
        } else if let Some(answers) = command.get("answers").and_then(Value::as_object) {
            json!({"answers": answers})
        } else {
            return Err(BridgeError::message(
                "answers or response must be an object; item/tool/requestUserInput expects answers keyed by question id, mcpServer/elicitation/request expects response",
            ));
        };
        let sender = {
            let mut state = self.state.lock().await;
            if let Some(expected) = state
                .pending_server_request_coworker
                .get(&server_request_id)
                && expected != coworker_id
            {
                return Err(BridgeError::message(
                    "server_request_id belongs to another Coworker",
                ));
            }
            state
                .pending_server_request_futures
                .remove(&server_request_id)
        };
        let Some(sender) = sender else {
            return Err(BridgeError::message(format!(
                "unknown or resolved server_request_id: {server_request_id}"
            )));
        };
        let _ = sender.send(payload);
        self.forget_pending_request(&server_request_id, None).await;
        Ok(json!({
            "type": "desktop.command.result",
            "request_id": request_id,
            "codex_id": self.config.codex_id,
            "ok": true,
            "server_request_id": server_request_id,
        }))
    }

    async fn resolve_server_request_id(
        &self,
        command: &Map<String, Value>,
        coworker_id: &str,
    ) -> Result<String> {
        if let Some(server_request_id) = server_request_id_field(command) {
            return Ok(server_request_id);
        }
        let state = self.state.lock().await;
        let candidates: Vec<String> = state
            .pending_server_request_futures
            .keys()
            .filter(|server_request_id| {
                state
                    .pending_server_request_coworker
                    .get(*server_request_id)
                    .is_some_and(|expected| expected == coworker_id)
            })
            .cloned()
            .collect();
        match candidates.as_slice() {
            [single] => Ok(single.clone()),
            [] => Err(BridgeError::message(
                "server_request_id is required; no pending server request exists for this Coworker",
            )),
            _ => Err(BridgeError::message(format!(
                "server_request_id is required; multiple pending server requests exist for this Coworker: {}",
                candidates.join(", ")
            ))),
        }
    }

    async fn start_or_steer_turn(
        &self,
        thread_id: &str,
        text: &str,
        collaboration_mode_name: Option<&str>,
    ) -> Result<Value> {
        self.resume_thread_if_needed(thread_id).await?;
        let input = json!([{"type": "text", "text": text}]);
        let (method, mut params, requested_mode) = {
            let state = self.state.lock().await;
            let active_turn_id = state.thread_active_turn.get(thread_id).cloned();
            if let (Some("active"), Some(active_turn_id)) = (
                state.thread_status.get(thread_id).map(String::as_str),
                active_turn_id,
            ) {
                if collaboration_mode_name.is_some() {
                    return Err(BridgeError::message(
                        "collaboration_mode cannot be applied while the thread requires turn/steer",
                    ));
                }
                (
                    "turn/steer",
                    json!({
                        "threadId": thread_id,
                        "expectedTurnId": active_turn_id,
                        "input": input,
                    }),
                    None,
                )
            } else {
                let requested = collaboration_mode_name.map(str::to_owned).or_else(|| {
                    state
                        .thread_pending_collaboration_mode
                        .get(thread_id)
                        .cloned()
                });
                (
                    "turn/start",
                    json!({
                        "threadId": thread_id,
                        "input": input,
                    }),
                    requested,
                )
            }
        };
        if method == "turn/start"
            && let Some(mode) = requested_mode.as_deref()
        {
            params["collaborationMode"] = self
                .resolve_collaboration_mode(mode, Some(thread_id))
                .await?;
        }
        let expected_turn_id = params
            .get("expectedTurnId")
            .and_then(Value::as_str)
            .unwrap_or("");
        info!(
            method,
            thread_id,
            expected_turn_id,
            collaboration_mode = requested_mode.as_deref().unwrap_or(""),
            "Sending input to Codex thread"
        );
        let response = match self.client.request(method, params.clone()).await {
            Ok(response) => response,
            Err(error) if error.to_string().contains("thread not found") => {
                warn!(
                    thread_id,
                    method, "Codex thread not found while sending input; resuming and retrying"
                );
                self.resume_thread(thread_id).await?;
                self.client.request(method, params).await?
            }
            Err(error) => return Err(error),
        };
        let mut state = self.state.lock().await;
        state
            .thread_status
            .insert(thread_id.to_owned(), "active".into());
        if method == "turn/start"
            && let Some(mode) = requested_mode
        {
            state
                .thread_collaboration_mode
                .insert(thread_id.to_owned(), mode.clone());
            if state.thread_pending_collaboration_mode.get(thread_id) == Some(&mode) {
                state.thread_pending_collaboration_mode.remove(thread_id);
                self.save_state_locked(&state);
            }
        }
        Ok(response)
    }

    async fn resume_thread_if_needed(&self, thread_id: &str) -> Result<()> {
        if self
            .state
            .lock()
            .await
            .thread_status
            .get(thread_id)
            .map(String::as_str)
            != Some(NOT_LOADED_THREAD_STATUS)
        {
            return Ok(());
        }
        self.resume_thread(thread_id).await
    }

    async fn resume_thread(&self, thread_id: &str) -> Result<()> {
        info!(thread_id, "Resuming Codex thread");
        let response = self
            .client
            .request("thread/resume", json!({"threadId": thread_id}))
            .await?;
        let resumed_thread_id = response
            .pointer("/thread/id")
            .and_then(Value::as_str)
            .ok_or_else(|| BridgeError::message("thread/resume response missing thread.id"))?;
        if resumed_thread_id != thread_id {
            return Err(BridgeError::message(
                "thread/resume returned a different Codex thread",
            ));
        }
        let mut state = self.state.lock().await;
        remember_thread_from_response(&mut state, thread_id, &response, Some("idle"));
        info!(thread_id, "Resumed Codex thread");
        Ok(())
    }

    async fn validated_collaboration_mode(
        &self,
        value: Option<&Value>,
        _request_id: &str,
    ) -> Result<Option<String>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let Some(mode) = value.as_str().filter(|s| !s.is_empty()) else {
            return Err(BridgeError::message(
                "collaboration_mode must be 'plan' or 'default'",
            ));
        };
        if !matches!(mode, "plan" | "default") {
            return Err(BridgeError::message(format!(
                "unknown collaboration_mode: {mode}"
            )));
        }
        let _ = self.resolve_collaboration_mode(mode, None).await?;
        Ok(Some(mode.to_owned()))
    }

    async fn resolve_collaboration_mode(
        &self,
        mode_name: &str,
        thread_id: Option<&str>,
    ) -> Result<Value> {
        if self
            .state
            .lock()
            .await
            .collaboration_modes_by_name
            .is_none()
        {
            let response = self
                .client
                .request("collaborationMode/list", json!({}))
                .await?;
            let modes = extract_collaboration_modes(&response);
            let by_name = modes
                .into_iter()
                .filter_map(|mode| {
                    mode.get("mode")
                        .and_then(Value::as_str)
                        .map(|name| (name.to_owned(), mode.clone()))
                })
                .collect();
            self.state.lock().await.collaboration_modes_by_name = Some(by_name);
        }
        let mut mode = self
            .state
            .lock()
            .await
            .collaboration_modes_by_name
            .as_ref()
            .and_then(|m| m.get(mode_name).cloned())
            .ok_or_else(|| {
                BridgeError::message(format!("unknown collaboration_mode: {mode_name}"))
            })?;
        if let Some(settings) = mode.get_mut("settings").and_then(Value::as_object_mut)
            && thread_id.is_some()
            && !settings.get("model").is_some_and(Value::is_string)
        {
            settings.insert(
                "model".into(),
                json!(self.resolve_collaboration_model(thread_id).await?),
            );
        }
        Ok(mode)
    }

    async fn resolve_collaboration_model(&self, thread_id: Option<&str>) -> Result<String> {
        if let Some(thread_id) = thread_id
            && let Some(model) = self.state.lock().await.thread_model.get(thread_id).cloned()
        {
            return Ok(model);
        }
        if let Some(model) = self.state.lock().await.default_model.clone() {
            return Ok(model);
        }
        let response = self.client.request("model/list", json!({})).await?;
        let model = response
            .get("data")
            .and_then(Value::as_array)
            .and_then(|items| {
                items.iter().find_map(|item| {
                    let obj = item.as_object()?;
                    (obj.get("isDefault").and_then(Value::as_bool) == Some(true))
                        .then(|| obj.get("model").and_then(Value::as_str).map(str::to_owned))
                        .flatten()
                })
            })
            .ok_or_else(|| BridgeError::message("model/list did not return a default model"))?;
        self.state.lock().await.default_model = Some(model.clone());
        Ok(model)
    }

    async fn thread_requires_steer(&self, thread_id: &str) -> bool {
        let state = self.state.lock().await;
        state.thread_status.get(thread_id).map(String::as_str) == Some("active")
            && state.thread_active_turn.contains_key(thread_id)
    }

    fn developer_instructions(&self) -> String {
        [
            "You are collaborating with Coworker instances through CoWorker Desktop.",
            "Coworker（搭档）是一个虚拟生命体项目；消息头里的 Coworker:<id> 是回复目标 id。",
            "When you need to send progress, questions, or final handoff back to a Coworker, call send_to_coworker with the Coworker id shown in the incoming message header — a normal final answer is NOT automatically forwarded to the Coworker.",
            "Use list_coworkers if you need to inspect reachable Coworker ids.",
        ].join("\n")
    }

    fn dynamic_tool_specs(&self) -> Value {
        json!([
            {
                "name": SEND_TO_COWORKER_TOOL,
                "type": "function",
                "description": "Send an intentional message from this Codex thread to a specific Coworker.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "coworker_id": {"type": "string", "description": "Target id from the message header, such as cw_01 in [来自Coworker:cw_01]."},
                        "message": {"type": "string", "description": "Message content to send to that Coworker."},
                        "attachments": {
                            "type": "array",
                            "description": "Optional local files to send to that Coworker.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Local file path."},
                                    "filename": {"type": "string", "description": "Optional display filename."},
                                    "media_type": {"type": "string", "description": "Optional MIME type."}
                                },
                                "required": ["path"]
                            }
                        }
                    },
                    "required": ["coworker_id"]
                }
            },
            {
                "name": LIST_COWORKERS_TOOL,
                "type": "function",
                "description": "List Coworkers currently reachable through this bridge.",
                "inputSchema": {"type": "object", "properties": {}, "required": []}
            }
        ])
    }

    async fn handle_app_server_request(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
    ) -> Result<Value> {
        let thread_id = thread_id_from_params(&params).or_else(|| extract_thread_id(&params));
        let turn_id = turn_id_from_params(&params).unwrap_or_default();
        info!(
            method,
            request_id = %request_id,
            thread_id = thread_id.as_deref().unwrap_or(""),
            turn_id,
            "Handling app-server request"
        );
        match method {
            "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
                self.handle_new_approval_request(method, params, request_id)
                    .await
            }
            "item/permissions/requestApproval" => {
                self.handle_permissions_approval_request(method, params, request_id)
                    .await
            }
            "applyPatchApproval" | "execCommandApproval" => {
                self.handle_legacy_approval_request(method, params, request_id)
                    .await
            }
            "currentTime/read" => Ok(json!({
                "currentTimeAt": SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs()
            })),
            "account/chatgptAuthTokens/refresh" => {
                let message = "Unsupported app-server request method: account/chatgptAuthTokens/refresh; CoWorker Desktop cannot perform ChatGPT token refresh";
                self.publish_app_server_request_error(method, &params, &request_id, message)
                    .await;
                Err(BridgeError::message(message))
            }
            "attestation/generate" => {
                let message = "Unsupported app-server request method: attestation/generate; CoWorker Desktop cannot generate attestation tokens";
                self.publish_app_server_request_error(method, &params, &request_id, message)
                    .await;
                Err(BridgeError::message(message))
            }
            "item/tool/requestUserInput" | "mcpServer/elicitation/request" => {
                self.await_user_input_response(method, params, request_id)
                    .await
            }
            "item/tool/call" => {
                let tool = params.get("tool").and_then(Value::as_str).unwrap_or("");
                info!(
                    tool,
                    request_id = %request_id,
                    thread_id = thread_id.as_deref().unwrap_or(""),
                    "Handling dynamic tool call"
                );
                match tool {
                    SEND_TO_COWORKER_TOOL => self.handle_send_to_coworker(&params).await,
                    LIST_COWORKERS_TOOL => Ok(self.handle_list_coworkers()),
                    _ => Ok(dynamic_tool_error(format!("Unknown dynamic tool: {tool}"))),
                }
            }
            _ => Err(BridgeError::message(format!(
                "Unsupported app-server request method: {method}; thread_id={thread_id:?}"
            ))),
        }
    }

    async fn handle_new_approval_request(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
    ) -> Result<Value> {
        if self.approvals_are_danger_full_access() {
            self.publish_approval_requested(method, &params, &request_id, "accept")
                .await;
            return Ok(json!({"decision": "accept"}));
        }
        if self.approvals_wait_for_coworker() {
            let fallback = json!({"decision": "decline"});
            let response = self
                .await_approval_response(method, params, request_id, fallback.clone())
                .await;
            let decision = response.get("decision").and_then(Value::as_str);
            if matches!(
                decision,
                Some("accept" | "acceptForSession" | "decline" | "cancel")
            ) {
                return Ok(response);
            }
            warn!(
                method,
                response = %response,
                "Coworker approval response was invalid; declining request"
            );
            return Ok(fallback);
        }
        self.publish_approval_requested(method, &params, &request_id, "decline")
            .await;
        Ok(json!({"decision": "decline"}))
    }

    async fn handle_legacy_approval_request(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
    ) -> Result<Value> {
        if self.approvals_are_danger_full_access() {
            self.publish_approval_requested(method, &params, &request_id, "approved")
                .await;
            return Ok(json!({"decision": "approved"}));
        }
        if self.approvals_wait_for_coworker() {
            let fallback = json!({"decision": "timed_out"});
            let response = self
                .await_approval_response(method, params, request_id, fallback.clone())
                .await;
            let decision = response.get("decision").and_then(Value::as_str);
            if matches!(
                decision,
                Some("approved" | "approved_for_session" | "denied" | "timed_out" | "abort")
            ) {
                return Ok(response);
            }
            warn!(
                method,
                response = %response,
                "Coworker legacy approval response was invalid; timing out request"
            );
            return Ok(fallback);
        }
        self.publish_approval_requested(method, &params, &request_id, "denied")
            .await;
        Ok(json!({"decision": "denied"}))
    }

    async fn handle_permissions_approval_request(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
    ) -> Result<Value> {
        if self.approvals_are_danger_full_access() {
            let response = approved_permissions_response(&params);
            self.publish_approval_requested(method, &params, &request_id, "accept")
                .await;
            return Ok(response);
        }
        if self.approvals_wait_for_coworker() {
            let fallback = denied_permissions_response();
            let response = self
                .await_approval_response(method, params, request_id, fallback.clone())
                .await;
            if response.get("permissions").is_some_and(Value::is_object) {
                return Ok(with_permission_response_defaults(response));
            }
            warn!(
                method,
                response = %response,
                "Coworker permissions approval response was invalid; returning empty permissions"
            );
            return Ok(fallback);
        }
        self.publish_approval_requested(method, &params, &request_id, "decline")
            .await;
        Ok(denied_permissions_response())
    }

    fn approvals_wait_for_coworker(&self) -> bool {
        self.config.approvals_reviewer == "coworker"
    }

    fn approvals_are_danger_full_access(&self) -> bool {
        self.config.permissions_mode == "danger-full-access"
            && self.config.approvals_reviewer == "none"
    }

    async fn handle_send_to_coworker(&self, params: &Map<String, Value>) -> Result<Value> {
        let Some(args) = params.get("arguments").and_then(Value::as_object) else {
            warn!("Rejected send_to_coworker tool call: arguments must be an object");
            return Ok(dynamic_tool_error("arguments must be an object"));
        };
        let coworker_id = string_field(args, "coworker_id").unwrap_or_default();
        let message = string_field(args, "message").unwrap_or_default();
        let thread_id = string_field(params, "threadId").unwrap_or_default();
        let attachments = match self.encode_local_attachments(args.get("attachments")) {
            Ok(attachments) => attachments,
            Err(error) => {
                warn!(
                    coworker_id,
                    thread_id,
                    %error,
                    "Rejected send_to_coworker tool call: invalid attachments"
                );
                return Ok(dynamic_tool_error(format!("invalid attachments: {error}")));
            }
        };
        if !self.coworkers.contains_key(&coworker_id) {
            warn!(
                coworker_id,
                thread_id, "Rejected send_to_coworker tool call: unknown coworker_id"
            );
            return Ok(dynamic_tool_error(format!(
                "unknown coworker_id: {coworker_id}"
            )));
        }
        if message.trim().is_empty() && attachments.is_empty() {
            warn!(
                coworker_id,
                thread_id,
                "Rejected send_to_coworker tool call: message or attachments are required"
            );
            return Ok(dynamic_tool_error("message or attachments are required"));
        }
        if thread_id.is_empty() {
            warn!(
                coworker_id,
                "Rejected send_to_coworker tool call: threadId is required"
            );
            return Ok(dynamic_tool_error("threadId is required"));
        }
        match self
            .post_message_to_coworker(&coworker_id, &thread_id, &message, &attachments)
            .await
        {
            Ok(()) => {
                info!(
                    coworker_id,
                    thread_id, "send_to_coworker tool call succeeded"
                );
                Ok(dynamic_tool_ok(format!("sent to Coworker:{coworker_id}")))
            }
            Err(error) => {
                warn!(
                    coworker_id,
                    thread_id,
                    %error,
                    "send_to_coworker tool call failed"
                );
                Ok(dynamic_tool_error(format!("send failed: {error}")))
            }
        }
    }

    fn handle_list_coworkers(&self) -> Value {
        let data: Vec<_> = self
            .config
            .coworkers
            .iter()
            .map(|c| json!({"coworker_id": c.coworker_id, "display_name": c.display_name}))
            .collect();
        dynamic_tool_ok(serde_json::to_string(&data).unwrap_or_default())
    }

    async fn await_user_input_response(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
    ) -> Result<Value> {
        let (tx, rx) = oneshot::channel();
        let (server_request_id, thread_id) = self
            .remember_pending_request(method, &params, &request_id, Some(tx))
            .await;
        info!(
            method,
            server_request_id,
            thread_id = thread_id.as_deref().unwrap_or(""),
            "Publishing user input request"
        );
        self.publish_bridge_event(
            json!({
                "type": "desktop.user_input.requested",
                "codex_id": self.config.codex_id,
                "server_request_id": server_request_id,
                "method": method,
                "params": params,
                "resolve_hint": user_input_resolve_hint(method, &params),
            }),
            thread_id.as_deref(),
        )
        .await;
        let timeout_ms = params.get("autoResolutionMs").and_then(Value::as_u64);
        let result = if method == "item/tool/requestUserInput" {
            if let Some(timeout_ms) = timeout_ms {
                match tokio::time::timeout(Duration::from_millis(timeout_ms), rx).await {
                    Ok(Ok(value)) => value,
                    _ => {
                        warn!(
                            method,
                            server_request_id,
                            thread_id = thread_id.as_deref().unwrap_or(""),
                            "User input request timed out"
                        );
                        json!({"answers": {}})
                    }
                }
            } else {
                rx.await.unwrap_or_else(|_| json!({"answers": {}}))
            }
        } else {
            rx.await.unwrap_or_else(|_| json!({}))
        };
        self.forget_pending_request(&server_request_id, thread_id.as_deref())
            .await;
        Ok(result)
    }

    async fn await_approval_response(
        &self,
        method: &str,
        params: Map<String, Value>,
        request_id: Value,
        fallback: Value,
    ) -> Value {
        let (tx, rx) = oneshot::channel();
        let (server_request_id, thread_id) = self
            .remember_pending_request(method, &params, &request_id, Some(tx))
            .await;
        info!(
            method,
            server_request_id,
            thread_id = thread_id.as_deref().unwrap_or(""),
            timeout_seconds = self.config.approval_timeout_seconds,
            "Publishing pending approval request"
        );
        self.publish_bridge_event(
            json!({
                "type": "desktop.approval.requested",
                "codex_id": self.config.codex_id,
                "server_request_id": server_request_id,
                "method": method,
                "params": params,
                "status": "pending",
            }),
            thread_id.as_deref(),
        )
        .await;
        let result = match tokio::time::timeout(
            Duration::from_secs(self.config.approval_timeout_seconds),
            rx,
        )
        .await
        {
            Ok(Ok(value)) => value,
            _ => {
                warn!(
                    method,
                    server_request_id,
                    thread_id = thread_id.as_deref().unwrap_or(""),
                    "Approval request timed out or was dropped"
                );
                fallback
            }
        };
        self.forget_pending_request(&server_request_id, thread_id.as_deref())
            .await;
        result
    }

    async fn publish_approval_requested(
        &self,
        method: &str,
        params: &Map<String, Value>,
        request_id: &Value,
        decision: &str,
    ) {
        let (server_request_id, thread_id) = self
            .remember_pending_request(method, params, request_id, None)
            .await;
        info!(
            method,
            server_request_id,
            thread_id = thread_id.as_deref().unwrap_or(""),
            decision,
            "Publishing approval request"
        );
        self.publish_bridge_event(
            json!({
                "type": "desktop.approval.requested",
                "codex_id": self.config.codex_id,
                "server_request_id": server_request_id,
                "method": method,
                "params": params,
                "decision": decision,
            }),
            thread_id.as_deref(),
        )
        .await;
        self.forget_pending_request(&server_request_id, thread_id.as_deref())
            .await;
    }

    async fn publish_app_server_request_error(
        &self,
        method: &str,
        params: &Map<String, Value>,
        request_id: &Value,
        message: &str,
    ) {
        let server_request_id = server_request_id(params, request_id, 1);
        let thread_id = extract_thread_id(params);
        self.publish_bridge_event(
            json!({
                "type": "desktop.error",
                "codex_id": self.config.codex_id,
                "ok": false,
                "request_id": server_request_id,
                "server_request_id": server_request_id,
                "method": method,
                "params": params,
                "message": message,
            }),
            thread_id.as_deref(),
        )
        .await;
    }

    async fn remember_pending_request(
        &self,
        method: &str,
        params: &Map<String, Value>,
        request_id: &Value,
        response_sender: Option<oneshot::Sender<Value>>,
    ) -> (String, Option<String>) {
        let thread_id = thread_id_from_params(params);
        let mut state = self.state.lock().await;
        let server_request_id = server_request_id(
            params,
            request_id,
            state.pending_server_request_futures.len() + 1,
        );
        let mut pending = json!({
            "server_request_id": server_request_id,
            "method": method,
        });
        if let Some(thread_id) = thread_id.as_deref() {
            pending["thread_id"] = json!(thread_id);
        }
        if let Some(turn_id) = params.get("turnId").and_then(Value::as_str) {
            pending["turn_id"] = json!(turn_id);
        }
        if let Some(item_id) = params.get("itemId").and_then(Value::as_str) {
            pending["item_id"] = json!(item_id);
        }
        if let Some(thread_id) = thread_id.as_deref() {
            state
                .thread_pending_requests
                .entry(thread_id.to_owned())
                .or_default()
                .insert(server_request_id.clone(), pending);
        }
        let coworker_id = thread_id
            .as_deref()
            .and_then(|tid| state.thread_last_coworker.get(tid).cloned())
            .unwrap_or_else(|| self.config.coworkers[0].coworker_id.clone());
        state
            .pending_server_request_coworker
            .insert(server_request_id.clone(), coworker_id);
        if let Some(response_sender) = response_sender {
            state
                .pending_server_request_futures
                .insert(server_request_id.clone(), response_sender);
        }
        (server_request_id, thread_id)
    }

    async fn forget_pending_request(&self, server_request_id: &str, thread_id: Option<&str>) {
        let mut state = self.state.lock().await;
        if let Some(thread_id) = thread_id {
            if let Some(requests) = state.thread_pending_requests.get_mut(thread_id) {
                requests.remove(server_request_id);
                if requests.is_empty() {
                    state.thread_pending_requests.remove(thread_id);
                }
            }
        } else {
            let thread_ids: Vec<_> = state.thread_pending_requests.keys().cloned().collect();
            for thread_id in thread_ids {
                if let Some(requests) = state.thread_pending_requests.get_mut(&thread_id) {
                    requests.remove(server_request_id);
                    if requests.is_empty() {
                        state.thread_pending_requests.remove(&thread_id);
                    }
                }
            }
        }
        state
            .pending_server_request_futures
            .remove(server_request_id);
        state
            .pending_server_request_coworker
            .remove(server_request_id);
    }

    async fn resolve_pending_server_requests_for_shutdown(&self) {
        let pending = {
            let mut state = self.state.lock().await;
            let methods = state
                .thread_pending_requests
                .values()
                .flat_map(|requests| requests.iter())
                .filter_map(|(server_request_id, request)| {
                    request
                        .get("method")
                        .and_then(Value::as_str)
                        .map(|method| (server_request_id.clone(), method.to_owned()))
                })
                .collect::<HashMap<_, _>>();
            let pending = state
                .pending_server_request_futures
                .drain()
                .map(|(server_request_id, sender)| {
                    let method = methods.get(&server_request_id).cloned().unwrap_or_default();
                    (server_request_id, method, sender)
                })
                .collect::<Vec<_>>();
            state.pending_server_request_coworker.clear();
            state.thread_pending_requests.clear();
            pending
        };

        for (server_request_id, method, sender) in pending {
            if sender
                .send(default_pending_request_response(&method))
                .is_err()
            {
                warn!(
                    server_request_id,
                    method, "Pending server request receiver dropped during bridge shutdown"
                );
            } else {
                info!(
                    server_request_id,
                    method, "Resolved pending server request during bridge shutdown"
                );
            }
        }
    }

    async fn publish_bridge_event(&self, mut payload: Value, thread_id: Option<&str>) {
        let coworker_id = {
            let state = self.state.lock().await;
            thread_id
                .and_then(|tid| state.thread_last_coworker.get(tid).cloned())
                .unwrap_or_else(|| self.config.coworkers[0].coworker_id.clone())
        };
        let event_type_str = payload
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("desktop.thread.event")
            .to_owned();
        let Some(event_type) = DesktopEventType::from_wire_str(&event_type_str) else {
            warn!(
                coworker_id,
                event_type = %event_type_str,
                "Unknown Desktop event type; dropping outbound event"
            );
            return;
        };
        debug_assert!(event_type_str.starts_with("desktop."));
        if let Some(mapping) = payload.as_object_mut() {
            mapping.remove("type");
            mapping.remove("thread_id");
        }
        let (response, received) = oneshot::channel();
        let send = self
            .outbound
            .send(ActorOutboundRequest {
                actor_id: ActorId::Codex,
                coworker_id: coworker_id.clone(),
                conversation_id: thread_id.map(str::to_owned),
                event_type,
                payload,
                response,
            })
            .await;
        let result = match send {
            Ok(()) => received
                .await
                .map_err(|_| BridgeError::message("Desktop outbound response was dropped"))
                .and_then(|result| result),
            Err(_) => Err(BridgeError::message("Desktop outbound channel is closed")),
        };
        if let Err(error) = result {
            warn!(%error, %event_type, coworker_id, "Failed to publish Desktop actor event");
        }
    }

    async fn handle_notification_async(&self, msg: Value) {
        let interrupted = self.handle_notification(msg).await;
        if let Some((thread_id, turn)) = interrupted {
            self.auto_continue_interrupted_turn(&thread_id, &turn).await;
        }
    }

    async fn handle_notification(&self, msg: Value) -> Option<(String, Value)> {
        let method = msg.get("method").and_then(Value::as_str)?.to_owned();
        let params = msg.get("params").and_then(Value::as_object)?.clone();
        let thread_id = extract_thread_id(&params);
        match method.as_str() {
            "turn/started" | "turn/completed" => {
                if let (Some(thread_id), Some(turn)) = (
                    params.get("threadId").and_then(Value::as_str),
                    params.get("turn"),
                ) {
                    let error_kind = codex_error_kind(turn.get("error"));
                    let turn_id = turn.get("id").and_then(Value::as_str).unwrap_or("");
                    let mut should_compact = false;
                    {
                        let mut state = self.state.lock().await;
                        remember_turn(&mut state, thread_id, turn);
                        if method == "turn/completed" {
                            let status = turn.get("status").and_then(Value::as_str);
                            state.thread_status.insert(
                                thread_id.to_owned(),
                                if status == Some("failed") {
                                    "failed"
                                } else {
                                    "idle"
                                }
                                .into(),
                            );
                            state.thread_active_turn.remove(thread_id);
                            if let Some(error) = extract_error_message(turn.get("error")) {
                                state.thread_last_error.insert(thread_id.to_owned(), error);
                            }
                            let was_auto_compact = state
                                .thread_auto_compact_turn
                                .get(thread_id)
                                .is_some_and(|id| id == turn_id);
                            if was_auto_compact {
                                state.thread_auto_compact_turn.remove(thread_id);
                            }
                            should_compact =
                                error_kind == Some("contextWindowExceeded") && !was_auto_compact;
                        }
                    }
                    if method == "turn/completed" {
                        // turn/* 是结构性通知：payload 无 message 字段，转发给 coworker 只会变成
                        // 空消息。只转发本轮真正产出的消息内容，
                        // 不再转发 turn 事件本身，避免提出计划时连发数条空消息。
                        self.handle_text_tool_calls_from_value(thread_id, turn, None)
                            .await;
                        if turn.get("status").and_then(Value::as_str) == Some("interrupted") {
                            return Some((thread_id.to_owned(), turn.clone()));
                        }
                        if error_kind == Some("usageLimitExceeded") {
                            self.expose_usage_limit(thread_id, turn.get("error")).await;
                        } else if should_compact {
                            self.start_context_compaction(thread_id).await;
                        }
                        self.state
                            .lock()
                            .await
                            .thread_auto_continue_attempts
                            .remove(thread_id);
                    }
                }
            }
            "thread/status/changed" => {
                if let (Some(thread_id), Some(status)) = (
                    params.get("threadId").and_then(Value::as_str),
                    params.get("status").and_then(Value::as_object),
                ) {
                    let mut state = self.state.lock().await;
                    remember_thread_status(&mut state, thread_id, status);
                }
                // 结构性通知，不再转发给 coworker（避免空消息）
            }
            "thread/settings/updated" => {
                if let Some(thread_id) = thread_id.as_deref()
                    && let Some(settings) = params.get("threadSettings").and_then(Value::as_object)
                {
                    let mut state = self.state.lock().await;
                    remember_thread_settings(&mut state, thread_id, settings);
                }
                trace_state_only_notification(&method, thread_id.as_deref());
            }
            "serverRequest/resolved" => {
                let request_id = params
                    .get("requestId")
                    .map(value_to_string)
                    .unwrap_or_default();
                if !request_id.is_empty() {
                    self.forget_pending_request(&request_id, thread_id.as_deref())
                        .await;
                }
                self.publish_bridge_event(
                    json!({
                        "type": "desktop.server_request.resolved",
                        "codex_id": self.config.codex_id,
                        "server_request_id": request_id,
                        "params": params,
                    }),
                    thread_id.as_deref(),
                )
                .await;
            }
            "error" => {
                if let Some(thread_id) = thread_id.as_deref() {
                    let message = extract_error_message(params.get("error"))
                        .unwrap_or_else(|| "Codex app-server error".into());
                    self.state
                        .lock()
                        .await
                        .thread_last_error
                        .insert(thread_id.to_owned(), message);
                }
                trace_state_only_notification(&method, thread_id.as_deref());
            }
            "thread/closed" | "thread/archived" | "thread/deleted" => {
                if let Some(thread_id) = thread_id.as_deref() {
                    self.clear_thread_runtime_state(thread_id).await;
                }
                // 结构性通知，不再转发给 coworker（避免空消息）
            }
            "thread/compacted" => {
                if let Some(thread_id) = thread_id.as_deref() {
                    let mut state = self.state.lock().await;
                    state.thread_last_error.remove(thread_id);
                    state.thread_auto_compact_turn.remove(thread_id);
                }
                trace_state_only_notification(&method, thread_id.as_deref());
            }
            "warning"
            | "configWarning"
            | "deprecationNotice"
            | "guardianWarning"
            | "windows/worldWritableWarning" => {
                log_warning_notification(&method, thread_id.as_deref(), &params);
            }
            "thread/started"
            | "thread/unarchived"
            | "thread/name/updated"
            | "thread/goal/updated"
            | "thread/goal/cleared"
            | "thread/realtime/started"
            | "thread/realtime/itemAdded"
            | "thread/realtime/transcript/delta"
            | "thread/realtime/transcript/done"
            | "thread/realtime/outputAudio/delta"
            | "thread/realtime/sdp"
            | "thread/realtime/error"
            | "thread/realtime/closed"
            | "thread/tokenUsage/updated"
            | "turn/diff/updated"
            | "turn/plan/updated"
            | "turn/moderationMetadata"
            | "hook/started"
            | "hook/completed"
            | "model/rerouted"
            | "model/verification"
            | "model/safetyBuffering/updated"
            | "mcpServer/startupStatus/updated"
            | "mcpServer/oauthLogin/completed"
            | "item/agentMessage/delta"
            | "item/commandExecution/outputDelta"
            | "item/commandExecution/terminalInteraction"
            | "item/fileChange/outputDelta"
            | "item/fileChange/patchUpdated"
            | "item/mcpToolCall/progress"
            | "item/plan/delta"
            | "item/reasoning/summaryPartAdded"
            | "item/reasoning/summaryTextDelta"
            | "item/reasoning/textDelta"
            | "item/autoApprovalReview/started"
            | "item/autoApprovalReview/completed"
            | "item/started"
            | "item/completed"
            | "account/login/completed"
            | "account/rateLimits/updated"
            | "account/updated"
            | "app/list/updated"
            | "command/exec/outputDelta"
            | "externalAgentConfig/import/completed"
            | "externalAgentConfig/import/progress"
            | "fs/changed"
            | "fuzzyFileSearch/sessionCompleted"
            | "fuzzyFileSearch/sessionUpdated"
            | "process/exited"
            | "process/outputDelta"
            | "remoteControl/status/changed"
            | "skills/changed"
            | "windowsSandbox/setupCompleted" => {
                if method == "item/completed"
                    && let (Some(thread_id), Some(item)) =
                        (thread_id.as_deref(), params.get("item"))
                {
                    let turn_id = params.get("turnId").and_then(Value::as_str);
                    self.handle_text_tool_calls_from_value(thread_id, item, turn_id)
                        .await;
                }
                trace_state_only_notification(&method, thread_id.as_deref());
            }
            _ => {
                debug!(
                    method,
                    thread_id = thread_id.as_deref().unwrap_or(""),
                    "Ignoring unknown Codex app-server notification as state-only"
                );
            }
        }
        if let Some(thread_id) = thread_id.as_deref() {
            let message = session::delta_message(thread_id, &method, &params);
            session::publish_session_event(session::SessionEvent {
                thread_id: thread_id.to_owned(),
                event_type: if message.is_some() {
                    "session-message-delta".to_owned()
                } else {
                    "session-updated".to_owned()
                },
                message,
            });
        }
        None
    }

    async fn clear_thread_runtime_state(&self, thread_id: &str) {
        let pending = {
            let mut state = self.state.lock().await;
            state.thread_status.remove(thread_id);
            state.thread_active_turn.remove(thread_id);
            state.thread_active_flags.remove(thread_id);
            state.thread_last_error.remove(thread_id);
            state.thread_auto_compact_turn.remove(thread_id);
            state.thread_auto_continue_attempts.remove(thread_id);

            let requests = state.thread_pending_requests.remove(thread_id);
            requests
                .into_iter()
                .flat_map(|requests| requests.into_iter())
                .filter_map(|(server_request_id, request)| {
                    let method = request
                        .get("method")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned();
                    state
                        .pending_server_request_coworker
                        .remove(&server_request_id);
                    state
                        .pending_server_request_futures
                        .remove(&server_request_id)
                        .map(|sender| (server_request_id, method, sender))
                })
                .collect::<Vec<_>>()
        };

        for (server_request_id, method, sender) in pending {
            let response = default_pending_request_response(&method);
            if sender.send(response).is_err() {
                warn!(
                    thread_id,
                    server_request_id,
                    method,
                    "Pending server request receiver dropped during thread cleanup"
                );
            } else {
                info!(
                    thread_id,
                    server_request_id,
                    method,
                    "Resolved pending server request during thread cleanup"
                );
            }
        }
    }

    async fn auto_continue_interrupted_turn(&self, thread_id: &str, turn: &Value) {
        if !self.config.auto_continue_interrupted_turns
            || self.config.auto_continue_interrupted_max_attempts == 0
        {
            return;
        }
        let turn_id = turn
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        {
            let mut state = self.state.lock().await;
            if !turn_id.is_empty()
                && !state
                    .auto_continued_turn_ids
                    .insert((thread_id.to_owned(), turn_id.clone()))
            {
                return;
            }
            let attempts = state
                .thread_auto_continue_attempts
                .get(thread_id)
                .copied()
                .unwrap_or(0);
            if attempts >= self.config.auto_continue_interrupted_max_attempts {
                return;
            }
            state
                .thread_auto_continue_attempts
                .insert(thread_id.to_owned(), attempts + 1);
        }
        match self
            .start_or_steer_turn(
                thread_id,
                &self.config.auto_continue_interrupted_message,
                None,
            )
            .await
        {
            Ok(response) => {
                let mut state = self.state.lock().await;
                remember_turn_from_response(&mut state, thread_id, &response);
            }
            Err(error) => {
                self.state.lock().await.thread_last_error.insert(
                    thread_id.to_owned(),
                    format!("auto-continue failed: {error}"),
                );
            }
        }
    }

    async fn expose_usage_limit(&self, thread_id: &str, error: Option<&Value>) {
        let message = extract_error_message(error)
            .unwrap_or_else(|| "Codex usage limit exceeded.".to_owned());
        let overlay = session::overlay_record(
            "codex",
            None,
            "Codex",
            "system",
            message.clone(),
            Vec::new(),
        );
        if let Err(error) = session::append_overlay_message(&self.config, thread_id, overlay) {
            warn!(%error, thread_id, "Failed to expose Codex usage limit in session");
        }
        let coworker_id = self
            .state
            .lock()
            .await
            .thread_last_coworker
            .get(thread_id)
            .cloned();
        if let Some(coworker_id) = coworker_id
            && let Err(error) = self
                .post_message_to_coworker(&coworker_id, thread_id, &message, &[])
                .await
        {
            warn!(%error, thread_id, coworker_id, "Failed to expose Codex usage limit to Coworker");
        }
    }

    async fn start_context_compaction(&self, thread_id: &str) {
        match self
            .client
            .request("thread/compact/start", json!({"threadId": thread_id}))
            .await
        {
            Ok(response) => {
                let mut state = self.state.lock().await;
                if let Some(turn) = response.get("turn") {
                    if let Some(turn_id) = turn.get("id").and_then(Value::as_str) {
                        state
                            .thread_auto_compact_turn
                            .insert(thread_id.to_owned(), turn_id.to_owned());
                    }
                    remember_turn(&mut state, thread_id, turn);
                }
            }
            Err(error) => {
                warn!(%error, thread_id, "Failed to compact Codex context automatically");
                self.state.lock().await.thread_last_error.insert(
                    thread_id.to_owned(),
                    format!("automatic context compaction failed: {error}"),
                );
            }
        }
    }

    async fn handle_text_tool_calls_from_value(
        &self,
        thread_id: &str,
        value: &Value,
        turn_id: Option<&str>,
    ) {
        let owned_turn_id = turn_id
            .map(str::to_owned)
            .or_else(|| {
                value
                    .get("id")
                    .map(value_to_string)
                    .filter(|value| !value.is_empty())
            })
            .unwrap_or_default();
        let mut texts = Vec::new();
        collect_assistant_texts(value, false, &mut texts);
        for text in texts {
            self.handle_text_tool_call(thread_id, &owned_turn_id, &text)
                .await;
        }
    }

    async fn handle_text_tool_call(&self, thread_id: &str, turn_id: &str, text: &str) {
        let block = text.trim();
        let Some(payload) = parse_coworker_frontmatter_message(block) else {
            return;
        };
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        block.hash(&mut hasher);
        let dedupe_key = format!("{thread_id}:{turn_id}:{}", hasher.finish());
        if !self
            .state
            .lock()
            .await
            .handled_text_tool_calls
            .insert(dedupe_key)
        {
            return;
        }
        let name = payload.get("name").and_then(Value::as_str).unwrap_or("");
        if name == LIST_COWORKERS_TOOL {
            let result = self.handle_list_coworkers();
            let message = text_tool_result_message(LIST_COWORKERS_TOOL, &result);
            if let Ok(response) = self.start_or_steer_turn(thread_id, &message, None).await {
                let mut state = self.state.lock().await;
                remember_turn_from_response(&mut state, thread_id, &response);
            }
            return;
        }
        if name != SEND_TO_COWORKER_TOOL {
            return;
        }
        let Some(args) = payload.get("arguments").and_then(Value::as_object) else {
            return;
        };
        let coworker_id = string_field(args, "coworker_id").unwrap_or_default();
        let message = string_field(args, "message").unwrap_or_default();
        if self.coworkers.contains_key(&coworker_id) && !message.trim().is_empty() {
            let _ = self
                .post_message_to_coworker(&coworker_id, thread_id, &message, &[])
                .await;
        }
    }

    fn coworker(&self, coworker_id: Option<&str>) -> Result<BridgeCoworker> {
        let id = coworker_id.unwrap_or(DEFAULT_COWORKER_ID);
        if coworker_id.is_none() {
            return self
                .config
                .coworkers
                .first()
                .cloned()
                .ok_or_else(|| BridgeError::message("no coworkers configured"));
        }
        self.coworkers
            .get(id)
            .cloned()
            .ok_or_else(|| BridgeError::message(format!("unknown coworker_id: {id}")))
    }

    fn save_state_locked(&self, state: &BridgeState) {
        let Some(path) = self.config.state_path.as_ref() else {
            return;
        };
        let path = PathBuf::from(path);
        let mut persisted = PersistedState {
            bootstrapped_thread_ids: sorted_set(&state.bootstrapped_thread_ids),
            coworker_started_thread_ids: sorted_set(&state.coworker_started_thread_ids),
            bridge_started_thread_ids: sorted_set(&state.bridge_started_thread_ids),
            pending_collaboration_modes: state.thread_pending_collaboration_mode.clone(),
        };
        if persisted.pending_collaboration_modes.is_empty() {
            persisted.pending_collaboration_modes = HashMap::new();
        }
        if let Err(error) = persist_state(&path, &persisted) {
            warn!(state_path = %path.display(), %error, "Failed to persist Codex bridge state");
        }
    }
}

fn persist_state(path: &Path, state: &PersistedState) -> Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent)?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(tmp.as_file_mut(), state)?;
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|error| error.error)?;
    Ok(())
}

fn load_state(path: Option<PathBuf>) -> PersistedState {
    let Some(path) = path else {
        return PersistedState::default();
    };
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

#[derive(Clone)]
struct ThreadListQuery {
    limit: Option<usize>,
    paginate: bool,
    filter_source_kinds: bool,
}

struct ThreadFetchResult {
    threads: Vec<Value>,
    complete: bool,
}

fn build_thread_list_params(
    config: &BridgeConfig,
    limit: Option<usize>,
    cursor: Option<&str>,
    filter_source_kinds: bool,
) -> Value {
    let mut params = json!({
        "sortKey": "recency_at",
        "sortDirection": "desc",
    });
    if filter_source_kinds {
        params["sourceKinds"] = json!(config.snapshot_source_kinds);
    }
    if let Some(limit) = limit.filter(|limit| *limit > 0) {
        params["limit"] = json!(limit);
    }
    if let Some(cursor) = cursor.filter(|cursor| !cursor.is_empty()) {
        params["cursor"] = json!(cursor);
    }
    params
}

fn thread_matches_query(thread: &Value, _query: &ThreadListQuery) -> bool {
    thread.is_object()
}

fn string_field(mapping: &Map<String, Value>, key: &str) -> Option<String> {
    mapping
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .map(str::to_owned)
}

fn content_with_attachments(content: &str, attachments: &[SavedAttachment]) -> String {
    if attachments.is_empty() {
        return content.to_owned();
    }
    let manifest = attachment_manifest(attachments);
    if content.trim().is_empty() {
        manifest
    } else {
        format!("{content}\n\n{manifest}")
    }
}

fn attachment_manifest(attachments: &[SavedAttachment]) -> String {
    let mut lines = vec!["[附件]".to_owned()];
    for (index, attachment) in attachments.iter().enumerate() {
        lines.push(format!(
            "{}. {} ({}, {} bytes) saved_path={}",
            index + 1,
            attachment.filename,
            attachment.media_type,
            attachment.size,
            attachment.saved_path.display()
        ));
    }
    lines.join("\n")
}

fn attachment_metadata(attachments: &[SavedAttachment]) -> Value {
    Value::Array(
        attachments
            .iter()
            .map(|attachment| {
                json!({
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "saved_path": attachment.saved_path.display().to_string(),
                    "size": attachment.size,
                })
            })
            .collect(),
    )
}

fn current_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}

fn sanitize_file_component(value: &str) -> String {
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

fn guess_media_type(path_or_filename: &str) -> &'static str {
    let extension = Path::new(path_or_filename)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match extension.as_str() {
        "jpg" | "jpeg" => "image/jpeg",
        "png" => "image/png",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        "md" => "text/markdown",
        "json" => "application/json",
        "csv" => "text/csv",
        "html" | "htm" => "text/html",
        _ => "application/octet-stream",
    }
}

fn value_to_string(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Bool(value) => value.to_string(),
        _ => String::new(),
    }
}

fn denied_permissions_response() -> Value {
    json!({
        "permissions": {"fileSystem": {"entries": []}, "network": {"enabled": false}},
        "scope": "turn",
        "strictAutoReview": true,
    })
}

fn approved_permissions_response(params: &Map<String, Value>) -> Value {
    let permissions = params
        .get("permissions")
        .cloned()
        .unwrap_or_else(|| json!({"fileSystem": {"entries": []}, "network": {"enabled": false}}));
    let mut response = json!({
        "permissions": permissions,
        "scope": params
            .get("scope")
            .and_then(Value::as_str)
            .unwrap_or("turn"),
        "strictAutoReview": false,
    });
    if !matches!(response["scope"].as_str(), Some("turn" | "session")) {
        response["scope"] = json!("turn");
    }
    response
}

fn with_permission_response_defaults(mut response: Value) -> Value {
    if response.get("scope").is_none() {
        response["scope"] = json!("turn");
    }
    if !matches!(response["scope"].as_str(), Some("turn" | "session")) {
        response["scope"] = json!("turn");
    }
    if response.get("strictAutoReview").is_none() {
        response["strictAutoReview"] = json!(true);
    }
    response
}

fn trace_state_only_notification(method: &str, thread_id: Option<&str>) {
    debug!(
        method,
        thread_id = thread_id.unwrap_or(""),
        "Handled Codex app-server notification without publishing to Coworker"
    );
}

fn log_warning_notification(method: &str, thread_id: Option<&str>, params: &Map<String, Value>) {
    let warning_params = serialize_warning_params(params);
    warn!(
        target: "codex_app_server",
        method,
        thread_id = thread_id.unwrap_or(""),
        params = %warning_params,
        "Received Codex warning notification"
    );
}

fn serialize_warning_params(params: &Map<String, Value>) -> String {
    Value::Object(params.clone()).to_string()
}

fn server_request_id_field(mapping: &Map<String, Value>) -> Option<String> {
    string_field(mapping, "server_request_id")
        .filter(|s| !s.is_empty())
        .or_else(|| string_field(mapping, "serverRequestId").filter(|s| !s.is_empty()))
}

fn remember_thread_model(state: &mut BridgeState, thread_id: &str, value: &Map<String, Value>) {
    if let Some(model) = string_field(value, "model").filter(|s| !s.is_empty()) {
        state.thread_model.insert(thread_id.to_owned(), model);
    }
}

fn remember_thread_from_response(
    state: &mut BridgeState,
    thread_id: &str,
    response: &Value,
    default_status: Option<&str>,
) {
    if let Some(obj) = response.as_object() {
        remember_thread_model(state, thread_id, obj);
    }
    if let Some(thread) = response.get("thread").and_then(Value::as_object) {
        remember_thread_model(state, thread_id, thread);
        if let Some(status) = thread.get("status").and_then(Value::as_object) {
            remember_thread_status(state, thread_id, status);
            return;
        }
    }
    if let Some(status) = default_status {
        state
            .thread_status
            .insert(thread_id.to_owned(), status.into());
    }
}

fn remember_thread_status(state: &mut BridgeState, thread_id: &str, status: &Map<String, Value>) {
    if let Some(status_type) = string_field(status, "type") {
        state
            .thread_status
            .insert(thread_id.to_owned(), status_type.clone());
        let flags = status
            .get("activeFlags")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        state
            .thread_active_flags
            .insert(thread_id.to_owned(), flags);
        if status_type != "active" {
            state.thread_active_turn.remove(thread_id);
        }
    }
}

fn remember_thread_settings(
    state: &mut BridgeState,
    thread_id: &str,
    settings: &Map<String, Value>,
) {
    remember_thread_model(state, thread_id, settings);
    if let Some(mode) = settings
        .get("collaborationMode")
        .and_then(Value::as_object)
        .and_then(|m| string_field(m, "mode"))
    {
        state
            .thread_collaboration_mode
            .insert(thread_id.to_owned(), mode);
    }
}

fn remember_turn_from_response(state: &mut BridgeState, thread_id: &str, response: &Value) {
    if let Some(turn) = response.get("turn") {
        remember_turn(state, thread_id, turn);
    }
}

fn remember_turn(state: &mut BridgeState, thread_id: &str, turn: &Value) {
    if let Some(turn_id) = turn
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        state
            .thread_active_turn
            .insert(thread_id.to_owned(), turn_id.to_owned());
    }
    if let Some(status) = turn.get("status").and_then(Value::as_str) {
        let mapped = match status {
            "inProgress" | "running" => "active",
            "failed" => "failed",
            _ => "idle",
        };
        state
            .thread_status
            .insert(thread_id.to_owned(), mapped.into());
    }
}

fn extract_collaboration_modes(response: &Value) -> Vec<Value> {
    response
        .get("data")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(collaboration_mode_from_mask)
        .collect()
}

fn collaboration_mode_from_mask(value: &Value) -> Option<Value> {
    let obj = value.as_object()?;
    let mode = obj.get("mode")?.as_str()?;
    if !matches!(mode, "plan" | "default") {
        return None;
    }
    let mut settings = json!({
        "developer_instructions": Value::Null,
        "reasoning_effort": obj.get("reasoning_effort").and_then(Value::as_str),
    });
    if let Some(model) = obj
        .get("model")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        settings["model"] = json!(model);
    }
    Some(json!({"mode": mode, "settings": settings}))
}

fn thread_id_from_params(params: &Map<String, Value>) -> Option<String> {
    string_field(params, "threadId")
        .or_else(|| string_field(params, "thread_id"))
        .or_else(|| string_field(params, "conversationId"))
}

fn turn_id_from_params(params: &Map<String, Value>) -> Option<String> {
    string_field(params, "turnId").or_else(|| string_field(params, "turn_id"))
}

fn extract_thread_id(params: &Map<String, Value>) -> Option<String> {
    thread_id_from_params(params)
        .or_else(|| {
            params
                .get("thread")
                .and_then(Value::as_object)
                .and_then(|m| string_field(m, "id").or_else(|| thread_id_from_params(m)))
        })
        .or_else(|| {
            params
                .get("turn")
                .and_then(Value::as_object)
                .and_then(thread_id_from_params)
        })
        .or_else(|| {
            params
                .get("item")
                .and_then(Value::as_object)
                .and_then(thread_id_from_params)
        })
}

fn extract_error_message(error: Option<&Value>) -> Option<String> {
    match error {
        Some(Value::Object(obj)) => string_field(obj, "message"),
        Some(Value::String(value)) if !value.is_empty() => Some(value.clone()),
        _ => None,
    }
}

fn codex_error_kind(error: Option<&Value>) -> Option<&str> {
    error?.get("codexErrorInfo").and_then(Value::as_str)
}

fn server_request_id(
    params: &Map<String, Value>,
    request_id: &Value,
    fallback_index: usize,
) -> String {
    match request_id {
        Value::String(value) if !value.is_empty() => value.clone(),
        Value::Number(value) => value.to_string(),
        _ => params
            .get("itemId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("server-request-{fallback_index}")),
    }
}

fn user_input_resolve_hint(method: &str, params: &Map<String, Value>) -> Value {
    if method == "item/tool/requestUserInput" {
        let question_ids: Vec<_> = params
            .get("questions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|q| q.as_object().and_then(|o| string_field(o, "id")))
            .collect();
        return json!({
            "field": "answers",
            "question_ids": question_ids,
            "shape": {"answers": {"<question_id>": {"answers": ["<selected option label or free-form answer>"]}}},
            "note": "Use params.questions[].id as keys; answers must be an object.",
        });
    }
    if method == "mcpServer/elicitation/request" {
        return json!({
            "field": "response",
            "shape": {"response": {"action": "accept", "content": {"<field>": "<value>"}}},
            "note": "Pass the elicitation response object through response.",
        });
    }
    json!({
        "field": "response",
        "shape": {"response": {"<key>": "<value>"}},
        "note": "Unknown user-input method; pass the app-server response object.",
    })
}

fn default_pending_request_response(method: &str) -> Value {
    if method == "item/tool/requestUserInput" {
        json!({"answers": {}})
    } else {
        json!({})
    }
}

async fn join_or_abort_task<T>(name: &'static str, mut handle: JoinHandle<T>) {
    tokio::select! {
        result = &mut handle => {
            if let Err(error) = result
                && !error.is_cancelled()
            {
                warn!(task = name, %error, "Bridge task ended with an error during shutdown");
            }
        }
        _ = sleep(BRIDGE_TASK_SHUTDOWN_GRACE) => {
            warn!(task = name, "Bridge task did not stop within grace period; aborting");
            handle.abort();
            let _ = handle.await;
        }
    }
}

fn dynamic_tool_ok(text: impl Into<String>) -> Value {
    json!({"success": true, "contentItems": [{"type": "inputText", "text": text.into()}]})
}

fn dynamic_tool_error(text: impl Into<String>) -> Value {
    json!({"success": false, "contentItems": [{"type": "inputText", "text": text.into()}]})
}

fn parse_coworker_frontmatter_message(text: &str) -> Option<Value> {
    let re = Regex::new(
        r"(?s)\A[ \t]*---[ \t]*\r?\n(?P<header>.*?)(?:\r?\n)---[ \t]*(?:\r?\n(?P<body>.*))?\z",
    )
    .ok()?;
    let captures = re.captures(text)?;
    let header = parse_coworker_block_header(captures.name("header")?.as_str());
    if header.get("type").map(String::as_str) != Some(COWORKER_TOOL_CALL_TYPE) {
        return None;
    }
    let tool = header.get("tool").or_else(|| header.get("name"))?.as_str();
    if tool == LIST_COWORKERS_TOOL {
        return Some(json!({"name": LIST_COWORKERS_TOOL, "arguments": {}}));
    }
    if tool != SEND_TO_COWORKER_TOOL {
        return Some(json!({"name": tool, "arguments": {}}));
    }
    let coworker_id = header
        .get("to")
        .or_else(|| header.get("coworker_id"))
        .cloned();
    let message = captures
        .name("body")
        .map(|m| m.as_str().trim().to_owned())
        .unwrap_or_default();
    Some(json!({
        "name": SEND_TO_COWORKER_TOOL,
        "arguments": {"coworker_id": coworker_id, "message": message},
    }))
}

fn parse_coworker_block_header(header: &str) -> HashMap<String, String> {
    header
        .lines()
        .filter_map(|line| {
            let stripped = line.trim();
            if stripped.is_empty() || stripped.starts_with('#') || !stripped.contains(':') {
                return None;
            }
            let (key, value) = stripped.split_once(':')?;
            Some((
                key.trim().to_owned(),
                value.trim().trim_matches(&['"', '\''][..]).to_owned(),
            ))
        })
        .collect()
}

fn collect_assistant_texts(value: &Value, assistant_context: bool, texts: &mut Vec<String>) {
    match value {
        Value::String(text) if assistant_context => texts.push(text.clone()),
        Value::Array(items) => {
            for item in items {
                collect_assistant_texts(item, assistant_context, texts);
            }
        }
        Value::Object(obj) => {
            let context = assistant_context
                || obj.get("type").and_then(Value::as_str) == Some("agentMessage");
            if context && let Some(text) = obj.get("text").and_then(Value::as_str) {
                texts.push(text.to_owned());
            }
            for key in ["content", "items", "output"] {
                if let Some(value) = obj.get(key) {
                    collect_assistant_texts(value, context, texts);
                }
            }
        }
        _ => {}
    }
}

fn text_tool_result_message(tool: &str, result: &Value) -> String {
    let status = if result.get("success").and_then(Value::as_bool) == Some(true) {
        "success"
    } else {
        "error"
    };
    let text = result
        .get("contentItems")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(Value::as_object)
        .and_then(|item| item.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("");
    format!("[coworker text tool result]\ntool: {tool}\nstatus: {status}\n{text}")
}

fn sorted_set(values: &HashSet<String>) -> Vec<String> {
    let mut values: Vec<_> = values.iter().cloned().collect();
    values.sort();
    values
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        sync::Arc,
        time::{SystemTime, UNIX_EPOCH},
    };

    use tokio::sync::Mutex;

    use super::*;

    #[tokio::test]
    async fn shutdown_resolves_pending_server_requests() {
        let bridge = bridge_with(
            config(None),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        let (tx, rx) = oneshot::channel();
        {
            let mut state = bridge.state.lock().await;
            state
                .thread_pending_requests
                .entry("thr_1".into())
                .or_default()
                .insert(
                    "srv_1".into(),
                    json!({
                        "server_request_id": "srv_1",
                        "method": "item/tool/requestUserInput",
                    }),
                );
            state
                .pending_server_request_futures
                .insert("srv_1".into(), tx);
            state
                .pending_server_request_coworker
                .insert("srv_1".into(), DEFAULT_COWORKER_ID.into());
        }

        bridge.resolve_pending_server_requests_for_shutdown().await;

        assert_eq!(rx.await.expect("shutdown response"), json!({"answers": {}}));
        let state = bridge.state.lock().await;
        assert!(state.pending_server_request_futures.is_empty());
        assert!(state.pending_server_request_coworker.is_empty());
        assert!(state.thread_pending_requests.is_empty());
    }

    #[derive(Default)]
    struct FakeCodexClient {
        calls: Mutex<Vec<(String, Value)>>,
        responses: Mutex<VecDeque<Value>>,
    }

    impl FakeCodexClient {
        fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }

        async fn push_response(&self, response: Value) {
            self.responses.lock().await.push_back(response);
        }

        async fn calls(&self) -> Vec<(String, Value)> {
            self.calls.lock().await.clone()
        }
    }

    #[async_trait::async_trait]
    impl CodexClient for FakeCodexClient {
        async fn request(&self, method: &str, params: Value) -> Result<Value> {
            self.calls
                .lock()
                .await
                .push((method.to_owned(), params.clone()));
            if method == "collaborationMode/list" {
                return Ok(json!({
                    "data": [
                        {"name": "Default", "mode": "default", "model": Value::Null, "reasoning_effort": Value::Null},
                        {"name": "Plan", "mode": "plan", "model": Value::Null, "reasoning_effort": "medium"}
                    ]
                }));
            }
            if method == "model/list" {
                return Ok(json!({
                    "data": [{
                        "isDefault": true,
                        "model": "gpt-5.4",
                        "displayName": "GPT 5.4"
                    }]
                }));
            }
            if let Some(response) = self.responses.lock().await.pop_front() {
                return Ok(response);
            }
            if method == "thread/list" {
                return Ok(json!({
                    "data": [{
                        "id": "thr_1",
                        "name": "Bridge design",
                        "preview": "Bridge design",
                        "status": {"type": "idle"},
                        "updatedAt": 1782386400,
                        "project": {
                            "id": "coworker",
                            "name": "Coworker",
                            "path": "D:\\Projects\\coworker"
                        }
                    }]
                }));
            }
            if method == "turn/start" || method == "turn/steer" {
                return Ok(json!({"turn": {"id": "turn_1", "status": "running"}}));
            }
            Ok(json!({}))
        }
    }

    #[derive(Default)]
    struct RecordingTransport {
        bridge_posts: Mutex<Vec<(String, Value)>>,
        codex_messages: Mutex<Vec<(String, String, String, Vec<CoworkerMessageAttachment>)>>,
    }

    impl RecordingTransport {
        fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }

        async fn bridge_posts(&self) -> Vec<(String, Value)> {
            self.bridge_posts.lock().await.clone()
        }

        async fn codex_messages(
            &self,
        ) -> Vec<(String, String, String, Vec<CoworkerMessageAttachment>)> {
            self.codex_messages.lock().await.clone()
        }
    }

    fn config(state_path: Option<String>) -> BridgeConfig {
        BridgeConfig {
            codex_id: "codex-local".into(),
            display_name: "Local Codex".into(),
            coworkers: vec![BridgeCoworker {
                coworker_id: DEFAULT_COWORKER_ID.into(),
                display_name: "搭档".into(),
                base_url: "http://localhost:8000".into(),
            }],
            command: "codex".into(),
            args: vec!["app-server".into()],
            snapshot_thread_limit: 20,
            snapshot_scan_thread_limit: 200,
            snapshot_interval_seconds: 300,
            reconnect_seconds: 5,
            state_path,
            codex_home_dir: temp_attachment_dir("codex-home")
                .to_string_lossy()
                .into_owned(),
            session_overlay_dir: temp_attachment_dir("session-overlay")
                .to_string_lossy()
                .into_owned(),
            service_name: "coworker_desktop".into(),
            snapshot_source_kinds: vec!["cli".into(), "vscode".into(), "appServer".into()],
            permissions_mode: "read-only".into(),
            approvals_reviewer: "none".into(),
            approval_timeout_seconds: 300,
            auto_continue_interrupted_turns: true,
            auto_continue_interrupted_max_attempts: 3,
            auto_continue_interrupted_message: "继续".into(),
            attachment_store_dir: "data/coworker_desktop_attachments".into(),
            attachment_max_bytes: 20 * 1024 * 1024,
            attachment_max_count: 5,
            logs_dir: "data/logs".into(),
            log_level: "INFO".into(),
            file_log_level: "DEBUG".into(),
            chat_workspaces_dir: temp_attachment_dir("chat-workspaces")
                .to_string_lossy()
                .into_owned(),
        }
    }

    fn multi_config() -> BridgeConfig {
        let mut cfg = config(None);
        cfg.coworkers = vec![
            BridgeCoworker {
                coworker_id: "cw_01".into(),
                display_name: "搭档A".into(),
                base_url: "http://a".into(),
            },
            BridgeCoworker {
                coworker_id: "cw_02".into(),
                display_name: "搭档B".into(),
                base_url: "http://b".into(),
            },
        ];
        cfg
    }

    fn ask_coworker_config() -> BridgeConfig {
        let mut cfg = config(None);
        cfg.permissions_mode = "workspace-write".into();
        cfg.approvals_reviewer = "coworker".into();
        cfg.approval_timeout_seconds = 300;
        cfg
    }

    fn danger_full_access_config() -> BridgeConfig {
        let mut cfg = config(None);
        cfg.permissions_mode = "danger-full-access".into();
        cfg.approvals_reviewer = "none".into();
        cfg
    }

    fn temp_state_path(name: &str) -> String {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time")
            .as_nanos();
        std::env::temp_dir()
            .join(format!("coworker-bridge-{name}-{nonce}.json"))
            .to_string_lossy()
            .into_owned()
    }

    fn temp_attachment_dir(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("coworker-bridge-{name}-{nonce}"));
        std::fs::create_dir_all(&path).expect("attachment temp dir");
        path
    }

    #[tokio::test]
    async fn bridge_started_thread_ids_survive_state_replacement_and_restart() {
        let state_path = temp_state_path("replace-existing");
        std::fs::write(
            &state_path,
            r#"{"bridge_started_thread_ids":["thr_existing"]}"#,
        )
        .expect("existing state");
        let cfg = config(Some(state_path.clone()));
        let bridge = bridge_with(
            cfg.clone(),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        {
            let mut state = bridge.state.lock().await;
            state.bridge_started_thread_ids.insert("thr_new".into());
            bridge.save_state_locked(&state);
        }

        let restarted = bridge_with(cfg, FakeCodexClient::new(), RecordingTransport::new());
        let runtime = restarted.runtime_session_state().await;
        assert!(runtime.owned_thread_ids.contains("thr_existing"));
        assert!(runtime.owned_thread_ids.contains("thr_new"));

        let sessions = session::list_sessions(&restarted.config, &[], runtime, 10)
            .expect("restarted sessions");
        assert!(
            sessions
                .iter()
                .any(|session| { session.thread_id == "thr_new" && session.owned_by_bridge })
        );
        let _ = std::fs::remove_file(state_path);
    }

    #[tokio::test]
    async fn codex_conversation_list_includes_bridge_sources_after_restart() {
        let client = FakeCodexClient::new();
        client
            .push_response(json!({
                "data": [{
                    "id": "thr_recovered",
                    "name": "Recovered conversation",
                    "status": {"type": "notLoaded"},
                    "updatedAt": "2026-07-13T00:00:00Z"
                }]
            }))
            .await;
        let cfg = config(None);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        std::fs::create_dir_all(&session_dir).expect("session dir");
        std::fs::write(
            session_dir.join("rollout-thr_recovered.jsonl"),
            r#"{"type":"session_meta","payload":{"id":"thr_recovered","thread_source":"coworker-desktop","timestamp":"2026-07-13T00:00:00Z"}}"#,
        )
        .expect("session metadata");
        let bridge = bridge_with(cfg, client.clone(), RecordingTransport::new());

        let sessions = bridge
            .list_codex_conversations(10)
            .await
            .expect("Codex conversations");

        assert_eq!(sessions[0].thread_id, "thr_recovered");
        assert_eq!(sessions[0].title, "Recovered conversation");
        assert!(sessions[0].owned_by_bridge);
        assert!(client.calls().await[0].1.get("sourceKinds").is_none());
        let _ = std::fs::remove_dir_all(&bridge.config.codex_home_dir);
    }

    #[tokio::test]
    async fn codex_conversation_snapshot_uses_cursor_completeness_and_project_metadata() {
        let complete_client = FakeCodexClient::new();
        complete_client
            .push_response(json!({
                "data": [{
                    "id": "thr_complete",
                    "name": "Complete",
                    "project": {
                        "id": "native-project",
                        "name": "Native project",
                        "path": "D:\\worktrees\\one"
                    }
                }]
            }))
            .await;
        let complete_bridge = bridge_with(config(None), complete_client, RecordingTransport::new());

        let (conversations, complete) = complete_bridge
            .list_codex_session_snapshot(1)
            .await
            .expect("complete snapshot");
        assert!(complete);
        assert_eq!(
            conversations[0].project_id.as_deref(),
            Some("native-project")
        );
        assert_eq!(
            conversations[0].project_name.as_deref(),
            Some("Native project")
        );

        let incomplete_client = FakeCodexClient::new();
        incomplete_client
            .push_response(json!({
                "nextCursor": "cursor-2",
                "data": [{"id": "thr_incomplete", "name": "Incomplete"}]
            }))
            .await;
        let incomplete_bridge =
            bridge_with(config(None), incomplete_client, RecordingTransport::new());
        let (_, complete) = incomplete_bridge
            .list_codex_session_snapshot(1)
            .await
            .expect("incomplete snapshot");
        assert!(!complete);
    }

    fn bridge_with(
        cfg: BridgeConfig,
        client: Arc<FakeCodexClient>,
        transport: Arc<RecordingTransport>,
    ) -> Arc<CodexBridge> {
        let (outbound, mut requests) = mpsc::channel::<ActorOutboundRequest>(8);
        let outbound_transport = transport.clone();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                while let Some(request) = requests.recv().await {
                    let mut recorded = request.payload.clone();
                    recorded["type"] = json!(request.event_type);
                    if let Some(conversation_id) = request.conversation_id.as_deref() {
                        recorded["thread_id"] = json!(conversation_id);
                    }
                    outbound_transport
                        .bridge_posts
                        .lock()
                        .await
                        .push((request.coworker_id.clone(), recorded));
                    if let Some(message) = request.payload.get("message").and_then(Value::as_str) {
                        let attachments = request
                            .payload
                            .get("attachments")
                            .cloned()
                            .and_then(|value| serde_json::from_value(value).ok())
                            .unwrap_or_default();
                        outbound_transport.codex_messages.lock().await.push((
                            request.coworker_id.clone(),
                            request.conversation_id.clone().unwrap_or_default(),
                            message.to_owned(),
                            attachments,
                        ));
                    }
                    let _ = request.response.send(Ok(()));
                }
            });
        }
        CodexBridge::new_with_client(cfg, client, outbound).expect("bridge")
    }

    #[tokio::test]
    async fn native_codex_history_can_be_resumed_without_becoming_bridge_owned() {
        let client = FakeCodexClient::new();
        client
            .push_response(json!({
                "thread": {"id": "external_thread", "status": {"type": "idle"}}
            }))
            .await;
        client
            .push_response(json!({"turn": {"id": "turn_external", "status": "running"}}))
            .await;
        let cfg = config(None);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions/2026/07/25");
        fs::create_dir_all(&session_dir).expect("session dir");
        fs::write(
            session_dir.join("rollout-external_thread.jsonl"),
            r#"{"type":"session_meta","payload":{"id":"external_thread","source":"vscode"}}"#,
        )
        .expect("native Codex history");
        let bridge = bridge_with(cfg, client.clone(), RecordingTransport::new());

        bridge
            .send_actor_conversation_message(
                Some("external_thread".into()),
                "hello".into(),
                Vec::new(),
                None,
                None,
                None,
                "local".into(),
                Some("desktop-test".into()),
                Some("本机".into()),
                None,
            )
            .await
            .expect("native Codex history should continue");

        bridge
            .set_codex_conversation_mode("external_thread", "plan")
            .await
            .expect("native Codex history should allow mode changes");

        bridge
            .rename_codex_conversation("external_thread", "renamed")
            .await
            .expect("native Codex history should allow rename");

        let calls = client.calls().await;
        assert_eq!(calls[0].0, "thread/resume");
        assert_eq!(calls[0].1["threadId"], "external_thread");
        assert!(calls.iter().any(|(method, _)| method == "turn/start"));
        assert!(calls.iter().any(|(method, _)| method == "thread/name/set"));
        assert!(
            !bridge
                .state
                .lock()
                .await
                .bridge_started_thread_ids
                .contains("external_thread")
        );
    }

    #[tokio::test]
    async fn unknown_codex_thread_is_rejected_before_an_overlay_is_written() {
        let bridge = bridge_with(
            config(None),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );

        let error = bridge
            .send_actor_conversation_message(
                Some("unknown_thread".into()),
                "hello".into(),
                Vec::new(),
                None,
                None,
                Some("message-unknown".into()),
                "coworker".into(),
                Some("cw_02".into()),
                Some("搭档 B".into()),
                Some("cw_02".into()),
            )
            .await
            .expect_err("unknown Codex thread should be rejected");

        assert!(
            error
                .to_string()
                .contains("thread/resume response missing thread.id")
        );
        let page = session::load_session_messages(&bridge.config, "unknown_thread", None, 20)
            .expect("empty page");
        assert!(page.messages.is_empty());
    }

    #[tokio::test]
    async fn actor_conversation_create_forwards_project_attachments_and_source_overlay() {
        let client = FakeCodexClient::new();
        client
            .push_response(json!({"thread": {"id": "thr_desktop", "status": {"type": "idle"}}}))
            .await;
        let attachment_store = temp_attachment_dir("desktop-actor-store");
        let mut cfg = multi_config();
        cfg.attachment_store_dir = attachment_store.to_string_lossy().into_owned();
        let transport = RecordingTransport::new();
        let bridge = bridge_with(cfg, client.clone(), transport.clone());
        let attachment_dir = temp_attachment_dir("desktop-actor-input");
        let attachment_path = attachment_dir.join("brief.txt");
        std::fs::write(&attachment_path, "brief").expect("attachment");
        let attachment_paths = vec![attachment_path.to_string_lossy().into_owned()];

        let result = bridge
            .send_actor_conversation_message(
                None,
                "请查看附件".into(),
                attachment_paths,
                None,
                Some("D:\\Projects\\demo".into()),
                Some("desktop-message-1".into()),
                "coworker".into(),
                Some("cw_02".into()),
                Some("搭档 B".into()),
                Some("cw_02".into()),
            )
            .await
            .expect("actor conversation message");

        assert_eq!(result["conversation_id"], "thr_desktop");
        let calls = client.calls().await;
        assert_eq!(calls[0].0, "thread/start");
        assert_eq!(calls[0].1["cwd"], "D:\\Projects\\demo");
        let prompt = calls[1].1["input"][0]["text"].as_str().expect("prompt");
        assert!(prompt.starts_with("[来自Coworker:cw_02][搭档 B]的消息:\n请查看附件"));
        assert_eq!(calls[2].0, "thread/name/set");
        assert_eq!(calls[2].1["name"], "请查看附件");
        let page = session::load_session_messages(&bridge.config, "thr_desktop", None, 20)
            .expect("Codex messages");
        let message = page.messages.last().expect("source overlay");
        assert_eq!(message.id, "overlay-desktop-message-1");
        assert_eq!(message.author_kind, "coworker");
        assert_eq!(message.author_id.as_deref(), Some("cw_02"));
        assert_eq!(message.author_label, "搭档 B");
        assert_eq!(message.attachments.len(), 1);

        bridge
            .handle_app_server_request(
                "item/commandExecution/requestApproval",
                json!({"threadId": "thr_desktop", "command": "echo hi"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("approval-1"),
            )
            .await
            .expect("approval response");
        assert_eq!(transport.bridge_posts().await[0].0, "cw_02");

        let _ = std::fs::remove_dir_all(attachment_dir);
        let _ = std::fs::remove_dir_all(attachment_store);
    }

    #[tokio::test]
    async fn rename_codex_conversation_uses_thread_name_set_for_owned_thread() {
        let client = FakeCodexClient::new();
        client.push_response(json!({"ok": true})).await;
        let bridge = bridge_with(config(None), client.clone(), RecordingTransport::new());
        bridge
            .state
            .lock()
            .await
            .bridge_started_thread_ids
            .insert("thr_1".into());

        let result = bridge
            .rename_codex_conversation("thr_1", "  New    Session   Title  ")
            .await
            .expect("rename should succeed");

        assert_eq!(result["ok"], true);
        assert_eq!(result["title"], "New Session Title");
        assert_eq!(
            client.calls().await[0],
            (
                "thread/name/set".into(),
                json!({
                    "threadId": "thr_1",
                    "name": "New Session Title",
                })
            )
        );
    }

    #[tokio::test]
    async fn thread_settings_updated_tracks_collaboration_mode() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        bridge
            .handle_notification(json!({
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "thr_1",
                    "threadSettings": {
                        "collaborationMode": {"mode": "plan", "settings": {"model": "gpt-5.4"}}
                    }
                }
            }))
            .await;
        assert_eq!(
            bridge.state.lock().await.thread_collaboration_mode["thr_1"],
            "plan"
        );
        assert!(transport.bridge_posts().await.is_empty());
    }

    #[tokio::test]
    async fn pending_collaboration_mode_restored_from_state() {
        let state_path = temp_state_path("pending-mode");
        std::fs::write(
            &state_path,
            r#"{"pending_collaboration_modes":{"thr_1":"plan"}}"#,
        )
        .expect("state");
        let bridge = bridge_with(
            config(Some(state_path)),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );

        assert_eq!(
            bridge
                .runtime_session_state()
                .await
                .thread_pending_collaboration_mode["thr_1"],
            "plan"
        );
    }

    #[test]
    fn extract_collaboration_modes_rejects_non_schema_response_shapes() {
        assert!(
            extract_collaboration_modes(&json!({
                "collaboration_modes": [{"mode": "default"}]
            }))
            .is_empty()
        );
        assert!(
            extract_collaboration_modes(&json!({
                "collaborationModes": {"default": {"settings": {"model": "gpt-5.4"}}}
            }))
            .is_empty()
        );
    }

    #[test]
    fn extract_collaboration_modes_accepts_experimental_mask_response() {
        let modes = extract_collaboration_modes(&json!({
            "data": [
                {"name": "Default", "mode": "default", "model": "gpt-5.4", "reasoning_effort": "medium"},
                {"name": "Plan", "mode": "plan", "model": "gpt-5.4", "reasoning_effort": "high"}
            ]
        }));

        assert_eq!(modes.len(), 2);
        assert_eq!(modes[1]["mode"], "plan");
        assert_eq!(
            modes[1]["settings"],
            json!({
                "developer_instructions": Value::Null,
                "model": "gpt-5.4",
                "reasoning_effort": "high"
            })
        );
    }

    #[test]
    fn extract_collaboration_modes_accepts_nullable_schema_mask_fields() {
        let modes = extract_collaboration_modes(&json!({
            "data": [
                {"name": "No mode", "mode": Value::Null, "model": "gpt-5.4"},
                {"name": "Plan", "mode": "plan", "model": Value::Null, "reasoning_effort": "medium"},
                {"name": "Default", "mode": "default", "model": Value::Null, "reasoning_effort": Value::Null}
            ]
        }));

        assert_eq!(
            modes,
            vec![
                json!({"mode": "plan", "settings": {"developer_instructions": Value::Null, "reasoning_effort": "medium"}}),
                json!({"mode": "default", "settings": {"developer_instructions": Value::Null, "reasoning_effort": Value::Null}})
            ]
        );
    }

    #[test]
    fn extract_thread_id_accepts_plan_update_event_shapes() {
        let snake_case = json!({"thread_id": "thr_snake"});
        assert_eq!(
            extract_thread_id(snake_case.as_object().unwrap()).as_deref(),
            Some("thr_snake")
        );

        let nested_turn = json!({"turn": {"threadId": "thr_turn"}});
        assert_eq!(
            extract_thread_id(nested_turn.as_object().unwrap()).as_deref(),
            Some("thr_turn")
        );

        let nested_item = json!({"item": {"thread_id": "thr_item"}});
        assert_eq!(
            extract_thread_id(nested_item.as_object().unwrap()).as_deref(),
            Some("thr_item")
        );
    }

    #[tokio::test]
    async fn interrupted_turn_notification_auto_continues() {
        let client = FakeCodexClient::new();
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), client.clone(), transport.clone());

        bridge
            .handle_notification_async(json!({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_1",
                    "turn": {"id": "turn_1", "status": "interrupted"}
                }
            }))
            .await;

        let calls = client.calls().await;
        assert_eq!(calls.last().unwrap().0, "turn/start");
        assert_eq!(calls.last().unwrap().1["input"][0]["text"], "继续");
        // turn/completed 是结构性通知，不再转发给 coworker（避免空消息）
        let posts = transport.bridge_posts().await;
        assert!(
            posts
                .iter()
                .all(|(_, payload)| payload["event_type"].as_str() != Some("turn/completed")),
            "turn/completed should not be forwarded to coworker"
        );
    }

    #[tokio::test]
    async fn turn_started_notification_updates_state_without_publishing() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        bridge
            .handle_notification(json!({
                "method": "turn/started",
                "params": {
                    "threadId": "thr_1",
                    "turn": {"id": "turn_1", "status": "inProgress"}
                }
            }))
            .await;

        {
            let state = bridge.state.lock().await;
            assert_eq!(
                state.thread_status.get("thr_1").map(String::as_str),
                Some("active")
            );
            assert_eq!(
                state.thread_active_turn.get("thr_1").map(String::as_str),
                Some("turn_1")
            );
        }
        // turn/started 是结构性通知，不再转发给 coworker（避免空消息）
        let posts = transport.bridge_posts().await;
        assert!(
            posts
                .iter()
                .all(|(_, payload)| payload["event_type"].as_str() != Some("turn/started")),
            "turn/started should not be forwarded to coworker"
        );
    }

    #[tokio::test]
    async fn stage_notifications_are_published_and_metadata_notifications_stay_local() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        bridge
            .state
            .lock()
            .await
            .thread_last_error
            .insert("thr_1".into(), "old error".into());

        for method in [
            "thread/name/updated",
            "thread/unarchived",
            "thread/goal/updated",
            "thread/goal/cleared",
            "thread/compacted",
            "thread/tokenUsage/updated",
            "turn/diff/updated",
            "turn/plan/updated",
        ] {
            bridge
                .handle_notification(json!({
                    "method": method,
                    "params": {
                        "threadId": "thr_1",
                        "value": method
                    }
                }))
                .await;
        }

        assert!(
            !bridge
                .state
                .lock()
                .await
                .thread_last_error
                .contains_key("thr_1")
        );
        let posts = transport.bridge_posts().await;
        let methods: Vec<_> = posts
            .iter()
            .map(|(_, payload)| payload["event_type"].as_str().unwrap().to_owned())
            .collect();
        // 这些结构性通知均无 message 字段，不再转发给 coworker
        assert!(methods.is_empty());
        assert!(
            posts
                .iter()
                .all(|(_, payload)| payload["thread_id"] == "thr_1")
        );
    }

    #[tokio::test]
    async fn high_frequency_and_unknown_notifications_are_state_only() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        for method in [
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/fileChange/patchUpdated",
            "item/mcpToolCall/progress",
            "process/outputDelta",
            "fs/changed",
            "unknown/newNotification",
        ] {
            bridge
                .handle_notification(json!({
                    "method": method,
                    "params": {
                        "threadId": "thr_1",
                        "value": method
                    }
                }))
                .await;
        }

        assert!(transport.bridge_posts().await.is_empty());
    }

    #[tokio::test]
    async fn closing_thread_notifications_clear_runtime_state_and_publish() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        {
            let mut state = bridge.state.lock().await;
            state.thread_status.insert("thr_1".into(), "active".into());
            state
                .thread_active_turn
                .insert("thr_1".into(), "turn_1".into());
            state
                .thread_active_flags
                .insert("thr_1".into(), vec!["busy".into()]);
            state
                .thread_last_error
                .insert("thr_1".into(), "boom".into());
            let (tx, _rx) = oneshot::channel();
            state
                .pending_server_request_futures
                .insert("srv_1".into(), tx);
            state
                .pending_server_request_coworker
                .insert("srv_1".into(), DEFAULT_COWORKER_ID.into());
            state.thread_pending_requests.insert(
                "thr_1".into(),
                HashMap::from([(
                    "srv_1".into(),
                    json!({"method": "item/tool/requestUserInput"}),
                )]),
            );
        }

        bridge
            .handle_notification(json!({
                "method": "thread/closed",
                "params": {"threadId": "thr_1"}
            }))
            .await;

        {
            let state = bridge.state.lock().await;
            assert!(!state.thread_status.contains_key("thr_1"));
            assert!(!state.thread_active_turn.contains_key("thr_1"));
            assert!(!state.thread_active_flags.contains_key("thr_1"));
            assert!(!state.thread_last_error.contains_key("thr_1"));
            assert!(!state.thread_pending_requests.contains_key("thr_1"));
            assert!(!state.pending_server_request_futures.contains_key("srv_1"));
            assert!(!state.pending_server_request_coworker.contains_key("srv_1"));
        }
        // thread/closed 是结构性通知，不再转发给 coworker（避免空消息）
        let posts = transport.bridge_posts().await;
        assert!(
            posts
                .iter()
                .all(|(_, payload)| payload["event_type"].as_str() != Some("thread/closed")),
            "thread/closed should not be forwarded to coworker"
        );
    }

    #[tokio::test]
    async fn thread_deleted_resolves_pending_user_input_request() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        let bridge_for_request = bridge.clone();
        let params = json!({
            "threadId": "thr_1",
            "itemId": "item_1",
            "questions": [{
                "id": "choice",
                "question": "Pick one",
                "options": [{"label": "A", "description": "Option A"}]
            }]
        })
        .as_object()
        .unwrap()
        .clone();
        let pending = tokio::spawn(async move {
            bridge_for_request
                .handle_app_server_request("item/tool/requestUserInput", params, json!("srv_1"))
                .await
        });

        for _ in 0..50 {
            if bridge
                .state
                .lock()
                .await
                .pending_server_request_futures
                .contains_key("srv_1")
            {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert!(
            bridge
                .state
                .lock()
                .await
                .pending_server_request_futures
                .contains_key("srv_1")
        );

        bridge
            .handle_notification(json!({
                "method": "thread/deleted",
                "params": {"threadId": "thr_1"}
            }))
            .await;

        let result = tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .expect("pending user input request should be resolved during cleanup")
            .expect("task should complete")
            .expect("request should succeed");
        assert_eq!(result, json!({"answers": {}}));
        {
            let state = bridge.state.lock().await;
            assert!(!state.thread_pending_requests.contains_key("thr_1"));
            assert!(!state.pending_server_request_futures.contains_key("srv_1"));
            assert!(!state.pending_server_request_coworker.contains_key("srv_1"));
        }
        // thread/deleted 是结构性通知，不再转发给 coworker（避免空消息）
        let posts = transport.bridge_posts().await;
        assert!(
            posts
                .iter()
                .all(|(_, payload)| payload["event_type"].as_str() != Some("thread/deleted")),
            "thread/deleted should not be forwarded to coworker"
        );
    }

    #[tokio::test]
    async fn warning_notifications_are_not_published() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        bridge
            .handle_notification(json!({
                "method": "warning",
                "params": {"message": "background note"}
            }))
            .await;
        bridge
            .handle_notification(json!({
                "method": "guardianWarning",
                "params": {"message": "guardian check"}
            }))
            .await;
        bridge
            .handle_notification(json!({
                "method": "configWarning",
                "params": {"message": "config failed to load"}
            }))
            .await;
        bridge
            .handle_notification(json!({
                "method": "deprecationNotice",
                "params": {"message": "old option"}
            }))
            .await;
        bridge
            .handle_notification(json!({
                "method": "warning",
                "params": {"threadId": "thr_1", "message": "thread warning"}
            }))
            .await;
        bridge
            .handle_notification(json!({
                "method": "windows/worldWritableWarning",
                "params": {
                    "threadId": "thr_1",
                    "message": "workspace permissions are too broad",
                    "path": "C:\\workspace"
                }
            }))
            .await;

        assert!(transport.bridge_posts().await.is_empty());
    }

    #[test]
    fn warning_log_serialization_preserves_complete_params() {
        let params = json!({
            "threadId": "thr_1",
            "message": "first line\nsecond line",
            "details": {
                "path": "C:\\workspace",
                "reasons": ["world writable", "untrusted owner"]
            }
        });
        let params = params.as_object().expect("warning params");
        let serialized = serialize_warning_params(params);

        assert_eq!(
            serde_json::from_str::<Value>(&serialized).expect("serialized warning params"),
            Value::Object(params.clone())
        );
    }

    #[tokio::test]
    async fn interrupted_turn_auto_continue_respects_attempt_limit() {
        let mut cfg = config(None);
        cfg.auto_continue_interrupted_max_attempts = 1;
        let client = FakeCodexClient::new();
        let bridge = bridge_with(cfg, client.clone(), RecordingTransport::new());

        for turn_id in ["turn_1", "turn_2"] {
            bridge
                .handle_notification_async(json!({
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr_1",
                        "turn": {"id": turn_id, "status": "interrupted"}
                    }
                }))
                .await;
        }

        let starts = client
            .calls()
            .await
            .into_iter()
            .filter(|(method, _)| method == "turn/start")
            .count();
        assert_eq!(starts, 1);
    }

    #[tokio::test]
    async fn list_coworkers_dynamic_tool_returns_configured_ids() {
        let bridge = bridge_with(
            multi_config(),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );

        let result = bridge
            .handle_app_server_request(
                "item/tool/call",
                json!({"tool": LIST_COWORKERS_TOOL})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!(1),
            )
            .await
            .expect("tool result");

        assert_eq!(result["success"], true);
        let text = result["contentItems"][0]["text"].as_str().unwrap();
        assert!(text.contains("cw_01"));
        assert!(text.contains("搭档B"));
    }

    #[tokio::test]
    async fn current_time_read_returns_unix_seconds() {
        let bridge = bridge_with(
            config(None),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        let before = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time")
            .as_secs();

        let result = bridge
            .handle_app_server_request(
                "currentTime/read",
                json!({}).as_object().unwrap().clone(),
                json!("srv_time"),
            )
            .await
            .expect("time result");

        let after = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time")
            .as_secs();
        let current = result["currentTimeAt"].as_u64().expect("unix seconds");
        assert!(current >= before);
        assert!(current <= after);
    }

    #[tokio::test]
    async fn legacy_apply_patch_approval_notifies_and_denies() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        let result = bridge
            .handle_app_server_request(
                "applyPatchApproval",
                json!({"conversationId": "thr_1", "callId": "patch_1", "fileChanges": {}})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("legacy_patch"),
            )
            .await
            .expect("legacy approval result");

        assert_eq!(result["decision"], "denied");
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.approval.requested");
        assert_eq!(posts[0].1["method"], "applyPatchApproval");
        assert_eq!(posts[0].1["decision"], "denied");
        assert_eq!(posts[0].1["thread_id"], "thr_1");
    }

    #[tokio::test]
    async fn legacy_exec_command_approval_notifies_and_denies() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        let result = bridge
            .handle_app_server_request(
                "execCommandApproval",
                json!({"conversationId": "thr_1", "callId": "exec_1", "command": "npm test"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("legacy_exec"),
            )
            .await
            .expect("legacy approval result");

        assert_eq!(result["decision"], "denied");
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.approval.requested");
        assert_eq!(posts[0].1["method"], "execCommandApproval");
        assert_eq!(posts[0].1["decision"], "denied");
        assert_eq!(posts[0].1["params"]["command"], "npm test");
    }

    #[tokio::test]
    async fn auth_refresh_request_publishes_error_and_returns_error() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        let error = bridge
            .handle_app_server_request(
                "account/chatgptAuthTokens/refresh",
                json!({"reason": "unauthorized"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("srv_auth"),
            )
            .await
            .expect_err("auth refresh should be unsupported");

        assert!(error.to_string().contains("token refresh"));
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.error");
        assert_eq!(posts[0].1["method"], "account/chatgptAuthTokens/refresh");
        assert_eq!(posts[0].1["server_request_id"], "srv_auth");
        assert!(
            posts[0].1["message"]
                .as_str()
                .unwrap()
                .contains("token refresh")
        );
    }

    #[tokio::test]
    async fn attestation_generate_publishes_error_and_returns_error() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        let error = bridge
            .handle_app_server_request(
                "attestation/generate",
                json!({}).as_object().unwrap().clone(),
                json!("srv_attest"),
            )
            .await
            .expect_err("attestation should be unsupported");

        assert!(error.to_string().contains("attestation"));
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.error");
        assert_eq!(posts[0].1["method"], "attestation/generate");
        assert_eq!(posts[0].1["server_request_id"], "srv_attest");
        assert!(
            posts[0].1["message"]
                .as_str()
                .unwrap()
                .contains("attestation")
        );
    }

    #[tokio::test]
    async fn send_to_coworker_dynamic_tool_posts_to_selected_coworker_only() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(multi_config(), FakeCodexClient::new(), transport.clone());

        let result = bridge
            .handle_app_server_request(
                "item/tool/call",
                json!({
                    "tool": SEND_TO_COWORKER_TOOL,
                    "threadId": "thr_1",
                    "arguments": {"coworker_id": "cw_02", "message": "完成了"}
                })
                .as_object()
                .unwrap()
                .clone(),
                json!(1),
            )
            .await
            .expect("tool result");

        assert_eq!(result["success"], true);
        assert_eq!(
            transport.codex_messages().await,
            vec![("cw_02".into(), "thr_1".into(), "完成了".into(), vec![])]
        );
    }

    #[tokio::test]
    async fn send_to_coworker_dynamic_tool_posts_attachments_with_conversation() {
        let attachment_dir = temp_attachment_dir("send-attachments");
        let file_path = attachment_dir.join("note.txt");
        std::fs::write(&file_path, "hello coworker").expect("attachment source");
        let transport = RecordingTransport::new();
        let bridge = bridge_with(multi_config(), FakeCodexClient::new(), transport.clone());

        let result = bridge
            .handle_app_server_request(
                "item/tool/call",
                json!({
                    "tool": SEND_TO_COWORKER_TOOL,
                    "threadId": "thr_1",
                    "arguments": {
                        "coworker_id": "cw_02",
                        "attachments": [{"path": file_path.to_string_lossy()}]
                    }
                })
                .as_object()
                .unwrap()
                .clone(),
                json!(1),
            )
            .await
            .expect("tool result");

        assert_eq!(result["success"], true);
        assert_eq!(
            transport.codex_messages().await,
            vec![(
                "cw_02".into(),
                "thr_1".into(),
                "".into(),
                vec![CoworkerMessageAttachment {
                    filename: "note.txt".into(),
                    media_type: "text/plain".into(),
                    data: BASE64_STANDARD.encode("hello coworker"),
                }],
            )]
        );
    }

    #[tokio::test]
    async fn send_to_coworker_dynamic_tool_returns_tool_error_for_non_object_arguments() {
        let bridge = bridge_with(
            multi_config(),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );

        let result = bridge
            .handle_app_server_request(
                "item/tool/call",
                json!({
                    "tool": SEND_TO_COWORKER_TOOL,
                    "threadId": "thr_1",
                    "arguments": "bad"
                })
                .as_object()
                .unwrap()
                .clone(),
                json!(1),
            )
            .await
            .expect("tool result");

        assert_eq!(result["success"], false);
        assert_eq!(
            result["contentItems"][0]["text"],
            "arguments must be an object"
        );
    }

    #[tokio::test]
    async fn command_approval_notifies_and_declines() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        {
            bridge
                .state
                .lock()
                .await
                .thread_last_coworker
                .insert("thr_1".into(), DEFAULT_COWORKER_ID.into());
        }

        let result = bridge
            .handle_app_server_request(
                "item/commandExecution/requestApproval",
                json!({"threadId": "thr_1", "command": "echo hi"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!(42),
            )
            .await
            .expect("approval");

        assert_eq!(result["decision"], "decline");
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.approval.requested");
        assert_eq!(posts[0].1["server_request_id"], "42");
    }

    #[tokio::test]
    async fn ask_coworker_command_approval_waits_for_resolution() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(
            ask_coworker_config(),
            FakeCodexClient::new(),
            transport.clone(),
        );
        let task_bridge = bridge.clone();
        let pending = tokio::spawn(async move {
            task_bridge
                .handle_app_server_request(
                    "item/commandExecution/requestApproval",
                    json!({"threadId": "thr_1", "command": "echo hi"})
                        .as_object()
                        .unwrap()
                        .clone(),
                    json!("srv_approve"),
                )
                .await
                .expect("approval")
        });
        tokio::task::yield_now().await;

        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.approval.requested");
        assert_eq!(posts[0].1["status"], "pending");
        assert!(posts[0].1.get("decision").is_none());

        let result = bridge
            .resolve_server_request_for_desktop(
                DEFAULT_COWORKER_ID,
                "thr_1",
                "srv_approve",
                json!({"decision": "accept"}),
            )
            .await
            .expect("resolve approval");

        assert_eq!(result["ok"], true);
        assert_eq!(pending.await.unwrap()["decision"], "accept");
    }

    #[tokio::test]
    async fn user_input_request_waits_for_coworker_resolution() {
        let bridge = bridge_with(
            config(None),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        let task_bridge = bridge.clone();
        let pending = tokio::spawn(async move {
            task_bridge
                .handle_app_server_request(
                    "item/tool/requestUserInput",
                    json!({
                        "threadId": "thr_1",
                        "questions": [{"id": "choice", "options": [{"label": "A"}]}]
                    })
                    .as_object()
                    .unwrap()
                    .clone(),
                    json!("srv_1"),
                )
                .await
                .expect("user input")
        });
        tokio::task::yield_now().await;

        let result = bridge
            .resolve_server_request_for_desktop(
                DEFAULT_COWORKER_ID,
                "thr_1",
                "srv_1",
                json!({"answers": {"choice": {"answers": ["A"]}}}),
            )
            .await
            .expect("resolve input");

        assert_eq!(result["ok"], true);
        assert_eq!(
            pending.await.unwrap()["answers"]["choice"]["answers"][0],
            "A"
        );
    }

    #[tokio::test]
    async fn permissions_approval_notifies_and_returns_empty_permissions() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());

        let result = bridge
            .handle_app_server_request(
                "item/permissions/requestApproval",
                json!({"threadId": "thr_1", "scope": "turn"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("srv_perm"),
            )
            .await
            .expect("permissions result");

        assert_eq!(result["permissions"]["fileSystem"]["entries"], json!([]));
        assert_eq!(result["permissions"]["network"]["enabled"], false);
        assert_eq!(result["scope"], "turn");
        assert_eq!(result["strictAutoReview"], true);
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.approval.requested");
        assert_eq!(posts[0].1["server_request_id"], "srv_perm");
    }

    #[tokio::test]
    async fn ask_coworker_permissions_approval_uses_response_payload() {
        let bridge = bridge_with(
            ask_coworker_config(),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        let task_bridge = bridge.clone();
        let pending = tokio::spawn(async move {
            task_bridge
                .handle_app_server_request(
                    "item/permissions/requestApproval",
                    json!({
                        "threadId": "thr_1",
                        "permissions": {
                            "fileSystem": {"entries": []},
                            "network": {"enabled": true}
                        }
                    })
                    .as_object()
                    .unwrap()
                    .clone(),
                    json!("srv_perm"),
                )
                .await
                .expect("permissions approval")
        });
        tokio::task::yield_now().await;

        let result = bridge
            .resolve_server_request_for_desktop(
                DEFAULT_COWORKER_ID,
                "thr_1",
                "srv_perm",
                json!({
                    "permissions": {
                        "fileSystem": {"entries": []},
                        "network": {"enabled": true}
                    },
                    "scope": "session",
                    "strictAutoReview": false
                }),
            )
            .await
            .expect("resolve permissions");

        assert_eq!(result["ok"], true);
        let approval = pending.await.unwrap();
        assert_eq!(approval["permissions"]["network"]["enabled"], true);
        assert_eq!(approval["scope"], "session");
        assert_eq!(approval["strictAutoReview"], false);
    }

    #[tokio::test]
    async fn approval_timeout_fails_closed() {
        let mut cfg = ask_coworker_config();
        cfg.approval_timeout_seconds = 0;
        let bridge = bridge_with(cfg, FakeCodexClient::new(), RecordingTransport::new());

        let result = bridge
            .handle_app_server_request(
                "item/commandExecution/requestApproval",
                json!({"threadId": "thr_1", "command": "echo hi"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("srv_timeout"),
            )
            .await
            .expect("approval");

        assert_eq!(result["decision"], "decline");
        assert!(
            bridge
                .state
                .lock()
                .await
                .pending_server_request_futures
                .is_empty()
        );
    }

    #[tokio::test]
    async fn danger_full_access_approval_accepts_without_pending_request() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(
            danger_full_access_config(),
            FakeCodexClient::new(),
            transport.clone(),
        );

        let result = bridge
            .handle_app_server_request(
                "item/commandExecution/requestApproval",
                json!({"threadId": "thr_1", "command": "echo hi"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("srv_approve"),
            )
            .await
            .expect("approval");

        assert_eq!(result["decision"], "accept");
        assert!(
            bridge
                .state
                .lock()
                .await
                .pending_server_request_futures
                .is_empty()
        );
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["decision"], "accept");
    }

    #[tokio::test]
    async fn mcp_elicitation_uses_response_payload_from_coworker_resolution() {
        let bridge = bridge_with(
            config(None),
            FakeCodexClient::new(),
            RecordingTransport::new(),
        );
        let task_bridge = bridge.clone();
        let pending = tokio::spawn(async move {
            task_bridge
                .handle_app_server_request(
                    "mcpServer/elicitation/request",
                    json!({"threadId": "thr_1", "message": "Need value"})
                        .as_object()
                        .unwrap()
                        .clone(),
                    json!("srv_1"),
                )
                .await
                .expect("elicitation")
        });
        tokio::task::yield_now().await;

        let result = bridge
            .resolve_server_request_for_desktop(
                DEFAULT_COWORKER_ID,
                "thr_1",
                "srv_1",
                json!({"action": "accept", "content": {"field": "value"}}),
            )
            .await
            .expect("resolve elicitation");

        assert_eq!(result["ok"], true);
        assert_eq!(pending.await.unwrap()["action"], "accept");
    }

    #[tokio::test]
    async fn server_request_resolved_notification_clears_pending_and_posts_event() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        {
            let mut state = bridge.state.lock().await;
            state
                .thread_pending_requests
                .entry("thr_1".into())
                .or_default()
                .insert(
                    "srv_1".into(),
                    json!({"server_request_id": "srv_1", "method": "item/tool/requestUserInput"}),
                );
            state
                .pending_server_request_coworker
                .insert("srv_1".into(), DEFAULT_COWORKER_ID.into());
        }

        bridge
            .handle_notification(json!({
                "method": "serverRequest/resolved",
                "params": {"threadId": "thr_1", "requestId": "srv_1"}
            }))
            .await;

        assert!(
            !bridge
                .state
                .lock()
                .await
                .thread_pending_requests
                .contains_key("thr_1")
        );
        let posts = transport.bridge_posts().await;
        assert_eq!(posts[0].1["type"], "desktop.server_request.resolved");
        assert_eq!(posts[0].1["server_request_id"], "srv_1");
    }

    #[tokio::test]
    async fn failed_turn_updates_runtime_status() {
        let client = FakeCodexClient::new();
        client
            .push_response(json!({
                "data": [{
                    "id": "thr_1",
                    "name": "Failing",
                    "preview": "Failing",
                    "status": {"type": "active", "activeFlags": ["busy", "waiting"]},
                    "updatedAt": 1,
                    "project": {"id": "coworker", "name": "Coworker"}
                }]
            }))
            .await;
        let bridge = bridge_with(config(None), client, RecordingTransport::new());
        bridge
            .handle_notification(json!({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_1",
                    "turn": {
                        "id": "turn_1",
                        "status": "failed",
                        "error": {"message": "boom"}
                    }
                }
            }))
            .await;

        let state = bridge.state.lock().await;
        assert_eq!(state.thread_status["thr_1"], "failed");
        assert_eq!(state.thread_last_error["thr_1"], "boom");
    }

    #[tokio::test]
    async fn usage_limit_is_exposed_in_the_session_and_to_coworker() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(config(None), FakeCodexClient::new(), transport.clone());
        bridge
            .state
            .lock()
            .await
            .thread_last_coworker
            .insert("thr_1".into(), DEFAULT_COWORKER_ID.into());

        bridge
            .handle_notification(json!({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_1",
                    "turn": {
                        "id": "turn_1",
                        "status": "failed",
                        "error": {
                            "message": "You have reached your usage limit.",
                            "codexErrorInfo": "usageLimitExceeded"
                        }
                    }
                }
            }))
            .await;

        let page = session::load_session_messages(&bridge.config, "thr_1", None, 20)
            .expect("session messages");
        assert_eq!(
            page.messages.last().unwrap().text,
            "You have reached your usage limit."
        );
        assert_eq!(
            transport.codex_messages().await,
            vec![(
                DEFAULT_COWORKER_ID.into(),
                "thr_1".into(),
                "You have reached your usage limit.".into(),
                vec![]
            )]
        );
    }

    #[tokio::test]
    async fn context_window_failure_starts_compaction() {
        let client = FakeCodexClient::new();
        client
            .push_response(json!({"turn": {"id": "compact_1", "status": "running"}}))
            .await;
        let bridge = bridge_with(config(None), client.clone(), RecordingTransport::new());

        bridge
            .handle_notification(json!({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_1",
                    "turn": {
                        "id": "turn_1",
                        "status": "failed",
                        "error": {
                            "message": "Context window exceeded.",
                            "codexErrorInfo": "contextWindowExceeded"
                        }
                    }
                }
            }))
            .await;

        let calls = client.calls().await;
        assert_eq!(calls.last().unwrap().0, "thread/compact/start");
        assert_eq!(calls.last().unwrap().1, json!({"threadId": "thr_1"}));
        assert_eq!(
            bridge.state.lock().await.thread_auto_compact_turn["thr_1"],
            "compact_1"
        );
    }

    #[tokio::test]
    async fn approval_routes_to_last_coworker_for_thread() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(multi_config(), FakeCodexClient::new(), transport.clone());
        bridge
            .state
            .lock()
            .await
            .thread_last_coworker
            .insert("thr_1".into(), "cw_02".into());

        let result = bridge
            .handle_app_server_request(
                "item/commandExecution/requestApproval",
                json!({"threadId": "thr_1", "command": "echo hi"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!("srv_1"),
            )
            .await
            .expect("approval");

        assert_eq!(result["decision"], "decline");
        assert_eq!(transport.bridge_posts().await[0].0, "cw_02");
    }

    #[tokio::test]
    async fn frontmatter_tool_call_posts_from_item_completed_notification() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(multi_config(), FakeCodexClient::new(), transport.clone());

        bridge
            .handle_notification(json!({
                "method": "item/completed",
                "params": {
                    "threadId": "thr_1",
                    "turnId": "turn_1",
                    "item": {
                        "type": "agentMessage",
                        "text": "---\ntype: coworker_tool_call\ntool: send_to_coworker\nto: cw_02\n---\n完成了"
                    }
                }
            }))
            .await;

        assert_eq!(
            transport.codex_messages().await,
            vec![("cw_02".into(), "thr_1".into(), "完成了".into(), vec![])]
        );
    }

    #[tokio::test]
    async fn frontmatter_tool_call_dedupes_per_turn_like_python_bridge() {
        let transport = RecordingTransport::new();
        let bridge = bridge_with(multi_config(), FakeCodexClient::new(), transport.clone());
        let item = json!({
            "type": "agentMessage",
            "text": "---\ntype: coworker_tool_call\ntool: send_to_coworker\nto: cw_02\n---\n完成了"
        });

        for turn_id in ["turn_1", "turn_1", "turn_2"] {
            bridge
                .handle_notification(json!({
                    "method": "item/completed",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": turn_id,
                        "item": item.clone()
                    }
                }))
                .await;
        }

        assert_eq!(
            transport.codex_messages().await,
            vec![
                ("cw_02".into(), "thr_1".into(), "完成了".into(), vec![]),
                ("cw_02".into(), "thr_1".into(), "完成了".into(), vec![]),
            ]
        );
    }

    #[tokio::test]
    async fn frontmatter_list_coworkers_tool_call_injects_result_to_thread() {
        let client = FakeCodexClient::new();
        let bridge = bridge_with(multi_config(), client.clone(), RecordingTransport::new());

        bridge
            .handle_notification(json!({
                "method": "item/completed",
                "params": {
                    "threadId": "thr_1",
                    "turnId": "turn_1",
                    "item": {
                        "type": "agentMessage",
                        "text": "---\ntype: coworker_tool_call\ntool: list_coworkers\n---\n"
                    }
                }
            }))
            .await;

        let calls = client.calls().await;
        assert_eq!(calls[0].0, "turn/start");
        let text = calls[0].1["input"][0]["text"].as_str().unwrap();
        assert!(text.contains("[coworker text tool result]"));
        assert!(text.contains("cw_01"));
        assert!(text.contains("cw_02"));
    }

    #[test]
    fn frontmatter_tool_call_parses_send_to_coworker() {
        let payload = parse_coworker_frontmatter_message(
            r#"---
type: coworker_tool_call
tool: send_to_coworker
to: cw_01
---
完成了"#,
        )
        .expect("frontmatter");

        assert_eq!(payload["name"], SEND_TO_COWORKER_TOOL);
        assert_eq!(payload["arguments"]["coworker_id"], "cw_01");
        assert_eq!(payload["arguments"]["message"], "完成了");
    }

    #[test]
    fn frontmatter_without_coworker_tool_call_type_is_ignored() {
        assert!(parse_coworker_frontmatter_message("---\ntype: note\n---\nhi").is_none());
    }
}
