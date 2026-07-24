import { Check, ChevronUp, Copy, Download, FolderOpen, GripVertical, Lock, MessageSquare, Paperclip, Pencil, Plus, Quote, RefreshCw, Send, X } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject, type UIEvent } from "react";
import { MessageIcon, MessageText, ToolResultDisclosure } from "../components/MessageParts";
import { useI18n } from "../i18n";
import type { DictKey } from "../i18n/en";
import {
  fileName,
  formatSessionTime,
  type BubbleTimelineMeta,
  type TimelineAttachment,
  type TimelineMessageGroup,
} from "../lib/bridgeLogic";
import type { ActorConversation, BridgeCoworker } from "../tauri";

type Translate = (key: DictKey, vars?: Record<string, string | number>) => string;
const loadEarlierScrollThreshold = 48;

export type SessionsPresentation = Partial<{
  ariaLabel: string;
  sidebarEyebrow: string;
  sidebarTitle: string;
  refreshLabel: string;
  newSessionLabel: string;
  noSessionsLabel: string;
  draftTitle: string;
  newSessionSubtitle: string;
  emptyTimelineLabel: string;
  messageLabel: string;
  composerPlaceholder: string;
  statusText: string;
  readOnlyNotice: string;
}>;

export type SessionModeOption = {
  value: string;
  label: string;
  riskLabel?: string;
};

function sessionSubtitle(session: ActorConversation | null, t: Translate) {
  if (!session) return t("sessions.subtitleNewMessage");
  if (session.project_path) return session.project_path;
  return t("sessions.subtitleNoProjectPath");
}

