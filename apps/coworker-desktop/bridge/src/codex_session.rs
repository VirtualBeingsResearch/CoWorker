use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::{
    actor::{ActorStreamEvent, publish_actor_stream_event, session_message_to_actor},
    config::BridgeConfig,
    desktop_protocol::ActorId,
    error::{BridgeError, Result},
};

pub const BRIDGE_THREAD_SOURCE: &str = "coworker-desktop";
const PREVIOUS_BRIDGE_THREAD_SOURCE: &str = "coworker-codex-bridge";
const DEFAULT_PAGE_SIZE: usize = 80;
const MAX_PAGE_SIZE: usize = 200;
const MAX_TEXT_CHARS: usize = 16 * 1024;
const JSONL_TAIL_CHUNK_BYTES: u64 = 256 * 1024;
const EMPTY_RESPONSE_MESSAGE: &str =
    "Codex 已结束本轮，但没有返回任何消息。请重试；如果仍然发生，请重启桌面端以重新连接 Codex。";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionSummary {
    pub thread_id: String,
    pub title: String,
    pub project_id: Option<String>,
    pub project_name: Option<String>,
    pub project_path: Option<String>,
    pub status: String,
    pub last_active_at: String,
    pub owned_by_bridge: bool,
    pub can_continue: bool,
    pub collaboration_mode: Option<String>,
    pub pending_collaboration_mode: Option<String>,
    pub source: Option<String>,
    pub participants: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionAttachment {
    pub filename: String,
    pub media_type: String,
    pub size: Option<u64>,
    pub path: Option<String>,
    pub downloadable: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionMessage {
    pub id: String,
    pub timestamp: String,
    pub author_kind: String,
    pub author_id: Option<String>,
    pub author_label: String,
    pub kind: String,
    pub text: String,
    pub attachments: Vec<SessionAttachment>,
    pub turn_id: Option<String>,
    pub item_id: Option<String>,
    pub streaming: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionMessagePage {
    pub messages: Vec<SessionMessage>,
    pub next_before_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionDetail {
    pub summary: SessionSummary,
    pub messages: Vec<SessionMessage>,
    pub next_before_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionEvent {
    pub thread_id: String,
    pub event_type: String,
    pub message: Option<SessionMessage>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionOverlayRecord {
    pub id: String,
    pub timestamp: String,
    pub author_kind: String,
    pub author_id: Option<String>,
    pub author_label: String,
    pub kind: String,
    pub text: String,
    #[serde(default)]
    pub attachments: Vec<SessionAttachment>,
    pub turn_id: Option<String>,
    pub item_id: Option<String>,
    #[serde(default)]
    pub streaming: bool,
}

#[derive(Debug, Clone, Default)]
pub struct RuntimeSessionState {
    pub owned_thread_ids: HashSet<String>,
    pub thread_status: HashMap<String, String>,
    pub thread_collaboration_mode: HashMap<String, String>,
    pub thread_pending_collaboration_mode: HashMap<String, String>,
}

#[derive(Debug, Clone)]
struct SessionIndexEntry {
    thread_id: String,
    title: String,
    updated_at: String,
}

#[derive(Debug, Clone)]
struct SessionMeta {
    thread_id: Option<String>,
    source: Option<String>,
    thread_source: Option<String>,
    cwd: Option<String>,
    timestamp: Option<String>,
}

#[derive(Debug, Clone, Copy)]
struct SessionPageCursor {
    native_end: u64,
    overlay_end: usize,
    skip: usize,
}

pub fn publish_session_event(event: SessionEvent) {
    let event_type = if event.event_type == "session-message-delta" {
        "message_delta"
    } else {
        "conversation_updated"
    };
    let message_id = event.message.as_ref().map(|message| message.id.clone());
    let message = event
        .message
        .map(|message| session_message_to_actor(&event.thread_id, message));
    publish_actor_stream_event(ActorStreamEvent {
        actor_id: ActorId::Codex,
        conversation_id: event.thread_id,
        message_id,
        event: json!({"type": event_type, "message": message}),
    });
}

pub fn list_sessions(
    config: &BridgeConfig,
    app_threads: &[Value],
    runtime: RuntimeSessionState,
    limit: usize,
) -> Result<Vec<SessionSummary>> {
    let persisted_owned = owned_thread_ids_from_state(config);
    let session_files = collect_session_files_once(config)?;
    let mut summaries = Vec::new();
    let mut seen = HashSet::new();

    for thread in app_threads {
        let Some(obj) = thread.as_object() else {
            continue;
        };
        let thread_id = first_string(Some(obj), &["id", "thread_id", "threadId"]);
        let Some(thread_id) = thread_id.filter(|value| !value.is_empty()) else {
            continue;
        };
        let meta = read_session_meta_from_index(&session_files, &thread_id)
            .ok()
            .flatten();
        let owned = is_owned_by_bridge(&thread_id, meta.as_ref(), &persisted_owned, &runtime);
        seen.insert(thread_id.clone());
        summaries.push(summary_from_thread(config, obj, meta, owned, &runtime));
    }

    for entry in read_session_index(config)?.into_iter() {
        if seen.contains(&entry.thread_id) {
            continue;
        }
        let meta = read_session_meta_from_index(&session_files, &entry.thread_id)
            .ok()
            .flatten();
        let owned = is_owned_by_bridge(&entry.thread_id, meta.as_ref(), &persisted_owned, &runtime);
        seen.insert(entry.thread_id.clone());
        summaries.push(summary_from_index(config, entry, meta, owned, &runtime));
    }

    let mut recovered = 0;
    for path in session_files.iter().rev() {
        if recovered >= limit.max(1) {
            break;
        }
        let Some(meta) = read_session_meta_from_path(path).ok().flatten() else {
            continue;
        };
        if !meta
            .thread_source
            .as_deref()
            .is_some_and(is_bridge_thread_source)
        {
            continue;
        }
        let Some(thread_id) = meta.thread_id.clone().filter(|id| !id.is_empty()) else {
            continue;
        };
        if !seen.insert(thread_id.clone()) {
            continue;
        }
        summaries.push(summary_from_index(
            config,
            SessionIndexEntry {
                thread_id,
                title: String::new(),
                updated_at: file_modified_timestamp(path),
            },
            Some(meta),
            true,
            &runtime,
        ));
        recovered += 1;
    }

    // Threads we know are bridge-owned (persisted across restarts, or created earlier
    // this run) can be absent from both `thread/list` (its sourceKinds filter may not
    // cover them) and the on-disk session index (Codex may not have indexed them yet).
    // Fall back to a synthesized summary so they still show up in the sidebar.
    for thread_id in persisted_owned
        .iter()
        .chain(runtime.owned_thread_ids.iter())
    {
        if seen.contains(thread_id) {
            continue;
        }
        seen.insert(thread_id.clone());
        summaries.push(fallback_summary(config, thread_id, &runtime));
    }

    summaries.sort_by(|left, right| {
        session_sort_key(right)
            .cmp(&session_sort_key(left))
            .then_with(|| right.thread_id.cmp(&left.thread_id))
    });
    summaries.truncate(limit.max(1));
    Ok(summaries)
}

pub fn get_session_detail(
    config: &BridgeConfig,
    thread_id: &str,
    runtime: RuntimeSessionState,
    page_size: usize,
) -> Result<SessionDetail> {
    let summary = match read_session_index(config)?
        .into_iter()
        .find(|entry| entry.thread_id == thread_id)
    {
        Some(entry) => {
            let meta = read_session_meta(config, thread_id)?;
            let persisted_owned = owned_thread_ids_from_state(config);
            let owned = is_owned_by_bridge(thread_id, meta.as_ref(), &persisted_owned, &runtime);
            summary_from_index(config, entry, meta, owned, &runtime)
        }
        None => fallback_summary(config, thread_id, &runtime),
    };
    let page = load_session_messages(config, thread_id, None, page_size)?;
    Ok(SessionDetail {
        summary,
        messages: page.messages,
        next_before_cursor: page.next_before_cursor,
    })
}

pub fn load_session_messages(
    config: &BridgeConfig,
    thread_id: &str,
    before_cursor: Option<&str>,
    page_size: usize,
) -> Result<SessionMessagePage> {
    let page_size = page_size.clamp(1, MAX_PAGE_SIZE);
    let native_path = find_session_file(config, thread_id)?;
    let current_native_end = native_path
        .as_ref()
        .map(fs::metadata)
        .transpose()?
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let mut overlay_messages = read_overlay_messages(config, thread_id)?;
    let cursor = before_cursor
        .and_then(parse_session_page_cursor)
        .unwrap_or(SessionPageCursor {
            native_end: current_native_end,
            overlay_end: overlay_messages.len(),
            skip: 0,
        });
    let native_end = cursor.native_end.min(current_native_end);
    overlay_messages.truncate(cursor.overlay_end.min(overlay_messages.len()));
    let overlay_end = overlay_messages.len();
    let target = cursor.skip.saturating_add(page_size);
    let mut wanted_native = target;

    let (messages, has_older_native) = loop {
        let (native_messages, has_older_native) =
            read_codex_messages_tail(native_path.as_deref(), thread_id, native_end, wanted_native)?;
        let mut messages = native_messages;
        messages.extend(overlay_messages.iter().cloned());
        let messages = normalize_session_messages(config, messages);
        if messages.len() >= target || !has_older_native {
            break (messages, has_older_native);
        }
        let next = wanted_native.saturating_mul(2);
        if next == wanted_native {
            break (messages, has_older_native);
        }
        wanted_native = next;
    };

    let end = messages.len().saturating_sub(cursor.skip);
    let start = end.saturating_sub(page_size);
    let has_older = start > 0 || has_older_native;
    let next_before_cursor = has_older.then(|| {
        format_session_page_cursor(SessionPageCursor {
            native_end,
            overlay_end,
            skip: cursor.skip.saturating_add(end - start),
        })
    });
    Ok(SessionMessagePage {
        messages: messages[start..end].to_vec(),
        next_before_cursor,
    })
}

fn normalize_session_messages(
    config: &BridgeConfig,
    mut messages: Vec<SessionMessage>,
) -> Vec<SessionMessage> {
    messages.sort_by(|left, right| {
        left.timestamp
            .cmp(&right.timestamp)
            .then_with(|| left.id.cmp(&right.id))
    });
    // Exact protocol ids are safe to deduplicate globally. Text is not: a
    // user can legitimately send "continue" twice, and a local -> Coworker
    // overlay may have the same text as a Codex prompt.
    let mut seen_ids: HashSet<String> = HashSet::new();
    let mut seen_items: HashSet<(String, String, String)> = HashSet::new();
    messages.retain(|message| {
        if !seen_ids.insert(message.id.clone()) {
            return false;
        }
        if let Some(item_id) = message.item_id.as_deref().filter(|value| !value.is_empty()) {
            let key = (
                item_id.to_owned(),
                message.turn_id.clone().unwrap_or_default(),
                message.kind.clone(),
            );
            if !seen_items.insert(key) {
                return false;
            }
        }
        true
    });

    // A Desktop prompt is written optimistically as an overlay and later
    // appears in the native Codex transcript. Pair those two sources one for
    // one and keep the overlay (it owns attachment metadata and author data).
    // Do not collapse two native turns or two overlays with identical text.
    let mut native_input_positions: HashMap<String, Vec<usize>> = HashMap::new();
    for (index, message) in messages.iter().enumerate() {
        if matches!(message.author_kind.as_str(), "local" | "coworker")
            && !message.id.starts_with("overlay-")
        {
            native_input_positions
                .entry(format!("{}\u{0}{}", message.kind, message.text))
                .or_default()
                .push(index);
        }
    }
    let mut paired_native_positions = HashSet::new();
    for message in &messages {
        let is_codex_input_overlay = message.id.starts_with("overlay-")
            && (message.author_kind == "coworker"
                || (message.author_kind == "local"
                    && (message.author_id.as_deref() == Some(config.codex_id.as_str())
                        || message.author_label == "本机")));
        if !is_codex_input_overlay {
            continue;
        }
        let key = format!("{}\u{0}{}", message.kind, message.text);
        if let Some(position) = native_input_positions
            .get(&key)
            .and_then(|positions| {
                positions
                    .iter()
                    .find(|position| !paired_native_positions.contains(*position))
            })
            .copied()
        {
            paired_native_positions.insert(position);
        }
    }
    if !paired_native_positions.is_empty() {
        messages = messages
            .into_iter()
            .enumerate()
            .filter_map(|(index, message)| {
                (!paired_native_positions.contains(&index)).then_some(message)
            })
            .collect();
    }
    messages
}

pub fn append_overlay_message(
    config: &BridgeConfig,
    thread_id: &str,
    mut record: SessionOverlayRecord,
) -> Result<SessionMessage> {
    if record.id.trim().is_empty() {
        record.id = format!("overlay-{thread_id}-{}", uuid::Uuid::new_v4());
    }
    if record.timestamp.trim().is_empty() {
        record.timestamp = Utc::now().to_rfc3339();
    }
    let message = overlay_record_to_message(&record);
    let path = overlay_path(config, thread_id);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(file, "{}", serde_json::to_string(&record)?)?;
    publish_session_event(SessionEvent {
        thread_id: thread_id.to_owned(),
        event_type: "session-updated".to_owned(),
        message: Some(message.clone()),
    });
    Ok(message)
}

pub fn save_session_attachment(
    config: &BridgeConfig,
    thread_id: &str,
    source_path: &str,
) -> Result<SessionAttachment> {
    let source = Path::new(source_path);
    let filename = source
        .file_name()
        .and_then(|value| value.to_str())
        .map(sanitize_file_component)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "attachment".to_owned());
    let bytes = fs::read(source)?;
    if bytes.len() as u64 > config.attachment_max_bytes {
        return Err(BridgeError::message(format!(
            "attachment exceeds size limit: {} > {} bytes",
            bytes.len(),
            config.attachment_max_bytes
        )));
    }
    let dir = Path::new(&config.attachment_store_dir)
        .join(sanitize_file_component(thread_id))
        .join("ui");
    fs::create_dir_all(&dir)?;
    let target = dir.join(format!("{}_{}", now_millis(), filename));
    fs::write(&target, bytes)?;
    let target = fs::canonicalize(&target).unwrap_or(target);
    let size = fs::metadata(&target).ok().map(|meta| meta.len());
    Ok(SessionAttachment {
        filename,
        media_type: guess_media_type(source_path).to_owned(),
        size,
        path: Some(target.to_string_lossy().into_owned()),
        downloadable: true,
        reason: None,
    })
}

pub fn copy_attachment_to_path(
    source_path: &str,
    destination_path: &str,
) -> Result<SessionAttachment> {
    let source = Path::new(source_path);
    if !source.exists() {
        return Err(BridgeError::message("attachment source does not exist"));
    }
    let destination = Path::new(destination_path);
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::copy(source, destination)?;
    let filename = destination
        .file_name()
        .and_then(|value| value.to_str())
        .map(str::to_owned)
        .unwrap_or_else(|| "attachment".to_owned());
    let size = fs::metadata(destination).ok().map(|meta| meta.len());
    Ok(SessionAttachment {
        filename,
        media_type: guess_media_type(destination_path).to_owned(),
        size,
        path: Some(destination.display().to_string()),
        downloadable: true,
        reason: None,
    })
}

pub fn owned_thread_ids_from_state(config: &BridgeConfig) -> HashSet<String> {
    let Some(path) = config.state_path.as_ref() else {
        return HashSet::new();
    };
    let Ok(text) = fs::read_to_string(path) else {
        return HashSet::new();
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return HashSet::new();
    };
    let mut ids = HashSet::new();
    for field in ["coworker_started_thread_ids", "bridge_started_thread_ids"] {
        if let Some(items) = value.get(field).and_then(Value::as_array) {
            ids.extend(items.iter().filter_map(Value::as_str).map(str::to_owned));
        }
    }
    ids
}

pub fn is_thread_owned(
    config: &BridgeConfig,
    thread_id: &str,
    runtime: &RuntimeSessionState,
) -> bool {
    if runtime.owned_thread_ids.contains(thread_id) {
        return true;
    }
    let persisted = owned_thread_ids_from_state(config);
    let meta = read_session_meta(config, thread_id).ok().flatten();
    is_owned_by_bridge(thread_id, meta.as_ref(), &persisted, runtime)
}

fn summary_from_thread(
    config: &BridgeConfig,
    obj: &Map<String, Value>,
    meta: Option<SessionMeta>,
    owned: bool,
    runtime: &RuntimeSessionState,
) -> SessionSummary {
    let thread_id = first_string(Some(obj), &["id", "thread_id", "threadId"]).unwrap_or_default();
    let project = obj
        .get("project")
        .and_then(Value::as_object)
        .or_else(|| obj.get("workspace").and_then(Value::as_object))
        .or_else(|| obj.get("worktree").and_then(Value::as_object));
    let status = obj
        .get("status")
        .and_then(Value::as_object)
        .and_then(|status| first_string(Some(status), &["type"]))
        .or_else(|| runtime.thread_status.get(&thread_id).cloned())
        .unwrap_or_else(|| "unknown".to_owned());
    SessionSummary {
        title: first_string(Some(obj), &["name", "preview", "title"])
            .or_else(|| {
                read_session_index(config).ok().and_then(|entries| {
                    entries
                        .into_iter()
                        .find(|entry| entry.thread_id == thread_id)
                        .map(|entry| entry.title)
                })
            })
            .unwrap_or_else(|| "未命名会话".to_owned()),
        project_id: project_id_from_thread(obj, project, meta.as_ref()),
        project_name: project_name_from_thread(obj, project, meta.as_ref()),
        project_path: project_path_from_thread(obj, project, meta.as_ref()),
        last_active_at: newest_timestamp(vec![
            first_value_string(Some(obj), &["updatedAt", "updated_at", "recencyAt"]),
            meta.as_ref().and_then(|m| m.timestamp.clone()),
            overlay_last_timestamp(config, &thread_id).ok().flatten(),
        ]),
        collaboration_mode: runtime.thread_collaboration_mode.get(&thread_id).cloned(),
        pending_collaboration_mode: runtime
            .thread_pending_collaboration_mode
            .get(&thread_id)
            .cloned(),
        source: meta
            .as_ref()
            .and_then(|m| m.thread_source.clone().or_else(|| m.source.clone())),
        participants: participants(owned),
        can_continue: owned,
        owned_by_bridge: owned,
        thread_id,
        status,
    }
}

fn summary_from_index(
    config: &BridgeConfig,
    entry: SessionIndexEntry,
    meta: Option<SessionMeta>,
    owned: bool,
    runtime: &RuntimeSessionState,
) -> SessionSummary {
    let status = runtime
        .thread_status
        .get(&entry.thread_id)
        .cloned()
        .unwrap_or_else(|| "notLoaded".to_owned());
    SessionSummary {
        thread_id: entry.thread_id.clone(),
        title: if entry.title.is_empty() {
            "未命名会话".to_owned()
        } else {
            entry.title
        },
        project_id: meta.as_ref().and_then(|m| m.cwd.clone()),
        project_name: meta
            .as_ref()
            .and_then(|m| m.cwd.as_ref())
            .and_then(|cwd| project_name_from_path(cwd)),
        project_path: meta.as_ref().and_then(|m| m.cwd.clone()),
        last_active_at: newest_timestamp(vec![
            Some(entry.updated_at.clone()),
            meta.as_ref().and_then(|m| m.timestamp.clone()),
            overlay_last_timestamp(config, &entry.thread_id)
                .ok()
                .flatten(),
        ]),
        owned_by_bridge: owned,
        can_continue: owned,
        collaboration_mode: runtime
            .thread_collaboration_mode
            .get(&entry.thread_id)
            .cloned(),
        pending_collaboration_mode: runtime
            .thread_pending_collaboration_mode
            .get(&entry.thread_id)
            .cloned(),
        source: meta
            .as_ref()
            .and_then(|m| m.thread_source.clone().or_else(|| m.source.clone())),
        participants: participants(owned),
        status,
    }
}

fn fallback_summary(
    config: &BridgeConfig,
    thread_id: &str,
    runtime: &RuntimeSessionState,
) -> SessionSummary {
    let meta = read_session_meta(config, thread_id).ok().flatten();
    let persisted = owned_thread_ids_from_state(config);
    let owned = is_owned_by_bridge(thread_id, meta.as_ref(), &persisted, runtime);
    SessionSummary {
        thread_id: thread_id.to_owned(),
        title: "未命名会话".to_owned(),
        project_id: meta.as_ref().and_then(|m| m.cwd.clone()),
        project_name: meta
            .as_ref()
            .and_then(|m| m.cwd.as_ref())
            .and_then(|cwd| project_name_from_path(cwd)),
        project_path: meta.as_ref().and_then(|m| m.cwd.clone()),
        status: runtime
            .thread_status
            .get(thread_id)
            .cloned()
            .unwrap_or_else(|| "unknown".to_owned()),
        last_active_at: newest_timestamp(vec![
            meta.and_then(|m| m.timestamp),
            overlay_last_timestamp(config, thread_id).ok().flatten(),
        ]),
        owned_by_bridge: owned,
        can_continue: owned,
        collaboration_mode: runtime.thread_collaboration_mode.get(thread_id).cloned(),
        pending_collaboration_mode: runtime
            .thread_pending_collaboration_mode
            .get(thread_id)
            .cloned(),
        source: None,
        participants: participants(owned),
    }
}

fn participants(owned: bool) -> Vec<String> {
    if owned {
        vec![
            "local".to_owned(),
            "codex".to_owned(),
            "coworker".to_owned(),
        ]
    } else {
        vec!["codex".to_owned()]
    }
}

fn project_id_from_thread(
    thread: &Map<String, Value>,
    project: Option<&Map<String, Value>>,
    meta: Option<&SessionMeta>,
) -> Option<String> {
    first_string(
        project,
        &[
            "id",
            "project_id",
            "projectId",
            "workspace_id",
            "workspaceId",
        ],
    )
    .or_else(|| {
        first_string(
            Some(thread),
            &["project_id", "projectId", "workspace_id", "workspaceId"],
        )
    })
    .or_else(|| project_path_from_thread(thread, project, meta))
}

fn project_name_from_thread(
    thread: &Map<String, Value>,
    project: Option<&Map<String, Value>>,
    meta: Option<&SessionMeta>,
) -> Option<String> {
    first_string(
        project,
        &[
            "name",
            "title",
            "display_name",
            "displayName",
            "project_name",
            "projectName",
            "workspace_name",
            "workspaceName",
        ],
    )
    .or_else(|| {
        first_string(
            Some(thread),
            &[
                "project_name",
                "projectName",
                "workspace_name",
                "workspaceName",
                "worktree_name",
                "worktreeName",
            ],
        )
    })
    .or_else(|| {
        project_path_from_thread(thread, project, meta)
            .and_then(|path| project_name_from_path(&path))
    })
}

fn project_name_from_path(path: &str) -> Option<String> {
    path.trim_end_matches(['/', '\\'])
        .rsplit(['/', '\\'])
        .next()
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
}

fn project_path_from_thread(
    thread: &Map<String, Value>,
    project: Option<&Map<String, Value>>,
    meta: Option<&SessionMeta>,
) -> Option<String> {
    first_string(
        project,
        &[
            "path",
            "root",
            "cwd",
            "project_path",
            "projectPath",
            "workspace_path",
            "workspacePath",
            "worktree_path",
            "worktreePath",
        ],
    )
    .or_else(|| {
        first_string(
            Some(thread),
            &[
                "project_path",
                "projectPath",
                "workspace_path",
                "workspacePath",
                "worktree_path",
                "worktreePath",
                "root",
                "cwd",
            ],
        )
    })
    .or_else(|| meta.and_then(|m| m.cwd.clone()))
}

fn is_owned_by_bridge(
    thread_id: &str,
    meta: Option<&SessionMeta>,
    persisted_owned: &HashSet<String>,
    runtime: &RuntimeSessionState,
) -> bool {
    runtime.owned_thread_ids.contains(thread_id)
        || persisted_owned.contains(thread_id)
        || meta
            .and_then(|m| m.thread_source.as_deref().or(m.source.as_deref()))
            .is_some_and(is_bridge_thread_source)
}

fn is_bridge_thread_source(source: &str) -> bool {
    source == BRIDGE_THREAD_SOURCE || source == PREVIOUS_BRIDGE_THREAD_SOURCE
}

fn read_session_index(config: &BridgeConfig) -> Result<Vec<SessionIndexEntry>> {
    let path = Path::new(&config.codex_home_dir).join("session_index.jsonl");
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = fs::File::open(path)?;
    let mut entries = Vec::new();
    for line in BufReader::new(file).lines() {
        let line = line?;
        let Ok(value) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(obj) = value.as_object() else {
            continue;
        };
        let thread_id =
            first_string(Some(obj), &["id", "thread_id", "threadId"]).unwrap_or_default();
        if thread_id.is_empty() {
            continue;
        }
        entries.push(SessionIndexEntry {
            thread_id,
            title: first_string(Some(obj), &["thread_name", "name", "title"]).unwrap_or_default(),
            updated_at: first_string(Some(obj), &["updated_at", "updatedAt"]).unwrap_or_default(),
        });
    }
    Ok(entries)
}

fn read_session_meta(config: &BridgeConfig, thread_id: &str) -> Result<Option<SessionMeta>> {
    let Some(path) = find_session_file(config, thread_id)? else {
        return Ok(None);
    };
    read_session_meta_from_path(&path)
}

fn read_session_meta_from_index(files: &[PathBuf], thread_id: &str) -> Result<Option<SessionMeta>> {
    let Some(path) = latest_session_file_for_thread(files, thread_id) else {
        return Ok(None);
    };
    read_session_meta_from_path(path)
}

fn read_session_meta_from_path(path: &Path) -> Result<Option<SessionMeta>> {
    let file = fs::File::open(path)?;
    for line in BufReader::new(file).lines() {
        let line = line?;
        let Ok(value) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if let Some(meta) = parse_session_meta(&value) {
            return Ok(Some(meta));
        }
    }
    Ok(None)
}

fn read_codex_messages_tail(
    path: Option<&Path>,
    thread_id: &str,
    end_offset: u64,
    wanted_messages: usize,
) -> Result<(Vec<SessionMessage>, bool)> {
    let Some(path) = path else {
        return Ok((Vec::new(), false));
    };
    let mut file = fs::File::open(path)?;
    let end_offset = end_offset.min(file.metadata()?.len());
    if end_offset == 0 {
        return Ok((Vec::new(), false));
    }

    let mut position = end_offset;
    let mut partial_line = Vec::new();
    let mut records = Vec::new();
    let mut parsed_messages = 0usize;
    let wanted_with_context = wanted_messages.saturating_add(16);
    loop {
        let next_start = position.saturating_sub(JSONL_TAIL_CHUNK_BYTES);
        let mut chunk = vec![0; (position - next_start) as usize];
        file.seek(SeekFrom::Start(next_start))?;
        file.read_exact(&mut chunk)?;
        chunk.extend_from_slice(&partial_line);

        let first_newline = (next_start > 0)
            .then(|| chunk.iter().position(|byte| *byte == b'\n'))
            .flatten();
        let complete_start = if next_start == 0 {
            0
        } else {
            first_newline
                .map(|position| position + 1)
                .unwrap_or(chunk.len())
        };
        let mut ranges = Vec::new();
        let mut line_start = complete_start;
        while line_start < chunk.len() {
            let line_end = chunk[line_start..]
                .iter()
                .position(|byte| *byte == b'\n')
                .map(|offset| line_start + offset)
                .unwrap_or(chunk.len());
            if line_end > line_start {
                ranges.push((line_start, line_end));
            }
            line_start = line_end.saturating_add(1);
        }
        for (line_start, line_end) in ranges.into_iter().rev() {
            let mut line = &chunk[line_start..line_end];
            if line.last() == Some(&b'\r') {
                line = &line[..line.len() - 1];
            }
            let Ok(value) = serde_json::from_slice::<Value>(line) else {
                continue;
            };
            let line_offset = next_start.saturating_add(line_start as u64);
            let line_id = usize::try_from(line_offset).unwrap_or(usize::MAX);
            let mut ignored_call_names = HashMap::new();
            let mut message =
                parse_codex_message(thread_id, line_id, &value, &mut ignored_call_names);
            if let Some(message) = message.as_mut() {
                message.text = trim_large_text(&message.text);
                parsed_messages += 1;
            }
            records.push((line_id, value, message));
        }
        if next_start == 0 || parsed_messages >= wanted_with_context {
            records.reverse();
            return Ok((finalize_tail_messages(thread_id, records), next_start > 0));
        }
        partial_line = first_newline
            .map(|position| chunk[..position].to_vec())
            .unwrap_or(chunk);
        position = next_start;
    }
}

fn finalize_tail_messages(
    thread_id: &str,
    records: Vec<(usize, Value, Option<SessionMessage>)>,
) -> Vec<SessionMessage> {
    let mut messages = Vec::new();
    let mut call_names: HashMap<String, String> = HashMap::new();
    let mut pending_plan_turn: Option<Option<String>> = None;
    for (line_id, value, preparsed) in records {
        let obj = value.as_object();
        let envelope_type = first_string(obj, &["type"]);
        let payload = obj
            .and_then(|obj| obj.get("payload"))
            .and_then(Value::as_object)
            .or(obj);
        let item_type = payload.and_then(|payload| first_string(Some(payload), &["type"]));
        if envelope_type.as_deref() == Some("response_item")
            && item_type.as_deref() == Some("function_call")
        {
            if let (Some(call_id), Some(name)) = (
                payload.and_then(|payload| first_string(Some(payload), &["call_id"])),
                payload.and_then(|payload| first_string(Some(payload), &["name"])),
            ) {
                call_names.insert(call_id, name);
            }
        }
        let mut message = if envelope_type.as_deref() == Some("response_item")
            && item_type.as_deref() == Some("function_call_output")
        {
            parse_codex_message(thread_id, line_id, &value, &mut call_names)
        } else {
            preparsed
        };
        if let Some(message) = message.as_mut() {
            message.text = trim_large_text(&message.text);
        }
        if let Some(message) = message {
            if message.kind == "plan" {
                pending_plan_turn = Some(message.turn_id.clone());
            }
            if message.text == EMPTY_RESPONSE_MESSAGE {
                let plan_is_this_turn = pending_plan_turn.as_ref().is_some_and(|plan_turn| {
                    plan_turn.is_none() || plan_turn.as_ref() == message.turn_id.as_ref()
                });
                pending_plan_turn = None;
                if plan_is_this_turn {
                    continue;
                }
            }
            messages.push(message);
        }
    }
    messages
}

fn parse_session_page_cursor(value: &str) -> Option<SessionPageCursor> {
    let mut parts = value.split(':');
    if parts.next()? != "tail-v1" {
        return None;
    }
    let cursor = SessionPageCursor {
        native_end: parts.next()?.parse().ok()?,
        overlay_end: parts.next()?.parse().ok()?,
        skip: parts.next()?.parse().ok()?,
    };
    parts.next().is_none().then_some(cursor)
}

fn format_session_page_cursor(cursor: SessionPageCursor) -> String {
    format!(
        "tail-v1:{}:{}:{}",
        cursor.native_end, cursor.overlay_end, cursor.skip
    )
}

fn read_overlay_messages(config: &BridgeConfig, thread_id: &str) -> Result<Vec<SessionMessage>> {
    let path = overlay_path(config, thread_id);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = fs::File::open(path)?;
    let mut messages = Vec::new();
    for line in BufReader::new(file).lines() {
        let line = line?;
        let Ok(record) = serde_json::from_str::<SessionOverlayRecord>(&line) else {
            continue;
        };
        messages.push(overlay_record_to_message(&record));
    }
    Ok(messages)
}

fn overlay_last_timestamp(config: &BridgeConfig, thread_id: &str) -> Result<Option<String>> {
    let path = overlay_path(config, thread_id);
    if !path.exists() {
        return Ok(None);
    }
    let file = fs::File::open(path)?;
    let mut latest: Option<String> = None;
    for line in BufReader::new(file).lines() {
        let line = line?;
        let Ok(record) = serde_json::from_str::<SessionOverlayRecord>(&line) else {
            continue;
        };
        if record.timestamp.trim().is_empty() {
            continue;
        }
        if latest.as_deref().is_none_or(|current| {
            timestamp_sort_key(&record.timestamp) > timestamp_sort_key(current)
        }) {
            latest = Some(record.timestamp);
        }
    }
    Ok(latest)
}

fn overlay_record_to_message(record: &SessionOverlayRecord) -> SessionMessage {
    SessionMessage {
        id: record.id.clone(),
        timestamp: record.timestamp.clone(),
        author_kind: record.author_kind.clone(),
        author_id: record.author_id.clone(),
        author_label: record.author_label.clone(),
        kind: record.kind.clone(),
        text: trim_large_text(&record.text),
        attachments: record.attachments.clone(),
        turn_id: record.turn_id.clone(),
        item_id: record.item_id.clone(),
        streaming: record.streaming,
    }
}

fn parse_session_meta(value: &Value) -> Option<SessionMeta> {
    let obj = value.as_object()?;
    if first_string(Some(obj), &["type"]).as_deref() != Some("session_meta") {
        return None;
    }
    let payload = obj.get("payload").and_then(Value::as_object).unwrap_or(obj);
    Some(SessionMeta {
        thread_id: first_string(
            Some(payload),
            &["id", "session_id", "sessionId", "thread_id", "threadId"],
        ),
        source: first_string(Some(payload), &["source", "originator"]),
        thread_source: first_string(Some(payload), &["thread_source", "threadSource"]),
        cwd: first_string(Some(payload), &["cwd", "project_path", "projectPath"]),
        timestamp: first_string(Some(payload), &["timestamp"]),
    })
}

fn file_modified_timestamp(path: &Path) -> String {
    fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .map(DateTime::<Utc>::from)
        .map(|timestamp| timestamp.to_rfc3339())
        .unwrap_or_default()
}

fn parse_codex_message(
    thread_id: &str,
    line_index: usize,
    value: &Value,
    call_names: &mut HashMap<String, String>,
) -> Option<SessionMessage> {
    let obj = value.as_object()?;
    let envelope_type = first_string(Some(obj), &["type"]).unwrap_or_default();
    let payload = obj.get("payload").and_then(Value::as_object).unwrap_or(obj);
    let timestamp = first_string(Some(obj), &["timestamp"])
        .or_else(|| first_string(Some(payload), &["timestamp"]))
        .unwrap_or_default();
    match envelope_type.as_str() {
        "event_msg" => parse_event_msg(thread_id, line_index, payload, timestamp),
        "response_item" => {
            parse_response_item(thread_id, line_index, payload, timestamp, call_names)
        }
        "compacted" => parse_compacted(thread_id, line_index, payload, timestamp),
        _ => None,
    }
}

fn parse_event_msg(
    thread_id: &str,
    line_index: usize,
    payload: &Map<String, Value>,
    timestamp: String,
) -> Option<SessionMessage> {
    let message_type = first_string(Some(payload), &["type"]).unwrap_or_default();

    let (author_kind, author_label, kind, text): (&str, &str, &str, String) =
        match message_type.as_str() {
            // "user_message"/"agent_message" duplicate the corresponding
            // response_item "message" entry (same text, ~1ms apart); and
            // "exec_command_end"/"patch_apply_end" duplicate the result
            // already carried by the response_item function_call_output /
            // custom_tool_call_output for the same call_id - skip all four
            // here so each turn/tool result renders exactly once.
            "web_search_end" => ("tool", "搜索", "tool_call", format_web_search_end(payload)),
            "mcp_tool_call_end" => (
                "tool",
                "MCP 工具",
                "tool_call",
                format_mcp_tool_call_end(payload)?,
            ),
            "error" => (
                "system",
                "系统",
                "system",
                format!("⚠️ {}", first_string(Some(payload), &["message"])?),
            ),
            "turn_aborted" => (
                "system",
                "系统",
                "system",
                format!(
                    "本轮已中断{}",
                    first_string(Some(payload), &["reason"])
                        .map(|reason| format!("（{reason}）"))
                        .unwrap_or_default()
                ),
            ),
            "task_complete" if payload.get("last_agent_message").is_none_or(Value::is_null) => (
                "system",
                "系统",
                "system",
                EMPTY_RESPONSE_MESSAGE.to_owned(),
            ),
            "item_completed" => {
                let item = payload.get("item").and_then(Value::as_object)?;
                if first_string(Some(item), &["type"]).as_deref() != Some("Plan") {
                    return None;
                }
                (
                    "system",
                    "计划",
                    "plan",
                    first_string(Some(item), &["text"])?,
                )
            }
            _ => return None,
        };

    Some(SessionMessage {
        id: format!("codex-{thread_id}-{line_index}"),
        timestamp,
        author_kind: author_kind.to_owned(),
        author_id: None,
        author_label: author_label.to_owned(),
        kind: kind.to_owned(),
        text,
        attachments: extract_attachments(&Value::Object(payload.clone())),
        turn_id: first_string(Some(payload), &["turn_id", "turnId"]),
        item_id: first_string(Some(payload), &["item_id", "itemId", "call_id"]),
        streaming: false,
    })
}

fn parse_response_item(
    thread_id: &str,
    line_index: usize,
    item: &Map<String, Value>,
    timestamp: String,
    call_names: &mut HashMap<String, String>,
) -> Option<SessionMessage> {
    let item_type = first_string(Some(item), &["type"]).unwrap_or_default();
    let call_id = first_string(Some(item), &["call_id"]);
    let mut author_id = None;

    let (author_kind, author_label, kind, text): (String, String, String, String) =
        match item_type.as_str() {
            "message" => {
                let role = first_string(Some(item), &["role"]).unwrap_or_default();
                if role == "developer" || role == "system" {
                    return None;
                }
                let text = response_item_text(item);
                if text.trim().is_empty() {
                    return None;
                }
                if let Some((coworker_message_id, coworker_name, coworker_text)) =
                    parse_wrapped_coworker_message(&text)
                {
                    author_id = Some(coworker_message_id);
                    (
                        "coworker".to_owned(),
                        coworker_name,
                        "message".to_owned(),
                        coworker_text,
                    )
                } else if response_item_is_local_input(item) {
                    (
                        "local".to_owned(),
                        "本机".to_owned(),
                        "message".to_owned(),
                        text,
                    )
                } else {
                    (
                        "codex".to_owned(),
                        "Codex".to_owned(),
                        "message".to_owned(),
                        text,
                    )
                }
            }
            "user_message" | "user_instruction" | "input_message" => {
                let text = response_item_text(item);
                if text.trim().is_empty() {
                    return None;
                }
                (
                    "local".to_owned(),
                    "本机".to_owned(),
                    "message".to_owned(),
                    text,
                )
            }
            "reasoning" => (
                "codex".to_owned(),
                "思考".to_owned(),
                "reasoning".to_owned(),
                reasoning_summary_text(item)?,
            ),
            "function_call" => {
                let name = first_string(Some(item), &["name"]).unwrap_or_default();
                let arguments = first_string(Some(item), &["arguments"]).unwrap_or_default();
                if let Some(id) = call_id.clone() {
                    call_names.insert(id, name.clone());
                }
                if name == "update_plan" {
                    (
                        "system".to_owned(),
                        "计划".to_owned(),
                        "plan".to_owned(),
                        format_update_plan(&arguments)?,
                    )
                } else if name == "shell_command" {
                    (
                        "tool".to_owned(),
                        "终端".to_owned(),
                        "tool_call".to_owned(),
                        format_shell_command_call(&arguments),
                    )
                } else {
                    (
                        "tool".to_owned(),
                        "工具调用".to_owned(),
                        "tool_call".to_owned(),
                        format_generic_call(&name, &arguments),
                    )
                }
            }
            "function_call_output" => {
                let name = call_id.as_deref().and_then(|id| call_names.get(id));
                if name.map(String::as_str) == Some("update_plan") {
                    return None;
                }
                let output = response_item_output_text(item);
                if output.trim().is_empty() {
                    return None;
                }
                let output = strip_ansi_escape_codes(&output);
                let text = if matches!(
                    name.map(String::as_str),
                    Some("shell_command") | Some("js") | Some("read_thread_terminal")
                ) {
                    format!("```\n{}\n```", output.trim())
                } else {
                    output
                };
                (
                    "tool".to_owned(),
                    "工具结果".to_owned(),
                    "tool_result".to_owned(),
                    text,
                )
            }
            "custom_tool_call" => {
                let name = first_string(Some(item), &["name"]).unwrap_or_default();
                let input = first_string(Some(item), &["input"]).unwrap_or_default();
                if name == "apply_patch" {
                    (
                        "tool".to_owned(),
                        "补丁".to_owned(),
                        "patch".to_owned(),
                        format!("```diff\n{}\n```", input.trim()),
                    )
                } else {
                    (
                        "tool".to_owned(),
                        "工具调用".to_owned(),
                        "tool_call".to_owned(),
                        format_generic_call(&name, &input),
                    )
                }
            }
            "custom_tool_call_output" => {
                let raw = response_item_output_text(item);
                let text = parse_tool_output_text(&raw);
                if text.trim().is_empty() {
                    return None;
                }
                (
                    "tool".to_owned(),
                    "工具结果".to_owned(),
                    "tool_result".to_owned(),
                    text,
                )
            }
            "web_search_call" => {
                let url = item
                    .get("action")
                    .and_then(Value::as_object)
                    .and_then(|action| first_string(Some(action), &["url", "query"]));
                (
                    "tool".to_owned(),
                    "搜索".to_owned(),
                    "tool_call".to_owned(),
                    format!("🔍 {}", url.unwrap_or_else(|| "网络搜索".to_owned())),
                )
            }
            "image_generation_call" => (
                "tool".to_owned(),
                "工具调用".to_owned(),
                "tool_call".to_owned(),
                "🖼 生成图片".to_owned(),
            ),
            "tool_search_call" => (
                "tool".to_owned(),
                "工具调用".to_owned(),
                "tool_call".to_owned(),
                "🔎 检索可用工具".to_owned(),
            ),
            _ => return None,
        };

    Some(SessionMessage {
        id: first_string(Some(item), &["id"])
            .unwrap_or_else(|| format!("codex-{thread_id}-{line_index}")),
        timestamp,
        author_kind,
        author_id,
        author_label,
        kind,
        text,
        attachments: extract_attachments(&Value::Object(item.clone())),
        turn_id: first_string(Some(item), &["turn_id", "turnId"]),
        // `call_id` (when present) is the token shared between a tool call and its
        // output item, used by the UI to pair them into one bubble - each item also
        // carries its own unique `id`, which would never match across the pair.
        item_id: call_id
            .clone()
            .or_else(|| first_string(Some(item), &["id"])),
        streaming: false,
    })
}

fn parse_compacted(
    thread_id: &str,
    line_index: usize,
    payload: &Map<String, Value>,
    timestamp: String,
) -> Option<SessionMessage> {
    let summary = first_string(Some(payload), &["message"])?;
    Some(SessionMessage {
        id: format!("codex-{thread_id}-{line_index}"),
        timestamp,
        author_kind: "system".to_owned(),
        author_id: None,
        author_label: "系统".to_owned(),
        kind: "system".to_owned(),
        text: format!("上下文已压缩\n\n{summary}"),
        attachments: Vec::new(),
        turn_id: None,
        item_id: None,
        streaming: false,
    })
}

fn reasoning_summary_text(item: &Map<String, Value>) -> Option<String> {
    let summary = item.get("summary").and_then(Value::as_array)?;
    let parts: Vec<String> = summary
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|part| first_string(Some(part), &["text"]))
        .collect();
    (!parts.is_empty()).then(|| parts.join("\n\n"))
}

fn format_update_plan(arguments: &str) -> Option<String> {
    let value: Value = serde_json::from_str(arguments).ok()?;
    let obj = value.as_object()?;
    let steps = obj.get("plan").and_then(Value::as_array)?;
    let mut lines = Vec::new();
    if let Some(explanation) = first_string(Some(obj), &["explanation"]) {
        lines.push(explanation);
        lines.push(String::new());
    }
    for step in steps {
        let Some(step_obj) = step.as_object() else {
            continue;
        };
        let text = first_string(Some(step_obj), &["step"]).unwrap_or_default();
        let status = first_string(Some(step_obj), &["status"]).unwrap_or_default();
        let marker = match status.as_str() {
            "completed" | "done" => "✓",
            "in_progress" => "●",
            _ => "○",
        };
        lines.push(format!("- {marker} {text}"));
    }
    (!lines.is_empty()).then(|| lines.join("\n"))
}

fn format_shell_command_call(arguments: &str) -> String {
    let command = serde_json::from_str::<Value>(arguments)
        .ok()
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|obj| first_string(Some(obj), &["command"]))
        .unwrap_or_else(|| arguments.to_owned());
    format!("```\n$ {}\n```", command.trim())
}

fn format_generic_call(name: &str, arguments: &str) -> String {
    let summary = compact_inline(arguments, 200);
    if summary.is_empty() {
        format!("🔧 {name}")
    } else {
        format!("🔧 {name}\n\n`{summary}`")
    }
}

fn compact_inline(text: &str, max_len: usize) -> String {
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.chars().count() <= max_len {
        collapsed
    } else {
        let truncated: String = collapsed.chars().take(max_len).collect();
        format!("{truncated}…")
    }
}

fn response_item_output_text(item: &Map<String, Value>) -> String {
    match item.get("output") {
        Some(Value::String(output)) => output.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(Value::as_object)
            .filter_map(|part| first_string(Some(part), &["text", "content"]))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

fn parse_tool_output_text(raw: &str) -> String {
    let text = if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(raw) {
        first_string(Some(&obj), &["output"]).unwrap_or_else(|| raw.to_owned())
    } else {
        raw.to_owned()
    };
    strip_ansi_escape_codes(&text)
}

/// Terminal tool output (shell commands, etc.) commonly includes ANSI escape
/// sequences (color codes, cursor movement) meant for a terminal emulator;
/// the desktop UI renders plain text, so these show up as garbled control
/// characters if left in.
fn strip_ansi_escape_codes(input: &str) -> String {
    if !input.contains('\u{1b}') {
        return input.to_owned();
    }

    let mut output = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch != '\u{1b}' {
            output.push(ch);
            continue;
        }
        match chars.peek() {
            Some('[') => {
                chars.next();
                for next in chars.by_ref() {
                    if next.is_ascii_alphabetic() || next == '~' {
                        break;
                    }
                }
            }
            Some(']') => {
                chars.next();
                loop {
                    match chars.peek() {
                        Some('\u{7}') => {
                            chars.next();
                            break;
                        }
                        Some('\u{1b}') | None => break,
                        Some(_) => {
                            chars.next();
                        }
                    }
                }
            }
            Some(_) => {
                chars.next();
            }
            None => {}
        }
    }
    output
}

fn format_web_search_end(payload: &Map<String, Value>) -> String {
    let query = first_string(Some(payload), &["query"]);
    let url = payload
        .get("action")
        .and_then(Value::as_object)
        .and_then(|action| first_string(Some(action), &["url", "query"]));
    format!(
        "🔍 {}",
        query.or(url).unwrap_or_else(|| "网络搜索".to_owned())
    )
}

fn format_mcp_tool_call_end(payload: &Map<String, Value>) -> Option<String> {
    let invocation = payload.get("invocation").and_then(Value::as_object)?;
    let server = first_string(Some(invocation), &["server"]).unwrap_or_default();
    let tool = first_string(Some(invocation), &["tool"]).unwrap_or_default();
    Some(format!("🔌 {server}.{tool}"))
}

fn parse_wrapped_coworker_message(text: &str) -> Option<(String, String, String)> {
    let normalized = text.replace("\r\n", "\n");
    let wrapped = if normalized.starts_with("[协作背景]") {
        let start = normalized.find("\n[来自Coworker:")? + 1;
        &normalized[start..]
    } else {
        &normalized
    };
    let (header, body) = wrapped.split_once('\n')?;
    let header = header.trim();
    let payload = header
        .strip_prefix("[来自Coworker:")?
        .strip_suffix("]的消息:")?;
    let (coworker_id, display_name) = payload.split_once("][")?;
    Some((
        coworker_id.to_owned(),
        display_name.to_owned(),
        body.to_owned(),
    ))
}

fn response_item_is_local_input(item: &Map<String, Value>) -> bool {
    let explicit = first_string(
        Some(item),
        &[
            "role",
            "author_kind",
            "authorKind",
            "sender_kind",
            "senderKind",
        ],
    )
    .map(|value| value.to_ascii_lowercase());
    if let Some(value) = explicit {
        if matches!(value.as_str(), "user" | "local" | "human") {
            return true;
        }
        if matches!(value.as_str(), "assistant" | "codex" | "agent") {
            return false;
        }
    }

    let sender = first_string(
        Some(item),
        &[
            "sender",
            "sender_id",
            "senderId",
            "author",
            "author_id",
            "authorId",
        ],
    )
    .map(|value| value.to_ascii_lowercase());
    if let Some(value) = sender {
        if value.starts_with("local:") || matches!(value.as_str(), "user" | "local" | "human") {
            return true;
        }
        if value.starts_with("codex:") || matches!(value.as_str(), "assistant" | "codex" | "agent")
        {
            return false;
        }
    }

    response_item_content_looks_like_user_input(item)
}

fn response_item_content_looks_like_user_input(item: &Map<String, Value>) -> bool {
    let Some(content) = item.get("content").and_then(Value::as_array) else {
        return false;
    };
    let mut saw_input = false;
    for part in content {
        let Some(obj) = part.as_object() else {
            continue;
        };
        let content_type = first_string(Some(obj), &["type"])
            .unwrap_or_default()
            .to_ascii_lowercase();
        if matches!(
            content_type.as_str(),
            "output_text" | "summary_text" | "refusal" | "tool_result"
        ) {
            return false;
        }
        if matches!(
            content_type.as_str(),
            "input_text" | "input_image" | "input_audio" | "input_file"
        ) {
            saw_input = true;
        }
    }
    saw_input
}

fn response_item_text(item: &Map<String, Value>) -> String {
    if let Some(content) = item.get("content").and_then(Value::as_array) {
        let parts = content
            .iter()
            .filter_map(|part| {
                let obj = part.as_object()?;
                first_string(Some(obj), &["text", "content"]).or_else(|| {
                    if first_string(Some(obj), &["type"]).as_deref() == Some("input_image") {
                        Some("[图片附件]".to_owned())
                    } else {
                        None
                    }
                })
            })
            .collect::<Vec<_>>();
        if !parts.is_empty() {
            return parts.join("\n");
        }
    }
    if let Some(output) = first_string(Some(item), &["output", "arguments", "summary", "text"]) {
        return output;
    }
    if let Some(name) = first_string(Some(item), &["name"]) {
        return format!(
            "{name} {}",
            first_string(Some(item), &["arguments"]).unwrap_or_default()
        )
        .trim()
        .to_owned();
    }
    compact_json(item).unwrap_or_default()
}

fn extract_attachments(value: &Value) -> Vec<SessionAttachment> {
    let mut attachments = Vec::new();
    collect_attachments(value, &mut attachments);
    attachments
}

fn collect_attachments(value: &Value, attachments: &mut Vec<SessionAttachment>) {
    match value {
        Value::Object(obj) => {
            if let Some(path) = first_string(Some(obj), &["path", "saved_path", "savedPath"]) {
                let filename = first_string(Some(obj), &["filename", "name"])
                    .or_else(|| {
                        Path::new(&path)
                            .file_name()
                            .map(|value| value.to_string_lossy().into_owned())
                    })
                    .unwrap_or_else(|| "attachment".to_owned());
                let exists = Path::new(&path).exists();
                attachments.push(SessionAttachment {
                    filename,
                    media_type: first_string(Some(obj), &["media_type", "mediaType", "mime_type"])
                        .unwrap_or_else(|| guess_media_type(&path).to_owned()),
                    size: obj.get("size").and_then(Value::as_u64),
                    path: Some(path),
                    downloadable: exists,
                    reason: (!exists).then(|| "本机路径当前不可读".to_owned()),
                });
            } else if first_string(Some(obj), &["type"]).as_deref() == Some("input_image") {
                attachments.push(SessionAttachment {
                    filename: "image".to_owned(),
                    media_type: "image/*".to_owned(),
                    size: None,
                    path: None,
                    downloadable: false,
                    reason: Some("Codex JSONL 中仅保留图片内嵌数据，UI 不加载大 base64".to_owned()),
                });
            }
            for (key, child) in obj {
                if matches!(key.as_str(), "data" | "image_url" | "b64_json")
                    && child.as_str().is_some_and(|text| text.len() > 1024)
                {
                    continue;
                }
                collect_attachments(child, attachments);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_attachments(item, attachments);
            }
        }
        _ => {}
    }
}

fn find_session_file(config: &BridgeConfig, thread_id: &str) -> Result<Option<PathBuf>> {
    Ok(latest_session_file_for_thread(&collect_session_files_once(config)?, thread_id).cloned())
}

fn collect_session_files_once(config: &BridgeConfig) -> Result<Vec<PathBuf>> {
    let root = Path::new(&config.codex_home_dir);
    let mut files = Vec::new();
    for dir in [root.join("sessions"), root.join("archived_sessions")] {
        collect_session_files(&dir, &mut files)?;
    }
    files.sort_by_key(|path| fs::metadata(path).and_then(|m| m.modified()).ok());
    Ok(files)
}

fn latest_session_file_for_thread<'a>(
    files: &'a [PathBuf],
    thread_id: &str,
) -> Option<&'a PathBuf> {
    files.iter().rev().find(|path| {
        path.file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| name.contains(thread_id) && name.ends_with(".jsonl"))
    })
}

fn collect_session_files(root: &Path, matches: &mut Vec<PathBuf>) -> Result<()> {
    if !root.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            collect_session_files(&path, matches)?;
        } else if path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| name.ends_with(".jsonl"))
        {
            matches.push(path);
        }
    }
    Ok(())
}

