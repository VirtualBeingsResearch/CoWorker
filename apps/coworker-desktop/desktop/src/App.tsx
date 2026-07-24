import {
  ArrowDownToLine,
  BookOpen,
  Gauge,
  HeartPulse,
  MessagesSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Settings2,
  Sparkles,
  ScrollText,
  Square,
  X,
} from "lucide-react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import appMetadata from "../package.json";
import { FeedbackIcon } from "./components/Field";
import { LogDetailDialog } from "./components/LogDetailDialog";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { useI18n } from "./i18n";
import {
  classifyDesktopUpdateErrorKey,
  clampLogText,
  diagnosticsForCoworker,
  enabledCoworkers,
  logLevels,
  maxRenderedLogEntries,
  moodColor,
  nextCoworker,
  normalizeCoworkers,
  normalizeTimeoutSeconds,
  parseLog,
  resolvedApprovalConfig,
  runtimeMood,
  toastDurationMs,
  validateConfig,
  type ApprovalConfigView,
  type DesktopUpdateState,
  type FeedbackTone,
  type InlineNotice,
  type LogEntryViewModel,
  type LogLevel,
  type LogLevelFilter,
  type ToastNotification,
  type View,
} from "./lib/bridgeLogic";
import {
  BridgeCoworker,
  checkDesktopUpdate,
  deleteCommunicateRegistration,
  getBridgeStatus,
  listDesktopApprovals,
  resolveDesktopApproval,
  getConfigInfo,
  getDefaultDesktopUpdateUrl,
  installDesktopUpdate,
  listCommunicateRegistrations,
  listenActorStreamEvents,
  listenBridgeLogChunks,
  readBridgeLog,
  runDiagnostics,
  saveConfig,
  ConfigValue,
  LogOutputLevel,
  BridgeStatus,
  ActorStreamEvent,
  CommunicateRegistration,
  DesktopUpdateInfo,
  DiagnosticResult,
  DesktopApproval,
  type DesktopActorId,
  ResolveApprovalResult,
  startBridge,
  startBridgeLogStream,
  stopBridge,
  stopBridgeLogStream,
  setCloseToTray,
  setTrayCopy,
} from "./tauri";
import { ConfigView } from "./views/ConfigView";
import { LogsView } from "./views/LogsView";
import { ConversationsWorkspace } from "./views/ConversationsWorkspace";
import { ApprovalPanel } from "./views/ApprovalPanel";
import { StatusView } from "./views/StatusView";

const defaultPath = "coworker_desktop.json";
const lifePanelStorageKey = "coworker-desktop-life-panel-collapsed";
const onboardingCompletedStorageKey = "coworker-desktop-onboarding-completed";

type ActiveConversation = {
  actorId: DesktopActorId;
  conversationId: string;
};

function readInitialLifePanelCollapsed() {
  try {
    return window.localStorage.getItem(lifePanelStorageKey) === "true";
  } catch {
    return false;
  }
}

export function shouldNotifyActorEvent(
  update: ActorStreamEvent,
  notifiedIds: Set<string>,
): boolean {
  const eventType = String(update.event.type ?? "");
  const message = update.event.message && typeof update.event.message === "object" && !Array.isArray(update.event.message)
    ? update.event.message as Record<string, unknown>
    : null;
  const metadata = message?.metadata && typeof message.metadata === "object" && !Array.isArray(message.metadata)
    ? message.metadata as Record<string, unknown>
    : null;
  const authorKind = String(message?.author_kind ?? "");
  const messageKind = String(metadata?.kind ?? "");
  const streaming = metadata?.streaming === true;
  const isIncomingCoworker = eventType === "conversation_updated"
    && (
      authorKind === "coworker"
      || (message === null && update.message_id !== null && (update.actor_id === "local" || update.actor_id === "claude"))
    );
  const isCodexTerminalMessage = update.actor_id === "codex"
    && eventType === "conversation_updated"
    && !streaming
    && (
      (authorKind === "codex" && messageKind === "message")
      || (authorKind === "system" && ["empty_response", "plan", "system"].includes(messageKind))
    );
  const isClaudeResult = update.actor_id === "claude" && eventType === "result";
  if (!isIncomingCoworker && !isCodexTerminalMessage && !isClaudeResult) return false;
  const notificationId = update.message_id
    ?? `${update.actor_id}:${update.conversation_id}:${eventType}`;
  if (notifiedIds.has(notificationId)) return false;
  notifiedIds.add(notificationId);
  return true;
}

export function isActiveConversationEvent(
  update: ActorStreamEvent,
  activeConversation: ActiveConversation | null,
): boolean {
  return activeConversation?.actorId === update.actor_id
    && activeConversation.conversationId === update.conversation_id;
}

export function shouldSendNativeNotification(
  page: Pick<Document, "hasFocus" | "visibilityState"> = document,
): boolean {
  return page.visibilityState !== "visible" || !page.hasFocus();
}

