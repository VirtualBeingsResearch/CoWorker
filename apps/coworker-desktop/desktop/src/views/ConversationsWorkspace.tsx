import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";
import {
  applyActorStreamEvent,
  conversationTitleFromMessage,
  groupToolMessages,
  mergeMessages,
  sessionListLimit,
  sessionMessagePageSize,
  type BubbleTimelineMeta,
  type FeedbackTone,
  type InlineNotice,
  type TimelineAttachment,
  type TimelineMessage,
} from "../lib/bridgeLogic";
import {
  copyDesktopAttachment,
  listDesktopConversations,
  listenDesktopFileDrops,
  listenActorStreamEvents,
  loadDesktopMessages,
  renameDesktopConversation,
  sendDesktopCoworkerMessage,
  sendDesktopMessage,
  setDesktopConversationMode,
  startDesktopFileDrag,
  type ActorConversation,
  type ActorMessage,
  type ActorStreamEvent,
  type BridgeCoworker,
  type BridgeStatus,
  type DesktopActorId,
} from "../tauri";
import { actorMessagesToTimelineMessages, ActorRail, isTimelineNearBottom } from "./ActorConversationParts";
import { SessionsView, type SessionModeOption, type SessionsPresentation } from "./SessionsView";

type ComposerMode = "default" | "acceptEdits" | "plan" | "bypassPermissions";

type CachedConversation = {
  messages: TimelineMessage[];
  cursor: string | null;
  composerMode: ComposerMode;
  composerText: string;
  attachments: string[];
  rawActorMessages: ActorMessage[];
  scrollTop: number;
  followLatest: boolean;
};

type Feedback = {
  error: (error: unknown) => void;
  notice: (text: string, tone?: InlineNotice["tone"]) => void;
  toast: (text: string, tone?: FeedbackTone) => void;
};

type WorkspaceProps = {
  active: boolean;
  configPath: string;
  status: BridgeStatus | null;
  coworkers: BridgeCoworker[];
  selectedCoworkerId: string;
  onSelectCoworker: (coworkerId: string) => void;
  feedback: Feedback;
  onActiveConversationChange: (activeConversation: {
    actorId: DesktopActorId;
    conversationId: string;
  } | null) => void;
};

function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

function normalizeMode(value: string | null | undefined): ComposerMode {
  return value === "acceptEdits" || value === "plan" || value === "bypassPermissions" ? value : "default";
}

function selectedPaths(value: string | string[] | null): string[] {
  return Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
}

async function downloadAttachment(
  attachment: TimelineAttachment,
  title: string,
  unavailableText: string,
  savedText: string,
  feedback: Feedback,
) {
  if (!attachment.path || !attachment.downloadable) {
    feedback.notice(attachment.reason ?? unavailableText, "warning");
    return;
  }
  const destination = await saveDialog({ defaultPath: attachment.filename, title });
  if (!destination) return;
  await copyDesktopAttachment(attachment.path, destination);
  feedback.toast(savedText, "success");
}