fn overlay_path(config: &BridgeConfig, thread_id: &str) -> PathBuf {
    Path::new(&config.session_overlay_dir)
        .join(format!("{}.jsonl", sanitize_file_component(thread_id)))
}

fn first_string(mapping: Option<&Map<String, Value>>, keys: &[&str]) -> Option<String> {
    let mapping = mapping?;
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn first_value_string(mapping: Option<&Map<String, Value>>, keys: &[&str]) -> Option<String> {
    let mapping = mapping?;
    keys.iter()
        .find_map(|key| {
            mapping.get(*key).and_then(|value| match value {
                Value::String(text) => Some(text.trim().to_owned()),
                Value::Number(number) => Some(number.to_string()),
                _ => None,
            })
        })
        .filter(|value| !value.is_empty())
}

fn newest_timestamp(candidates: Vec<Option<String>>) -> String {
    candidates
        .into_iter()
        .flatten()
        .filter(|value| !value.trim().is_empty())
        .max_by_key(|value| timestamp_sort_key(value))
        .map(normalize_numeric_timestamp)
        .unwrap_or_default()
}

fn normalize_numeric_timestamp(value: String) -> String {
    let Ok(number) = value.trim().parse::<i64>() else {
        return value;
    };
    let millis = if number > 10_000_000_000 {
        number
    } else {
        number.saturating_mul(1000)
    };
    DateTime::<Utc>::from_timestamp_millis(millis)
        .map(|date| date.to_rfc3339())
        .unwrap_or(value)
}

fn session_sort_key(summary: &SessionSummary) -> i128 {
    timestamp_sort_key(&summary.last_active_at)
}

fn timestamp_sort_key(value: &str) -> i128 {
    let value = value.trim();
    if value.is_empty() {
        return 0;
    }
    if let Ok(number) = value.parse::<i128>() {
        return if number > 10_000_000_000 {
            number
        } else {
            number * 1000
        };
    }
    DateTime::parse_from_rfc3339(value)
        .map(|date| date.timestamp_millis() as i128)
        .or_else(|_| {
            DateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f %z")
                .map(|date| date.timestamp_millis() as i128)
        })
        .unwrap_or(0)
}

fn compact_json(mapping: &Map<String, Value>) -> Option<String> {
    let mut value = Value::Object(mapping.clone());
    remove_large_strings(&mut value);
    serde_json::to_string(&value)
        .ok()
        .map(|text| trim_large_text(&text))
}

fn remove_large_strings(value: &mut Value) {
    match value {
        Value::Object(obj) => {
            for (key, child) in obj {
                if matches!(key.as_str(), "data" | "image_url" | "b64_json")
                    && child.as_str().is_some_and(|text| text.len() > 1024)
                {
                    *child = Value::String("[large content omitted]".to_owned());
                } else {
                    remove_large_strings(child);
                }
            }
        }
        Value::Array(items) => {
            for child in items {
                remove_large_strings(child);
            }
        }
        Value::String(text) if text.len() > MAX_TEXT_CHARS => {
            *text = trim_large_text(text);
        }
        _ => {}
    }
}

fn trim_large_text(text: &str) -> String {
    if text.len() <= MAX_TEXT_CHARS {
        return text.to_owned();
    }
    let end = text
        .char_indices()
        .map(|(index, _)| index)
        .take_while(|index| *index <= MAX_TEXT_CHARS)
        .last()
        .unwrap_or(0);
    format!(
        "{}\n\n[内容过长，已截断 {} 字节]",
        &text[..end],
        text.len() - end
    )
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
    sanitized.trim_matches(['.', ' ']).trim().to_owned()
}

fn guess_media_type(path_or_filename: &str) -> &'static str {
    match Path::new(path_or_filename)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "jpg" | "jpeg" => "image/jpeg",
        "png" => "image/png",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        "md" => "text/markdown",
        "json" => "application/json",
        "csv" => "text/csv",
        _ => "application/octet-stream",
    }
}

