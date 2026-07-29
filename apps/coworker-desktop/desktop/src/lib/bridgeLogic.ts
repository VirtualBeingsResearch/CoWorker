import type { DictKey } from "../i18n/en";
import type {
  BridgeCoworker,
  BridgeStatus,
  ConfigValue,
  DiagnosticResult,
  PermissionsMode,
  ApprovalsReviewer,
  ActorMessage,
  ActorStreamEvent,
} from "../tauri";

export type TimelineAttachment = {
  filename: string;
  media_type: string;
  size: number | null;
  path: string | null;
  downloadable: boolean;
  reason: string | null;
};

export type BubbleTimelineMeta = {
  id: string;
  kind: "handoff" | "reply";
  phase: "start" | "end" | null;
  resumed: boolean;
};

export type TimelineMessage = {
  id: string;
  timestamp: string;
  author_kind: string;
  author_id: string | null;
  author_label: string;
  kind: string;
  text: string;
  attachments: TimelineAttachment[];
  turn_id: string | null;
  item_id: string | null;
  streaming: boolean;
  tool_name?: string | null;
  is_error?: boolean;
  bubble?: BubbleTimelineMeta | null;
};

export type View = "status" | "config" | "sessions" | "logs";
export type RuntimeMood = "loading" | "running" | "stopped" | "exited" | "error";
export type OverviewReadiness = "loading" | "ready" | "partial" | "stopped" | "exited" | "failed";
export type DesktopUpdateState = "idle" | "checking" | "available" | "downloading" | "installed" | "error";
export type FeedbackTone = "success" | "info" | "warning" | "error";

export type ValidationIssue = {
  path: string;
  key: DictKey;
  vars?: Record<string, string | number>;
};

export type ToastNotification = {
  id: number;
  tone: FeedbackTone;
  text: string;
};

export type InlineNotice = {
  tone: Exclude<FeedbackTone, "success">;
  text: string;
};

export type LogEntryViewModel = {
  id: string;
  time: string;
  timeDate: string;
  timeClock: string;
  level: "error" | "warn" | "info" | "debug" | "trace" | "raw";
  target: string;
  source: string;
  message: string;
  text: string;
};

export type LogLevel = LogEntryViewModel["level"];
export type LogLevelFilter = "all" | LogLevel;

export type ApprovalConfigView = {
  permissionsMode: PermissionsMode;
  approvalsReviewer: ApprovalsReviewer;
  approvalTimeoutSeconds: number;
};

export const logLevelValues: LogLevelFilter[] = ["all", "error", "warn", "info", "debug", "trace", "raw"];

export function isRelayBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      /^\/i\/cw_[A-Za-z0-9_-]{8,80}$/.test(url.pathname)
    );
  } catch {
    return false;
  }
}
export const logLevels: LogLevel[] = ["error", "warn", "info", "debug", "trace", "raw"];
export const permissionsModeValues: PermissionsMode[] = ["read-only", "workspace-write", "danger-full-access"];
export const approvalsReviewerValues: ApprovalsReviewer[] = ["none", "coworker"];

export const maxLogTextChars = 512 * 1024;
export const maxParsedLogEntries = 900;
export const maxRenderedLogEntries = 220;
export const toastDurationMs = 3200;
export const sessionListPageSize = 25;
export const localSessionListPageSize = 120;
export const sessionMessagePageSize = 50;
export const conversationTitleMaxChars = 60;

export function conversationTitleFromMessage(content: string, fallback: string): string {
  const collapsed = content.trim().split(/\s+/u).filter(Boolean).join(" ");
  const characters = Array.from(collapsed);
  if (!characters.length) return fallback;
  if (characters.length <= conversationTitleMaxChars) return collapsed;
  return `${characters.slice(0, conversationTitleMaxChars - 1).join("")}…`;
}

const localLogDateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "2-digit",
  day: "2-digit",
});
const localLogTimeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export const moodColor: Record<RuntimeMood, string> = {
  loading: "#6e6e73",
  running: "#16794f",
  stopped: "#71717a",
  exited: "#9a6700",
  error: "#b42318",
};

export function normalizeCoworkers(config: ConfigValue): BridgeCoworker[] {
  if (!Array.isArray(config.coworkers)) return [];
  return config.coworkers.map((coworker, index) => {
    const coworkerId = coworker.coworker_id || `cw_${String(index + 1).padStart(2, "0")}`;
    return {
      ...coworker,
      coworker_id: coworkerId,
      display_name: coworker.display_name || coworkerId,
      base_url: coworker.base_url || "",
      enabled: coworker.enabled !== false,
    };
  });
}

export function enabledCoworkers(coworkers: BridgeCoworker[]): BridgeCoworker[] {
  return coworkers.filter((coworker) => coworker.enabled !== false);
}