export function App() {
  const { t, lang, setLang } = useI18n();
  const [view, setView] = useState<View>("status");
  const [isLifePanelCollapsed, setIsLifePanelCollapsed] = useState(readInitialLifePanelCollapsed);
  const [selectedCoworkerId, setSelectedCoworkerId] = useState<string>("");
  const [selectedCoworkerIndex, setSelectedCoworkerIndex] = useState(0);
  const [configPath, setConfigPath] = useState(defaultPath);
  const [config, setConfig] = useState<ConfigValue>({});
  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [bootstrapPhase, setBootstrapPhase] = useState<"loading" | "ready" | "error">("loading");
  const [log, setLog] = useState("");
  const [logEntries, setLogEntries] = useState<LogEntryViewModel[]>([]);
  const [selectedLogEntry, setSelectedLogEntry] = useState<LogEntryViewModel | null>(null);
  const [logParsePending, setLogParsePending] = useState(false);
  const [logLevelUpdating, setLogLevelUpdating] = useState(false);
  const [logLevelFilter, setLogLevelFilter] = useState<LogLevelFilter>("all");
  const [liveLogs, setLiveLogs] = useState(true);
  const [followLatest, setFollowLatest] = useState(true);
  const [diagnostics, setDiagnostics] = useState<DiagnosticResult[]>([]);
  const [diagnosticsRunning, setDiagnosticsRunning] = useState(false);
  const [registrations, setRegistrations] = useState<CommunicateRegistration[]>([]);
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [inlineNotice, setInlineNotice] = useState<InlineNotice | null>(null);
  const [desktopUpdate, setDesktopUpdate] = useState<DesktopUpdateInfo | null>(null);
  const [desktopUpdateState, setDesktopUpdateState] = useState<DesktopUpdateState>("idle");
  const [desktopUpdateError, setDesktopUpdateError] = useState("");
  const [desktopUpdateUrlPlaceholder, setDesktopUpdateUrlPlaceholder] = useState("");
  const [desktopUpdateDownloaded, setDesktopUpdateDownloaded] = useState(0);
  const [desktopUpdateContentLength, setDesktopUpdateContentLength] = useState<number | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [lastConfigModifiedMs, setLastConfigModifiedMs] = useState<number | null>(null);
  const [developmentWarningDismissed, setDevelopmentWarningDismissed] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [desktopApprovals, setDesktopApprovals] = useState<DesktopApproval[]>([]);
  const [resolvingApproval, setResolvingApproval] = useState(false);
  const ledgerRef = useRef<HTMLDivElement | null>(null);
  const logStreamActiveRef = useRef(false);
  const approvalRequestGenerationRef = useRef(0);
  const resolvingApprovalRef = useRef(false);
  const diagnosticsRequestRef = useRef(0);
  const diagnosticsRunningRef = useRef(false);
  const desktopUpdateCheckInFlightRef = useRef(false);
  const handledDesktopUpdatePushesRef = useRef(new Set<string>());
  const notifiedDesktopUpdateVersionsRef = useRef(new Set<string>());
  const notifiedMessageIdsRef = useRef(new Set<string>());
  const activeConversationRef = useRef<ActiveConversation | null>(null);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Map<number, number>>(new Map());
  const savedConfigRef = useRef<ConfigValue>({});

  useEffect(() => {
    if (status?.state !== "running") {
      setDesktopApprovals([]);
      return;
    }
    let active = true;
    const refreshApprovals = () => {
      if (resolvingApprovalRef.current) return;
      const generation = approvalRequestGenerationRef.current + 1;
      approvalRequestGenerationRef.current = generation;
      listDesktopApprovals()
        .then((items) => {
          if (active && generation === approvalRequestGenerationRef.current && !resolvingApprovalRef.current) {
            setDesktopApprovals(items);
          }
        })
        .catch(() => undefined);
    };
    refreshApprovals();
    let unlisten: (() => void) | undefined;
    listenActorStreamEvents((event) => {
      if (["desktop.approval.requested", "desktop.approval.changed", "stream-lagged"].includes(
        String(event.event.type),
      )) {
        refreshApprovals();
      }
    }).then((fn) => {
      if (active) {
        unlisten = fn;
      } else {
        fn();
      }
    });
    return () => {
      active = false;
      approvalRequestGenerationRef.current += 1;
      unlisten?.();
    };
  }, [status?.state]);

  const resolveApproval = async (
    approval: DesktopApproval,
    response: { behavior: "allow" | "deny"; updatedInput?: unknown; message?: string },
  ) => {
    resolvingApprovalRef.current = true;
    approvalRequestGenerationRef.current += 1;
    setResolvingApproval(true);
    try {
      const result: ResolveApprovalResult = await resolveDesktopApproval(approval, response);
      if (result.ok) {
        setDesktopApprovals((current) => current.filter((item) => item.request_id !== approval.request_id));
      } else {
        // The request was already resolved (e.g. by Coworker) or expired.
        // Remove it from the local list and show a brief toast.
        setDesktopApprovals((current) => current.filter((item) => item.request_id !== approval.request_id));
        const reason = result.reason === "already_resolved"
          ? t("approval.alreadyResolved")
          : t("approval.expired");
        showToast(reason);
      }
    } catch (error) {
      reportError(error);
    } finally {
      resolvingApprovalRef.current = false;
      setResolvingApproval(false);
    }
  };

  const coworkers = useMemo(() => {
    const configured = normalizeCoworkers(config);
    return configured.length ? configured : [{ coworker_id: "cw_default", display_name: "Partner", base_url: "http://localhost:8000", enabled: true }];
  }, [config]);
  const activeCoworkers = useMemo(() => enabledCoworkers(coworkers), [coworkers]);
  const visibleCoworkers = status?.coworkers?.length ? status.coworkers : activeCoworkers;
  const selectedIndex = Math.min(selectedCoworkerIndex, Math.max(coworkers.length - 1, 0));
  const selectedCoworker = coworkers[selectedIndex] ?? coworkers[0];
  const operationalCoworker = visibleCoworkers.find((coworker) => coworker.coworker_id === selectedCoworkerId) ?? visibleCoworkers[0];
  const operationalCoworkerId = operationalCoworker?.coworker_id ?? "";
  const issues = useMemo(() => validateConfig({ ...config, coworkers }), [config, coworkers]);
  const moodKey = runtimeMood(status);
  const selectedClientId = `${String(config.desktop_id ?? "desktop-local")}:*:${selectedCoworker?.coworker_id ?? ""}`;
  const selectedRegistrations = useMemo(
    () => registrations.filter((item) =>
      item.kind === "coworker-desktop" && item.metadata?.coworker_id === selectedCoworker?.coworker_id
    ),
    [registrations, selectedCoworker?.coworker_id],
  );
  const logLevelCounts = useMemo(() => {
    const counts = Object.fromEntries(logLevels.map((level) => [level, 0])) as Record<LogLevel, number>;
    logEntries.forEach((entry) => {
      counts[entry.level] += 1;
    });
    return counts;
  }, [logEntries]);
  const filteredLogEntries = useMemo(
    () => (logLevelFilter === "all" ? logEntries : logEntries.filter((entry) => entry.level === logLevelFilter)),
    [logEntries, logLevelFilter],
  );
  const renderedLogEntries = useMemo(() => filteredLogEntries.slice(-maxRenderedLogEntries), [filteredLogEntries]);
  const hiddenLogEntryCount = filteredLogEntries.length - renderedLogEntries.length;
  const latestVisibleLogId = renderedLogEntries[renderedLogEntries.length - 1]?.id ?? "";
  const bridgeRunning = status?.state === "running";
  const conversationFeedback = useMemo(
    () => ({ error: reportError, notice: showInlineNotice, toast: showToast }),
    [],
  );

  function dismissToast(id: number) {
    const timer = toastTimersRef.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      toastTimersRef.current.delete(id);
    }
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }

  function showToast(text: string, tone: FeedbackTone = "success") {
    const id = toastIdRef.current + 1;
    toastIdRef.current = id;
    setToasts((current) => [...current, { id, tone, text }]);
    const timer = window.setTimeout(() => dismissToast(id), toastDurationMs);
    toastTimersRef.current.set(id, timer);
  }

  function showInlineNotice(text: string, tone: InlineNotice["tone"] = "warning") {
    setInlineNotice({ tone, text });
  }

  function reportError(error: unknown) {
    showInlineNotice(String(error), "error");
  }

  useEffect(() => {
    void setTrayCopy({
      tooltip: t("tray.tooltip"),
      open: t("tray.open"),
      hide: t("tray.hide"),
      quit: t("tray.quit"),
    }).catch(() => undefined);
  }, [lang, t]);

  useEffect(() => {
    void setCloseToTray(config.close_to_tray !== false).catch(() => undefined);
  }, [config.close_to_tray]);

  useEffect(() => {
    return () => {
      toastTimersRef.current.forEach((timer) => window.clearTimeout(timer));
      toastTimersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(lifePanelStorageKey, String(isLifePanelCollapsed));
    } catch {
      // Ignore storage errors; the UI can still keep the preference in memory.
    }
  }, [isLifePanelCollapsed]);

  useEffect(() => {
    setDevelopmentWarningDismissed(false);
  }, [status?.development_mode, status?.config_path]);

  useEffect(() => {
    if (view !== "logs") {
      setLogParsePending(false);
      return;
    }

    setLogParsePending(true);
    let disposed = false;
    const frame = window.requestAnimationFrame(() => {
      if (disposed) return;
      setLogEntries(parseLog(log, coworkers));
      setLogParsePending(false);
    });

    return () => {
      disposed = true;
      window.cancelAnimationFrame(frame);
    };
  }, [coworkers, log, view]);

  useEffect(() => {
    if (selectedCoworkerIndex >= coworkers.length) {
      setSelectedCoworkerIndex(0);
      setSelectedCoworkerId(coworkers[0]?.coworker_id ?? "");
      return;
    }
    const currentId = coworkers[selectedCoworkerIndex]?.coworker_id ?? "";
    if (currentId !== selectedCoworkerId) {
      setSelectedCoworkerId(currentId);
    }
  }, [coworkers, selectedCoworkerId, selectedCoworkerIndex]);

  useEffect(() => {
    if (view !== "logs" || !followLatest || !ledgerRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      if (ledgerRef.current) {
        ledgerRef.current.scrollTop = ledgerRef.current.scrollHeight;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [followLatest, latestVisibleLogId, logLevelFilter, view]);

  useEffect(() => {
    if (view !== "logs" || !liveLogs) {
      if (logStreamActiveRef.current) {
        logStreamActiveRef.current = false;
        stopBridgeLogStream().catch(() => undefined);
      }
      return;
    }

    let disposed = false;
    let unlisten: (() => void) | undefined;
    let pendingLog: { reset: boolean; text: string } | null = null;
    let flushFrame: number | undefined;

    listenBridgeLogChunks((chunk) => {
      if (chunk.reset || !pendingLog) {
        pendingLog = { reset: chunk.reset, text: chunk.text };
      } else {
        pendingLog.text += chunk.text;
      }
      if (flushFrame === undefined) {
        flushFrame = window.requestAnimationFrame(() => {
          const next = pendingLog;
          pendingLog = null;
          flushFrame = undefined;
          if (next && !disposed) {
            setLog((current) => clampLogText(next.reset ? next.text : `${current}${next.text}`));
          }
        });
      }
    })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
          return;
        }
        unlisten = nextUnlisten;
        startBridgeLogStream(configPath)
          .then(() => {
            if (disposed) {
              stopBridgeLogStream().catch(() => undefined);
              return;
            }
            logStreamActiveRef.current = true;
          })
          .catch(reportError);
      })
      .catch(reportError);

    return () => {
      disposed = true;
      if (flushFrame !== undefined) {
        window.cancelAnimationFrame(flushFrame);
      }
      unlisten?.();
      if (logStreamActiveRef.current) {
        logStreamActiveRef.current = false;
        stopBridgeLogStream().catch(() => undefined);
      }
    };
  }, [configPath, liveLogs, view]);

  async function refresh() {
    try {
      const [nextConfig, nextStatus, nextLog, defaultDesktopUpdateUrl] = await Promise.all([
        getConfigInfo(configPath),
        getBridgeStatus(),
        readBridgeLog(configPath).catch(() => ""),
        getDefaultDesktopUpdateUrl().catch(() => ""),
      ]);
      const normalizedCoworkers = normalizeCoworkers(nextConfig.config);
      const normalizedConfig = { ...nextConfig.config, coworkers: normalizedCoworkers };
      savedConfigRef.current = normalizedConfig;
      setConfig(normalizedConfig);
      setLastConfigModifiedMs(nextConfig.modified_ms);
      setIsDirty(false);
      setStatus(nextStatus);
      setLog(clampLogText(nextLog));
      setDesktopUpdateUrlPlaceholder(defaultDesktopUpdateUrl);
      setSelectedCoworkerId((current) => {
        const nextIndex = normalizedCoworkers.findIndex((coworker) => coworker.coworker_id === current);
        setSelectedCoworkerIndex(nextIndex >= 0 ? nextIndex : 0);
        return nextIndex >= 0 ? current : normalizedCoworkers[0]?.coworker_id ?? "";
      });
      invalidateDiagnostics();
      setBootstrapPhase("ready");
    } catch (error) {
      setBootstrapPhase("error");
      throw error;
    }
  }

  async function checkConfigFile() {
    const nextConfig = await getConfigInfo(configPath);
    if (nextConfig.modified_ms === lastConfigModifiedMs) {
      return;
    }
    if (isDirty) {
      showInlineNotice(t("config.fileChangedOnDisk"), "warning");
      return;
    }
    invalidateDiagnostics();
    const normalizedConfig = { ...nextConfig.config, coworkers: normalizeCoworkers(nextConfig.config) };
    savedConfigRef.current = normalizedConfig;
    setConfig(normalizedConfig);
    setLastConfigModifiedMs(nextConfig.modified_ms);
    showToast(nextConfig.exists ? t("config.reloadedFromDisk") : t("config.usingDefault"), "info");
  }

  useEffect(() => {
    setBootstrapPhase("loading");
    invalidateDiagnostics();
    refresh().catch(reportError);
  }, [configPath]);

  useEffect(() => {
    if (bootstrapPhase !== "ready") return;
    let disposed = false;
    checkForDesktopUpdate(false).catch(() => {
      if (!disposed) {
        setDesktopUpdateState("idle");
      }
    });
    return () => {
      disposed = true;
    };
  }, [bootstrapPhase]);

  useEffect(() => {
    if (bootstrapPhase !== "ready") return;
    try {
      if (window.localStorage.getItem(onboardingCompletedStorageKey) !== "true") {
        setOnboardingOpen(true);
      }
    } catch {
      // localStorage unavailable; skip auto-opening the tutorial.
    }
  }, [bootstrapPhase]);

  useEffect(() => {
    if (bootstrapPhase !== "ready") return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listenActorStreamEvents((event) => {
      if (event.event.type !== "desktop_update_check_requested") return;
      const publishedVersion = event.event.published_version;
      const pushKey = typeof publishedVersion === "string" ? publishedVersion : event.message_id;
      if (pushKey && handledDesktopUpdatePushesRef.current.has(pushKey)) return;
      if (pushKey) handledDesktopUpdatePushesRef.current.add(pushKey);
      checkForDesktopUpdate(false).catch(() => undefined);
    })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
        } else {
          unlisten = nextUnlisten;
        }
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [bootstrapPhase, config.desktop_update_url, desktopUpdateUrlPlaceholder]);

  useEffect(() => {
    if (bootstrapPhase !== "ready") return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listenActorStreamEvents((update) => {
      if (!shouldNotifyActorEvent(update, notifiedMessageIdsRef.current)) return;
      if (!shouldSendNativeNotification()) {
        if (isActiveConversationEvent(update, activeConversationRef.current)) return;
        showToast(t("messages.notification.title", { actor: actorDisplayName(update.actor_id) }), "info");
        return;
      }
      notifyIncomingMessage(update.actor_id).catch(() => undefined);
    })
      .then((nextUnlisten) => {
        if (disposed) nextUnlisten();
        else unlisten = nextUnlisten;
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [bootstrapPhase, lang]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      getBridgeStatus().then(setStatus).catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      checkConfigFile().catch(() => undefined);
    }, 5000);
    const onFocus = () => checkConfigFile().catch(() => undefined);
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [configPath, isDirty, lastConfigModifiedMs]);

  function validationMessage() {
    const nextIssues = validateConfig({ ...config, coworkers });
    if (!nextIssues.length) return "";
    return t("validation.issuesNeedAttention", { count: nextIssues.length });
  }

  async function persist() {
    const nextMessage = validationMessage();
    if (nextMessage) {
      showInlineNotice(nextMessage, "warning");
      return;
    }
    const saved = await saveConfig(configPath, { ...config, coworkers });
    const normalizedCoworkers = normalizeCoworkers(saved.config);
    const normalizedConfig = { ...saved.config, coworkers: normalizedCoworkers };
    savedConfigRef.current = normalizedConfig;
    setConfig(normalizedConfig);
    setLastConfigModifiedMs(saved.modified_ms);
    setIsDirty(false);
    setInlineNotice(null);
    showToast(t("config.toast.saved"), "success");
  }

  async function applyLogOutputLevel(level: LogOutputLevel) {
    if (isDirty) {
      showInlineNotice(t("logs.saveChangesBeforeLevel"), "warning");
      return;
    }

    setLogLevelUpdating(true);
    try {
      const saved = await saveConfig(configPath, {
        ...config,
        coworkers,
        log_level: level,
        file_log_level: level,
      });
      const normalizedCoworkers = normalizeCoworkers(saved.config);
      const normalizedConfig = { ...saved.config, coworkers: normalizedCoworkers };
      savedConfigRef.current = normalizedConfig;
      setConfig(normalizedConfig);
      setLastConfigModifiedMs(saved.modified_ms);
      setInlineNotice(null);
      showToast(t("logs.levelUpdated", { level }), "success");
    } finally {
      setLogLevelUpdating(false);
    }
  }

  function discardConfigChanges() {
    const savedConfig = savedConfigRef.current;
    const savedCoworkers = normalizeCoworkers(savedConfig);
    setConfig({ ...savedConfig, coworkers: savedCoworkers });
    setSelectedCoworkerId((current) => {
      const nextIndex = savedCoworkers.findIndex((coworker) => coworker.coworker_id === current);
      setSelectedCoworkerIndex(nextIndex >= 0 ? nextIndex : 0);
      return nextIndex >= 0 ? current : savedCoworkers[0]?.coworker_id ?? "";
    });
    setIsDirty(false);
    setInlineNotice(null);
    invalidateDiagnostics();
    showToast(t("config.toast.changesDiscarded"), "info");
  }

  async function start() {
    const nextMessage = validationMessage();
    if (nextMessage) {
      showInlineNotice(nextMessage, "warning");
      return;
    }
    const saved = await saveConfig(configPath, { ...config, coworkers });
    const normalizedCoworkers = normalizeCoworkers(saved.config);
    const normalizedConfig = { ...saved.config, coworkers: normalizedCoworkers };
    savedConfigRef.current = normalizedConfig;
    setConfig(normalizedConfig);
    setLastConfigModifiedMs(saved.modified_ms);
    setIsDirty(false);
    setStatus(await startBridge(configPath));
    setInlineNotice(null);
    showToast(t("config.toast.savedAndStarted"), "success");
  }

  async function stop() {
    setStatus(await stopBridge());
    showToast(t("config.toast.stopped"), "success");
  }

  function closeOnboarding() {
    try {
      window.localStorage.setItem(onboardingCompletedStorageKey, "true");
    } catch {
      // Ignore storage errors; the preference persists only when storage is available.
    }
    setOnboardingOpen(false);
  }

  async function saveFromOnboarding(andStart: boolean) {
    try {
      if (andStart) {
        await start();
      } else {
        await persist();
      }
    } catch (error) {
      reportError(error);
      throw error;
    }
  }

  async function diagnose() {
    if (diagnosticsRunningRef.current || isDirty || bootstrapPhase !== "ready") return;
    const requestId = diagnosticsRequestRef.current + 1;
    diagnosticsRequestRef.current = requestId;
    diagnosticsRunningRef.current = true;
    setDiagnosticsRunning(true);
    setDiagnostics([]);
    try {
      const nextDiagnostics = await runDiagnostics(configPath);
      if (diagnosticsRequestRef.current === requestId) {
        setDiagnostics(nextDiagnostics);
      }
    } finally {
      if (diagnosticsRequestRef.current === requestId) {
        diagnosticsRunningRef.current = false;
        setDiagnosticsRunning(false);
      }
    }
  }

  async function checkForDesktopUpdate(showResult = true) {
    if (desktopUpdateState === "downloading" || desktopUpdateCheckInFlightRef.current) return;
    desktopUpdateCheckInFlightRef.current = true;
    setDesktopUpdateState("checking");
    setDesktopUpdateError("");
    setDesktopUpdate(null);
    const endpoint = typeof config.desktop_update_url === "string" ? config.desktop_update_url.trim() : "";
    const effectiveEndpoint = endpoint || desktopUpdateUrlPlaceholder;
    setDesktopUpdateDownloaded(0);
    setDesktopUpdateContentLength(null);
    if (showResult) {
      setInlineNotice(null);
    }
    try {
      const update = await checkDesktopUpdate(endpoint);
      setDesktopUpdate(update);
      if (update) {
        setDesktopUpdateState("available");
        await notifyDesktopUpdate(update.version);
      } else {
        setDesktopUpdateState("idle");
        if (showResult) {
          showToast(t("update.toast.upToDate"), "info");
        }
      }
    } catch (error) {
      setDesktopUpdateState(showResult ? "error" : "idle");
      setDesktopUpdateError(t(classifyDesktopUpdateErrorKey(error, effectiveEndpoint)));
      if (!showResult) {
        setDesktopUpdateError("");
      }
    } finally {
      desktopUpdateCheckInFlightRef.current = false;
    }
  }

  async function notifyDesktopUpdate(version: string) {
    if (notifiedDesktopUpdateVersionsRef.current.has(version)) return;
    notifiedDesktopUpdateVersionsRef.current.add(version);
    try {
      const permissionGranted = await isPermissionGranted();
      if (permissionGranted || (await requestPermission()) === "granted") {
        sendNotification({
          title: t("update.notification.title"),
          body: t("update.notification.body", { version }),
        });
      }
    } catch {
      // The in-app update notice remains available when native notifications fail.
    }
  }

  async function notifyIncomingMessage(actorId: string) {
    try {
      const permissionGranted = await isPermissionGranted();
      if (permissionGranted || (await requestPermission()) === "granted") {
        sendNotification({
          title: t("messages.notification.title", { actor: actorDisplayName(actorId) }),
          body: t("messages.notification.body"),
        });
      }
    } catch {
      // Conversation updates remain visible in the app when native notifications fail.
    }
  }

  function actorDisplayName(actorId: string) {
    if (actorId === "local") return t("actors.local");
    if (actorId === "claude") return t("actors.claude");
    return t("actors.codex");
  }

  async function installUpdate() {
    if (!desktopUpdate) return;
    setDesktopUpdateState("downloading");
    setDesktopUpdateError("");
    setDesktopUpdateDownloaded(0);
    setDesktopUpdateContentLength(null);
    await installDesktopUpdate((event) => {
      if (event.event === "Started") {
        setDesktopUpdateContentLength(event.data.contentLength);
        setDesktopUpdateDownloaded(0);
      } else if (event.event === "Progress") {
        setDesktopUpdateDownloaded((current) => current + event.data.chunkLength);
      } else if (event.event === "Finished") {
        setDesktopUpdateState("installed");
      }
    }).catch((error) => {
      setDesktopUpdateState("error");
      setDesktopUpdateError(String(error));
      throw error;
    });
  }

  async function refreshRegistrations() {
    if (!selectedCoworker?.base_url) {
      showInlineNotice(t("registrations.notice.baseUrlRequired"), "warning");
      return;
    }
    setRegistrations(await listCommunicateRegistrations(selectedCoworker.base_url, selectedCoworker.bearer_token));
  }

  async function removeRegistration(registration: CommunicateRegistration) {
    if (!selectedCoworker?.base_url) return;
    await deleteCommunicateRegistration(selectedCoworker.base_url, registration.registration_id, selectedCoworker.bearer_token);
    await refreshRegistrations();
    showToast(t("registrations.toast.deleted"), "success");
  }

  function markDirty() {
    invalidateDiagnostics();
    setIsDirty(true);
    setInlineNotice((current) => (current?.tone === "error" ? current : null));
  }

  function invalidateDiagnostics() {
    diagnosticsRequestRef.current += 1;
    diagnosticsRunningRef.current = false;
    setDiagnosticsRunning(false);
    setDiagnostics([]);
  }

  function updateConfig(nextConfig: ConfigValue) {
    setConfig({ ...nextConfig, coworkers: normalizeCoworkers(nextConfig) });
    markDirty();
  }

  async function chooseChatWorkspacesDir() {
    const selected = await openDialog({
      directory: true,
      multiple: false,
      title: t("config.dialog.selectChatWorkspacesDir"),
    });
    if (typeof selected === "string") {
      updateConfig({ ...config, chat_workspaces_dir: selected });
    }
  }

  async function chooseConfigFile() {
    const selected = await openDialog({
      directory: false,
      multiple: false,
      filters: [{ name: "JSON", extensions: ["json"] }],
      title: t("config.dialog.selectConfigFile"),
    });
    if (typeof selected === "string") {
      setConfigPath(selected);
    }
  }

  function updateApprovalConfig(nextConfig: Partial<ApprovalConfigView>) {
    const current = resolvedApprovalConfig(config);
    updateConfig({
      ...config,
      permissions_mode: nextConfig.permissionsMode ?? current.permissionsMode,
      approvals_reviewer: nextConfig.approvalsReviewer ?? current.approvalsReviewer,
      approval_timeout_seconds: nextConfig.approvalTimeoutSeconds ?? current.approvalTimeoutSeconds,
    });
  }

  function updateCodexId(value: string) {
    setConfig({
      ...config,
      codex_id: value,
      coworkers,
    });
    markDirty();
  }

  function updateCoworker(field: keyof BridgeCoworker, value: BridgeCoworker[keyof BridgeCoworker]) {
    const nextCoworkers = coworkers.map((coworker, index) => {
      if (index !== selectedIndex) return coworker;
      return { ...coworker, [field]: value };
    });
    setConfig({ ...config, coworkers: nextCoworkers });
    if (field === "coworker_id") setSelectedCoworkerId(String(value ?? ""));
    markDirty();
  }

  function moveSelectedCoworker(offset: -1 | 1) {
    const nextIndex = selectedIndex + offset;
    if (nextIndex < 0 || nextIndex >= coworkers.length) return;
    const nextCoworkers = [...coworkers];
    [nextCoworkers[selectedIndex], nextCoworkers[nextIndex]] = [
      nextCoworkers[nextIndex],
      nextCoworkers[selectedIndex],
    ];
    setConfig({ ...config, coworkers: nextCoworkers });
    setSelectedCoworkerIndex(nextIndex);
    setSelectedCoworkerId(nextCoworkers[nextIndex]?.coworker_id ?? "");
    markDirty();
  }

  function addCoworker() {
    const coworker = nextCoworker(coworkers, (index) => t("coworker.defaultNameIndexed", { index }));
    setConfig({ ...config, coworkers: [...coworkers, coworker] });
    setSelectedCoworkerId(coworker.coworker_id);
    setSelectedCoworkerIndex(coworkers.length);
    setView("config");
    markDirty();
  }

  function removeSelectedCoworker() {
    if (coworkers.length <= 1) {
      showInlineNotice(t("validation.atLeastOneCoworker"), "warning");
      return;
    }
    const nextCoworkers = coworkers.filter((_, index) => index !== selectedIndex);
    setConfig({ ...config, coworkers: nextCoworkers });
    setSelectedCoworkerId(nextCoworkers[0]?.coworker_id ?? "");
    setSelectedCoworkerIndex(0);
    markDirty();
  }

  function fieldError(path: string) {
    return issues.find((issue) => issue.path === path);
  }

  const approvalConfig = resolvedApprovalConfig(config);
  const isFullAccessDirect =
    approvalConfig.permissionsMode === "danger-full-access" && approvalConfig.approvalsReviewer === "none";
  const isCoworkerReview = approvalConfig.approvalsReviewer === "coworker";
  const hasVisibleIssues = bootstrapPhase === "ready" && issues.length > 0;
  const visibleInlineNotice: InlineNotice | null = hasVisibleIssues
    ? {
        tone: "warning",
        text: t("validation.issuesNeedAttentionShort", { count: issues.length }),
      }
    : inlineNotice;
  const updatePercent =
    desktopUpdateContentLength && desktopUpdateContentLength > 0
      ? Math.min(100, Math.round((desktopUpdateDownloaded / desktopUpdateContentLength) * 100))
      : null;
  const updateNoticeText =
    desktopUpdateState === "available" && desktopUpdate
      ? t("update.notice.available", { version: desktopUpdate.version })
      : desktopUpdateState === "checking"
        ? t("update.notice.checking")
        : desktopUpdateState === "downloading" && desktopUpdate
          ? t("update.notice.downloading", {
              version: `${desktopUpdate.version}${updatePercent === null ? "" : ` (${updatePercent}%)`}`,
            })
          : desktopUpdateState === "installed"
            ? t("update.notice.installed")
            : desktopUpdateState === "error"
              ? desktopUpdateError || t("update.error.generic")
              : "";
  const lifePanelToggleLabel = isLifePanelCollapsed ? t("aria.expandLifePanel") : t("aria.collapseLifePanel");
  const activeViewLabel =
    view === "status"
      ? t("nav.status")
      : view === "config"
        ? t("nav.config")
        : view === "sessions"
          ? t("nav.sessions")
          : t("nav.logs");

  return (
    <main
      className="shell"
      data-sidebar={isLifePanelCollapsed ? "collapsed" : "expanded"}
      data-mood={moodKey}
      style={{ "--active-color": moodColor[moodKey] } as CSSProperties}
    >
      <aside className="lifePanel" aria-label="Bridge life signs and Coworker roster">
        <div className="lifePanelHeader">
          <div className="brandBlock">
            <span className="brandMark" aria-hidden="true">
              <Sparkles size={18} />
            </span>
            <span className="brandCopy">
              <h1>{t("brand.eyebrow")}</h1>
              <small>{t("brand.title")}</small>
            </span>
          </div>
          <button
            className="iconButton lifePanelToggle"
            onClick={() => setIsLifePanelCollapsed((current) => !current)}
            title={lifePanelToggleLabel}
            aria-label={lifePanelToggleLabel}
            aria-expanded={!isLifePanelCollapsed}
            type="button"
          >
            <span className="sidebarToggleGlyph" aria-hidden="true">
              <PanelLeftClose className="sidebarToggleClose" size={17} />
              <PanelLeftOpen className="sidebarToggleOpen" size={17} />
            </span>
          </button>
        </div>

        <section className="identityCard" aria-label={t("aria.bridgeRuntimeStatus")}>
          <div className="identityCardRow">
            <div className="statusOrb" aria-hidden="true">
              <HeartPulse size={22} />
            </div>
            <div className="runtimeSummary">
              <strong>{t(`mood.${moodKey}.label`)}</strong>
              {view !== "status" && <p>{status?.last_error ?? t(`mood.${moodKey}.desc`)}</p>}
            </div>
          </div>
        </section>

        <nav className="segmentedNav sideNav" aria-label={t("aria.desktopBridgeViews")}>
          <button
            aria-current={view === "status" ? "page" : undefined}
            aria-label={t("nav.status")}
            className={view === "status" ? "active" : ""}
            onClick={() => setView("status")}
          >
            <Gauge size={17} /> <span>{t("nav.status")}</span>
          </button>
          <button
            aria-current={view === "config" ? "page" : undefined}
            aria-label={t("nav.config")}
            className={view === "config" ? "active" : ""}
            onClick={() => setView("config")}
          >
            <Settings2 size={17} /> <span>{t("nav.config")}</span>
          </button>
          <button
            aria-current={view === "sessions" ? "page" : undefined}
            aria-label={t("nav.sessions")}
            className={view === "sessions" ? "active" : ""}
            onClick={() => setView("sessions")}
          >
            <MessagesSquare size={17} /> <span>{t("nav.sessions")}</span>
          </button>
          <button
            aria-current={view === "logs" ? "page" : undefined}
            aria-label={t("nav.logs")}
            className={view === "logs" ? "active" : ""}
            onClick={() => setView("logs")}
          >
            <ScrollText size={17} /> <span>{t("nav.logs")}</span>
          </button>
        </nav>

        <section className="rosterPanel">
          <div className="sectionHead">
            <div>
              <p className="eyebrow">{t("roster.eyebrow")}</p>
              <h2>{t("roster.title")}</h2>
            </div>
            <button className="iconButton" onClick={addCoworker} title={t("roster.addCoworker")} aria-label={t("roster.addCoworker")}>
              <Plus size={16} />
            </button>
          </div>
          <div className="coworkerList" role="list">
            {visibleCoworkers.map((coworker) => {
              const diag = diagnosticsForCoworker(diagnostics, coworker);
              const health = diag ? (diag.ok ? "ok" : "bad") : "unknown";
              const coworkerName = coworker.display_name || coworker.coworker_id;
              const coworkerLabel = `${coworkerName}, ${t("common.id")} ${coworker.coworker_id}`;
              return (
                <button
                  className={coworker.coworker_id === operationalCoworkerId ? "coworkerItem active" : "coworkerItem"}
                  key={coworker.coworker_id}
                  onClick={() => {
                    setSelectedCoworkerId(coworker.coworker_id);
                    const index = coworkers.findIndex((configured) => configured.coworker_id === coworker.coworker_id);
                    if (index >= 0) setSelectedCoworkerIndex(index);
                  }}
                  aria-pressed={coworker.coworker_id === operationalCoworkerId}
                  aria-label={coworkerLabel}
                  title={coworkerLabel}
                >
                  <span className={`healthDot ${health}`} aria-hidden="true" />
                  <span>
                    <strong>{coworkerName}</strong>
                    <small>{coworker.coworker_id}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        <button
          className="onboardingEntry"
          onClick={() => setOnboardingOpen(true)}
          title={t("aria.openOnboarding")}
          aria-label={t("aria.openOnboarding")}
          type="button"
        >
          <BookOpen size={14} />
          <span>{t("aria.openOnboarding")}</span>
        </button>

        <footer
          className="appVersion"
          aria-label={`CoWorker Desktop version ${appMetadata.version}`}
          title={`CoWorker Desktop v${appMetadata.version}`}
        >
          v{appMetadata.version}
        </footer>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="titleBlock">
            <p className="eyebrow">{t("topbar.eyebrow")}</p>
            <div className="titleLine">
              <h2>{activeViewLabel}</h2>
              {view !== "status" && (
                <span className="contextChip" title={operationalCoworker?.base_url}>
                  <i className={`healthDot ${bridgeRunning ? "ok" : ""}`} aria-hidden="true" />
                  {operationalCoworker?.display_name ?? t("topbar.noCoworkerSelected")}
                </span>
              )}
            </div>
          </div>

          {status?.development_mode && !developmentWarningDismissed && (
            <div className="developmentWarning" role="status" title={t("development.detail")}>
              <span>{t("development.notice")}</span>
              <button onClick={() => setDevelopmentWarningDismissed(true)} aria-label={t("development.dismiss")} type="button"><X size={13} /></button>
            </div>
          )}

          <div className="toolbar" aria-label={t("aria.bridgeActions")}>
            <button
              className="langToggle"
              onClick={() => setLang(lang === "en" ? "zh" : "en")}
              title={t("aria.toggleLanguage")}
              aria-label={t("aria.toggleLanguage")}
              type="button"
            >
              {lang === "en" ? "中文" : "EN"}
            </button>
            <button title={t("common.refresh")} aria-label={t("common.refresh")} onClick={() => refresh().catch(reportError)}>
              <RefreshCw size={16} />
            </button>
            <button
              title={t("update.checkForUpdates")}
              aria-label={t("aria.checkForDesktopUpdates")}
              onClick={() => checkForDesktopUpdate(true)}
              disabled={desktopUpdateState === "checking" || desktopUpdateState === "downloading"}
            >
              <ArrowDownToLine size={16} />
            </button>
            {(view === "config" || isDirty) && (
              <>
                {isDirty && (
                  <button
                    className="discardAction"
                    title={t("config.discardChanges")}
                    aria-label={t("aria.discardConfigurationChanges")}
                    onClick={discardConfigChanges}
                  >
                    <RotateCcw size={16} />
                    <span>{t("common.cancel")}</span>
                  </button>
                )}
                <button
                  className={isDirty ? "saveAction pending" : "saveAction"}
                  title={t("aria.saveConfiguration")}
                  aria-label={t("aria.saveConfiguration")}
                  onClick={() => persist().catch(reportError)}
                >
                  <Save size={16} />
                  <span>{t("config.saveAndApply")}</span>
                </button>
              </>
            )}
            {bridgeRunning ? (
              <button
                className="runtimeAction stopAction"
                title={t("common.stop")}
                aria-label={t("aria.stopBridge")}
                onClick={() => stop().catch(reportError)}
              >
                <Square size={14} />
                <span>{t("common.stop")}</span>
              </button>
            ) : (
              <button
                className="primaryAction runtimeAction"
                title={t("common.start")}
                aria-label={t("aria.startBridge")}
                onClick={() => start().catch(reportError)}
              >
                <Play size={15} />
                <span>{t("common.start")}</span>
              </button>
            )}
          </div>
        </header>

        <div className="workspaceNotices">
          {visibleInlineNotice && (
            <div
              className={`notice notice-${visibleInlineNotice.tone}`}
              aria-live={visibleInlineNotice.tone === "error" ? "assertive" : "polite"}
              role={visibleInlineNotice.tone === "error" ? "alert" : "status"}
            >
              <FeedbackIcon tone={visibleInlineNotice.tone} />
              <span>{visibleInlineNotice.text}</span>
              {!hasVisibleIssues && (
                <button className="noticeClose" onClick={() => setInlineNotice(null)} type="button" aria-label={t("aria.dismissNotice")}>
                  <X size={15} />
                </button>
              )}
            </div>
          )}

          {updateNoticeText && (
            <div
              className={desktopUpdateState === "error" ? "notice notice-error updateNotice" : "notice notice-info updateNotice"}
              aria-live={desktopUpdateState === "error" ? "assertive" : "polite"}
              role={desktopUpdateState === "error" ? "alert" : "status"}
            >
              <FeedbackIcon tone={desktopUpdateState === "error" ? "error" : "info"} />
              <span>{updateNoticeText}</span>
              {desktopUpdateState === "available" && (
                <button className="softButton" onClick={() => installUpdate().catch(() => undefined)}>
                  <ArrowDownToLine size={15} /> {t("update.install")}
                </button>
              )}
            </div>
          )}
        </div>

        <div className="workspaceContent">

        {view === "status" && (
          <StatusView
            status={status}
            bootstrapPhase={bootstrapPhase}
            isDirty={isDirty}
            diagnostics={diagnostics}
            diagnosticsRunning={diagnosticsRunning}
            selectedCoworker={operationalCoworker}
            config={config}
            configPath={configPath}
            configurationReady={issues.length === 0}
            onOpenSettings={() => setView("config")}
            onRefresh={() => refresh().catch(reportError)}
            onRunDiagnostics={() => diagnose().catch(reportError)}
          />
        )}

        {view === "config" && (
          <ConfigView
            configPath={configPath}
            setConfigPath={setConfigPath}
            config={config}
            isDirty={isDirty}
            fieldError={fieldError}
            updateConfig={updateConfig}
            updateCodexId={updateCodexId}
            desktopUpdateUrlPlaceholder={desktopUpdateUrlPlaceholder}
            onChooseConfigFile={() => chooseConfigFile().catch(reportError)}
            onChooseChatWorkspacesDir={() => chooseChatWorkspacesDir()}
            approvalConfig={approvalConfig}
            updateApprovalConfig={updateApprovalConfig}
            isFullAccessDirect={isFullAccessDirect}
            isCoworkerReview={isCoworkerReview}
            coworkers={coworkers}
            selectedIndex={selectedIndex}
            selectedCoworker={selectedCoworker}
            onSelectCoworker={(index) => {
              setSelectedCoworkerIndex(index);
              setSelectedCoworkerId(coworkers[index]?.coworker_id ?? "");
            }}
            updateCoworker={updateCoworker}
            onMoveSelectedCoworker={moveSelectedCoworker}
            onAddCoworker={addCoworker}
            onRemoveSelectedCoworker={removeSelectedCoworker}
            selectedRegistrations={selectedRegistrations}
            selectedClientId={selectedClientId}
            onRefreshRegistrations={() => refreshRegistrations().catch(reportError)}
            onRemoveRegistration={(registration) => removeRegistration(registration).catch(reportError)}
          />
        )}

        <ConversationsWorkspace
          active={view === "sessions"}
          configPath={configPath}
          status={status}
          coworkers={visibleCoworkers}
          selectedCoworkerId={operationalCoworkerId}
          onSelectCoworker={(coworkerId) => {
            setSelectedCoworkerId(coworkerId);
            const index = coworkers.findIndex((coworker) => coworker.coworker_id === coworkerId);
            if (index >= 0) setSelectedCoworkerIndex(index);
          }}
          feedback={conversationFeedback}
          onActiveConversationChange={(activeConversation) => {
            activeConversationRef.current = activeConversation;
          }}
        />

        {desktopApprovals[0] && (
          <ApprovalPanel
            approval={desktopApprovals[0]}
            busy={resolvingApproval}
            queueCount={desktopApprovals.length - 1}
            onResolve={(response) => resolveApproval(desktopApprovals[0], response)}
          />
        )}

        {view === "logs" && (
          <>
          <LogsView
            ledgerRef={ledgerRef}
            logParsePending={logParsePending}
            logEntries={logEntries}
            filteredLogEntries={filteredLogEntries}
            renderedLogEntries={renderedLogEntries}
            hiddenLogEntryCount={hiddenLogEntryCount}
            logLevelCounts={logLevelCounts}
            logLevelFilter={logLevelFilter}
            setLogLevelFilter={setLogLevelFilter}
            liveLogs={liveLogs}
            setLiveLogs={setLiveLogs}
            followLatest={followLatest}
            setFollowLatest={setFollowLatest}
            logOutputLevel={config.file_log_level ?? config.log_level}
            logLevelUpdating={logLevelUpdating}
            setLogOutputLevel={(level: LogOutputLevel) => applyLogOutputLevel(level).catch(reportError)}
            onRefreshLog={() => readBridgeLog(configPath).then((text) => setLog(clampLogText(text))).catch(reportError)}
            onSelectEntry={setSelectedLogEntry}
          />
          {selectedLogEntry && (
            <LogDetailDialog
              entry={selectedLogEntry}
              onClose={() => setSelectedLogEntry(null)}
              onCopied={(success) =>
                showToast(success ? t("logs.copyDone") : t("logs.copyFailed"), success ? "success" : "error")
              }
            />
          )}
          </>
        )}
        </div>
      </section>

      {toasts.length > 0 && (
        <div className="toastStack" aria-live="polite" aria-label="Notifications">
          {toasts.map((toast) => (
            <div className={`toast toast-${toast.tone}`} key={toast.id} role="status">
              <FeedbackIcon tone={toast.tone} />
              <span>{toast.text}</span>
              <button className="toastClose" onClick={() => dismissToast(toast.id)} type="button" aria-label={t("aria.dismissNotice")}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      <OnboardingWizard
        open={onboardingOpen}
        onClose={() => closeOnboarding()}
        onFinished={() => closeOnboarding()}
        config={config}
        selectedCoworker={selectedCoworker}
        selectedIndex={selectedIndex}
        approvalConfig={approvalConfig}
        fieldError={fieldError}
        updateConfig={updateConfig}
        updateCodexId={updateCodexId}
        updateCoworker={updateCoworker}
        updateApprovalConfig={updateApprovalConfig}
        onChooseChatWorkspacesDir={() => chooseChatWorkspacesDir()}
        coworkers={coworkers}
        desktopUpdateUrlPlaceholder={desktopUpdateUrlPlaceholder}
        issuesCount={issues.length}
        onSave={() => saveFromOnboarding(false)}
        onSaveAndStart={() => saveFromOnboarding(true)}
      />
    </main>
  );
}