fn now_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}

pub fn overlay_record(
    author_kind: &str,
    author_id: Option<String>,
    author_label: &str,
    kind: &str,
    text: String,
    attachments: Vec<SessionAttachment>,
) -> SessionOverlayRecord {
    SessionOverlayRecord {
        id: format!("overlay-{}", uuid::Uuid::new_v4()),
        timestamp: Utc::now().to_rfc3339(),
        author_kind: author_kind.to_owned(),
        author_id,
        author_label: author_label.to_owned(),
        kind: kind.to_owned(),
        text,
        attachments,
        turn_id: None,
        item_id: None,
        streaming: false,
    }
}

pub fn delta_message(
    thread_id: &str,
    event_type: &str,
    params: &Map<String, Value>,
) -> Option<SessionMessage> {
    let text = ["delta", "text", "outputDelta", "transcriptDelta", "chunk"]
        .iter()
        .find_map(|key| params.get(*key).and_then(Value::as_str))?
        .to_owned();
    if text.is_empty() {
        return None;
    }
    Some(SessionMessage {
        id: first_string(Some(params), &["itemId", "item_id"])
            .or_else(|| first_string(Some(params), &["turnId", "turn_id"]))
            .map(|id| format!("stream-{thread_id}-{id}"))
            .unwrap_or_else(|| format!("stream-{thread_id}-{}", now_millis())),
        timestamp: Utc::now().to_rfc3339(),
        author_kind: if event_type.contains("commandExecution") || event_type.contains("process") {
            "tool".to_owned()
        } else {
            "codex".to_owned()
        },
        author_id: None,
        author_label: if event_type.contains("commandExecution") || event_type.contains("process") {
            "Tool".to_owned()
        } else {
            "Codex".to_owned()
        },
        kind: if event_type.contains("plan") {
            "plan".to_owned()
        } else if event_type.contains("commandExecution") || event_type.contains("process") {
            "tool".to_owned()
        } else {
            "message".to_owned()
        },
        text,
        attachments: Vec::new(),
        turn_id: first_string(Some(params), &["turnId", "turn_id"]),
        item_id: first_string(Some(params), &["itemId", "item_id"]),
        streaming: true,
    })
}

