import { Channel, invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow, type DragDropEvent } from "@tauri-apps/api/window";
import { startDrag } from "@crabnebula/tauri-plugin-drag";
import fileDragIcon from "../src-tauri/icons/32x32.png";

const DEFAULT_LOG_MAX_BYTES = 512 * 1024;

export type RuntimeState = "stopped" | "running" | "exited";

export function listenDesktopFileDrops(handler: (event: DragDropEvent) => void): Promise<UnlistenFn> {
  return getCurrentWindow().onDragDropEvent(({ payload }) => handler(payload));
}

export function startDesktopFileDrag(path: string): Promise<void> {
  return startDrag({ item: [path], icon: fileDragIcon, mode: "copy" });
}

export type BridgeCoworker = {
  coworker_id: string;
  display_name: string;
  base_url: string;
  bearer_token?: string;
  enabled?: boolean;
};

export type PermissionsMode = "read-only" | "workspace-write" | "danger-full-access";
export type ApprovalsReviewer = "none" | "coworker";
export type LogOutputLevel = "ERROR" | "WARN" | "INFO" | "DEBUG" | "TRACE";

export type BridgeStatus = {
  state: RuntimeState;
  config_path: string | null;
  codex_id: string | null;
  desktop_id?: string | null;
  coworkers: BridgeCoworker[];
  actors?: ActorHealth[];
  development_mode?: boolean;
  last_error: string | null;
};

export type DesktopActorId = "local" | "codex" | "claude";

export type DesktopApproval = {
  request_id: string;
  actor_id: DesktopActorId;
  conversation_id: string;
  coworker_id: string;
  owner_id: string;
  tool_name: string;
  input: unknown;
  status: string;
  response?: Record<string, unknown> | null;
  expires_at: string;
  server_request_id?: string | null;
};

/** Result of resolving an approval from the Desktop UI. */
export type ResolveApprovalResult = {
  ok: boolean;
  reason?: string | null;
};

/** Event emitted when an approval's status changes (resolved, expired, etc.). */
export type ApprovalChangedEvent = {
  type: "desktop.approval.changed";
  request_id: string;
  actor_id: string;
  status: "resolved" | "expired";
  resolver: "desktop" | "coworker" | "timeout";
};

export type ActorHealth = {
  actor_id: DesktopActorId;
  available: boolean;
  message: string;
};

export type ActorConversation = {
  actor_id: DesktopActorId;
  conversation_id: string;
  title: string;
  project_path: string | null;
  writable: boolean;
  updated_at: string | null;
  mode: "default" | "plan" | string | null;
};

export type ActorMessage = {
  id: string;
  actor_id: DesktopActorId;
  conversation_id: string;
  author_kind: string;
  content: string;
  created_at: string;
  metadata: Record<string, unknown> | null;
};

export type ActorMessagePage = {
  messages: ActorMessage[];
  next_before_cursor: string | null;
};

export type DiagnosticResult = {
  name: string;
  ok: boolean;
  message: string;
};

export type ConfigValue = {
  codex_id?: string;
  display_name?: string;
  command?: string;
  args?: string[];
  chat_workspaces_dir?: string;
  desktop_update_url?: string;
  logs_dir?: string;
  log_level?: LogOutputLevel;
  file_log_level?: LogOutputLevel;
  permissions_mode?: PermissionsMode;
  approvals_reviewer?: ApprovalsReviewer;
  approval_timeout_seconds?: number;
  close_to_tray?: boolean;
  coworkers?: BridgeCoworker[];
  [key: string]: unknown;
};

export type ConfigInfo = {
  config: ConfigValue;
  exists: boolean;
  modified_ms: number | null;
};

export type BridgeLogChunk = {
  path: string;
  text: string;
  reset: boolean;
};

export type DesktopUpdateInfo = {
  version: string;
  currentVersion: string;
};

export type DesktopUpdateDownloadEvent =
  | { event: "Started"; data: { contentLength: number | null } }
  | { event: "Progress"; data: { chunkLength: number } }
  | { event: "Finished" };

export type CommunicateRegistration = {
  registration_id: string;
  participant_id: string;
  kind: string;
  client_id: string;
  display_name: string;
  active: boolean;
  created_at: string;
  last_registered_at: string;
  metadata: Record<string, unknown>;
};

export type ActorStreamEvent = {
  actor_id: DesktopActorId;
  conversation_id: string;
  message_id: string | null;
  event: Record<string, unknown>;
};

export function getConfig(path?: string): Promise<ConfigValue> {
  return invoke("get_config", { path });
}

export function getConfigInfo(path?: string): Promise<ConfigInfo> {
  return invoke("get_config_info", { path });
}

export function saveConfig(path: string | undefined, config: ConfigValue): Promise<ConfigInfo> {
  return invoke("save_config", { path, config });
}

export function startBridge(path?: string): Promise<BridgeStatus> {
  return invoke("start_bridge", { path });
}

export function stopBridge(): Promise<BridgeStatus> {
  return invoke("stop_bridge");
}

