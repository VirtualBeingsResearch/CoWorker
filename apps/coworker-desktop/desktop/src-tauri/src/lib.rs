use std::{
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    process::Stdio,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant, UNIX_EPOCH},
};

use coworker_desktop_core::{
    actor::{
        ActorConversation, ActorMessagePage, ActorStreamEvent, session_message_to_actor,
        subscribe_actor_stream_events,
    },
    claude::{list_history_conversations, merge_history_messages},
    codex_session as session,
    codex_session::{RuntimeSessionState, SessionSummary},
    command_resolver::resolve_command,
    config::{
        BridgeConfig, DEFAULT_DESKTOP_CONFIG_PATH, DesktopConfig, default_codex_names,
        default_config_value_with_display_name, read_config_value, write_config_value,
    },
    conversation_store::{ApprovalRequest, ConversationStore, ResolveApprovalResult},
    desktop_protocol::ActorId,
    logging::{error_chain, init_logging, log_file_path, subscribe_log_events},
    runtime::{BridgeRuntime, BridgeRuntimeStatus},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{
    Emitter, Manager, RunEvent, WindowEvent,
    ipc::Channel,
    menu::{Menu, MenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_updater::{Update, UpdaterExt};
use tauri_plugin_window_state::{AppHandleExt, StateFlags};
use tokio::{sync::oneshot, task::JoinHandle};
use tracing::{error, info, warn};
use url::Url;

const WINDOW_STATE_FILE: &str = "window-state.json";
const TRAY_ID: &str = "coworker-desktop-tray";
const DEFAULT_LOG_MAX_BYTES: usize = 2 * 1024 * 1024;
const DESKTOP_SERVICE_SHUTDOWN_GRACE: Duration = Duration::from_secs(6);
const DESKTOP_UPDATE_ENDPOINT_SUFFIX: &str =
    "/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}";

struct AppState {
    runtime: BridgeRuntime,
    log_stream: tokio::sync::Mutex<Option<RunningLogStream>>,
    pending_update: std::sync::Mutex<Option<Update>>,
    quitting: AtomicBool,
}

struct RunningLogStream {
    shutdown: oneshot::Sender<()>,
    handle: JoinHandle<()>,
}

#[derive(Debug, Serialize)]
struct DiagnosticResult {
    name: String,
    ok: bool,
    message: String,
}

#[derive(Debug, Serialize)]
struct ConfigInfo {
    config: Value,
    exists: bool,
    modified_ms: Option<u64>,
}

#[derive(Debug, Serialize, Clone)]
struct LogChunk {
    path: String,
    text: String,
    reset: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpdateMetadata {
    version: String,
    current_version: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(tag = "event", content = "data")]
enum DesktopUpdateDownloadEvent {
    #[serde(rename_all = "camelCase")]
    Started {
        content_length: Option<u64>,
    },
    #[serde(rename_all = "camelCase")]
    Progress {
        chunk_length: usize,
    },
    Finished,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct CommunicateRegistration {
    registration_id: String,
    participant_id: String,
    kind: String,
    client_id: String,
    display_name: String,
    active: bool,
    created_at: String,
    last_registered_at: String,
    metadata: Value,
}

#[derive(Debug, Deserialize)]
struct CommunicateRegistrationsResponse {
    registrations: Vec<CommunicateRegistration>,
}

#[derive(Debug, Deserialize)]
struct DeleteCommunicateRegistrationResponse {
    deleted: CommunicateRegistration,
}

#[tauri::command]
fn get_config(path: Option<String>, app: tauri::AppHandle) -> Result<Value, String> {
    Ok(config_info_for_path(config_path(&app, path)?)?.config)
}

#[tauri::command]
fn get_config_info(path: Option<String>, app: tauri::AppHandle) -> Result<ConfigInfo, String> {
    config_info_for_path(config_path(&app, path)?)
}

#[tauri::command]
async fn save_config(
    path: Option<String>,
    config: Value,
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<ConfigInfo, String> {
    let path = config_path(&app, path)?;
    write_config_value(&path, &config).map_err(to_message)?;
    let desktop_config = DesktopConfig::from_file(&path).map_err(to_message)?;
    state
        .runtime
        .apply_saved_config(&path, &desktop_config)
        .await
        .map_err(to_message)?;
    config_info_for_path(path)
}

fn parse_desktop_actor(actor_id: &str) -> Result<ActorId, String> {
    match actor_id {
        "local" => Ok(ActorId::Local),
        "codex" => Ok(ActorId::Codex),
        "claude" => Ok(ActorId::Claude),
        _ => Err(format!("Unknown Desktop actor: {actor_id}")),
    }
}

fn codex_summary_to_actor(summary: SessionSummary) -> ActorConversation {
    ActorConversation {
        actor_id: ActorId::Codex,
        conversation_id: summary.thread_id,
        title: summary.title,
        project_id: summary.project_id,
        project_name: summary.project_name,
        project_path: summary.project_path,
        writable: summary.owned_by_bridge,
        updated_at: Some(summary.last_active_at),
        mode: summary
            .pending_collaboration_mode
            .or(summary.collaboration_mode),
    }
}

#[tauri::command]
async fn start_bridge(
    path: Option<String>,
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<BridgeRuntimeStatus, String> {
    let path = match ensure_config_file(&app, path) {
        Ok(path) => path,
        Err(error) => {
            desktop_log_error(format!(
                "Bridge start failed before config load error={error}"
            ));
            return Err(error);
        }
    };
    desktop_log_info(format!(
        "Bridge start requested config_path={}",
        path.display()
    ));
    match state.runtime.start(&path).await {
        Ok(status) => {
            desktop_log_info("Bridge runtime started");
            Ok(status)
        }
        Err(error) => {
            let chain = error_chain(&error);
            desktop_log_error(format!("Bridge start failed error={chain}"));
            Err(chain)
        }
    }
}

#[tauri::command]
async fn stop_bridge(state: tauri::State<'_, AppState>) -> Result<BridgeRuntimeStatus, String> {
    state.runtime.stop().await.map_err(to_message)
}

#[tauri::command]
async fn get_bridge_status(
    state: tauri::State<'_, AppState>,
) -> Result<BridgeRuntimeStatus, String> {
    Ok(state.runtime.status().await)
}

#[tauri::command]
async fn list_desktop_conversations(
    actor_id: String,
    path: Option<String>,
    limit: Option<usize>,
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<Vec<ActorConversation>, String> {
    let actor = parse_desktop_actor(&actor_id)?;
    let limit = limit.unwrap_or(1000).max(1);
    if let Some(router) = state.runtime.desktop_router().await {
        return router
            .list_actor_conversations(actor, limit)
            .await
            .map_err(to_message);
    }
    if actor == ActorId::Codex {
        let config = load_config_or_default(&app, path)?;
        return session::list_sessions(&config, &[], RuntimeSessionState::default(), limit)
            .map(|items| items.into_iter().map(codex_summary_to_actor).collect())
            .map_err(to_message);
    }

    let desktop = load_desktop_config_or_default(&app, path)?;
    list_stored_desktop_conversations(&desktop, actor, limit)
}

fn list_stored_desktop_conversations(
    desktop: &DesktopConfig,
    actor: ActorId,
    limit: usize,
) -> Result<Vec<ActorConversation>, String> {
    let store =
        ConversationStore::open(desktop.storage_dir.join("desktop.sqlite3")).map_err(to_message)?;
    let mut conversations = store
        .list_stored_conversations(actor, limit)
        .map_err(to_message)?;
    if actor == ActorId::Claude && desktop.claude.enabled {
        for history in list_history_conversations(&desktop.claude, limit).map_err(to_message)? {
            if let Some(stored) = conversations.iter_mut().find(|item| {
                item.actor_id == ActorId::Claude && item.conversation_id == history.conversation_id
            }) {
                if stored.title == "搭档会话" {
                    stored.title = history.title;
                }
                stored.project_path = history.project_path;
                stored.updated_at = history.updated_at.or_else(|| stored.updated_at.clone());
            } else {
                conversations.push(history);
            }
        }
    }
    conversations.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
    conversations.truncate(limit);
    Ok(conversations)
}

#[tauri::command]
async fn send_desktop_message(
    actor_id: String,
    coworker_id: Option<String>,
    conversation_id: Option<String>,
    content: String,
    project_path: Option<String>,
    mode: Option<String>,
    attachment_paths: Vec<String>,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let actor = match actor_id.as_str() {
        "local" => ActorId::Local,
        "codex" => ActorId::Codex,
        "claude" => ActorId::Claude,
        _ => return Err(format!("Unknown Desktop actor: {actor_id}")),
    };
    let router = state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to send a message".to_owned())?;
    router
        .send_actor_message(
            actor,
            coworker_id.as_deref(),
            conversation_id.as_deref(),
            &content,
            project_path.as_deref(),
            mode.as_deref(),
            &attachment_paths,
        )
        .await
        .map_err(to_message)
}

#[tauri::command]
async fn load_desktop_messages(
    path: Option<String>,
    actor_id: String,
    conversation_id: String,
    before_cursor: Option<String>,
    page_size: Option<usize>,
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<ActorMessagePage, String> {
    let actor = parse_desktop_actor(&actor_id)?;
    let page_size = session::normalized_page_size(page_size);
    if let Some(router) = state.runtime.desktop_router().await {
        return router
            .load_messages(actor, &conversation_id, before_cursor.as_deref(), page_size)
            .await
            .map_err(to_message);
    }

    if actor == ActorId::Codex {
        let config = load_config_or_default(&app, path)?;
        let page = session::load_session_messages(
            &config,
            &conversation_id,
            before_cursor.as_deref(),
            page_size,
        )
        .map_err(to_message)?;
        return Ok(ActorMessagePage {
            messages: page
                .messages
                .into_iter()
                .map(|message| session_message_to_actor(&conversation_id, message))
                .collect(),
            next_before_cursor: page.next_before_cursor,
        });
    }

    let desktop = load_desktop_config_or_default(&app, path)?;
    let skip = before_cursor
        .as_deref()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0);
    let target = skip.saturating_add(page_size);
    let stored = ConversationStore::open(desktop.storage_dir.join("desktop.sqlite3"))
        .and_then(|store| store.list_messages(actor, &conversation_id, target.saturating_add(1)))
        .map_err(to_message)?;
    let messages = if actor == ActorId::Claude {
        merge_history_messages(
            &desktop.claude,
            &conversation_id,
            stored,
            target.saturating_add(1),
        )
    } else {
        stored
    };
    let end = messages.len().saturating_sub(skip);
    let start = end.saturating_sub(page_size);
    Ok(ActorMessagePage {
        messages: messages[start..end].to_vec(),
        next_before_cursor: (start > 0).then(|| skip.saturating_add(end - start).to_string()),
    })
}

#[tauri::command]
async fn set_desktop_conversation_mode(
    actor_id: String,
    conversation_id: String,
    mode: String,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let actor = match actor_id.as_str() {
        "local" => ActorId::Local,
        "codex" => ActorId::Codex,
        "claude" => ActorId::Claude,
        _ => return Err(format!("Unknown Desktop actor: {actor_id}")),
    };
    state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to change conversation mode".to_owned())?
        .set_actor_mode(actor, &conversation_id, &mode)
        .await
        .map_err(to_message)
}

#[tauri::command]
async fn rename_desktop_conversation(
    actor_id: String,
    conversation_id: String,
    title: String,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let actor = match actor_id.as_str() {
        "local" => ActorId::Local,
        "codex" => ActorId::Codex,
        "claude" => ActorId::Claude,
        _ => return Err(format!("Unknown Desktop actor: {actor_id}")),
    };
    state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to rename a conversation".to_owned())?
        .rename_actor_conversation(actor, &conversation_id, &title)
        .await
        .map_err(to_message)
}

#[tauri::command]
async fn send_desktop_coworker_message(
    actor_id: String,
    coworker_id: String,
    conversation_id: Option<String>,
    content: String,
    attachment_paths: Vec<String>,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let actor = match actor_id.as_str() {
        "local" => ActorId::Local,
        "codex" => ActorId::Codex,
        "claude" => ActorId::Claude,
        _ => return Err(format!("Unknown Desktop actor: {actor_id}")),
    };
    state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to send to Coworker".to_owned())?
        .send_actor_coworker_message(
            actor,
            &coworker_id,
            conversation_id.as_deref(),
            &content,
            &attachment_paths,
        )
        .await
        .map_err(to_message)
}

#[tauri::command]
async fn list_desktop_approvals(
    state: tauri::State<'_, AppState>,
) -> Result<Vec<ApprovalRequest>, String> {
    state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to list approvals".to_owned())?
        .pending_approvals()
        .map_err(to_message)
}

#[tauri::command]
async fn resolve_desktop_approval(
    request_id: String,
    actor_id: ActorId,
    conversation_id: String,
    coworker_id: String,
    response: Value,
    state: tauri::State<'_, AppState>,
) -> Result<ResolveApprovalResult, String> {
    state
        .runtime
        .desktop_router()
        .await
        .ok_or_else(|| "CoWorker Desktop must be running to resolve approvals".to_owned())?
        .resolve_approval(
            &request_id,
            actor_id,
            &conversation_id,
            &coworker_id,
            &response,
        )
        .await
        .map_err(to_message)
}

#[tauri::command]
async fn copy_desktop_attachment(
    source_path: String,
    destination_path: String,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let attachment = if let Some(bridge) = state.runtime.bridge().await {
        bridge
            .copy_codex_attachment(&source_path, &destination_path)
            .map_err(to_message)?
    } else {
        session::copy_attachment_to_path(&source_path, &destination_path).map_err(to_message)?
    };
    serde_json::to_value(attachment).map_err(to_message)
}

#[tauri::command]
fn read_bridge_log(
    path: Option<String>,
    max_bytes: Option<usize>,
    app: tauri::AppHandle,
) -> Result<String, String> {
    let _ = path;
    let log_path = desktop_log_path(&app)?;
    let max_bytes = max_bytes.unwrap_or(DEFAULT_LOG_MAX_BYTES);
    read_log_tail(&log_path, max_bytes)
}

#[tauri::command]
async fn start_bridge_log_stream(
    path: Option<String>,
    max_bytes: Option<usize>,
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let _ = path;
    stop_log_stream(&state).await;

    let log_path = desktop_log_path(&app)?;
    let max_bytes = max_bytes.unwrap_or(DEFAULT_LOG_MAX_BYTES);
    let initial_text = read_log_tail(&log_path, max_bytes)?;
    emit_log_chunk(&app, &log_path, initial_text, true);

    let (shutdown_tx, mut shutdown_rx) = oneshot::channel();
    let mut log_rx = subscribe_log_events();
    let handle = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut shutdown_rx => break,
                result = log_rx.recv() => {
                    match result {
                        Ok(text) => emit_log_chunk(&app, &log_path, text, false),
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(skipped)) => {
                            emit_log_chunk(
                                &app,
                                &log_path,
                                format!(
                                    "{} WARN coworker_desktop_app: live log stream skipped {skipped} entries because the UI could not keep up\n",
                                    chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
                                ),
                                false,
                            );
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    }
                }
            }
        }
    });

    *state.log_stream.lock().await = Some(RunningLogStream {
        shutdown: shutdown_tx,
        handle,
    });
    Ok(())
}

#[tauri::command]
async fn stop_bridge_log_stream(state: tauri::State<'_, AppState>) -> Result<(), String> {
    stop_log_stream(&state).await;
    Ok(())
}

#[tauri::command]
async fn run_diagnostics(
    path: Option<String>,
    app: tauri::AppHandle,
) -> Result<Vec<DiagnosticResult>, String> {
    let path = config_path(&app, path)?;
    let desktop = if path.exists() {
        DesktopConfig::from_file(&path).map_err(to_message)?
    } else {
        DesktopConfig::from_value(default_config_value_with_display_name(
            &default_codex_names().codex_id,
            &default_codex_names().display_name,
            "http://localhost:8000",
        ))
        .map_err(to_message)?
    };
    let config = &desktop.codex;
    let mut results = Vec::new();
    results.push(DiagnosticResult {
        name: "Desktop transport security".into(),
        ok: desktop.security.development_mode
            || config.coworkers.iter().all(|coworker| {
                coworker.base_url.starts_with("https://")
                    && desktop
                        .security
                        .bearer_tokens
                        .contains_key(&coworker.coworker_id)
            }),
        message: if desktop.security.development_mode {
            "development mode: HTTPS and Bearer requirements are disabled".into()
        } else {
            "production mode requires HTTPS and a Bearer token for every Coworker".into()
        },
    });
    if desktop.codex_enabled {
        results.push(check_named_command("Codex command", &config.command).await);
        results.push(check_app_server_command(&config.command, &config.args).await);
    }
    if desktop.claude.enabled {
        results.push(check_named_command("Claude Code", &desktop.claude.command).await);
    }
    results.push(DiagnosticResult {
        name: "Claude MCP sidecar".into(),
        ok: true,
        message: "built-in stdio sidecar is available with one-time token validation".into(),
    });
    for coworker in &config.coworkers {
        results.push(check_coworker(&coworker.display_name, &coworker.base_url).await);
    }
    Ok(results)
}

#[tauri::command]
async fn list_communicate_registrations(
    base_url: String,
    bearer_token: Option<String>,
) -> Result<Vec<CommunicateRegistration>, String> {
    let url = format!(
        "{}/api/communicate/register",
        base_url.trim_end_matches('/')
    );
    let request = reqwest::Client::new().get(url);
    let response = if let Some(token) = bearer_token.filter(|value| !value.trim().is_empty()) {
        request.bearer_auth(token)
    } else {
        request
    }
    .timeout(Duration::from_secs(5))
    .send()
    .await
    .map_err(to_message)?
    .error_for_status()
    .map_err(to_message)?
    .json::<CommunicateRegistrationsResponse>()
    .await
    .map_err(to_message)?;
    Ok(response.registrations)
}

#[tauri::command]
async fn delete_communicate_registration(
    base_url: String,
    registration_id: String,
    bearer_token: Option<String>,
) -> Result<CommunicateRegistration, String> {
    let url = format!(
        "{}/api/communicate/register/{}",
        base_url.trim_end_matches('/'),
        registration_id
    );
    let request = reqwest::Client::new().delete(url);
    let response = if let Some(token) = bearer_token.filter(|value| !value.trim().is_empty()) {
        request.bearer_auth(token)
    } else {
        request
    }
    .timeout(Duration::from_secs(5))
    .send()
    .await
    .map_err(to_message)?
    .error_for_status()
    .map_err(to_message)?
    .json::<DeleteCommunicateRegistrationResponse>()
    .await
    .map_err(to_message)?;
    Ok(response.deleted)
}

#[tauri::command]
async fn check_desktop_update(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    endpoint: Option<String>,
) -> Result<Option<DesktopUpdateMetadata>, String> {
    let configured_endpoint = desktop_update_configured_endpoint(&app);
    let requested_endpoint = endpoint
        .as_deref()
        .filter(|endpoint| !endpoint.trim().is_empty());
    let endpoint_source = if requested_endpoint.is_some() {
        "config"
    } else {
        "default"
    };
    let endpoint_for_log =
        normalize_desktop_update_endpoint(requested_endpoint.or(configured_endpoint.as_deref()))
            .ok_or_else(|| "desktop update endpoint is not configured".to_owned())?;
    desktop_log_info(format!(
        "CoWorker Desktop update check started endpoint_source={endpoint_source} endpoint={endpoint_for_log}"
    ));

    let endpoint = match Url::parse(&endpoint_for_log) {
        Ok(endpoint) => endpoint,
        Err(error) => {
            desktop_log_warn(format!(
                "CoWorker Desktop update check invalid endpoint endpoint_source={endpoint_source} endpoint={endpoint_for_log} error={error}"
            ));
            return Err(to_message(error));
        }
    };
    let builder = match app.updater_builder().endpoints(vec![endpoint]) {
        Ok(builder) => builder,
        Err(error) => {
            desktop_log_warn(format!(
                "CoWorker Desktop update check failed to configure endpoint endpoint_source={endpoint_source} endpoint={endpoint_for_log} error={error}"
            ));
            return Err(to_message(error));
        }
    };
    let updater = match builder.build() {
        Ok(updater) => updater,
        Err(error) => {
            desktop_log_warn(format!(
                "CoWorker Desktop update check failed to build updater endpoint_source={endpoint_source} endpoint={endpoint_for_log} error={error}"
            ));
            return Err(to_message(error));
        }
    };
    let update = match updater.check().await {
        Ok(update) => update,
        Err(error) => {
            desktop_log_warn(format!(
                "CoWorker Desktop update check failed endpoint_source={endpoint_source} endpoint={endpoint_for_log} error={error}"
            ));
            return Err(to_message(error));
        }
    };
    let metadata = update.as_ref().map(|update| DesktopUpdateMetadata {
        version: update.version.clone(),
        current_version: update.current_version.clone(),
    });
    if let Some(metadata) = &metadata {
        desktop_log_info(format!(
            "CoWorker Desktop update available endpoint_source={endpoint_source} version={} current_version={}",
            metadata.version, metadata.current_version
        ));
    } else {
        desktop_log_info(format!(
            "CoWorker Desktop update check finished endpoint_source={endpoint_source} result=up-to-date"
        ));
    }
    *state.pending_update.lock().map_err(to_message)? = update;
    Ok(metadata)
}

#[tauri::command]
fn get_default_desktop_update_url(app: tauri::AppHandle) -> String {
    desktop_update_configured_endpoint(&app)
        .and_then(|endpoint| desktop_update_base_url_from_endpoint(&endpoint))
        .unwrap_or_default()
}

fn desktop_update_configured_endpoint(app: &tauri::AppHandle) -> Option<String> {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|config| config.get("endpoints"))
        .and_then(|endpoints| endpoints.as_array())
        .and_then(|endpoints| endpoints.first())
        .and_then(|endpoint| endpoint.as_str())
        .map(str::to_owned)
}

#[tauri::command]
async fn install_desktop_update(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    on_event: Channel<DesktopUpdateDownloadEvent>,
) -> Result<(), String> {
    let update = {
        let mut pending = state.pending_update.lock().map_err(to_message)?;
        pending.take()
    }
    .ok_or_else(|| "there is no pending desktop update".to_owned())?;
    let update_version = update.version.clone();
    let current_version = update.current_version.clone();
    desktop_log_info(format!(
        "CoWorker Desktop update install started version={update_version} current_version={current_version}"
    ));

    prepare_for_update_install(&app).await?;

    let app_for_restart = app.clone();
    let mut started = false;
    let result = update
        .download_and_install(
            |chunk_length, content_length| {
                if !started {
                    let _ = on_event.send(DesktopUpdateDownloadEvent::Started { content_length });
                    started = true;
                }
                let _ = on_event.send(DesktopUpdateDownloadEvent::Progress { chunk_length });
            },
            || {
                let _ = on_event.send(DesktopUpdateDownloadEvent::Finished);
            },
        )
        .await;
    if let Err(error) = result {
        desktop_log_warn(format!(
            "CoWorker Desktop update install failed version={update_version} error={error}"
        ));
        return Err(to_message(error));
    }
    desktop_log_info("CoWorker Desktop update installed; restarting app");
    app_for_restart.restart()
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            desktop_log_info("CoWorker Desktop second instance requested focus");
            focus_main_window(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_drag::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_window_state::Builder::new()
                .with_filename(WINDOW_STATE_FILE)
                .with_state_flags(window_state_flags())
                .build(),
        )
        .manage(AppState {
            runtime: BridgeRuntime::new(),
            log_stream: tokio::sync::Mutex::new(None),
            pending_update: std::sync::Mutex::new(None),
            quitting: AtomicBool::new(false),
        })
        .setup(|app| {
            let default_names = default_codex_names();
            let mut config = BridgeConfig::from_value(default_config_value_with_display_name(
                &default_names.codex_id,
                &default_names.display_name,
                "http://localhost:8000",
            ))?;
            let logs_dir = app.path().app_log_dir()?;
            config.logs_dir = logs_dir.to_string_lossy().into_owned();
            init_logging(&config)?;
            desktop_log_info(format!(
                "CoWorker Desktop started version={} log_path={}",
                env!("CARGO_PKG_VERSION"),
                log_file_path(&logs_dir).display()
            ));
            install_tray(app)?;
            install_window_close_behavior(app)?;
            install_actor_stream_event_relay(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_config,
            get_config_info,
            save_config,
            start_bridge,
            stop_bridge,
            get_bridge_status,
            list_desktop_conversations,
            send_desktop_message,
            load_desktop_messages,
            set_desktop_conversation_mode,
            rename_desktop_conversation,
            send_desktop_coworker_message,
            list_desktop_approvals,
            resolve_desktop_approval,
            copy_desktop_attachment,
            read_bridge_log,
            start_bridge_log_stream,
            stop_bridge_log_stream,
            run_diagnostics,
            list_communicate_registrations,
            delete_communicate_registration,
            check_desktop_update,
            get_default_desktop_update_url,
            install_desktop_update,
            set_tray_copy
        ])
        .build(tauri::generate_context!())
        .expect("error while building CoWorker Desktop app");

    app.run(|app, event| {
        if let RunEvent::ExitRequested {
            code: None, api, ..
        } = event
        {
            api.prevent_exit();
            let state = app.state::<AppState>();
            if state.quitting.load(Ordering::SeqCst) {
                desktop_log_info(
                    "CoWorker Desktop exit requested while shutdown is already in progress",
                );
                return;
            }
            desktop_log_info("CoWorker Desktop exit requested; starting graceful shutdown");
            request_desktop_shutdown(app);
        }
    });
}

fn install_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let menu = build_tray_menu(app.handle(), "Open", "Hide to Tray", "Quit")?;
    let Some(icon) = app.default_window_icon().cloned() else {
        return Ok(());
    };
    TrayIconBuilder::with_id(TRAY_ID)
        .menu(&menu)
        .show_menu_on_left_click(!cfg!(windows))
        .icon(icon)
        .tooltip("CoWorker Desktop")
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } = event
            {
                focus_main_window(tray.app_handle());
            }
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                focus_main_window(app);
            }
            "hide" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
            "quit" => request_desktop_shutdown(app),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

fn build_tray_menu(
    app: &tauri::AppHandle,
    open: &str,
    hide: &str,
    quit: &str,
) -> tauri::Result<Menu<tauri::Wry>> {
    let open = MenuItem::with_id(app, "open", open, true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", hide, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", quit, true, None::<&str>)?;
    Menu::with_items(app, &[&open, &hide, &quit])
}

#[tauri::command]
fn set_tray_copy(
    tooltip: String,
    open: String,
    hide: String,
    quit: String,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let tray = app
        .tray_by_id(TRAY_ID)
        .ok_or_else(|| "desktop tray is not available".to_string())?;
    let menu = build_tray_menu(&app, &open, &hide, &quit).map_err(to_message)?;
    tray.set_tooltip(Some(tooltip)).map_err(to_message)?;
    tray.set_menu(Some(menu)).map_err(to_message)
}

fn window_state_flags() -> StateFlags {
    StateFlags::SIZE | StateFlags::POSITION | StateFlags::MAXIMIZED
}

fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn request_desktop_shutdown(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if state.quitting.swap(true, Ordering::SeqCst) {
        desktop_log_info("CoWorker Desktop shutdown already requested; ignoring duplicate request");
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let started = Instant::now();
        desktop_log_info(format!(
            "CoWorker Desktop shutdown started timeout_seconds={}",
            DESKTOP_SERVICE_SHUTDOWN_GRACE.as_secs()
        ));
        match tokio::time::timeout(
            DESKTOP_SERVICE_SHUTDOWN_GRACE,
            shutdown_desktop_services(&app),
        )
        .await
        {
            Ok(Ok(())) => {
                desktop_log_info(format!(
                    "CoWorker Desktop services stopped elapsed_ms={}",
                    started.elapsed().as_millis()
                ));
            }
            Ok(Err(error)) => {
                desktop_log_error(format!("CoWorker Desktop shutdown error error={error}"));
                eprintln!("CoWorker Desktop shutdown warning: {error}");
            }
            Err(_) => {
                desktop_log_warn(format!(
                    "CoWorker Desktop services did not stop before timeout timeout_seconds={} elapsed_ms={}",
                    DESKTOP_SERVICE_SHUTDOWN_GRACE.as_secs(),
                    started.elapsed().as_millis()
                ));
                eprintln!(
                    "CoWorker Desktop shutdown warning: services did not stop within {} seconds",
                    DESKTOP_SERVICE_SHUTDOWN_GRACE.as_secs()
                );
            }
        }
        desktop_log_info("CoWorker Desktop scheduling app.exit(0) on the main thread");
        let app_for_exit = app.clone();
        if let Err(error) = app.run_on_main_thread(move || {
            desktop_log_info("CoWorker Desktop calling app.exit(0) on the main thread");
            app_for_exit.exit(0);
        }) {
            desktop_log_warn(format!(
                "CoWorker Desktop failed to schedule main-thread exit error={error}"
            ));
            std::process::exit(0);
        }
    });
}

async fn shutdown_desktop_services(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>();
    desktop_log_info("CoWorker Desktop shutdown step started: stop log stream");
    stop_log_stream(&state).await;
    desktop_log_info("CoWorker Desktop shutdown step finished: stop log stream");

    let runtime_stop_started = Instant::now();
    desktop_log_info("CoWorker Desktop shutdown step started: stop bridge runtime");
    state.runtime.stop().await.map_err(to_message)?;
    desktop_log_info(format!(
        "CoWorker Desktop shutdown step finished: stop bridge runtime elapsed_ms={}",
        runtime_stop_started.elapsed().as_millis()
    ));

    if app.get_webview_window("main").is_some() {
        desktop_log_info("CoWorker Desktop shutdown step started: save window state");
        app.save_window_state(window_state_flags())
            .map_err(to_message)?;
        let state_path = app
            .path()
            .app_config_dir()
            .map_err(to_message)?
            .join(WINDOW_STATE_FILE);
        desktop_log_info(format!(
            "CoWorker Desktop shutdown step finished: save window state path={}",
            state_path.display()
        ));

        #[cfg(target_os = "windows")]
        if let Some(window) = app.get_webview_window("main") {
            desktop_log_info("CoWorker Desktop shutdown step started: destroy main window");
            window.destroy().map_err(to_message)?;
            desktop_log_info("CoWorker Desktop shutdown step finished: destroy main window");
        }
    } else {
        desktop_log_info("CoWorker Desktop shutdown skipped: main window not found");
    }
    Ok(())
}

async fn prepare_for_update_install(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>();
    desktop_log_info("CoWorker Desktop update step started: stop log stream");
    stop_log_stream(&state).await;
    desktop_log_info("CoWorker Desktop update step finished: stop log stream");

    desktop_log_info("CoWorker Desktop update step started: stop bridge runtime");
    state.runtime.stop().await.map_err(to_message)?;
    desktop_log_info("CoWorker Desktop update step finished: stop bridge runtime");

    if app.get_webview_window("main").is_some() {
        app.save_window_state(window_state_flags())
            .map_err(to_message)?;
        let state_path = app
            .path()
            .app_config_dir()
            .map_err(to_message)?
            .join(WINDOW_STATE_FILE);
        desktop_log_info(format!(
            "CoWorker Desktop update step finished: save window state path={}",
            state_path.display()
        ));
    }

    Ok(())
}

fn install_window_close_behavior(app: &mut tauri::App) -> tauri::Result<()> {
    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };
    let app_handle = app.handle().clone();
    let window_for_events = window.clone();
    window.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => {
            let _ = app_handle.save_window_state(window_state_flags());
            let state = app_handle.state::<AppState>();
            if state.quitting.load(Ordering::SeqCst) {
                desktop_log_info(
                    "CoWorker Desktop main window close requested during shutdown; allowing close",
                );
                return;
            }
            desktop_log_info("CoWorker Desktop main window close requested; hiding to tray");
            api.prevent_close();
            let _ = window_for_events.hide();
        }
        _ => {}
    });

    Ok(())
}