export function resolvedApprovalConfig(config: ConfigValue): ApprovalConfigView {
  return {
    permissionsMode: normalizePermissionsMode(config.permissions_mode, "read-only"),
    approvalsReviewer: normalizeApprovalsReviewer(config.approvals_reviewer, "none"),
    approvalTimeoutSeconds: normalizeTimeoutSeconds(config.approval_timeout_seconds),
  };
}

export function normalizePermissionsMode(value: unknown, fallback: PermissionsMode): PermissionsMode {
  return value === "read-only" || value === "workspace-write" || value === "danger-full-access" ? value : fallback;
}

export function normalizeApprovalsReviewer(value: unknown, fallback: ApprovalsReviewer): ApprovalsReviewer {
  return value === "none" || value === "coworker" ? value : fallback;
}

export function normalizeTimeoutSeconds(value: unknown): number {
  const numberValue = typeof value === "number" ? value : typeof value === "string" ? Number(value) : 300;
  return Number.isFinite(numberValue) && numberValue >= 0 ? Math.floor(numberValue) : 300;
}

export function runtimeMood(status: BridgeStatus | null): RuntimeMood {
  if (!status) return "loading";
  if (status.last_error) return "error";
  return status.state;
}

export function overviewReadiness(status: BridgeStatus | null): OverviewReadiness {
  if (!status) return "loading";
  if (status.last_error) return "failed";
  if (status.state === "exited") return "exited";
  if (status.state === "stopped") return "stopped";
  if (status.actors?.some((actor) => !actor.available)) return "partial";
  return "ready";
}

export function validateConfig(config: ConfigValue): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const coworkers = normalizeCoworkers(config);
  const seen = new Map<string, number>();

  if (!String(config.codex_id ?? "").trim()) {
    issues.push({ path: "codex_id", key: "validation.codexIdRequired" });
  }

  const desktopUpdateUrl = String(config.desktop_update_url ?? "").trim();
  if (desktopUpdateUrl && !/^https?:\/\//i.test(desktopUpdateUrl)) {
    issues.push({ path: "desktop_update_url", key: "validation.updateUrlFormat" });
  }

  if (coworkers.length === 0) {
    issues.push({ path: "coworkers", key: "validation.atLeastOneCoworker" });
  } else if (enabledCoworkers(coworkers).length === 0) {
    issues.push({ path: "coworkers", key: "validation.atLeastOneEnabledCoworker" });
  }

  coworkers.forEach((coworker, index) => {
    const id = coworker.coworker_id.trim();
    if (!id) {
      issues.push({ path: `coworkers.${index}.coworker_id`, key: "validation.coworkerIdRequired" });
    } else if (seen.has(id)) {
      issues.push({ path: `coworkers.${index}.coworker_id`, key: "validation.coworkerIdUnique" });
      issues.push({ path: `coworkers.${seen.get(id)}.coworker_id`, key: "validation.coworkerIdUnique" });
    } else {
      seen.set(id, index);
    }

    if (!coworker.base_url.trim()) {
      issues.push({ path: `coworkers.${index}.base_url`, key: "validation.baseUrlRequired" });
    }
  });

  return issues;
}

export function classifyDesktopUpdateErrorKey(error: unknown, endpoint: string): DictKey {
  const raw = String(error instanceof Error ? error.message : error).trim();
  const subscription = endpoint.trim();
  if (subscription.includes("coworker.example.com") || raw.includes("coworker.example.com")) {
    return "update.error.exampleUrl";
  }
  if (/builder error|relative url without a base|invalid url/i.test(raw)) {
    return "update.error.badFormat";
  }
  return "update.error.generic";
}

export function nextCoworker(coworkers: BridgeCoworker[], nameForIndex: (index: number) => string): BridgeCoworker {
  let index = coworkers.length + 1;
  let coworkerId = `cw_${String(index).padStart(2, "0")}`;
  const ids = new Set(coworkers.map((coworker) => coworker.coworker_id));
  while (ids.has(coworkerId)) {
    index += 1;
    coworkerId = `cw_${String(index).padStart(2, "0")}`;
  }
  return {
    coworker_id: coworkerId,
    display_name: nameForIndex(index),
    base_url: `http://localhost:${7999 + index}`,
    enabled: true,
  };
}

export function diagnosticsForCoworker(diagnostics: DiagnosticResult[], coworker: BridgeCoworker) {
  const expectedNames = new Set([
    `Coworker ${coworker.display_name}`,
    `Coworker ${coworker.coworker_id}`,
  ]);
  return diagnostics.find((item) => expectedNames.has(item.name));
}