function ConversationController({
  actor,
  active,
  configPath,
  running,
  coworkers,
  selectedCoworkerId,
  onSelectCoworker,
  feedback,
  onActiveConversationChange,
}: Omit<WorkspaceProps, "status"> & { actor: DesktopActorId; running: boolean }) {
  const { t } = useI18n();
  const actorLabel = actor === "codex" ? t("actors.codex") : actor === "claude" ? t("actors.claude") : t("actors.local");
  const [sessions, setSessions] = useState<ActorConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [messages, setMessages] = useState<TimelineMessage[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [composerText, setComposerText] = useState("");
  const [composerMode, setComposerMode] = useState<ComposerMode>("default");
  const [draftProjectPath, setDraftProjectPath] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const [attachmentsDragging, setAttachmentsDragging] = useState(false);
  const [coworkerPickerOpen, setCoworkerPickerOpen] = useState(false);
  const timelineRef = useRef<HTMLDivElement | null>(null);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const rawActorMessagesRef = useRef<ActorMessage[]>([]);
  const selectedIdRef = useLatest(selectedId);
  const sessionsRef = useLatest(sessions);
  const activeRef = useLatest(active);
  const composerTextRef = useLatest(composerText);
  const composerModeRef = useLatest(composerMode);
  const attachmentsRef = useLatest(attachments);
  const listRequestRef = useRef(0);
  const messagesRequestRef = useRef(0);
  const sessionsLoadedRef = useRef(false);
  const conversationCacheRef = useRef(new Map<string, CachedConversation>());
  const displayedConversationRef = useRef<string | null>(null);
  const sendingRef = useRef(false);
  const loadingEarlierRef = useRef(false);
  const followLatestRef = useRef(true);
  const scrollTopRef = useRef(0);
  const pendingScrollTopRef = useRef<number | null>(null);
  const restoringConversationRef = useRef(false);
  const sendContextRef = useRef<{ sourceId: string; title: string; projectPath: string | null; startedAt: string } | null>(null);

  const selected = sessions.find((session) => session.conversation_id === selectedId) ?? null;
  const canUseComposer = running && (!selected || selected.writable);
  const sessionMode = selected?.mode ?? composerMode;

  useEffect(() => {
    if (!active) return;
    onActiveConversationChange(selectedId
      ? { actorId: actor, conversationId: selectedId }
      : null);
  }, [active, actor, onActiveConversationChange, selectedId]);

  function conversationKey(conversationId: string) {
    return `${configPath}\0${actor}\0${conversationId}`;
  }

  function cacheCurrentConversation() {
    const conversationId = selectedIdRef.current;
    const key = conversationId ? conversationKey(conversationId) : null;
    if (!key) return;
    conversationCacheRef.current.set(key, {
      messages,
      cursor,
      composerMode,
      composerText,
      attachments,
      rawActorMessages: rawActorMessagesRef.current,
      scrollTop: timelineRef.current?.scrollTop ?? scrollTopRef.current,
      followLatest: followLatestRef.current,
    });
  }

  function restoreConversation(conversationId: string) {
    const key = conversationKey(conversationId);
    const cached = conversationCacheRef.current.get(key);
    if (!cached) return false;
    displayedConversationRef.current = key;
    rawActorMessagesRef.current = cached.rawActorMessages;
    scrollTopRef.current = cached.scrollTop;
    pendingScrollTopRef.current = cached.scrollTop;
    followLatestRef.current = cached.followLatest;
    restoringConversationRef.current = true;
    setMessages(cached.messages);
    setCursor(cached.cursor);
    setComposerMode(cached.composerMode);
    setComposerText(cached.composerText);
    setAttachments(cached.attachments);
    return true;
  }

  const refreshSessions = useCallback(async (preserve?: ActorConversation) => {
    const requestId = ++listRequestRef.current;
    setListLoading(true);
    try {
      const listed = await listDesktopConversations(actor, configPath, sessionListLimit);
      if (requestId !== listRequestRef.current) return;
      const next = preserve
        ? listed.some((session) => session.conversation_id === preserve.conversation_id)
          ? listed.map((session) => session.conversation_id === preserve.conversation_id
            && (!session.title.trim() || session.title === "未命名会话")
            ? { ...session, title: preserve.title }
            : session)
          : [preserve, ...listed]
        : listed;
      sessionsLoadedRef.current = true;
      setSessions(next);
      setSelectedId((current) => {
        const nextId = current && next.some((session) => session.conversation_id === current) ? current : next[0]?.conversation_id ?? "";
        selectedIdRef.current = nextId;
        return nextId;
      });
    } finally {
      if (requestId === listRequestRef.current) setListLoading(false);
    }
  }, [actor, configPath, selectedIdRef]);

  const loadConversation = useCallback(async (conversationId: string, showLoading = true, allowWhileSending = false) => {
    if (!conversationId || (sendingRef.current && !allowWhileSending)) return;
    const requestId = ++messagesRequestRef.current;
    if (showLoading) setMessagesLoading(true);
    try {
      const page = await loadDesktopMessages(configPath, actor, conversationId, null, sessionMessagePageSize);
      if (requestId !== messagesRequestRef.current || selectedIdRef.current !== conversationId) return;
      rawActorMessagesRef.current = page.messages;
      const nextMessages = actorMessagesToTimelineMessages(page.messages, actor, t);
      const nextMode = normalizeMode(sessionsRef.current.find((session) => session.conversation_id === conversationId)?.mode);
      conversationCacheRef.current.set(conversationKey(conversationId), {
        messages: nextMessages,
        cursor: page.next_before_cursor,
        composerMode: nextMode,
        composerText: composerTextRef.current,
        attachments: attachmentsRef.current,
        rawActorMessages: rawActorMessagesRef.current,
        scrollTop: scrollTopRef.current,
        followLatest: followLatestRef.current,
      });
      setMessages(nextMessages);
      setCursor(page.next_before_cursor);
      setComposerMode(nextMode);
    } finally {
      if (showLoading && requestId === messagesRequestRef.current) setMessagesLoading(false);
    }
  }, [actor, configPath, selectedIdRef, sessionsRef, t]);

  const actorEventHandler = useLatest((update: ActorStreamEvent) => {
    if (update.actor_id !== actor) return;
    if (!update.conversation_id) {
      void refreshSessions().catch(feedback.error);
      return;
    }
    if (update.event.type === "conversation_updated") {
      void refreshSessions().catch(feedback.error);
      if (selectedIdRef.current === update.conversation_id) {
        void loadConversation(update.conversation_id, false).catch(feedback.error);
      } else {
        conversationCacheRef.current.delete(conversationKey(update.conversation_id));
      }
      return;
    }
    const currentId = selectedIdRef.current;
    const context = sendContextRef.current;
    if (currentId && currentId !== update.conversation_id) return;
    if (!currentId && (!context || context.sourceId)) return;
    if (!currentId && context) {
      displayedConversationRef.current = conversationKey(update.conversation_id);
      selectedIdRef.current = update.conversation_id;
      setSelectedId(update.conversation_id);
      rawActorMessagesRef.current = rawActorMessagesRef.current.map((message) => ({ ...message, conversation_id: update.conversation_id }));
      const optimistic: ActorConversation = {
        actor_id: actor,
        conversation_id: update.conversation_id,
        title: context.title,
        project_path: context.projectPath,
        writable: true,
        updated_at: context.startedAt,
        mode: composerMode,
      };
      setSessions((current) => [optimistic, ...current.filter((session) => session.conversation_id !== update.conversation_id)]);
    }
    rawActorMessagesRef.current = applyActorStreamEvent(rawActorMessagesRef.current, update);
    setMessages(actorMessagesToTimelineMessages(rawActorMessagesRef.current, actor, t));
  });

  useEffect(() => {
    sessionsLoadedRef.current = false;
    conversationCacheRef.current.clear();
    displayedConversationRef.current = null;
  }, [actor, configPath]);

  useEffect(() => {
    if (!active) return;
    void refreshSessions().catch(feedback.error);
  }, [active, refreshSessions, feedback.error]);

  useEffect(() => {
    const loadKey = selectedId ? conversationKey(selectedId) : null;
    if (!active || !selectedId || displayedConversationRef.current === loadKey) return;
    if (restoreConversation(selectedId)) return;
    displayedConversationRef.current = loadKey;
    messagesRequestRef.current += 1;
    rawActorMessagesRef.current = [];
    setMessages([]);
    setCursor(null);
    scrollTopRef.current = 0;
    pendingScrollTopRef.current = 0;
    followLatestRef.current = true;
    void loadConversation(selectedId).catch((error) => {
      if (displayedConversationRef.current === loadKey) displayedConversationRef.current = null;
      feedback.error(error);
    });
  }, [active, selectedId, actor, configPath]);

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listenActorStreamEvents((event) => actorEventHandler.current(event))
      .then((item) => disposed ? item() : (unlisten = item))
      .catch(feedback.error);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [active, actorEventHandler, feedback.error]);

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listenDesktopFileDrops((event) => {
      if (event.type === "enter" || event.type === "over") {
        setAttachmentsDragging(canUseComposer && !sending);
      } else {
        setAttachmentsDragging(false);
        if (event.type === "drop" && canUseComposer && !sending) {
          setAttachments((current) => [...new Set([...current, ...event.paths])]);
        }
      }
    }).then((item) => disposed ? item() : (unlisten = item)).catch(feedback.error);
    return () => {
      disposed = true;
      setAttachmentsDragging(false);
      unlisten?.();
    };
  }, [active, canUseComposer, sending, feedback.error]);

  useEffect(() => {
    if (!editingTitle) {
      setTitleDraft(selected?.title ?? "");
      return;
    }
    titleInputRef.current?.focus();
    titleInputRef.current?.select();
  }, [editingTitle, selected?.conversation_id, selected?.title]);

  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline) return;
    const trackScroll = () => {
      if (pendingScrollTopRef.current !== null) return;
      scrollTopRef.current = timeline.scrollTop;
      followLatestRef.current = isTimelineNearBottom(timeline);
    };
    timeline.addEventListener("scroll", trackScroll, { passive: true });
    return () => timeline.removeEventListener("scroll", trackScroll);
  }, [selectedId]);

  useEffect(() => {
    if (!active || !timelineRef.current) return;
    const scrollTop = pendingScrollTopRef.current ?? scrollTopRef.current;
    const frame = window.requestAnimationFrame(() => {
      if (timelineRef.current) timelineRef.current.scrollTop = scrollTop;
      scrollTopRef.current = scrollTop;
      pendingScrollTopRef.current = null;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [active, selectedId]);

  useEffect(() => {
    if (restoringConversationRef.current) {
      restoringConversationRef.current = false;
      return;
    }
    if (!active || !followLatestRef.current || !timelineRef.current || messagesLoading) return;
    const frame = window.requestAnimationFrame(() => {
      if (timelineRef.current) timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, messagesLoading, selectedId]);

  useEffect(() => {
    if (!coworkerPickerOpen) return;
    const close = (event: KeyboardEvent) => event.key === "Escape" && setCoworkerPickerOpen(false);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [coworkerPickerOpen]);

  function selectConversation(conversationId: string) {
    if (conversationId === selectedIdRef.current) return;
    cacheCurrentConversation();
    messagesRequestRef.current += 1;
    selectedIdRef.current = conversationId;
    setSelectedId(conversationId);
    if (!restoreConversation(conversationId)) {
      displayedConversationRef.current = null;
      rawActorMessagesRef.current = [];
      scrollTopRef.current = 0;
      pendingScrollTopRef.current = 0;
      followLatestRef.current = true;
      setMessages([]);
      setCursor(null);
      setComposerText("");
      setAttachments([]);
      setComposerMode(normalizeMode(sessions.find((session) => session.conversation_id === conversationId)?.mode));
    }
    setEditingTitle(false);
    setCoworkerPickerOpen(false);
  }

  function newConversation() {
    cacheCurrentConversation();
    messagesRequestRef.current += 1;
    selectedIdRef.current = "";
    rawActorMessagesRef.current = [];
    setSelectedId("");
    setMessages([]);
    setCursor(null);
    setComposerText("");
    setAttachments([]);
    setEditingTitle(false);
    setComposerMode("default");
    setDraftProjectPath(actor === "claude"
      ? selected?.project_path ?? sessions.find((session) => session.project_path)?.project_path ?? ""
      : "");
  }

  async function loadEarlier() {
    if (!selectedId || !cursor || messagesLoading || loadingEarlierRef.current) return;
    const requestId = ++messagesRequestRef.current;
    const node = timelineRef.current;
    const previousHeight = node?.scrollHeight ?? 0;
    loadingEarlierRef.current = true;
    followLatestRef.current = false;
    setMessagesLoading(true);
    try {
      const page = await loadDesktopMessages(configPath, actor, selectedId, cursor, sessionMessagePageSize);
      if (requestId !== messagesRequestRef.current || selectedIdRef.current !== selectedId) return;
      rawActorMessagesRef.current = [...page.messages, ...rawActorMessagesRef.current];
      setMessages(mergeMessages(actorMessagesToTimelineMessages(rawActorMessagesRef.current, actor, t)));
      setCursor(page.next_before_cursor);
      window.requestAnimationFrame(() => {
        if (node) node.scrollTop = node.scrollHeight - previousHeight + node.scrollTop;
      });
    } finally {
      loadingEarlierRef.current = false;
      if (requestId === messagesRequestRef.current) setMessagesLoading(false);
    }
  }

  async function submit(coworkerId?: string | null, sendToCoworker = false) {
    const content = composerText.trim();
    if ((!content && attachments.length === 0) || sendingRef.current) return;
    if (!canUseComposer && selectedId) {
      feedback.notice(selected?.writable ? t("sessions.notice.bridgeNotRunningToast") : t("sessions.notice.notBridgeOwnedToast"), "warning");
      return;
    }
    if (sendToCoworker && actor === "codex" && !selectedId) {
      feedback.notice(t("sessions.notice.selectSessionFirst"), "warning");
      return;
    }
    if (sendToCoworker && !coworkerId && coworkers.length > 1) {
      setCoworkerPickerOpen(true);
      return;
    }

    const sourceId = selectedIdRef.current;
    const sentAttachments = attachments;
    const projectPath = !sourceId && actor !== "local" ? draftProjectPath.trim() || null : null;
    const startedAt = new Date().toISOString();
    const title = conversationTitleFromMessage(content, actor === "codex" ? t("sessions.newBridgeSessionDraftTitle") : t("actors.newConversation"));
    const optimisticId = `desktop-local-${Date.now()}`;
    sendingRef.current = true;
    sendContextRef.current = { sourceId, title, projectPath, startedAt };
    setSending(true);
    setComposerText("");
    setAttachments([]);
    rawActorMessagesRef.current = [...rawActorMessagesRef.current, {
        id: optimisticId,
        actor_id: actor,
        conversation_id: sourceId,
        author_kind: "local",
        content,
        created_at: startedAt,
        metadata: {
          source: "desktop-optimistic",
          kind: "text",
          attachments: sentAttachments.map((path) => ({ filename: path.split(/[\\/]/).pop() ?? "attachment", path })),
        },
      }];
    setMessages(actorMessagesToTimelineMessages(rawActorMessagesRef.current, actor, t));
    try {
      const targetCoworkerId = coworkerId || selectedCoworkerId || coworkers[0]?.coworker_id || null;
      const result = sendToCoworker
        ? await sendDesktopCoworkerMessage(actor, targetCoworkerId ?? "", sourceId || null, content, sentAttachments)
        : await sendDesktopMessage(
            actor,
            actor === "local" && !sourceId ? targetCoworkerId : null,
            sourceId || null,
            content,
            projectPath,
            actor === "local" ? null : composerMode,
            sentAttachments,
          );
      const conversationId = String(result.conversation_id ?? result.thread_id ?? sourceId ?? "");
      if (!activeRef.current) return;
      setCoworkerPickerOpen(false);
      if (sendToCoworker && sourceId) {
        await loadConversation(sourceId, true, true);
        feedback.toast(t("sessions.toast.sentToCoworker"), "success");
        return;
      }
      if (!conversationId) {
        await refreshSessions();
        return;
      }
      if (!sourceId) {
        displayedConversationRef.current = conversationKey(conversationId);
        rawActorMessagesRef.current = rawActorMessagesRef.current.map((message) => ({ ...message, conversation_id: conversationId }));
      }
      selectedIdRef.current = conversationId;
      setSelectedId(conversationId);
      const optimistic = !sourceId ? {
        actor_id: actor,
        conversation_id: conversationId,
        title,
        project_path: projectPath,
        writable: true,
        updated_at: startedAt,
        mode: actor === "local" ? null : composerMode,
      } satisfies ActorConversation : undefined;
      if (optimistic) setSessions((current) => [optimistic, ...current.filter((session) => session.conversation_id !== conversationId)]);
      await refreshSessions(optimistic);
      if (sendToCoworker) feedback.toast(t("sessions.toast.sentToCoworker"), "success");
    } catch (error) {
      setComposerText((current) => current || content);
      setAttachments((current) => current.length ? current : sentAttachments);
      rawActorMessagesRef.current = rawActorMessagesRef.current.filter((message) => message.id !== optimisticId);
      setMessages(actorMessagesToTimelineMessages(rawActorMessagesRef.current, actor, t));
      throw error;
    } finally {
      sendContextRef.current = null;
      sendingRef.current = false;
      setSending(false);
    }
  }

  async function saveTitle() {
    if (!selected) return;
    const title = titleDraft.trim();
    if (!title || title === selected.title) {
      setEditingTitle(false);
      return;
    }
    setRenaming(true);
    try {
      await renameDesktopConversation(actor, selected.conversation_id, title);
      const renamed = { ...selected, title };
      setSessions((current) => [renamed, ...current.filter((session) => session.conversation_id !== selected.conversation_id)]);
      setEditingTitle(false);
      await refreshSessions(renamed);
      feedback.toast(t("sessions.toast.titleUpdated"), "success");
    } finally {
      setRenaming(false);
    }
  }

  async function changeMode(mode: ComposerMode) {
    setComposerMode(mode);
    if (!selectedId || !running || actor === "local") return;
    await setDesktopConversationMode(actor, selectedId, actor === "codex" && mode !== "plan" ? "default" : mode);
    await refreshSessions();
    if (actor === "codex") feedback.toast(t("sessions.toast.modeWillApply", { mode }), "success");
  }

  const presentation: SessionsPresentation = {
    ariaLabel: `${actorLabel} conversations`,
    sidebarEyebrow: "CoWorker Desktop",
    sidebarTitle: t("actors.conversationsTitle", { actor: actorLabel }),
    refreshLabel: t("actors.refreshConversations"),
    newSessionLabel: t("actors.newConversation"),
    noSessionsLabel: t("actors.noConversations", { actor: actorLabel }),
    draftTitle: t("actors.newConversation"),
    ...(actor === "codex" ? {} : {
      newSessionSubtitle: t("actors.chatTitle", { actor: actorLabel }),
      emptyTimelineLabel: t("actors.emptyTimeline"),
      messageLabel: t("actors.messageLabel", { actor: actorLabel }),
      composerPlaceholder: actor === "local" ? t("actors.localPlaceholder") : t("actors.agentPlaceholder", { actor: actorLabel }),
      statusText: !selected?.writable && selected ? t("actors.readOnlyHistory") : running ? t("actors.continueAvailable") : t("actors.historyAvailable"),
      readOnlyNotice: t("actors.readOnlyNotice"),
    }),
  };
  const modeOptions: SessionModeOption[] | undefined = actor === "claude" ? [
    { value: "default", label: t("actors.modeDefault") },
    { value: "acceptEdits", label: t("actors.modeAcceptEdits") },
    { value: "plan", label: t("actors.modePlan") },
    { value: "bypassPermissions", label: t("actors.modeBypassPermissions"), riskLabel: t("actors.modeBypassWarning") },
  ] : undefined;
  const messageGroups = useMemo(() => groupToolMessages(messages), [messages]);
  const activeBubble = useMemo(() => {
    let current: BubbleTimelineMeta | null = null;
    for (const message of messages) {
      const bubble = message.bubble;
      if (!bubble || bubble.kind !== "handoff") continue;
      if (bubble.phase === "start") current = bubble;
      else if (bubble.phase === "end" && current?.id === bubble.id) current = null;
    }
    return current;
  }, [messages]);

  if (!active) return null;
  return <SessionsView
    sessions={sessions}
    sessionsLoading={listLoading}
    selectedSessionId={selectedId}
    onSelectSession={selectConversation}
    onNewSession={newConversation}
    onRefreshSessions={() => void Promise.all([
      refreshSessions(),
      selectedId ? loadConversation(selectedId) : Promise.resolve(),
    ]).catch(feedback.error)}
    selectedSession={selected}
    editingSessionTitle={editingTitle}
    sessionTitleDraft={titleDraft}
    setSessionTitleDraft={setTitleDraft}
    renamingSession={renaming}
    sessionTitleInputRef={titleInputRef}
    onSaveSessionTitle={() => void saveTitle().catch(feedback.error)}
    onCancelEditTitle={() => setEditingTitle(false)}
    onStartEditingSessionTitle={() => {
      if (!selected?.writable || !running) return;
      setTitleDraft(selected.title);
      setEditingTitle(true);
    }}
    sessionTimelineRef={timelineRef}
    sessionCursor={cursor}
    messagesLoading={messagesLoading}
    onLoadEarlierMessages={() => void loadEarlier().catch(feedback.error)}
    sessionMessageGroups={messageGroups}
    activeBubble={activeBubble}
    onDownloadAttachment={(attachment) => void downloadAttachment(
      attachment,
      t("sessions.dialogSaveAttachment"),
      t("sessions.attachment.notDownloadable"),
      t("sessions.toast.attachmentSaved"),
      feedback,
    ).catch(feedback.error)}
    onDragAttachment={(path) => void startDesktopFileDrag(path).catch(feedback.error)}
    canUseComposer={canUseComposer}
    composerDisabled={!canUseComposer || sending}
    composerText={composerText}
    setComposerText={setComposerText}
    composerAttachments={attachments}
    composerAttachmentsDragging={attachmentsDragging}
    onRemoveComposerAttachment={(path) => setAttachments((current) => current.filter((item) => item !== path))}
    onChooseAttachments={() => void openDialog({ multiple: true, title: t("sessions.dialogSelectAttachments") })
      .then((value) => selectedPaths(value))
      .then((paths) => setAttachments((current) => [...current, ...paths.filter((path) => !current.includes(path))]))
      .catch(feedback.error)}
    draftProjectPath={actor === "local" ? undefined : draftProjectPath}
    onChooseDraftProject={actor === "local" ? undefined : () => void openDialog({
      directory: true,
      defaultPath: draftProjectPath || selected?.project_path || undefined,
      title: t("sessions.dialogSelectProject"),
    }).then((value) => {
      if (typeof value === "string") setDraftProjectPath(value);
    }).catch(feedback.error)}
    onCopyMessageError={() => feedback.toast(t("sessions.toast.copyFailed"), "error")}
    onSubmitMessage={(coworkerId) => void submit(coworkerId).catch(feedback.error)}
    onSendToCoworker={(coworkerId) => void submit(coworkerId, true).catch(feedback.error)}
    sessionMode={sessionMode ?? "default"}
    composerMode={composerMode}
    onChangeSessionMode={(mode) => void changeMode(normalizeMode(mode)).catch(feedback.error)}
    coworkerPickerOpen={coworkerPickerOpen}
    setCoworkerPickerOpen={setCoworkerPickerOpen}
    visibleCoworkers={coworkers}
    bridgeRunning={running}
    presentation={presentation}
    showModeSelector={actor !== "local"}
    modeOptions={modeOptions}
    composerPrelude={actor === "local" && !selected ? (
      <select value={selectedCoworkerId} onChange={(event) => onSelectCoworker(event.target.value)} aria-label={t("actors.chooseCoworker")}>
        {coworkers.map((coworker) => <option key={coworker.coworker_id} value={coworker.coworker_id}>{coworker.display_name || coworker.coworker_id}</option>)}
      </select>
    ) : null}
  />;
}

export function ConversationsWorkspace({
  active,
  configPath,
  status,
  coworkers,
  selectedCoworkerId,
  onSelectCoworker,
  feedback,
  onActiveConversationChange,
}: WorkspaceProps) {
  const [actor, setActor] = useState<DesktopActorId>("codex");

  useEffect(() => {
    if (!active) onActiveConversationChange(null);
  }, [active, onActiveConversationChange]);

  return <div className={active ? "unifiedSessions" : undefined} hidden={!active}>
    <ActorRail actor={actor} health={status?.actors ?? []} onChange={setActor} />
    {(["local", "codex", "claude"] as DesktopActorId[]).map((controllerActor) => (
      <ConversationController
        key={controllerActor}
        actor={controllerActor}
        active={active && actor === controllerActor}
        configPath={configPath}
        running={status?.state === "running"
          && Boolean(status?.actors?.find((item) => item.actor_id === controllerActor)?.available ?? controllerActor !== "claude")}
        coworkers={coworkers}
        selectedCoworkerId={selectedCoworkerId}
        onSelectCoworker={onSelectCoworker}
        feedback={feedback}
        onActiveConversationChange={onActiveConversationChange}
      />
    ))}
  </div>;
}