fn install_actor_stream_event_relay(app: tauri::AppHandle) {
    let mut rx = subscribe_actor_stream_events();
    tauri::async_runtime::spawn(async move {
        loop {
            match rx.recv().await {
                Ok(event) => {
                    let _ = app.emit("actor-stream-event", event);
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                    for actor_id in ActorId::ALL {
                        let _ = app.emit(
                            "actor-stream-event",
                            ActorStreamEvent {
                                actor_id,
                                conversation_id: String::new(),
                                message_id: None,
                                event: serde_json::json!({"type": "stream-lagged"}),
                            },
                        );
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            }
        }
    });
}

fn config_path(app: &tauri::AppHandle, path: Option<String>) -> Result<PathBuf, String> {
    let config_dir = app.path().app_config_dir().map_err(to_message)?;
    let path = path
        .filter(|value| !value.trim().is_empty())
        .map(|value| PathBuf::from(value.trim()))
        .unwrap_or_else(|| PathBuf::from(DEFAULT_DESKTOP_CONFIG_PATH));
    if path.is_absolute() {
        Ok(path)
    } else {
        let current_path = config_dir.join(&path);
        if path == Path::new(DEFAULT_DESKTOP_CONFIG_PATH) {
            Ok(preferred_default_config_path(&config_dir, current_path))
        } else {
            Ok(current_path)
        }
    }
}

const PREVIOUS_APP_IDENTIFIERS: [&str; 2] =
    ["ai.fine.coworker.desktop", "ai.fine.coworker.codexbridge"];

fn preferred_default_config_path(config_dir: &Path, current_path: PathBuf) -> PathBuf {
    let Some(parent) = config_dir.parent() else {
        return current_path;
    };
    if config_has_stored_conversations(&current_path) {
        return current_path;
    }
    let previous_paths = PREVIOUS_APP_IDENTIFIERS
        .map(|identifier| parent.join(identifier).join(DEFAULT_DESKTOP_CONFIG_PATH));
    if let Some(path) = previous_paths
        .iter()
        .find(|path| path.is_file() && config_has_stored_conversations(path))
    {
        return path.clone();
    }
    previous_paths
        .into_iter()
        .find(|path| !current_path.is_file() && path.is_file())
        .unwrap_or(current_path)
}

fn config_has_stored_conversations(path: &Path) -> bool {
    let Ok(desktop) = DesktopConfig::from_file(path) else {
        return false;
    };
    let Ok(store) = ConversationStore::open(desktop.storage_dir.join("desktop.sqlite3")) else {
        return false;
    };
    ActorId::ALL.into_iter().any(|actor| {
        store
            .list_stored_conversations(actor, 1)
            .is_ok_and(|conversations| !conversations.is_empty())
    })
}

fn config_info_for_path(path: PathBuf) -> Result<ConfigInfo, String> {
    if path.exists() {
        let config = read_config_value(&path).map_err(to_message)?;
        if DesktopConfig::from_value(config.clone()).is_err() {
            let default_names = default_codex_names();
            return Ok(ConfigInfo {
                config: default_config_value_with_display_name(
                    &default_names.codex_id,
                    &default_names.display_name,
                    "http://localhost:8000",
                ),
                exists: false,
                modified_ms: None,
            });
        }
        Ok(ConfigInfo {
            config,
            exists: true,
            modified_ms: modified_ms(&path)?,
        })
    } else {
        let default_names = default_codex_names();
        Ok(ConfigInfo {
            config: default_config_value_with_display_name(
                &default_names.codex_id,
                &default_names.display_name,
                "http://localhost:8000",
            ),
            exists: false,
            modified_ms: None,
        })
    }
}

fn normalize_desktop_update_endpoint(endpoint: Option<&str>) -> Option<String> {
    let endpoint = endpoint?.trim().trim_end_matches('/');
    if endpoint.is_empty() {
        return None;
    }
    if endpoint.contains("{{target}}")
        && endpoint.contains("{{arch}}")
        && endpoint.contains("{{current_version}}")
    {
        Some(endpoint.to_owned())
    } else {
        let base_url = endpoint
            .split_once("/api/desktop-updates")
            .map_or(endpoint, |(base_url, _)| base_url)
            .trim_end_matches('/');
        Some(format!("{base_url}{DESKTOP_UPDATE_ENDPOINT_SUFFIX}"))
    }
}

fn desktop_update_base_url_from_endpoint(endpoint: &str) -> Option<String> {
    let endpoint = endpoint.trim().trim_end_matches('/');
    if endpoint.is_empty() {
        return None;
    }
    if let Some(base_url) = endpoint.strip_suffix(DESKTOP_UPDATE_ENDPOINT_SUFFIX) {
        return Some(base_url.trim_end_matches('/').to_owned());
    }
    endpoint
        .find("/api/desktop-updates")
        .map(|index| endpoint[..index].trim_end_matches('/').to_owned())
        .or_else(|| Some(endpoint.to_owned()))
}

fn modified_ms(path: &Path) -> Result<Option<u64>, String> {
    let metadata = std::fs::metadata(path).map_err(to_message)?;
    let modified = metadata.modified().map_err(to_message)?;
    let elapsed = modified.duration_since(UNIX_EPOCH).map_err(to_message)?;
    Ok(Some(elapsed.as_millis().try_into().unwrap_or(u64::MAX)))
}

fn load_config_or_default(
    app: &tauri::AppHandle,
    path: Option<String>,
) -> Result<BridgeConfig, String> {
    let path = config_path(app, path)?;
    if path.exists() {
        BridgeConfig::from_file(path).map_err(to_message)
    } else {
        let default_names = default_codex_names();
        BridgeConfig::from_value(default_config_value_with_display_name(
            &default_names.codex_id,
            &default_names.display_name,
            "http://localhost:8000",
        ))
        .map_err(to_message)
    }
}

fn load_desktop_config_or_default(
    app: &tauri::AppHandle,
    path: Option<String>,
) -> Result<DesktopConfig, String> {
    let path = config_path(app, path)?;
    if path.exists() {
        DesktopConfig::from_file(path).map_err(to_message)
    } else {
        let default_names = default_codex_names();
        DesktopConfig::from_value(default_config_value_with_display_name(
            &default_names.codex_id,
            &default_names.display_name,
            "http://localhost:8000",
        ))
        .map_err(to_message)
    }
}

async fn stop_log_stream(state: &tauri::State<'_, AppState>) {
    let Some(current) = state.log_stream.lock().await.take() else {
        return;
    };
    desktop_log_info("CoWorker Desktop stopping live log stream");
    let _ = current.shutdown.send(());
    let _ = current.handle.await;
    desktop_log_info("CoWorker Desktop stopped live log stream");
}

fn emit_log_chunk(app: &tauri::AppHandle, path: &Path, text: String, reset: bool) {
    let _ = app.emit(
        "bridge-log-chunk",
        LogChunk {
            path: path.display().to_string(),
            text,
            reset,
        },
    );
}

fn read_log_tail(path: &Path, max_bytes: usize) -> Result<String, String> {
    if !path.exists() {
        return Ok(String::new());
    }
    let mut file = std::fs::File::open(path).map_err(to_message)?;
    let len = file.metadata().map_err(to_message)?.len();
    let read_len = len.min(max_bytes as u64);
    file.seek(SeekFrom::Start(len.saturating_sub(read_len)))
        .map_err(to_message)?;

    let mut bytes = Vec::with_capacity(read_len as usize);
    file.take(read_len)
        .read_to_end(&mut bytes)
        .map_err(to_message)?;
    Ok(String::from_utf8_lossy(&bytes).to_string())
}

fn desktop_log_info(message: impl AsRef<str>) {
    let message = message.as_ref();
    info!("{message}");
}

fn desktop_log_warn(message: impl AsRef<str>) {
    let message = message.as_ref();
    warn!("{message}");
}

fn desktop_log_error(message: impl AsRef<str>) {
    let message = message.as_ref();
    error!("{message}");
}

fn desktop_log_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_log_dir()
        .map(log_file_path)
        .map_err(to_message)
}

fn ensure_config_file(app: &tauri::AppHandle, path: Option<String>) -> Result<PathBuf, String> {
    let path = config_path(app, path)?;
    let valid = path
        .exists()
        .then(|| read_config_value(&path).ok())
        .flatten()
        .is_some_and(|config| DesktopConfig::from_value(config).is_ok());
    if !valid {
        let default_names = default_codex_names();
        let default_config = default_config_value_with_display_name(
            &default_names.codex_id,
            &default_names.display_name,
            "http://localhost:8000",
        );
        write_config_value(&path, &default_config).map_err(to_message)?;
    }
    Ok(path)
}

async fn check_named_command(name: &str, command: &str) -> DiagnosticResult {
    let resolved = match resolve_command(command) {
        Ok(resolved) => resolved,
        Err(error) => {
            return DiagnosticResult {
                name: name.into(),
                ok: false,
                message: format!("could not resolve command {command:?}: {error}"),
            };
        }
    };
    let mut probe = resolved.command();
    probe
        .arg("--version")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    match probe.output().await {
        Ok(output) if output.status.success() => DiagnosticResult {
            name: name.into(),
            ok: true,
            message: first_output_line(&output.stdout, &output.stderr)
                .unwrap_or_else(|| "command is available".into()),
        },
        Ok(output) => DiagnosticResult {
            name: name.into(),
            ok: false,
            message: first_output_line(&output.stdout, &output.stderr)
                .unwrap_or_else(|| format!("command exited with {}", output.status)),
        },
        Err(error) => DiagnosticResult {
            name: name.into(),
            ok: false,
            message: error.to_string(),
        },
    }
}

async fn check_app_server_command(command: &str, args: &[String]) -> DiagnosticResult {
    let resolved = match resolve_command(command) {
        Ok(resolved) => resolved,
        Err(error) => {
            return DiagnosticResult {
                name: "Codex app-server".into(),
                ok: false,
                message: format!("could not resolve command {command:?}: {error}"),
            };
        }
    };
    let mut probe = resolved.command();
    probe
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    match probe.spawn() {
        Ok(mut child) => {
            let _ = child.kill().await;
            DiagnosticResult {
                name: "Codex app-server".into(),
                ok: true,
                message: "process starts successfully".into(),
            }
        }
        Err(error) => DiagnosticResult {
            name: "Codex app-server".into(),
            ok: false,
            message: error.to_string(),
        },
    }
}

async fn check_coworker(name: &str, base_url: &str) -> DiagnosticResult {
    let url = format!("{}/status", base_url.trim_end_matches('/'));
    let result = reqwest::Client::new()
        .get(&url)
        .timeout(Duration::from_secs(3))
        .send()
        .await;
    match result {
        Ok(response) if response.status().is_success() => DiagnosticResult {
            name: format!("Coworker {name}"),
            ok: true,
            message: format!("{url} returned {}", response.status()),
        },
        Ok(response) => DiagnosticResult {
            name: format!("Coworker {name}"),
            ok: false,
            message: format!("{url} returned {}", response.status()),
        },
        Err(error) => DiagnosticResult {
            name: format!("Coworker {name}"),
            ok: false,
            message: error.to_string(),
        },
    }
}

fn first_output_line(stdout: &[u8], stderr: &[u8]) -> Option<String> {
    let text = if stdout.is_empty() { stderr } else { stdout };
    String::from_utf8_lossy(text)
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_owned)
}