function parseLogTimestamp(value: string) {
  const normalized = value.trim().replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
  if (!/^\d{4}-\d{2}-\d{2}T/.test(normalized)) return null;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatLogTime(value: string) {
  const parsed = parseLogTimestamp(value);
  if (parsed) {
    return {
      date: localLogDateFormatter.format(parsed),
      clock: localLogTimeFormatter.format(parsed),
    };
  }

  const normalized = value.replace(" ", "T");
  const date = normalized.match(/^\d{4}-\d{2}-\d{2}/)?.[0] ?? "";
  const clock = normalized.match(/T?(\d{2}:\d{2}:\d{2})(?:\.\d+)?/)?.[1] ?? "";
  return {
    date: date ? date.slice(5) : "",
    clock: clock || value,
  };
}

function cleanLogText(line: string, time: string, level: LogEntryViewModel["level"]) {
  let text = line;
  if (time) {
    text = text.replace(time, "").trim();
  }
  if (level !== "raw") {
    text = text.replace(new RegExp(`^${level === "warn" ? "(WARN|WARNING)" : level.toUpperCase()}\\b`, "i"), "").trim();
  }
  return text || line;
}

function splitLogMessage(text: string) {
  const sanitizedText = sanitizeLogMessage(text);
  const match = sanitizedText.match(/^([a-zA-Z0-9_.:-]+):\s+(.+)$/);
  if (match) {
    const source = match[1];
    const message = match[2];
    const looksLikeTechnicalSource = source.includes("::") || source.includes(".") || source.includes("_");
    return looksLikeTechnicalSource ? { source, message } : { source: "", message: sanitizedText };
  }

  const spacedMatch = sanitizedText.match(/^([a-zA-Z0-9_.:-]+)\s+(.+)$/);
  if (spacedMatch) {
    const source = spacedMatch[1];
    const message = spacedMatch[2];
    const looksLikeTechnicalSource = source.includes("::") || source.includes(".") || source.includes("_");
    if (looksLikeTechnicalSource) {
      return { source, message };
    }
  }

  return { source: "", message: sanitizedText };
}

function sanitizeLogMessage(text: string) {
  return text
    .replace(/("signature"\s*:\s*)String\("[^"]*"\)/g, '$1String("<redacted>")')
    .replace(/\b(signature:\s*)[A-Za-z0-9+/=_-]{32,}/g, "$1<redacted>");
}

export function clampLogText(text: string) {
  if (text.length <= maxLogTextChars) return text;
  const clipped = text.slice(text.length - maxLogTextChars);
  const firstLineBreak = clipped.indexOf("\n");
  return firstLineBreak >= 0 ? clipped.slice(firstLineBreak + 1) : clipped;
}

export function parseLog(log: string, coworkers: BridgeCoworker[]): LogEntryViewModel[] {
  const lines = log
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const entryOffset = Math.max(0, lines.length - maxParsedLogEntries);

  return lines
    .slice(entryOffset)
    .map((line, index) => {
      const levelMatch = line.match(/\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE)\b/i);
      const rawLevel = levelMatch?.[1]?.toLowerCase() ?? "raw";
      const level = rawLevel === "warning" ? "warn" : (rawLevel as LogEntryViewModel["level"]);
      const time =
        line.match(/\b\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]+\b/)?.[0] ??
        line.match(/\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b/)?.[0] ??
        "";
      const formattedTime = formatLogTime(time);
      const text = cleanLogText(line, time, level);
      const { source, message } = splitLogMessage(text);
      const target =
        coworkers.find(
          (coworker) =>
            line.includes(coworker.coworker_id) ||
            line.includes(coworker.display_name) ||
            line.includes(coworker.base_url),
        )?.display_name ?? "Bridge";
      return {
        id: `${entryOffset + index}-${line.slice(0, 20)}`,
        time,
        timeDate: formattedTime.date,
        timeClock: formattedTime.clock,
        level,
        target,
        source,
        message,
        text,
      };
    });
}