export function getBridgeStatus(): Promise<BridgeStatus> {
  return invoke("get_bridge_status");
}

export function setTrayCopy(copy: {
  tooltip: string;
  open: string;
  hide: string;
  quit: string;
}): Promise<void> {
  return invoke("set_tray_copy", copy);
}

export function setCloseToTray(enabled: boolean): Promise<void> {
  return invoke("set_close_to_tray", { enabled });
}

export function listDesktopConversations(
  actorId: DesktopActorId,
  path?: string,
  limit = 1000,
): Promise<ActorConversation[]> {
  return invoke("list_desktop_conversations", { actorId, path, limit });
}

export function sendDesktopMessage(
  actorId: DesktopActorId,
  coworkerId: string | null,
  conversationId: string | null,
  content: string,
  projectPath?: string | null,
  mode?: string | null,
  attachmentPaths: string[] = [],
): Promise<Record<string, unknown>> {
  return invoke("send_desktop_message", {
    actorId,
    coworkerId,
    conversationId,
    content,
    projectPath,
    mode,
    attachmentPaths,
  });
}

export function loadDesktopMessages(
  path: string | undefined,
  actorId: DesktopActorId,
  conversationId: string,
  beforeCursor?: string | null,
  pageSize = 80,
): Promise<ActorMessagePage> {
  return invoke("load_desktop_messages", { path, actorId, conversationId, beforeCursor, pageSize });
}

export function setDesktopConversationMode(
  actorId: DesktopActorId,
  conversationId: string,
  mode: string,
): Promise<Record<string, unknown>> {
  return invoke("set_desktop_conversation_mode", { actorId, conversationId, mode });
}

export function renameDesktopConversation(
  actorId: DesktopActorId,
  conversationId: string,
  title: string,
): Promise<Record<string, unknown>> {
  return invoke("rename_desktop_conversation", { actorId, conversationId, title });
}

export function sendDesktopCoworkerMessage(
  actorId: DesktopActorId,
  coworkerId: string,
  conversationId: string | null,
  content: string,
  attachmentPaths: string[],
): Promise<Record<string, unknown>> {
  return invoke("send_desktop_coworker_message", { actorId, coworkerId, conversationId, content, attachmentPaths });
}

export function listDesktopApprovals(): Promise<DesktopApproval[]> {
  return invoke("list_desktop_approvals");
}

export function resolveDesktopApproval(
  approval: DesktopApproval,
  response: { behavior: "allow" | "deny"; updatedInput?: unknown; message?: string },
): Promise<ResolveApprovalResult> {
  return invoke("resolve_desktop_approval", {
    requestId: approval.request_id,
    actorId: approval.actor_id,
    conversationId: approval.conversation_id,
    coworkerId: approval.coworker_id,
    response,
  });
}

export function copyDesktopAttachment(sourcePath: string, destinationPath: string): Promise<Record<string, unknown>> {
  return invoke("copy_desktop_attachment", { sourcePath, destinationPath });
}

export function listenActorStreamEvents(handler: (event: ActorStreamEvent) => void): Promise<UnlistenFn> {
  return listen<ActorStreamEvent>("actor-stream-event", (event) => handler(event.payload));
}

export function readBridgeLog(path?: string, maxBytes = DEFAULT_LOG_MAX_BYTES): Promise<string> {
  return invoke("read_bridge_log", { path, maxBytes });
}

export function startBridgeLogStream(path?: string, maxBytes = DEFAULT_LOG_MAX_BYTES): Promise<void> {
  return invoke("start_bridge_log_stream", { path, maxBytes });
}

export function stopBridgeLogStream(): Promise<void> {
  return invoke("stop_bridge_log_stream");
}

export function listenBridgeLogChunks(handler: (chunk: BridgeLogChunk) => void): Promise<UnlistenFn> {
  return listen<BridgeLogChunk>("bridge-log-chunk", (event) => handler(event.payload));
}

export function runDiagnostics(path?: string): Promise<DiagnosticResult[]> {
  return invoke("run_diagnostics", { path });
}

export function listCommunicateRegistrations(baseUrl: string, bearerToken?: string): Promise<CommunicateRegistration[]> {
  return invoke("list_communicate_registrations", { baseUrl, bearerToken });
}

export function deleteCommunicateRegistration(baseUrl: string, registrationId: string, bearerToken?: string): Promise<CommunicateRegistration> {
  return invoke("delete_communicate_registration", { baseUrl, registrationId, bearerToken });
}

export function checkDesktopUpdate(endpoint?: string): Promise<DesktopUpdateInfo | null> {
  return invoke("check_desktop_update", { endpoint });
}

export function getDefaultDesktopUpdateUrl(): Promise<string> {
  return invoke("get_default_desktop_update_url");
}

export function installDesktopUpdate(onEvent: (event: DesktopUpdateDownloadEvent) => void): Promise<void> {
  const channel = new Channel<DesktopUpdateDownloadEvent>();
  channel.onmessage = onEvent;
  return invoke("install_desktop_update", { onEvent: channel });
}