export function SessionsView({
  sessions,
  sessionsLoading,
  selectedSessionId,
  onSelectSession,
  onNewSession,
  onRefreshSessions,
  selectedSession,
  editingSessionTitle,
  sessionTitleDraft,
  setSessionTitleDraft,
  renamingSession,
  sessionTitleInputRef,
  onSaveSessionTitle,
  onCancelEditTitle,
  onStartEditingSessionTitle,
  sessionTimelineRef,
  sessionCursor,
  messagesLoading,
  onLoadEarlierMessages,
  sessionMessageGroups,
  activeBubble,
  onDownloadAttachment,
  onDragAttachment,
  canUseComposer,
  composerDisabled,
  composerText,
  setComposerText,
  composerAttachments,
  composerAttachmentsDragging,
  onRemoveComposerAttachment,
  onChooseAttachments,
  onSubmitMessage,
  onSendToCoworker,
  sessionMode,
  composerMode,
  onChangeSessionMode,
  coworkerPickerOpen,
  setCoworkerPickerOpen,
  visibleCoworkers,
  bridgeRunning,
  presentation,
  allowRename = true,
  showAttachments = true,
  showModeSelector = true,
  showCoworkerAction = true,
  composerPrelude,
  modeOptions,
  draftProjectPath,
  onChooseDraftProject,
  onCopyMessageError,
}: {
  sessions: ActorConversation[];
  sessionsLoading: boolean;
  selectedSessionId: string;
  onSelectSession: (threadId: string) => void;
  onNewSession: () => void;
  onRefreshSessions: () => void;
  selectedSession: ActorConversation | null;
  editingSessionTitle: boolean;
  sessionTitleDraft: string;
  setSessionTitleDraft: (value: string) => void;
  renamingSession: boolean;
  sessionTitleInputRef: RefObject<HTMLInputElement | null>;
  onSaveSessionTitle: () => void;
  onCancelEditTitle: () => void;
  onStartEditingSessionTitle: () => void;
  sessionTimelineRef: RefObject<HTMLDivElement | null>;
  sessionCursor: string | null;
  messagesLoading: boolean;
  onLoadEarlierMessages: () => void;
  sessionMessageGroups: TimelineMessageGroup[];
  activeBubble?: BubbleTimelineMeta | null;
  onDownloadAttachment: (attachment: TimelineAttachment) => void;
  onDragAttachment: (path: string) => void;
  canUseComposer: boolean;
  composerDisabled: boolean;
  composerText: string;
  setComposerText: (value: string) => void;
  composerAttachments: string[];
  composerAttachmentsDragging: boolean;
  onRemoveComposerAttachment: (path: string) => void;
  onChooseAttachments: () => void;
  onSubmitMessage: (coworkerId?: string | null) => void;
  onSendToCoworker: (coworkerId?: string | null) => void;
  sessionMode: string;
  composerMode: string;
  onChangeSessionMode: (mode: string) => void;
  coworkerPickerOpen: boolean;
  setCoworkerPickerOpen: (open: boolean) => void;
  visibleCoworkers: BridgeCoworker[];
  bridgeRunning: boolean;
  presentation?: SessionsPresentation;
  allowRename?: boolean;
  showAttachments?: boolean;
  showModeSelector?: boolean;
  showCoworkerAction?: boolean;
  composerPrelude?: ReactNode;
  modeOptions?: SessionModeOption[];
  draftProjectPath?: string;
  onChooseDraftProject?: () => void;
  onCopyMessageError: () => void;
}) {
  const { t } = useI18n();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const [copiedMessageId, setCopiedMessageId] = useState("");
  const resolvedModeOptions = modeOptions ?? [
    { value: "default", label: t("sessions.modeDefault") },
    { value: "plan", label: t("sessions.modePlan") },
  ];
  const activeMode = sessionMode || composerMode;
  const activeModeRisk = resolvedModeOptions.find((option) => option.value === activeMode)?.riskLabel;
  const canRename = Boolean(allowRename && selectedSession?.writable && bridgeRunning);
  useLayoutEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    composer.style.height = "auto";
    composer.style.height = `${composer.scrollHeight}px`;
  }, [composerText]);

  function handleTimelineScroll(event: UIEvent<HTMLDivElement>) {
    if (!sessionCursor || messagesLoading) return;
    if (event.currentTarget.scrollTop <= loadEarlierScrollThreshold) {
      onLoadEarlierMessages();
    }
  }

  function quoteMessage(text: string) {
    const quoted = text
      .trim()
      .split(/\r?\n/)
      .map((line) => `> ${line}`)
      .join("\n");
    if (!quoted) return;
    setComposerText(composerText ? `${composerText}\n\n${quoted}\n\n` : `${quoted}\n\n`);
    window.requestAnimationFrame(() => composerRef.current?.focus());
  }

  async function copyMessage(messageId: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedMessageId(messageId);
      window.setTimeout(() => setCopiedMessageId((current) => current === messageId ? "" : current), 1600);
    } catch {
      setCopiedMessageId("");
      onCopyMessageError();
    }
  }

  return (
    <section className="sessionsView" aria-label={presentation?.ariaLabel ?? t("aria.codexSessions")}>
      <aside className="sessionSidebar" aria-label="Session list">
        <div className="sectionHead">
          <div>
            <p className="eyebrow">{presentation?.sidebarEyebrow ?? t("sessions.eyebrow")}</p>
            <h3>{presentation?.sidebarTitle ?? t("sessions.title")}</h3>
          </div>
          <button className="iconButton" onClick={onRefreshSessions} title={presentation?.refreshLabel ?? t("aria.refreshSessions")} aria-label={presentation?.refreshLabel ?? t("aria.refreshSessions")}>
            <RefreshCw size={16} />
          </button>
        </div>
        <button className="newSessionButton" disabled={!bridgeRunning} onClick={onNewSession} type="button" title={!bridgeRunning ? t("sessions.newNeedsConnection") : undefined}>
          <Plus size={16} /> {presentation?.newSessionLabel ?? t("sessions.newBridgeSession")}
        </button>
        <div className="sessionList" role="list">
          {sessionsLoading && !sessions.length ? (
            <div className="emptyLedger">{t("sessions.loadingSessions")}</div>
          ) : sessions.length ? (
            sessions.map((session) => (
              <button
                className={session.conversation_id === selectedSessionId ? "sessionListItem active" : "sessionListItem"}
                key={session.conversation_id}
                onClick={() => onSelectSession(session.conversation_id)}
                aria-pressed={session.conversation_id === selectedSessionId}
                title={session.title}
              >
                <span className={session.writable ? "sessionAccess owned" : "sessionAccess readOnly"}>
                  {session.writable ? <MessageSquare size={13} /> : <Lock size={13} />}
                </span>
                <span>
                  <strong title={session.title}>{session.title}</strong>
                  <small>
                    {formatSessionTime(session.updated_at ?? "")} · {session.writable ? t("actors.continueAvailable") : t("actors.readOnlyHistory")}
                  </small>
                </span>
              </button>
            ))
          ) : (
            <div className="emptyLedger">{presentation?.noSessionsLabel ?? t("sessions.noSessionsFound")}</div>
          )}
        </div>
      </aside>

      <section className="sessionMain">
        <header className="sessionHeader">
          <div>
            {editingSessionTitle && selectedSession ? (
              <div className="sessionTitleEditor">
                <input
                  aria-label={t("aria.sessionTitle")}
                  className="sessionTitleInput"
                  disabled={renamingSession}
                  ref={sessionTitleInputRef}
                  onChange={(event) => setSessionTitleDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      onSaveSessionTitle();
                    } else if (event.key === "Escape") {
                      onCancelEditTitle();
                    }
                  }}
                  value={sessionTitleDraft}
                />
                <div className="sessionTitleActions">
                  <button className="softButton" disabled={renamingSession} onClick={onCancelEditTitle} type="button">
                    {t("common.cancel")}
                  </button>
                  <button className="primaryAction" disabled={renamingSession} onClick={onSaveSessionTitle} type="button">
                    {t("common.save")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="sessionTitleRow">
                <h3
                  className={canRename ? "sessionTitleEditable" : undefined}
                  onDoubleClick={canRename ? onStartEditingSessionTitle : undefined}
                  onKeyDown={
                    canRename
                      ? (event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onStartEditingSessionTitle();
                          }
                        }
                      : undefined
                  }
                  tabIndex={canRename ? 0 : undefined}
                  title={
                    canRename
                      ? t("sessions.titleDoubleClickToRename", { title: selectedSession?.title ?? "" })
                      : selectedSession?.title ?? presentation?.draftTitle ?? t("sessions.newBridgeSessionDraftTitle")
                  }
                >
                  {selectedSession?.title ?? presentation?.draftTitle ?? t("sessions.newBridgeSessionDraftTitle")}
                </h3>
                {canRename && (
                  <button
                    aria-label={t("sessions.renameTitle")}
                    className="iconButton sessionRenameButton"
                    onClick={onStartEditingSessionTitle}
                    title={t("sessions.renameTitle")}
                    type="button"
                  >
                    <Pencil size={14} />
                  </button>
                )}
              </div>
            )}
            <div className="sessionProjectRow">
              <p className="sessionSubtitle">
                {selectedSession
                  ? selectedSession.project_path || presentation?.newSessionSubtitle || sessionSubtitle(selectedSession, t)
                  : draftProjectPath || presentation?.newSessionSubtitle || sessionSubtitle(null, t)}
              </p>
              {!selectedSession && onChooseDraftProject && (
                <button className="sessionProjectButton" onClick={onChooseDraftProject} type="button">
                  <FolderOpen size={14} />
                  {draftProjectPath ? t("sessions.changeProject") : t("sessions.chooseProject")}
                </button>
              )}
            </div>
            {selectedSession && (
              <p className="sessionId" aria-label={t("sessions.fieldThread")}>
                <span>{t("sessions.fieldThread")}</span>
                <code title={selectedSession.conversation_id}>{selectedSession.conversation_id}</code>
              </p>
            )}
          </div>
          <div className="sessionStateStack">
            {activeBubble && (
              <span className="bubbleHandoffPill" title={activeBubble.id}>
                <i aria-hidden="true">🫧</i>
                {t("actors.bubbleActive", { id: activeBubble.id })}
              </span>
            )}
            <span className={canUseComposer ? "statePill" : "dirtyMark"}>
              {presentation?.statusText ?? (selectedSession
                ? canUseComposer
                  ? t("sessions.statusContinueEnabled")
                  : selectedSession.writable
                    ? t("sessions.statusOfflineReadable")
                    : t("sessions.statusReadOnly")
                : bridgeRunning
                  ? t("sessions.statusDraft")
                  : t("sessions.statusOfflineReadable"))}
            </span>
          </div>
        </header>

        <div className="sessionTimeline" ref={sessionTimelineRef} onScroll={handleTimelineScroll}>
          {sessionCursor && (
            <button className="loadEarlierButton" onClick={onLoadEarlierMessages} disabled={messagesLoading}>
              <ChevronUp size={15} /> {t("sessions.loadEarlier")}
            </button>
          )}
          {messagesLoading && !sessionMessageGroups.length ? (
            <div className="emptyLedger">{t("sessions.loadingMessages")}</div>
          ) : sessionMessageGroups.length ? (
            sessionMessageGroups.map(({ message, result }) => {
              const attachments = result?.attachments.length ? [...message.attachments, ...result.attachments] : message.attachments;
              const messageText = message.kind === "empty_response" ? t("sessions.emptyResponse") : message.text;
              return (
                <article className={`sessionMessage from-${message.author_kind} kind-${message.kind}${result?.is_error ? " tool-error" : ""}`} key={message.id}>
                  <div className="messageAvatar" aria-hidden="true">
                    <MessageIcon message={message} />
                  </div>
                  <div className="messageBody">
                    <div className="messageMeta">
                      <strong>{message.author_label}</strong>
                      {message.bubble && <code className="bubbleMessageId">{message.bubble.id}</code>}
                      <span>{formatSessionTime(message.timestamp)}</span>
                      {message.streaming && <em>streaming</em>}
                      <span className="messageActions">
                        <button
                          onClick={() => void copyMessage(message.id, messageText)}
                          title={t("sessions.copyMessage")}
                          aria-label={t("sessions.copyMessage")}
                          type="button"
                        >
                          {copiedMessageId === message.id ? <Check size={13} /> : <Copy size={13} />}
                        </button>
                        <button
                          onClick={() => quoteMessage(messageText)}
                          disabled={!canUseComposer || !messageText.trim()}
                          title={t("sessions.quoteMessage")}
                          aria-label={t("sessions.quoteMessage")}
                          type="button"
                        >
                          <Quote size={13} />
                        </button>
                      </span>
                    </div>
                    <MessageText text={messageText} />
                    {result && <ToolResultDisclosure result={result} />}
                    {attachments.length > 0 && (
                      <div className="attachmentList">
                        {attachments.map((attachment, index) => (
                          <button
                            className="attachmentChip"
                            disabled={!attachment.downloadable}
                            draggable={attachment.downloadable}
                            key={`${message.id}-${index}-${attachment.filename}`}
                            onClick={() => onDownloadAttachment(attachment)}
                            onDragStart={(event) => {
                              event.preventDefault();
                              if (attachment.path) onDragAttachment(attachment.path);
                            }}
                            title={attachment.downloadable ? t("sessions.attachment.dragOrDownload") : attachment.reason ?? t("sessions.attachment.notDownloadable")}
                            type="button"
                          >
                            <span className="attachmentActionIcon" aria-hidden="true">
                              <Download className="attachmentClickIcon" size={13} />
                              <GripVertical className="attachmentDragIcon" size={14} />
                            </span>
                            {attachment.filename}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </article>
              );
            })
          ) : (
            <div className="emptyLedger">{presentation?.emptyTimelineLabel ?? t("sessions.emptyTimeline")}</div>
          )}
        </div>

        <div className={canUseComposer ? "sessionComposer" : "sessionComposer readOnly"}>
          {composerAttachmentsDragging && (
            <div className="composerDropHint" role="status">
              <Paperclip size={18} />
              <span>{t("sessions.dropAttachments")}</span>
            </div>
          )}
          {!canUseComposer && (
            <div className="readOnlyNotice" role="status">
              <Lock size={15} />
              <span>
                {!bridgeRunning
                  ? t("sessions.composerBridgeNotRunning")
                  : selectedSession && !selectedSession.writable
                    ? presentation?.readOnlyNotice ?? t("sessions.composerNotBridgeOwned")
                    : t("sessions.composerBridgeNotRunning")}
              </span>
            </div>
          )}
          {showAttachments && composerAttachments.length > 0 && (
            <div className="composerAttachments">
              {composerAttachments.map((path) => (
                <button
                  className="attachmentChip"
                  draggable
                  key={path}
                  onClick={() => onRemoveComposerAttachment(path)}
                  onDragStart={(event) => {
                    event.preventDefault();
                    onDragAttachment(path);
                  }}
                  type="button"
                  title={t("sessions.attachment.dragOrRemove")}
                >
                  <span className="attachmentActionIcon" aria-hidden="true">
                    <X className="attachmentClickIcon" size={13} />
                    <GripVertical className="attachmentDragIcon" size={14} />
                  </span>
                  {fileName(path)}
                </button>
              ))}
            </div>
          )}
          {composerPrelude}
          <textarea
            aria-label={presentation?.messageLabel ?? t("aria.sessionMessage")}
            disabled={composerDisabled}
            ref={composerRef}
            onChange={(event) => setComposerText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) {
                event.preventDefault();
                onSubmitMessage();
              }
            }}
            placeholder={!canUseComposer ? t("sessions.composerDisabledPlaceholder") : presentation?.composerPlaceholder ?? t("sessions.composerPlaceholder")}
            value={composerText}
          />
          <div className="composerBar">
            <div className="composerTools">
              {showAttachments && <button className="iconButton" onClick={onChooseAttachments} disabled={composerDisabled} title={t("common.attachFiles")} aria-label={t("common.attachFiles")}>
                <Paperclip size={16} />
              </button>}
              {showModeSelector && <select
                aria-label={t("aria.sessionMode")}
                disabled={composerDisabled}
                value={activeMode}
                onChange={(event) => onChangeSessionMode(event.target.value)}
              >
                {resolvedModeOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>}
              {showModeSelector && activeModeRisk && <span className="modeRiskHint" role="status">{activeModeRisk}</span>}
            </div>
            <div className="composerSendActions">
            {showCoworkerAction && <button
              className="softButton"
              onClick={() => onSendToCoworker()}
              disabled={composerDisabled || !selectedSession || (!composerText.trim() && composerAttachments.length === 0)}
            >
              {t("sessions.sendToCoworker")}
            </button>}
            <button
              className="primaryAction"
              onClick={() => onSubmitMessage()}
              disabled={composerDisabled || (!composerText.trim() && composerAttachments.length === 0)}
            >
              <Send size={16} /> {t("common.send")}
            </button>
            </div>
          </div>
        </div>
      </section>

      {coworkerPickerOpen && (
        <div className="modalBackdrop" role="presentation" onClick={() => setCoworkerPickerOpen(false)}>
          <div className="coworkerPicker" role="dialog" aria-modal="true" aria-label={t("aria.chooseCoworker")} onClick={(event) => event.stopPropagation()}>
            <div className="sectionHead">
              <div>
                <p className="eyebrow">{t("brand.eyebrow")}</p>
                <h3>{t("sessions.pickerTitle")}</h3>
              </div>
              <button className="iconButton" onClick={() => setCoworkerPickerOpen(false)} aria-label={t("aria.closeCoworkerPicker")}>
                <X size={15} />
              </button>
            </div>
            <div className="coworkerList">
              {visibleCoworkers.map((coworker) => (
                <button className="coworkerItem" key={coworker.coworker_id} onClick={() => onSendToCoworker(coworker.coworker_id)} type="button">
                  <span className="healthDot ok" aria-hidden="true" />
                  <span>
                    <strong>{coworker.display_name || coworker.coworker_id}</strong>
                    <small>{coworker.base_url}</small>
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