export function mergeMessages(messages: TimelineMessage[]) {
  const byId = new Map<string, TimelineMessage>();
  const order: string[] = [];
  messages.forEach((message) => {
    const key = message.id || `${message.turn_id ?? ""}:${message.item_id ?? ""}:${message.kind}`;
    const existing = byId.get(key);
    if (!existing) {
      byId.set(key, message);
      order.push(key);
      return;
    }
    byId.set(key, {
      ...existing,
      ...message,
      text: message.streaming ? `${existing.text}${message.text}` : message.text || existing.text,
      attachments: message.attachments.length ? message.attachments : existing.attachments,
      streaming: message.streaming,
    });
  });
  return order.map((key) => byId.get(key) as TimelineMessage);
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/** Applies Claude's nested stream-json event to the ephemeral desktop timeline. */
export function applyActorStreamEvent(messages: ActorMessage[], update: ActorStreamEvent): ActorMessage[] {
  const outer = objectValue(update.event);
  if (!outer) return messages;
  const streamEvent = outer.type === "stream_event" ? objectValue(outer.event) : null;
  const eventType = String(streamEvent?.type ?? outer.type ?? "");
  const messageId = update.message_id ?? `claude-${update.conversation_id}`;
  const blockIndex = Number(streamEvent?.index ?? -1);
  const delta = objectValue(streamEvent?.delta);
  const block = objectValue(streamEvent?.content_block);
  const now = new Date().toISOString();
  const messageKey = (kind: string) => `${messageId}:${blockIndex}:${kind}`;
  const upsert = (next: ActorMessage) => {
    const index = messages.findIndex((item) => item.id === next.id);
    if (index < 0) return [...messages, next];
    const copy = [...messages];
    const current = copy[index];
    copy[index] = {
      ...current,
      ...next,
      content: next.metadata?.streaming === true ? `${current.content}${next.content}` : next.content || current.content,
      metadata: { ...(current.metadata ?? {}), ...(next.metadata ?? {}) },
    };
    return copy;
  };
  const streamedMessage = (id: string, content: string, kind: string, streaming: boolean, metadata: Record<string, unknown> = {}): ActorMessage => ({
    id,
    actor_id: update.actor_id,
    conversation_id: update.conversation_id,
    author_kind: kind === "tool" ? "tool" : "assistant",
    content,
    created_at: now,
    metadata: {
      source: "claude-stream",
      kind,
      streaming,
      stream_message_id: messageId,
      block_index: blockIndex,
      ...metadata,
    },
  });

  if (eventType === "message_delta") {
    const message = objectValue(outer.message);
    if (!message) return messages;
    return upsert(message as unknown as ActorMessage);
  }

  if (eventType === "content_block_start" && block) {
    const blockType = String(block.type ?? "");
    if (blockType === "text") {
      return upsert(streamedMessage(messageKey("text"), String(block.text ?? ""), "text", true));
    }
    if (blockType === "thinking") {
      return upsert(streamedMessage(messageKey("reasoning"), String(block.thinking ?? ""), "reasoning", true));
    }
    if (blockType === "tool_use") {
      return upsert(streamedMessage(messageKey("tool"), "", "tool", true, {
        tool_use_id: String(block.id ?? messageKey("tool")),
        tool_name: String(block.name ?? "tool"),
        input: block.input ?? {},
      }));
    }
  }
  if (eventType === "content_block_delta" && delta) {
    const deltaType = String(delta.type ?? "");
    if (deltaType === "text_delta") {
      return upsert(streamedMessage(messageKey("text"), String(delta.text ?? ""), "text", true));
    }
    if (deltaType === "thinking_delta") {
      return upsert(streamedMessage(messageKey("reasoning"), String(delta.thinking ?? ""), "reasoning", true));
    }
    if (deltaType === "input_json_delta") {
      const id = messageKey("tool");
      const partial = String(delta.partial_json ?? "");
      const existing = messages.find((item) => item.id === id);
      const previous = String(existing?.metadata?.partial_input ?? "");
      const combined = `${previous}${partial}`;
      let input: unknown = combined;
      try {
        input = JSON.parse(combined);
      } catch {
        // Partial tool input is intentionally allowed to be invalid JSON.
      }
      return upsert(streamedMessage(id, "", "tool", true, {
        tool_use_id: String(existing?.metadata?.tool_use_id ?? id),
        tool_name: String(existing?.metadata?.tool_name ?? "tool"),
        partial_input: combined,
        input,
      }));
    }
  }
  if (eventType === "content_block_stop") {
    return messages.map((message) => {
      if (message.metadata?.stream_message_id !== messageId || message.metadata?.block_index !== blockIndex) return message;
      return { ...message, metadata: { ...(message.metadata ?? {}), streaming: false } };
    });
  }
  if (eventType === "message_stop" || eventType === "result") {
    return messages.map((message) => message.metadata?.stream_message_id === messageId
      ? { ...message, metadata: { ...(message.metadata ?? {}), streaming: false } }
      : message);
  }
  return messages;
}

export type TimelineMessageGroup = {
  message: TimelineMessage;
  result: TimelineMessage | null;
};

export function groupToolMessages(messages: TimelineMessage[]): TimelineMessageGroup[] {
  const groups: TimelineMessageGroup[] = [];
  const openCallIndex = new Map<string, number>();

  messages.forEach((message) => {
    if (message.kind === "tool_result" && message.item_id && openCallIndex.has(message.item_id)) {
      const index = openCallIndex.get(message.item_id) as number;
      groups[index] = { ...groups[index], result: message };
      openCallIndex.delete(message.item_id);
      return;
    }
    groups.push({ message, result: null });
    if ((message.kind === "tool_call" || message.kind === "patch") && message.item_id) {
      openCallIndex.set(message.item_id, groups.length - 1);
    }
  });

  return groups;
}

export function formatSessionTime(value: string) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function fileName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}