fn to_message(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_path(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("coworker-desktop-app-test-{}-{name}", unique))
    }

    #[test]
    fn config_read_treats_schema_v1_as_unconfigured() {
        let directory = temp_path("config-v1");
        std::fs::create_dir_all(&directory).expect("create test directory");
        let desktop_path = directory.join(DEFAULT_DESKTOP_CONFIG_PATH);
        std::fs::write(
            &desktop_path,
            r#"{"codex_id":"legacy-codex","command":"custom-codex","coworkers":[{"coworker_id":"cw-1","base_url":"http://localhost:8000"}]}"#,
        )
        .expect("write legacy config");

        let info = config_info_for_path(desktop_path.clone()).expect("read config");

        assert!(!info.exists);
        assert_eq!(info.config["schema_version"], 2);
        assert_ne!(info.config["desktop_id"], "legacy-codex");
        assert!(desktop_path.exists());

        std::fs::remove_dir_all(directory).expect("remove test directory");
    }

    #[test]
    fn default_config_reuses_historical_identifier_data_when_newer_stores_are_empty() {
        let root = temp_path("previous-identifier-data");
        let current_dir = root.join("ai.coworker.desktop");
        let previous_dir = root.join(PREVIOUS_APP_IDENTIFIERS[0]);
        let original_dir = root.join(PREVIOUS_APP_IDENTIFIERS[1]);
        std::fs::create_dir_all(&current_dir).expect("create current config directory");
        std::fs::create_dir_all(&previous_dir).expect("create previous config directory");
        std::fs::create_dir_all(&original_dir).expect("create original config directory");

        let write_config = |directory: &Path| {
            let mut config = default_config_value_with_display_name(
                "desktop-test",
                "Desktop Test",
                "http://localhost:8000",
            );
            config["security"]["development_mode"] = serde_json::Value::Bool(true);
            config["storage_dir"] =
                serde_json::Value::String(directory.join("data").to_string_lossy().into_owned());
            let path = directory.join(DEFAULT_DESKTOP_CONFIG_PATH);
            write_config_value(&path, &config).expect("write desktop config");
            DesktopConfig::from_file(path).expect("read desktop config")
        };
        let current = write_config(&current_dir);
        ConversationStore::open(current.storage_dir.join("desktop.sqlite3"))
            .expect("open empty current store");
        let previous = write_config(&previous_dir);
        ConversationStore::open(previous.storage_dir.join("desktop.sqlite3"))
            .expect("open empty previous store");
        let original = write_config(&original_dir);
        ConversationStore::open(original.storage_dir.join("desktop.sqlite3"))
            .expect("open previous store")
            .append_message(
                "message-1",
                ActorId::Local,
                "conversation-1",
                "cw-1",
                "coworker",
                "hello",
                &serde_json::json!({}),
            )
            .expect("append previous message");

        assert_eq!(
            preferred_default_config_path(
                &current_dir,
                current_dir.join(DEFAULT_DESKTOP_CONFIG_PATH)
            ),
            original_dir.join(DEFAULT_DESKTOP_CONFIG_PATH)
        );
        std::fs::remove_dir_all(root).expect("remove test directory");
    }

    #[test]
    fn stopped_desktop_lists_locally_stored_conversations() {
        let directory = temp_path("offline-conversations");
        let mut config = default_config_value_with_display_name(
            "desktop-test",
            "Desktop Test",
            "http://localhost:8000",
        );
        config["security"]["development_mode"] = serde_json::Value::Bool(true);
        let mut desktop = DesktopConfig::from_value(config).expect("desktop config");
        desktop.storage_dir = directory.clone();
        desktop.claude.enabled = false;
        let store = ConversationStore::open(directory.join("desktop.sqlite3"))
            .expect("open conversation store");
        store
            .append_message(
                "message-1",
                ActorId::Local,
                "conversation-1",
                "cw-1",
                "coworker",
                "hello",
                &serde_json::json!({}),
            )
            .expect("append message");
        drop(store);

        let conversations = list_stored_desktop_conversations(&desktop, ActorId::Local, 20)
            .expect("list conversations");

        assert_eq!(conversations.len(), 1);
        assert_eq!(conversations[0].actor_id, ActorId::Local);
        assert_eq!(conversations[0].conversation_id, "conversation-1");
        std::fs::remove_dir_all(directory).expect("remove test directory");
    }

    #[test]
    fn stopped_desktop_lists_native_claude_history() {
        let directory = temp_path("offline-claude-history");
        let project = directory.join("claude/projects/encoded-project");
        std::fs::create_dir_all(&project).expect("create Claude project directory");
        std::fs::write(
            project.join("session-native.jsonl"),
            concat!(
                "{\"type\":\"user\",\"sessionId\":\"session-native\",\"cwd\":\"D:\\\\project\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"uuid\":\"u1\",\"message\":{\"role\":\"user\",\"content\":\"hello\"}}\n",
                "{\"type\":\"ai-title\",\"sessionId\":\"session-native\",\"aiTitle\":\"Native Claude session\"}\n"
            ),
        )
        .expect("write Claude transcript");
        let mut config = default_config_value_with_display_name(
            "desktop-test",
            "Desktop Test",
            "http://localhost:8000",
        );
        config["security"]["development_mode"] = serde_json::Value::Bool(true);
        let mut desktop = DesktopConfig::from_value(config).expect("desktop config");
        desktop.storage_dir = directory.join("desktop-data");
        desktop.claude.home_dir = directory.join("claude");

        let conversations = list_stored_desktop_conversations(&desktop, ActorId::Claude, 20)
            .expect("list conversations");

        assert_eq!(conversations.len(), 1);
        assert_eq!(conversations[0].actor_id, ActorId::Claude);
        assert_eq!(conversations[0].conversation_id, "session-native");
        assert_eq!(conversations[0].title, "Native Claude session");
        std::fs::remove_dir_all(directory).expect("remove test directory");
    }

    #[test]
    fn normalize_desktop_update_endpoint_returns_none_for_blank_values() {
        assert_eq!(normalize_desktop_update_endpoint(None), None);
        assert_eq!(normalize_desktop_update_endpoint(Some("   ")), None);
    }

    #[test]
    fn normalize_desktop_update_endpoint_appends_tauri_suffix_to_base_url() {
        assert_eq!(
            normalize_desktop_update_endpoint(Some(" http://localhost:8000/ ")),
            Some(
                "http://localhost:8000/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}"
                    .to_owned()
            )
        );
    }

    #[test]
    fn normalize_desktop_update_endpoint_keeps_placeholder_endpoint() {
        assert_eq!(
            normalize_desktop_update_endpoint(Some(
                "https://coworker.example.com/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}/"
            )),
            Some(
                "https://coworker.example.com/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}"
                    .to_owned()
            )
        );
    }

    #[test]
    fn normalize_desktop_update_endpoint_repairs_partial_placeholder_endpoint() {
        assert_eq!(
            normalize_desktop_update_endpoint(Some(
                "http://updates.example.test:8000/api/desktop-updates/{{target}}/{{arch}}"
            )),
            Some(
                "http://updates.example.test:8000/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}"
                    .to_owned()
            )
        );
    }

    #[test]
    fn desktop_update_base_url_strips_tauri_endpoint_suffix() {
        assert_eq!(
            desktop_update_base_url_from_endpoint(
                "http://updates.example.test:8000/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}"
            ),
            Some("http://updates.example.test:8000".to_owned())
        );
    }

    #[test]
    fn desktop_update_base_url_strips_plain_update_path() {
        assert_eq!(
            desktop_update_base_url_from_endpoint(
                "https://coworker.example.com/api/desktop-updates"
            ),
            Some("https://coworker.example.com".to_owned())
        );
    }

    #[test]
    fn desktop_update_base_url_keeps_custom_endpoint_without_update_path() {
        assert_eq!(
            desktop_update_base_url_from_endpoint("https://updates.example.com/feed"),
            Some("https://updates.example.com/feed".to_owned())
        );
    }

    #[test]
    fn desktop_update_base_url_returns_none_for_blank_endpoint() {
        assert_eq!(desktop_update_base_url_from_endpoint("   "), None);
    }

    #[test]
    fn read_log_tail_returns_empty_for_missing_file() {
        let path = temp_path("missing.log");
        assert_eq!(
            read_log_tail(&path, 100).expect("missing log should be ok"),
            ""
        );
    }

    #[test]
    fn read_log_tail_reads_small_file_completely() {
        let path = temp_path("small.log");
        std::fs::write(&path, "one\ntwo\n").expect("write test log");

        assert_eq!(read_log_tail(&path, 1024).expect("read log"), "one\ntwo\n");

        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn read_log_tail_reads_only_tail_bytes() {
        let path = temp_path("tail.log");
        std::fs::write(&path, "abcdef").expect("write test log");

        assert_eq!(read_log_tail(&path, 4).expect("read log"), "cdef");

        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn read_log_tail_tolerates_invalid_utf8() {
        let path = temp_path("invalid.log");
        std::fs::write(&path, [0xff, b'o', b'k']).expect("write test log");

        let text = read_log_tail(&path, 10).expect("read log");
        assert!(text.contains("ok"));

        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn first_output_line_prefers_stdout_and_ignores_blank_lines() {
        assert_eq!(
            first_output_line(b"\n\ncodex 1.0\n", b"error"),
            Some("codex 1.0".to_owned())
        );
    }

    #[test]
    fn first_output_line_falls_back_to_stderr() {
        assert_eq!(
            first_output_line(b"", b"\nfailed to start\n"),
            Some("failed to start".to_owned())
        );
    }

    #[test]
    fn first_output_line_returns_none_when_output_is_blank() {
        assert_eq!(first_output_line(b"\n", b"\n"), None);
    }
}
