import { Bot, Laptop, TerminalSquare } from "lucide-react";
import { useI18n } from "../i18n";
import type { DictKey } from "../i18n/en";
import type { BubbleTimelineMeta, TimelineAttachment, TimelineMessage } from "../lib/bridgeLogic";
import type { ActorHealth, ActorMessage, DesktopActorId } from "../tauri";

function toolValueText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const text = value.flatMap((part) => {
      if (typeof part === "string") return [part];
      if (!part || typeof part !== "object") return [];
      const record = part as Record<string, unknown>;
      return typeof record.text === "string" ? [record.text] : [JSON.stringify(record, null, 2)];
    }).join("\n");
    if (text) return text;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function fencedToolValue(value: unknown, language = "text"): string {
  const text = toolValueText(value).trim();
  if (!text) return "";
  const fence = text.includes("```") ? "````" : "```";
  return `${fence}${language}\n${text}\n${fence}`;
}

function readBubbleMetadata(value: unknown): BubbleTimelineMeta | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const bubble = value as Record<string, unknown>;
  const id = typeof bubble.id === "string" ? bubble.id.trim() : "";
  const kind = bubble.kind === "handoff" || bubble.kind === "reply" ? bubble.kind : null;
  if (!id || !kind) return null;
  const phase = bubble.phase === "start" || bubble.phase === "end" ? bubble.phase : null;
  return { id, kind, phase, resumed: bubble.resumed === true };
}

function bubbleMessageText(
  bubble: BubbleTimelineMeta,
  content: string,
  t: (key: DictKey, vars?: Record<string, string | number>) => string,
): string {
  if (bubble.kind === "reply") return content;
  if (bubble.phase === "end") return t("actors.bubbleHandoffEnd", { id: bubble.id });
  return t(
    bubble.resumed ? "actors.bubbleHandoffResume" : "actors.bubbleHandoffStart",
    { id: bubble.id },
  );
}

export function isTimelineNearBottom(
  timeline: Pick<HTMLDivElement, "scrollHeight" | "scrollTop" | "clientHeight">,
  threshold = 72,
): boolean {
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= threshold;
}

export function normalizeActorMessageAuthorKind(authorKind: string, actor: DesktopActorId): string {
  if (authorKind === "local") return "local";
  if (authorKind === "assistant" || authorKind === actor) return "codex";
  return authorKind;
}