pub fn normalized_page_size(page_size: Option<usize>) -> usize {
    page_size
        .unwrap_or(DEFAULT_PAGE_SIZE)
        .clamp(1, MAX_PAGE_SIZE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_config(root: &Path) -> BridgeConfig {
        BridgeConfig::from_value(json!({
            "codex_id": "codex-local",
            "codex_home_dir": root.join("codex").to_string_lossy(),
            "session_overlay_dir": root.join("overlay").to_string_lossy(),
            "state_path": root.join("state.json").to_string_lossy(),
            "attachment_store_dir": root.join("attachments").to_string_lossy(),
        }))
        .expect("config")
    }

    #[test]
    fn normalizes_numeric_session_timestamps_for_display() {
        assert_eq!(
            newest_timestamp(vec![Some("1784042516".to_owned())]),
            "2026-07-14T15:21:56+00:00"
        );
    }

    #[test]
    fn delta_message_preserves_stream_whitespace() {
        let params = json!({
            "threadId": "thr_1",
            "itemId": "item_1",
            "delta": " world\n"
        });
        let message = delta_message(
            "thr_1",
            "item/agentMessage/delta",
            params.as_object().expect("params"),
        )
        .expect("message");

        assert_eq!(message.text, " world\n");
    }

    #[test]
    fn paginates_jsonl_and_overlay_messages() {
        let root = std::env::temp_dir().join(format!("session-test-{}", now_millis()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        fs::write(
            Path::new(&cfg.codex_home_dir).join("session_index.jsonl"),
            r#"{"id":"thr_1","thread_name":"Test","updated_at":"2026-07-04T00:00:00Z"}"#,
        )
        .expect("index");
        fs::write(
            session_dir.join("rollout-thr_1.jsonl"),
            [
                r#"{"type":"session_meta","payload":{"thread_source":"coworker-desktop","cwd":"D:/repo","timestamp":"2026-07-04T00:00:00Z"}}"#,
                r#"{"timestamp":"2026-07-04T00:00:01Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello"}]}}"#,
                r#"{"timestamp":"2026-07-04T00:00:02Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"world"}]}}"#,
            ]
            .join("\n"),
        )
        .expect("jsonl");
        append_overlay_message(
            &cfg,
            "thr_1",
            overlay_record(
                "coworker",
                Some("cw".into()),
                "搭档",
                "message",
                "side".into(),
                Vec::new(),
            ),
        )
        .expect("overlay");

        let page = load_session_messages(&cfg, "thr_1", None, 2).expect("messages");
        assert_eq!(page.messages.len(), 2);
        assert_eq!(page.messages[0].text, "world");
        assert!(page.next_before_cursor.is_some());
        let earlier = load_session_messages(&cfg, "thr_1", page.next_before_cursor.as_deref(), 2)
            .expect("earlier");
        assert_eq!(earlier.messages[0].text, "hello");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn session_cursor_keeps_earlier_page_stable_when_jsonl_grows() {
        let root =
            std::env::temp_dir().join(format!("session-cursor-test-{}", uuid::Uuid::new_v4()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        let path = session_dir.join("rollout-thr_cursor.jsonl");
        let message = |index| {
            format!(
                r#"{{"timestamp":"2026-07-04T00:00:0{index}Z","type":"response_item","payload":{{"id":"message-{index}","type":"message","role":"assistant","content":[{{"type":"output_text","text":"message {index}"}}]}}}}"#
            )
        };
        fs::write(&path, (1..=4).map(message).collect::<Vec<_>>().join("\n")).expect("jsonl");

        let latest = load_session_messages(&cfg, "thr_cursor", None, 2).expect("latest");
        assert_eq!(
            latest
                .messages
                .iter()
                .map(|item| item.text.as_str())
                .collect::<Vec<_>>(),
            vec!["message 3", "message 4"]
        );
        writeln!(
            fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .expect("open"),
            "\n{}",
            message(5)
        )
        .expect("append");

        let earlier =
            load_session_messages(&cfg, "thr_cursor", latest.next_before_cursor.as_deref(), 2)
                .expect("earlier");
        assert_eq!(
            earlier
                .messages
                .iter()
                .map(|item| item.text.as_str())
                .collect::<Vec<_>>(),
            vec!["message 1", "message 2"]
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn reads_recent_messages_without_scanning_jsonl_from_the_start() {
        let root = std::env::temp_dir().join(format!("session-tail-test-{}", uuid::Uuid::new_v4()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        let path = session_dir.join("rollout-thr_tail.jsonl");
        let mut file = fs::File::create(&path).expect("jsonl");
        file.write_all(&vec![0xff; 400 * 1024]).expect("old prefix");
        file.write_all(b"\n").expect("prefix newline");
        for index in 0..60 {
            writeln!(
                file,
                r#"{{"timestamp":"2026-07-04T00:00:{index:02}Z","type":"response_item","payload":{{"id":"tail-{index}","type":"message","role":"assistant","content":[{{"type":"output_text","text":"tail {index}"}}]}}}}"#
            )
            .expect("message");
        }
        drop(file);

        let file_len = fs::metadata(&path).expect("metadata").len();
        let (_, has_older) =
            read_codex_messages_tail(Some(&path), "thr_tail", file_len, 20).expect("tail");
        assert!(has_older, "the tail reader should stop before byte zero");

        let page = load_session_messages(&cfg, "thr_tail", None, 10).expect("messages");
        assert_eq!(
            page.messages.first().map(|item| item.text.as_str()),
            Some("tail 50")
        );
        assert_eq!(
            page.messages.last().map(|item| item.text.as_str()),
            Some("tail 59")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn repeated_local_text_is_preserved_as_distinct_codex_turns() {
        let root =
            std::env::temp_dir().join(format!("session-repeat-test-{}", uuid::Uuid::new_v4()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        fs::write(
            session_dir.join("rollout-thr_repeat.jsonl"),
            [
                r#"{"type":"session_meta","payload":{"id":"thr_repeat","timestamp":"2026-07-04T00:00:00Z"}}"#,
                r#"{"timestamp":"2026-07-04T00:00:01Z","type":"response_item","payload":{"id":"user-1","type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}}"#,
                r#"{"timestamp":"2026-07-04T00:00:02Z","type":"response_item","payload":{"id":"user-2","type":"message","role":"user","content":[{"type":"input_text","text":"继续"}]}}"#,
            ]
            .join("\n"),
        )
        .expect("jsonl");

        let page = load_session_messages(&cfg, "thr_repeat", None, 20).expect("messages");

        assert_eq!(
            page.messages
                .iter()
                .filter(|message| message.author_kind == "local" && message.text == "继续")
                .count(),
            2
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn codex_input_overlay_pairs_once_without_hiding_local_coworker_message() {
        let root = std::env::temp_dir().join(format!(
            "session-overlay-pair-test-{}",
            uuid::Uuid::new_v4()
        ));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        fs::write(
            session_dir.join("rollout-thr_pair.jsonl"),
            [
                r#"{"type":"session_meta","payload":{"id":"thr_pair","timestamp":"2026-07-04T00:00:00Z"}}"#,
                r#"{"timestamp":"2026-07-04T00:00:01Z","type":"response_item","payload":{"id":"user-1","type":"message","role":"user","content":[{"type":"input_text","text":"same"}]}}"#,
            ]
            .join("\n"),
        )
        .expect("jsonl");
        append_overlay_message(
            &cfg,
            "thr_pair",
            overlay_record(
                "local",
                Some("desktop-local".into()),
                "本机",
                "message",
                "same".into(),
                Vec::new(),
            ),
        )
        .expect("codex input overlay");
        append_overlay_message(
            &cfg,
            "thr_pair",
            overlay_record(
                "local",
                Some("cw-1".into()),
                "本机 → 搭档",
                "message",
                "same".into(),
                Vec::new(),
            ),
        )
        .expect("coworker output overlay");

        let page = load_session_messages(&cfg, "thr_pair", None, 20).expect("messages");

        assert_eq!(page.messages.len(), 2);
        assert!(
            page.messages
                .iter()
                .all(|message| message.id.starts_with("overlay-"))
        );
        assert!(
            page.messages
                .iter()
                .any(|message| message.author_id.as_deref() == Some("cw-1"))
        );
        assert!(
            page.messages
                .iter()
                .any(|message| message.author_id.as_deref() == Some("desktop-local"))
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_sessions_sorts_by_parsed_latest_activity_including_overlay() {
        let root = std::env::temp_dir().join(format!("session-sort-test-{}", now_millis()));
        let cfg = test_config(&root);
        fs::create_dir_all(&cfg.codex_home_dir).expect("codex home");
        fs::write(
            Path::new(&cfg.codex_home_dir).join("session_index.jsonl"),
            [
                r#"{"id":"old_index_new_overlay","thread_name":"Overlay","updated_at":"2026-07-01T00:00:00Z"}"#,
                r#"{"id":"new_index","thread_name":"Index","updated_at":"2026-07-03T00:00:00Z"}"#,
            ]
            .join("\n"),
        )
        .expect("index");
        let mut record =
            overlay_record("local", None, "本机", "message", "later".into(), Vec::new());
        record.timestamp = "2026-07-04T00:00:00Z".into();
        append_overlay_message(&cfg, "old_index_new_overlay", record).expect("overlay");

        let sessions =
            list_sessions(&cfg, &[], RuntimeSessionState::default(), 10).expect("sessions");

        assert_eq!(sessions[0].thread_id, "old_index_new_overlay");
        assert_eq!(sessions[1].thread_id, "new_index");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_sessions_includes_bridge_owned_thread_missing_from_app_threads_and_index() {
        let root =
            std::env::temp_dir().join(format!("session-owned-fallback-test-{}", now_millis()));
        let cfg = test_config(&root);
        fs::create_dir_all(&cfg.codex_home_dir).expect("codex home");

        let mut runtime = RuntimeSessionState::default();
        runtime
            .owned_thread_ids
            .insert("bridge_only_thread".to_owned());

        // Neither `thread/list` (app_threads) nor the on-disk session index
        // knows about this thread, but the bridge started it this run.
        let sessions = list_sessions(&cfg, &[], runtime, 10).expect("sessions");

        assert!(
            sessions
                .iter()
                .any(|session| session.thread_id == "bridge_only_thread"),
            "bridge-owned thread should still appear in the sidebar list"
        );
        assert!(
            sessions
                .iter()
                .find(|session| session.thread_id == "bridge_only_thread")
                .unwrap()
                .owned_by_bridge
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_sessions_recovers_bridge_thread_from_rollout_metadata() {
        let root =
            std::env::temp_dir().join(format!("session-rollout-recovery-test-{}", now_millis()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions");
        fs::create_dir_all(&session_dir).expect("session dir");
        fs::write(
            session_dir.join("rollout-thr_recovered.jsonl"),
            r#"{"type":"session_meta","payload":{"id":"thr_recovered","thread_source":"coworker-codex-bridge","cwd":"C:/Users/test/Documents/Codex/ui-1","timestamp":"2026-07-13T00:00:00Z"}}"#,
        )
        .expect("session metadata");

        let sessions = list_sessions(&cfg, &[], RuntimeSessionState::default(), 10)
            .expect("recovered sessions");

        let recovered = sessions
            .iter()
            .find(|session| session.thread_id == "thr_recovered")
            .expect("bridge thread recovered without app-server or state");
        assert!(recovered.owned_by_bridge);
        assert!(recovered.can_continue);
        assert_eq!(
            recovered.project_path.as_deref(),
            Some("C:/Users/test/Documents/Codex/ui-1")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_thread_summary_reads_project_path_aliases() {
        let root = std::env::temp_dir().join(format!("session-project-test-{}", now_millis()));
        let cfg = test_config(&root);
        let sessions = list_sessions(
            &cfg,
            &[json!({
                "id": "thr_project",
                "name": "Project thread",
                "updatedAt": "2026-07-04T00:00:00Z",
                "worktreePath": "D:\\Projects\\real-app"
            })],
            RuntimeSessionState::default(),
            10,
        )
        .expect("sessions");

        assert_eq!(
            sessions[0].project_path.as_deref(),
            Some("D:\\Projects\\real-app")
        );
        assert_eq!(sessions[0].project_name.as_deref(), Some("real-app"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_thread_rollout_path_does_not_become_project_path() {
        let root = std::env::temp_dir().join(format!("session-rollout-path-test-{}", now_millis()));
        let cfg = test_config(&root);
        let session_dir = Path::new(&cfg.codex_home_dir).join("sessions/2026/07/10");
        fs::create_dir_all(&session_dir).expect("session dir");
        let rollout = session_dir.join("rollout-2026-07-10-thr_project.jsonl");
        fs::write(
            &rollout,
            r#"{"type":"session_meta","payload":{"id":"thr_project","cwd":"D:\\Projects\\coworker","timestamp":"2026-07-10T00:00:00Z"}}"#,
        )
        .expect("session metadata");

        let sessions = list_sessions(
            &cfg,
            &[json!({
                "id": "thr_project",
                "name": "Project thread",
                "path": rollout.to_string_lossy()
            })],
            RuntimeSessionState::default(),
            10,
        )
        .expect("sessions");

        assert_eq!(
            sessions[0].project_path.as_deref(),
            Some("D:\\Projects\\coworker")
        );
        assert_eq!(sessions[0].project_id, sessions[0].project_path);
        assert_eq!(sessions[0].project_name.as_deref(), Some("coworker"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn large_inline_image_becomes_attachment_metadata() {
        let value = json!({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "x".repeat(4096)}]
            }
        });
        let msg = parse_codex_message("thr", 0, &value, &mut HashMap::new()).expect("message");
        assert_eq!(msg.text, "[图片附件]");
        assert_eq!(msg.attachments[0].downloadable, false);
    }

    #[test]
    fn response_item_input_text_without_role_is_local_message() {
        let value = json!({
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": [{"type": "input_text", "text": "用户补充答案"}]
            }
        });
        let msg = parse_codex_message("thr", 0, &value, &mut HashMap::new()).expect("message");

        assert_eq!(msg.author_kind, "local");
        assert_eq!(msg.author_label, "本机");
        assert_eq!(msg.text, "用户补充答案");
    }

    #[test]
    fn task_complete_without_agent_message_is_visible() {
        let value = json!({
            "timestamp": "2026-07-13T08:45:15Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn_1",
                "last_agent_message": Value::Null
            }
        });
        let msg = parse_codex_message("thr", 0, &value, &mut HashMap::new()).expect("message");

        assert_eq!(msg.author_kind, "system");
        assert!(msg.text.contains("没有返回任何消息"));
        assert_eq!(msg.turn_id.as_deref(), Some("turn_1"));
    }

    #[test]
    fn plan_completion_does_not_add_an_empty_response_warning() {
        let plan = json!({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn_1",
                "item": {"type": "Plan", "text": "实施计划"}
            }
        });
        let complete = json!({
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn_1",
                "last_agent_message": Value::Null
            }
        });
        let mut call_names = HashMap::new();
        let records = vec![
            (
                0,
                plan.clone(),
                parse_codex_message("thr", 0, &plan, &mut call_names),
            ),
            (
                1,
                complete.clone(),
                parse_codex_message("thr", 1, &complete, &mut call_names),
            ),
        ];

        let messages = finalize_tail_messages("thr", records);

        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].kind, "plan");
        assert_eq!(messages[0].text, "实施计划");
    }

    #[test]
    fn wrapped_coworker_message_is_rendered_as_coworker_input() {
        let value = json!({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "[来自Coworker:cw_02][搭档B]的消息:\n请帮我看看这个报错"
                }]
            }
        });
        let msg = parse_codex_message("thr", 0, &value, &mut HashMap::new()).expect("message");

        assert_eq!(msg.author_kind, "coworker");
        assert_eq!(msg.author_id.as_deref(), Some("cw_02"));
        assert_eq!(msg.author_label, "搭档B");
        assert_eq!(msg.text, "请帮我看看这个报错");
    }

    #[test]
    fn bootstrapped_coworker_message_is_rendered_as_coworker_input() {
        let value = json!({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "[协作背景]\n首次接管会话时注入的协议说明。\n\n[来自Coworker:cw_02][搭档B]的消息:\n请继续处理"
                }]
            }
        });
        let msg = parse_codex_message("thr", 0, &value, &mut HashMap::new()).expect("message");

        assert_eq!(msg.author_kind, "coworker");
        assert_eq!(msg.author_id.as_deref(), Some("cw_02"));
        assert_eq!(msg.author_label, "搭档B");
        assert_eq!(msg.text, "请继续处理");
    }

    #[test]
    fn developer_role_message_is_skipped() {
        let value = json!({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<permissions instructions>..."}]
            }
        });
        assert!(parse_codex_message("thr", 0, &value, &mut HashMap::new()).is_none());
    }

    #[test]
    fn session_meta_cwd_is_parsed_from_real_envelope_shape() {
        // Regression test: real Codex rollout files put "type" on the outer
        // envelope, not inside "payload" - the previous parser looked for
        // "type" inside payload and so never matched session_meta lines.
        let value = json!({
            "timestamp": "2026-04-21T08:51:28.977Z",
            "type": "session_meta",
            "payload": {
                "id": "019daf3c-c643-7d90-93f5-c85f400ce587",
                "cwd": "D:\\Projects\\work\\fineinsight_test",
                "originator": "codex-tui",
                "source": "cli"
            }
        });
        let meta = parse_session_meta(&value).expect("meta");
        assert_eq!(
            meta.cwd.as_deref(),
            Some("D:\\Projects\\work\\fineinsight_test")
        );
        assert_eq!(meta.source.as_deref(), Some("cli"));
    }

    #[test]
    fn shell_command_call_and_output_pair_via_call_id() {
        let mut call_names = HashMap::new();
        let call = json!({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": "{\"command\":\"git status\",\"workdir\":\"D:\\\\repo\"}",
                "call_id": "call_1"
            }
        });
        let call_msg = parse_codex_message("thr", 0, &call, &mut call_names).expect("call message");
        assert_eq!(call_msg.kind, "tool_call");
        assert!(call_msg.text.contains("git status"));

        let output = json!({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Exit code: 0\nOutput:\nnothing to commit"
            }
        });
        let output_msg =
            parse_codex_message("thr", 1, &output, &mut call_names).expect("output message");
        assert_eq!(output_msg.kind, "tool_result");
        assert!(output_msg.text.contains("nothing to commit"));
        assert_eq!(call_msg.item_id, output_msg.item_id);
    }

    #[test]
    fn call_and_output_item_id_pairs_via_call_id_even_when_each_has_its_own_distinct_id() {
        // Regression test: real Codex response items each carry their own unique
        // "id" (distinct between the call and its output) in addition to the
        // shared "call_id" - item_id must key off call_id so the desktop UI can
        // pair the two into a single bubble; keying off "id" would never match.
        let mut call_names = HashMap::new();
        let call = json!({
            "type": "response_item",
            "payload": {
                "id": "item_call_own_id",
                "type": "function_call",
                "name": "shell_command",
                "arguments": "{\"command\":\"git status\"}",
                "call_id": "call_42"
            }
        });
        let call_msg = parse_codex_message("thr", 0, &call, &mut call_names).expect("call message");

        let output = json!({
            "type": "response_item",
            "payload": {
                "id": "item_output_own_id",
                "type": "function_call_output",
                "call_id": "call_42",
                "output": "ok"
            }
        });
        let output_msg =
            parse_codex_message("thr", 1, &output, &mut call_names).expect("output message");

        assert_eq!(call_msg.item_id.as_deref(), Some("call_42"));
        assert_eq!(output_msg.item_id.as_deref(), Some("call_42"));
        assert_eq!(call_msg.item_id, output_msg.item_id);
        // "id" is still used for React keys / dedup and must stay distinct.
        assert_ne!(call_msg.id, output_msg.id);
    }

    #[test]
    fn content_block_tool_outputs_render_and_pair_via_call_id() {
        let mut call_names = HashMap::new();
        let call = json!({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "echo hello",
                "call_id": "call_blocks"
            }
        });
        let call_msg = parse_codex_message("thr", 0, &call, &mut call_names).expect("call message");

        let output = json!({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_blocks",
                "output": [
                    {"type": "input_text", "text": "first line"},
                    {"type": "input_text", "text": "second line"}
                ]
            }
        });
        let output_msg =
            parse_codex_message("thr", 1, &output, &mut call_names).expect("output message");

        assert_eq!(output_msg.kind, "tool_result");
        assert_eq!(output_msg.text, "first line\nsecond line");
        assert_eq!(call_msg.item_id, output_msg.item_id);
    }

    #[test]
    fn update_plan_call_renders_as_checklist_and_output_is_suppressed() {
        let mut call_names = HashMap::new();
        let call = json!({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "update_plan",
                "arguments": "{\"plan\":[{\"step\":\"read code\",\"status\":\"completed\"},{\"step\":\"write fix\",\"status\":\"pending\"}]}",
                "call_id": "call_plan"
            }
        });
        let msg = parse_codex_message("thr", 0, &call, &mut call_names).expect("plan message");
        assert_eq!(msg.kind, "plan");
        assert!(msg.text.contains("✓ read code"));
        assert!(msg.text.contains("○ write fix"));

        let output = json!({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_plan",
                "output": "Plan updated"
            }
        });
        assert!(parse_codex_message("thr", 1, &output, &mut call_names).is_none());
    }

    #[test]
    fn reasoning_with_empty_summary_is_skipped_but_text_is_rendered() {
        let empty = json!({
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [],
                "content": Value::Null,
                "encrypted_content": "gAAAAA..."
            }
        });
        assert!(parse_codex_message("thr", 0, &empty, &mut HashMap::new()).is_none());

        let with_summary = json!({
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "**Inspecting**\n\nLooking at the code."}]
            }
        });
        let msg =
            parse_codex_message("thr", 1, &with_summary, &mut HashMap::new()).expect("reasoning");
        assert_eq!(msg.kind, "reasoning");
        assert_eq!(msg.text, "**Inspecting**\n\nLooking at the code.");
    }

    #[test]
    fn trim_large_text_preserves_utf8_boundaries() {
        let text = "该".repeat(MAX_TEXT_CHARS);
        let trimmed = trim_large_text(&text);

        assert!(trimmed.starts_with('该'));
        assert!(trimmed.contains("内容过长"));
    }
}