export function actorMessagesToTimelineMessages(
  messages: ActorMessage[],
  actor: DesktopActorId,
  t: (key: DictKey, vars?: Record<string, string | number>) => string,
): TimelineMessage[] {
  const actorLabel = actor === "local"
    ? t("actors.local")
    : actor === "codex"
      ? t("actors.codex")
      : t("actors.claude");
  return messages.flatMap((message) => {
    const bubble = readBubbleMetadata(message.metadata?.bubble);
    const metadataKind = String(message.metadata?.kind ?? "text");
    const toolName = typeof message.metadata?.tool_name === "string" ? message.metadata.tool_name : null;
    const toolItemId = typeof message.metadata?.tool_use_id === "string"
      ? message.metadata.tool_use_id
      : typeof message.metadata?.item_id === "string"
        ? message.metadata.item_id
        : message.id;
    if (message.author_kind === "tool" && (metadataKind === "tool" || toolName)) {
      const input = message.metadata?.input;
      const output = message.metadata?.output;
      const hasResult = output !== null && output !== undefined;
      const isError = message.metadata?.is_error === true;
      const call: TimelineMessage = {
        id: `${message.id}:call`,
        timestamp: message.created_at,
        author_kind: "tool",
        author_id: null,
        author_label: t("actors.tool", { tool: toolName ?? "Tool" }),
        kind: "tool_call",
        text: fencedToolValue(input, "json") || message.content,
        attachments: [],
        turn_id: typeof message.metadata?.turn_id === "string" ? message.metadata.turn_id : null,
        item_id: toolItemId,
        streaming: !hasResult,
        tool_name: toolName,
        is_error: false,
      };
      if (!hasResult) return [call];
      return [call, {
        id: typeof message.metadata?.result_id === "string" ? message.metadata.result_id : `${message.id}:result`,
        timestamp: typeof message.metadata?.result_created_at === "string" ? message.metadata.result_created_at : message.created_at,
        author_kind: "tool",
        author_id: null,
        author_label: isError ? t("actors.toolError") : t("actors.toolResult"),
        kind: "tool_result",
        text: fencedToolValue(output),
        attachments: [],
        turn_id: call.turn_id,
        item_id: toolItemId,
        streaming: false,
        tool_name: toolName,
        is_error: isError,
      } satisfies TimelineMessage];
    }
    const kind = bubble?.kind === "handoff"
      ? "bubble_handoff"
      : bubble?.kind === "reply"
        ? "bubble_reply"
        : message.author_kind === "tool" && metadataKind === "text"
          ? "tool_result"
          : metadataKind;
    return [{
      id: message.id,
      timestamp: message.created_at,
      author_kind: bubble ? "bubble" : normalizeActorMessageAuthorKind(message.author_kind, actor),
      author_id: typeof message.metadata?.author_id === "string" ? message.metadata.author_id : null,
      author_label: bubble
        ? t("actors.bubble")
        : message.author_kind === "system"
          ? t("actors.system")
        : typeof message.metadata?.author_label === "string"
        ? message.metadata.author_label
        : message.author_kind === "local"
        ? t("actors.you")
        : message.author_kind === "coworker"
          ? t("actors.coworker")
          : message.author_kind === "tool"
            ? t("actors.tool", { tool: String(message.metadata?.tool_name ?? "Tool") })
            : kind === "reasoning"
              ? t("actors.reasoning")
              : actorLabel,
      kind,
      text: bubble ? bubbleMessageText(bubble, message.content, t) : message.content,
      attachments: Array.isArray(message.metadata?.attachments)
        ? message.metadata.attachments.flatMap((item): TimelineAttachment[] => {
            if (!item || typeof item !== "object") return [];
            const value = item as Record<string, unknown>;
            const path = typeof value.path === "string" ? value.path : typeof value.saved_path === "string" ? value.saved_path : null;
            return [{
              filename: String(value.filename ?? "attachment"),
              media_type: String(value.media_type ?? "application/octet-stream"),
              size: typeof value.size === "number" ? value.size : null,
              path,
              downloadable: Boolean(path),
              reason: path ? null : "Attachment path unavailable",
            }];
          })
        : [],
      turn_id: typeof message.metadata?.turn_id === "string" ? message.metadata.turn_id : null,
      item_id: typeof message.metadata?.item_id === "string" ? message.metadata.item_id : null,
      streaming: message.metadata?.streaming === true,
      tool_name: toolName,
      is_error: message.metadata?.is_error === true,
      bubble,
    }];
  });
}

export function ActorRail({
  actor,
  health,
  onChange,
}: {
  actor: DesktopActorId;
  health: ActorHealth[];
  onChange: (actor: DesktopActorId) => void;
}) {
  const { t } = useI18n();
  const actorLabels: Record<DesktopActorId, string> = {
    local: t("actors.local"),
    codex: t("actors.codex"),
    claude: t("actors.claude"),
  };
  const icons = { local: Laptop, codex: TerminalSquare, claude: Bot };
  return (
    <nav className="actorRail" aria-label={t("actors.identityNav")}>
      {(["local", "codex", "claude"] as DesktopActorId[]).map((id) => {
        const Icon = icons[id];
        const state = health.find((item) => item.actor_id === id);
        const available = state?.available === true;
        return (
          <button
            aria-label={actorLabels[id]}
            aria-pressed={actor === id}
            className={actor === id ? `actorRailItem actor-${id} active` : `actorRailItem actor-${id}`}
            key={id}
            onClick={() => onChange(id)}
            title={state?.message ?? actorLabels[id]}
            type="button"
          >
            <Icon size={17} />
            <span>{actorLabels[id]}</span>
            <i className={available ? "actorAvailability available" : "actorAvailability"} />
          </button>
        );
      })}
    </nav>
  );
}
