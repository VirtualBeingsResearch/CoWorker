import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App, shouldNotifyActorEvent } from "./App";
import { LanguageProvider } from "./i18n";
import * as tauri from "./tauri";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";
import type {
  BridgeStatus,
  ActorConversation,
  ActorMessage,
  ActorStreamEvent,
  CommunicateRegistration,
  ConfigInfo,
  ConfigValue,
  DiagnosticResult,
  DesktopApproval,
  DesktopUpdateDownloadEvent,
} from "./tauri";

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(),
  save: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-notification", () => ({
  isPermissionGranted: vi.fn(),
  requestPermission: vi.fn(),
  sendNotification: vi.fn(),
}));

vi.mock("./tauri", () => ({
  getConfigInfo: vi.fn(),
  getBridgeStatus: vi.fn(),
  readBridgeLog: vi.fn(),
  getDefaultDesktopUpdateUrl: vi.fn(),
  saveConfig: vi.fn(),
  startBridge: vi.fn(),
  stopBridge: vi.fn(),
  runDiagnostics: vi.fn(),
  checkDesktopUpdate: vi.fn(),
  installDesktopUpdate: vi.fn(),
  listCommunicateRegistrations: vi.fn(),
  deleteCommunicateRegistration: vi.fn(),
  listDesktopConversations: vi.fn(),
  listDesktopApprovals: vi.fn(),
  resolveDesktopApproval: vi.fn(),
  loadDesktopMessages: vi.fn(),
  sendDesktopMessage: vi.fn(),
  sendDesktopCoworkerMessage: vi.fn(),
  setDesktopConversationMode: vi.fn(),
  renameDesktopConversation: vi.fn(),
  copyDesktopAttachment: vi.fn(),
  startDesktopFileDrag: vi.fn(),
  listenActorStreamEvents: vi.fn(),
  listenDesktopFileDrops: vi.fn(),
  listenBridgeLogChunks: vi.fn(),
  startBridgeLogStream: vi.fn(),
  stopBridgeLogStream: vi.fn(),
  setCloseToTray: vi.fn(),
  setTrayCopy: vi.fn(),
}));

const baseConfig: ConfigValue = {
  codex_id: "codex-local",
  display_name: "Local Codex",
  command: "codex",
  args: ["app-server"],
  chat_workspaces_dir: "D:\\chats",
  desktop_update_url: "http://updates.local",
  permissions_mode: "read-only",
  approvals_reviewer: "none",
  approval_timeout_seconds: 300,
  coworkers: [
    { coworker_id: "cw_01", display_name: "Partner One", base_url: "http://localhost:8001" },
    { coworker_id: "cw_02", display_name: "Partner Two", base_url: "http://localhost:8002" },
  ],
};

const stoppedStatus: BridgeStatus = {
  state: "stopped",
  config_path: null,
  codex_id: null,
  coworkers: [],
  actors: [],
  last_error: null,
};

const runningStatus: BridgeStatus = {
  state: "running",
  config_path: "coworker_desktop.json",
  codex_id: "codex-local",
  coworkers: baseConfig.coworkers ?? [],
  actors: [],
  last_error: null,
};

const session: ActorConversation = {
  actor_id: "codex",
  conversation_id: "thread-1",
  title: "Bridge thread",
  project_path: "D:\\Projects\\coworker",
  updated_at: "2026-07-06T10:00:00Z",
  writable: true,
  mode: "default",
};

let actorStreamHandlers: Array<(event: ActorStreamEvent) => void> = [];
let desktopFileDropHandlers: Array<(event: { type: "enter" | "over" | "drop" | "leave"; paths?: string[] }) => void> = [];

const assistantMessage: ActorMessage = {
  id: "msg-1",
  actor_id: "codex",
  conversation_id: "thread-1",
  created_at: "2026-07-06T10:00:00Z",
  author_kind: "assistant",
  content: "Ready",
  metadata: {
    author_id: "codex",
    author_label: "Codex",
    kind: "message",
    attachments: [{
      filename: "summary.md",
      media_type: "text/markdown",
      size: 12,
      path: "D:\\tmp\\summary.md",
      downloadable: true,
      reason: null,
    }],
    turn_id: null,
    item_id: null,
    streaming: false,
  },
};

function configInfo(config: ConfigValue = baseConfig): ConfigInfo {
  return {
    config,
    exists: true,
    modified_ms: 123,
  };
}

function setDefaultMocks(status: BridgeStatus = stoppedStatus, config: ConfigValue = baseConfig) {
  actorStreamHandlers = [];
  desktopFileDropHandlers = [];
  vi.mocked(tauri.getConfigInfo).mockResolvedValue(configInfo(config));
  vi.mocked(tauri.getBridgeStatus).mockResolvedValue(status);
  vi.mocked(tauri.readBridgeLog).mockResolvedValue("2026-07-06T10:00:00Z INFO coworker_desktop_app: booted");
  vi.mocked(tauri.getDefaultDesktopUpdateUrl).mockResolvedValue("http://placeholder.local");
  vi.mocked(tauri.saveConfig).mockImplementation(async (_path, nextConfig) => configInfo(nextConfig));
  vi.mocked(tauri.startBridge).mockResolvedValue(runningStatus);
  vi.mocked(tauri.stopBridge).mockResolvedValue(stoppedStatus);
  vi.mocked(tauri.runDiagnostics).mockResolvedValue([]);
  vi.mocked(tauri.checkDesktopUpdate).mockResolvedValue(null);
  vi.mocked(tauri.installDesktopUpdate).mockImplementation(async () => undefined);
  vi.mocked(tauri.listCommunicateRegistrations).mockResolvedValue([]);
  vi.mocked(tauri.deleteCommunicateRegistration).mockImplementation(async (_baseUrl, registrationId) => registration(registrationId));
  vi.mocked(tauri.listDesktopConversations).mockImplementation(async (actorId) => actorId === "codex" ? [session] : []);
  vi.mocked(tauri.listDesktopApprovals).mockResolvedValue([]);
  vi.mocked(tauri.resolveDesktopApproval).mockResolvedValue({ ok: true });
  vi.mocked(tauri.loadDesktopMessages).mockImplementation(async (_path, actorId) => ({
    messages: actorId === "codex" ? [assistantMessage] : [],
    next_before_cursor: null,
  }));
  vi.mocked(tauri.sendDesktopMessage).mockResolvedValue({ conversation_id: "thread-1" });
  vi.mocked(tauri.sendDesktopCoworkerMessage).mockResolvedValue({ conversation_id: "thread-1" });
  vi.mocked(tauri.setDesktopConversationMode).mockResolvedValue({});
  vi.mocked(tauri.renameDesktopConversation).mockResolvedValue({});
  vi.mocked(tauri.copyDesktopAttachment).mockResolvedValue({});
  vi.mocked(tauri.startDesktopFileDrag).mockResolvedValue(undefined);
  vi.mocked(tauri.listenActorStreamEvents).mockImplementation(async (handler) => {
    actorStreamHandlers.push(handler);
    return () => undefined;
  });
  vi.mocked(tauri.listenDesktopFileDrops).mockImplementation(async (handler) => {
    desktopFileDropHandlers.push(handler as (event: { type: "enter" | "over" | "drop" | "leave"; paths?: string[] }) => void);
    return () => undefined;
  });
  vi.mocked(tauri.listenBridgeLogChunks).mockResolvedValue(vi.fn());
  vi.mocked(tauri.startBridgeLogStream).mockResolvedValue(undefined);
  vi.mocked(tauri.stopBridgeLogStream).mockResolvedValue(undefined);
  vi.mocked(tauri.setCloseToTray).mockResolvedValue(undefined);
  vi.mocked(tauri.setTrayCopy).mockResolvedValue(undefined);
  vi.mocked(openDialog).mockResolvedValue(null);
  vi.mocked(saveDialog).mockResolvedValue(null);
  vi.mocked(isPermissionGranted).mockResolvedValue(true);
  vi.mocked(requestPermission).mockResolvedValue("granted");
  vi.mocked(sendNotification).mockClear();
  // Existing suites assume the onboarding tutorial does not auto-open.
  window.localStorage.setItem("coworker-desktop-onboarding-completed", "true");
}

function registration(registrationId = "reg-1"): CommunicateRegistration {
  return {
    registration_id: registrationId,
    participant_id: "coworker-desktop:desktop-local:codex:cw_01:abc123",
    kind: "coworker-desktop",
    client_id: "desktop-local:codex:cw_01",
    display_name: "Partner One",
    active: false,
    created_at: "2026-07-06T10:00:00Z",
    last_registered_at: "2026-07-06T10:00:00Z",
    metadata: { coworker_id: "cw_01", actor_id: "codex" },
  };
}

async function renderApp(status: BridgeStatus = stoppedStatus, config: ConfigValue = baseConfig) {
  setDefaultMocks(status, config);
  const user = userEvent.setup();
  render(
    <LanguageProvider>
      <App />
    </LanguageProvider>,
  );
  await waitFor(() => expect(tauri.getConfigInfo).toHaveBeenCalledWith("coworker_desktop.json"));
  await waitFor(() => expect(screen.getAllByText("codex-local").length).toBeGreaterThan(0));
  return user;
}

async function openConfig(user: ReturnType<typeof userEvent.setup>) {
  const _user = user;
  const nav = screen.getByRole("navigation", { name: "Desktop Bridge views" });
  fireEvent.click(within(nav).getAllByRole("button")[1]);
  await waitFor(() => expect(document.querySelector("#config-path")).toBeInTheDocument());
  void _user;
}

async function openSessions(user: ReturnType<typeof userEvent.setup>) {
  const _user = user;
  const nav = screen.getByRole("navigation", { name: "Desktop Bridge views" });
  fireEvent.click(within(nav).getAllByRole("button")[2]);
  await waitFor(() => expect(tauri.listDesktopConversations).toHaveBeenCalledWith("codex", "coworker_desktop.json", 120));
  await waitFor(() => expect(screen.getAllByText("Bridge thread").length).toBeGreaterThan(0));
  void _user;
}

function inputById(id: string) {
  const input = document.querySelector<HTMLInputElement>(`#${id}`);
  if (!input) throw new Error(`Missing input #${id}`);
  return input;
}

describe("App backend operation wiring", () => {
  it("syncs tray copy with the selected language", async () => {
    window.localStorage.setItem("coworker-desktop-lang", "en");
    const user = await renderApp();

    await waitFor(() => expect(tauri.setTrayCopy).toHaveBeenCalledWith({
      tooltip: "CoWorker Desktop",
      open: "Open",
      hide: "Hide to Tray",
      quit: "Quit",
    }));

    await user.click(screen.getByRole("button", { name: "Toggle language" }));
    await waitFor(() => expect(tauri.setTrayCopy).toHaveBeenLastCalledWith({
      tooltip: "CoWorker 桌面端",
      open: "打开",
      hide: "隐藏到托盘",
      quit: "退出",
    }));
    window.localStorage.setItem("coworker-desktop-lang", "en");
  });

  it("lets closing the main window exit instead of always hiding to the tray", async () => {
    const user = await renderApp();
    await waitFor(() => expect(tauri.setCloseToTray).toHaveBeenCalledWith(true));
    await openConfig(user);

    await user.click(inputById("close-to-tray"));

    await waitFor(() => expect(tauri.setCloseToTray).toHaveBeenLastCalledWith(false));
  });

  it("reorders connection profiles", async () => {
    const user = await renderApp();
    await openConfig(user);
    await user.click(screen.getByRole("tab", { name: "Partner Two" }));

    await user.click(screen.getByRole("button", { name: "Move selected profile earlier" }));

    const tabs = within(screen.getByRole("tablist", { name: "Connection profiles" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Partner Two", "Partner One"]);
  });

  it("only sends native message notifications while the window is in the background", async () => {
    const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(true);
    await renderApp(runningStatus);
    vi.mocked(sendNotification).mockClear();

    act(() => actorStreamHandlers.forEach((handler) => handler({
      actor_id: "local",
      conversation_id: "local-thread",
      message_id: "incoming-local-foreground",
      event: { type: "conversation_updated" },
    })));
    expect(sendNotification).not.toHaveBeenCalled();
    expect(await screen.findByText("New Local message")).toBeInTheDocument();

    hasFocus.mockReturnValue(false);
    act(() => actorStreamHandlers.forEach((handler) => handler({
      actor_id: "local",
      conversation_id: "local-thread",
      message_id: "incoming-local-background",
      event: { type: "conversation_updated" },
    })));

    await waitFor(() => expect(sendNotification).toHaveBeenCalledWith({
      title: "New Local message",
      body: "Open CoWorker Desktop to view the conversation.",
    }));
    hasFocus.mockRestore();
  });

  it("notifies for Coworker writes and terminal results but ignores Codex intermediate events", () => {
    const notifiedIds = new Set<string>();
    const event = (
      messageId: string,
      authorKind: string,
      kind: string,
      streaming = false,
    ): ActorStreamEvent => ({
      actor_id: "codex",
      conversation_id: "thread-1",
      message_id: messageId,
      event: {
        type: "conversation_updated",
        message: {
          author_kind: authorKind,
          metadata: { kind, streaming },
        },
      },
    });

    expect(shouldNotifyActorEvent(event("coworker-1", "coworker", "message"), notifiedIds)).toBe(true);
    expect(shouldNotifyActorEvent(event("final-1", "codex", "message"), notifiedIds)).toBe(true);
    expect(shouldNotifyActorEvent(event("tool-1", "tool", "tool_call"), notifiedIds)).toBe(false);
    expect(shouldNotifyActorEvent(event("reasoning-1", "codex", "reasoning"), notifiedIds)).toBe(false);
    expect(shouldNotifyActorEvent(event("stream-1", "codex", "message", true), notifiedIds)).toBe(false);
  });

  it("does not toast for the conversation currently visible in the focused window", async () => {
    const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(true);
    const user = await renderApp(runningStatus);
    await openSessions(user);
    await user.click(await screen.findByRole("button", { name: /Bridge thread/ }));

    act(() => actorStreamHandlers.forEach((handler) => handler({
      actor_id: "codex",
      conversation_id: "thread-1",
      message_id: "coworker-active-thread",
      event: {
        type: "conversation_updated",
        message: {
          author_kind: "coworker",
          metadata: { kind: "message", streaming: false },
        },
      },
    })));

    expect(screen.queryByText("New Codex message")).not.toBeInTheDocument();
    expect(sendNotification).not.toHaveBeenCalled();
    hasFocus.mockRestore();
  });

  it("refreshes approvals from lifecycle events without polling", async () => {
    const user = await renderApp(runningStatus);
    await waitFor(() => expect(tauri.listDesktopApprovals).toHaveBeenCalledTimes(1));
    const approval: DesktopApproval = {
      request_id: "approval-1",
      actor_id: "claude",
      conversation_id: "session-1",
      coworker_id: "cw_02",
      owner_id: "desktop-local",
      tool_name: "Write",
      input: { file_path: "D:\\Projects\\coworker\\result.txt" },
      status: "pending",
      expires_at: "2099-07-14T12:00:00Z",
    };
    vi.mocked(tauri.listDesktopApprovals).mockResolvedValue([approval]);

    act(() => actorStreamHandlers.forEach((handler) => handler({
      actor_id: "claude",
      conversation_id: "session-1",
      message_id: null,
      event: { type: "desktop.approval.requested", request_id: "approval-1" },
    })));

    expect(await screen.findByRole("dialog", { name: "Permission request" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activity" }));
    expect(document.querySelector(".logPanel")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Permission request" })).toBeInTheDocument();
    vi.mocked(tauri.listDesktopApprovals).mockResolvedValue([]);
    act(() => actorStreamHandlers.forEach((handler) => handler({
      actor_id: "claude",
      conversation_id: "session-1",
      message_id: null,
      event: { type: "desktop.approval.changed", request_id: "approval-1" },
    })));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Permission request" })).not.toBeInTheDocument());
    expect(tauri.listDesktopApprovals).toHaveBeenCalledTimes(3);
  });

  it("loads config, status, log, and default update url on startup", async () => {
    await renderApp();

    expect(tauri.getBridgeStatus).toHaveBeenCalled();
    expect(tauri.readBridgeLog).toHaveBeenCalledWith("coworker_desktop.json");
    expect(tauri.getDefaultDesktopUpdateUrl).toHaveBeenCalled();
    expect(tauri.checkDesktopUpdate).toHaveBeenCalledTimes(1);
    expect(tauri.checkDesktopUpdate).toHaveBeenCalledWith("http://updates.local");
    expect(screen.getAllByText("Partner One").length).toBeGreaterThan(0);
    expect(screen.getAllByText("localhost:8001").length).toBeGreaterThan(0);
    expect(screen.getByTitle("http://localhost:8001")).toBeInTheDocument();
    expect(document.querySelector(".unifiedSessions")).toBeNull();
  });

  it("guides an unconfigured runtime to settings without rendering an empty actor panel", async () => {
    const unconfiguredStatus: BridgeStatus = {
      state: "stopped",
      config_path: null,
      codex_id: null,
      coworkers: [],
      actors: [],
      last_error: null,
    };
    setDefaultMocks(unconfiguredStatus, {});
    const user = userEvent.setup();
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Finish setup" })).toBeInTheDocument();
    expect(screen.queryByText("Desktop availability")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open settings" }));
    await waitFor(() => expect(document.querySelector("#config-path")).toBeInTheDocument());
  });

  it("does not downgrade readiness for an intentionally disabled actor", async () => {
    await renderApp(
      {
        ...runningStatus,
        actors: [{ actor_id: "claude", available: false, message: "Claude actor is disabled" }],
      },
      {
        ...baseConfig,
        actors: { claude: { enabled: false } },
      },
    );

    expect(screen.getByRole("heading", { name: "Ready" })).toBeInTheDocument();
    expect(screen.getByText("All disabled")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.queryByText("Claude actor is disabled")).not.toBeInTheDocument();
  });

  it("saves edited config with normalized coworkers", async () => {
    const user = await renderApp(undefined, {
      ...baseConfig,
      actors: { claude: { enabled: true, command: "claude" } },
    });
    await openConfig(user);

    fireEvent.change(inputById("codex-id"), { target: { value: "codex-edited" } });
    fireEvent.change(inputById("codex-command"), { target: { value: "custom-codex" } });
    fireEvent.change(inputById("claude-command"), { target: { value: "custom-claude" } });
    fireEvent.change(inputById("coworker-id"), { target: { value: "cw_ops" } });
    fireEvent.change(inputById("coworker-url"), { target: { value: "http://localhost:9001" } });
    vi.mocked(tauri.saveConfig).mockClear();

    await user.click(screen.getByRole("button", { name: "Save and apply configuration" }));

    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.saveConfig).toHaveBeenCalledWith(
      "coworker_desktop.json",
      expect.objectContaining({
        codex_id: "codex-edited",
        command: "custom-codex",
        actors: expect.objectContaining({
          codex: expect.objectContaining({ command: "custom-codex" }),
          claude: expect.objectContaining({ enabled: true, command: "custom-claude" }),
        }),
        coworkers: expect.arrayContaining([
          expect.objectContaining({ coworker_id: "cw_ops", base_url: "http://localhost:9001" }),
        ]),
      }),
    );
  });

  it("disables a coworker while keeping one enabled and removes it from session targets", async () => {
    const user = await renderApp();
    await openConfig(user);

    const tabs = screen.getByRole("tablist", { name: "Connection profiles" });
    await user.click(within(tabs).getByRole("tab", { name: "Partner Two" }));
    const enabledToggle = inputById("coworker-enabled");
    expect(enabledToggle).toBeChecked();
    expect(enabledToggle).not.toBeDisabled();

    await user.click(enabledToggle);
    expect(enabledToggle).not.toBeChecked();
    expect(within(tabs).getByText("disabled")).toBeInTheDocument();

    await user.click(within(tabs).getByRole("tab", { name: "Partner One" }));
    expect(inputById("coworker-enabled")).toBeDisabled();
    await user.click(within(tabs).getByRole("tab", { name: /Partner Two/ }));

    vi.mocked(tauri.saveConfig).mockClear();
    await user.click(screen.getByRole("button", { name: "Save and apply configuration" }));
    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.saveConfig).toHaveBeenCalledWith(
      "coworker_desktop.json",
      expect.objectContaining({
        coworkers: expect.arrayContaining([
          expect.objectContaining({ coworker_id: "cw_01", enabled: true }),
          expect.objectContaining({ coworker_id: "cw_02", enabled: false }),
        ]),
      }),
    );

    await openSessions(user);
    await user.click(screen.getByRole("button", { name: "Local" }));
    const coworkerSelect = await screen.findByRole("combobox", { name: "Choose Coworker" });
    expect(within(coworkerSelect).getByRole("option", { name: "Partner One" })).toBeInTheDocument();
    expect(within(coworkerSelect).queryByRole("option", { name: "Partner Two" })).not.toBeInTheDocument();
  });

  it("defaults the log output control to info and saves the selected level", async () => {
    const user = await renderApp();
    await openConfig(user);

    const levels = screen.getByRole("radiogroup", { name: "Log output level" });
    expect(within(levels).getByRole("radio", { name: "Info" })).toHaveAttribute("aria-checked", "true");

    await user.click(within(levels).getByRole("radio", { name: "Debug" }));
    vi.mocked(tauri.saveConfig).mockClear();
    await user.click(screen.getByRole("button", { name: "Save and apply configuration" }));

    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.saveConfig).toHaveBeenCalledWith(
      "coworker_desktop.json",
      expect.objectContaining({ log_level: "DEBUG", file_log_level: "DEBUG" }),
    );
  });

  it("applies and saves output level changes directly from the logs page", async () => {
    const user = await renderApp();
    const nav = screen.getByRole("navigation", { name: "Desktop Bridge views" });
    fireEvent.click(within(nav).getAllByRole("button")[3]);

    const outputLevels = await screen.findByRole("radiogroup", { name: "Output level" });
    vi.mocked(tauri.saveConfig).mockClear();
    await user.click(within(outputLevels).getByRole("radio", { name: "Warn" }));

    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.saveConfig).toHaveBeenCalledWith(
      "coworker_desktop.json",
      expect.objectContaining({ log_level: "WARN", file_log_level: "WARN" }),
    );
    expect(await screen.findByText("Log output level changed to WARN")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save and apply configuration" })).not.toBeInTheDocument();
  });

  it("coalesces live log chunks without starving the log view", async () => {
    await renderApp();
    let emitLogChunk: Parameters<typeof tauri.listenBridgeLogChunks>[0] | undefined;
    vi.mocked(tauri.listenBridgeLogChunks).mockImplementationOnce(async (handler) => {
      emitLogChunk = handler;
      return () => undefined;
    });

    const nav = screen.getByRole("navigation", { name: "Desktop Bridge views" });
    fireEvent.click(within(nav).getAllByRole("button")[3]);
    await waitFor(() => expect(emitLogChunk).toBeTypeOf("function"));

    act(() => {
      emitLogChunk?.({
        path: "coworker_desktop.log",
        text: "2026-07-14T10:00:00Z INFO coworker_desktop_app: first live entry\n",
        reset: false,
      });
      emitLogChunk?.({
        path: "coworker_desktop.log",
        text: "2026-07-14T10:00:01Z INFO coworker_desktop_app: second live entry\n",
        reset: false,
      });
    });
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });

    await waitFor(() =>
      expect(document.querySelector(".ledger")?.textContent).toContain("first live entry"),
    );
    expect(document.querySelector(".ledger")?.textContent).toContain("second live entry");
  });

  it("chooses and loads a config file from the settings path control", async () => {
    const user = await renderApp();
    await openConfig(user);
    vi.mocked(openDialog).mockResolvedValueOnce("D:\\configs\\desktop.json");

    await user.click(screen.getByRole("button", { name: "Choose config file" }));

    expect(openDialog).toHaveBeenCalledWith({
      directory: false,
      multiple: false,
      filters: [{ name: "JSON", extensions: ["json"] }],
      title: "Select CoWorker Desktop configuration",
    });
    await waitFor(() => expect(tauri.getConfigInfo).toHaveBeenCalledWith("D:\\configs\\desktop.json"));
    expect(inputById("config-path")).toHaveValue("D:\\configs\\desktop.json");
  });

  it("blocks save when validation fails", async () => {
    const user = await renderApp();
    await openConfig(user);

    fireEvent.change(inputById("codex-id"), { target: { value: "" } });
    vi.mocked(tauri.saveConfig).mockClear();
    await user.click(screen.getByRole("button", { name: "Save and apply configuration" }));

    expect(tauri.saveConfig).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Desktop ID is required.");
  });

  it("discards unsaved config changes without writing them", async () => {
    const user = await renderApp();
    await openConfig(user);

    fireEvent.change(inputById("codex-id"), { target: { value: "codex-edited" } });
    fireEvent.change(inputById("coworker-url"), { target: { value: "http://localhost:9001" } });
    vi.mocked(tauri.saveConfig).mockClear();

    await user.click(screen.getByRole("button", { name: "Discard configuration changes" }));

    expect(inputById("codex-id")).toHaveValue("codex-local");
    expect(inputById("coworker-url")).toHaveValue("http://localhost:8001");
    expect(tauri.saveConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Discard configuration changes" })).not.toBeInTheDocument();
  });

  it("saves before starting the bridge", async () => {
    const user = await renderApp(stoppedStatus);
    vi.mocked(tauri.saveConfig).mockClear();

    await user.click(screen.getByRole("button", { name: "Start Bridge" }));

    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.startBridge).toHaveBeenCalledWith("coworker_desktop.json");
  });

  it("stops a running bridge", async () => {
    const user = await renderApp(runningStatus);
    await user.click(screen.getByRole("button", { name: "Stop Bridge" }));

    await waitFor(() => expect(tauri.stopBridge).toHaveBeenCalled());
  });

  it("runs diagnostics and renders backend results", async () => {
    const user = await renderApp(runningStatus);
    vi.mocked(tauri.runDiagnostics).mockResolvedValue([
      { name: "Desktop transport security", ok: false, message: "Bearer token is required" },
      { name: "Codex command", ok: true, message: "codex 1.0.0" },
      { name: "Claude MCP sidecar", ok: true, message: "sidecar ready" },
      { name: "Coworker Partner One", ok: false, message: "connection refused" },
    ]);

    await user.click(screen.getByRole("button", { name: /Run diagnostics/ }));

    await waitFor(() => expect(tauri.runDiagnostics).toHaveBeenCalledWith("coworker_desktop.json"));
    expect(await screen.findByText("Bearer token is required")).toBeInTheDocument();
    expect(await screen.findByText("codex 1.0.0")).toBeInTheDocument();
    expect(screen.getByText("sidecar ready")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Partially available" })).toBeInTheDocument();
  });

  it("prevents concurrent diagnostic runs", async () => {
    const user = await renderApp();
    let finishDiagnostics!: (results: DiagnosticResult[]) => void;
    vi.mocked(tauri.runDiagnostics).mockImplementation(
      () => new Promise((resolve) => {
        finishDiagnostics = resolve;
      }),
    );
    const button = screen.getByRole("button", { name: /Run diagnostics/ });

    await user.click(button);
    expect(button).toBeDisabled();
    await user.click(button);
    expect(tauri.runDiagnostics).toHaveBeenCalledTimes(1);

    finishDiagnostics([]);
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("checks and installs desktop updates through backend wrappers", async () => {
    const user = await renderApp();
    vi.mocked(tauri.checkDesktopUpdate).mockClear();
    vi.mocked(tauri.checkDesktopUpdate).mockResolvedValue({ version: "0.2.0", currentVersion: "0.1.3" });

    await user.click(screen.getByRole("button", { name: "Check for desktop updates" }));

    await waitFor(() => expect(tauri.checkDesktopUpdate).toHaveBeenCalledWith("http://updates.local"));
    expect(await screen.findByText("Desktop update 0.2.0 is available.")).toBeInTheDocument();

    vi.mocked(tauri.installDesktopUpdate).mockImplementation(async (onEvent: (event: DesktopUpdateDownloadEvent) => void) => {
      onEvent({ event: "Started", data: { contentLength: 100 } });
      onEvent({ event: "Progress", data: { chunkLength: 40 } });
      onEvent({ event: "Finished" });
    });
    await user.click(screen.getByRole("button", { name: /Install/ }));

    await waitFor(() => expect(tauri.installDesktopUpdate).toHaveBeenCalled());
    expect(await screen.findByText("Desktop update installed. Restarting...")).toBeInTheDocument();
  });

  it("checks a pushed update once and sends a native notification", async () => {
    await renderApp(runningStatus);
    await waitFor(() => expect(actorStreamHandlers.length).toBeGreaterThan(0));
    vi.mocked(tauri.checkDesktopUpdate).mockClear();
    vi.mocked(tauri.checkDesktopUpdate).mockResolvedValue({ version: "0.2.0", currentVersion: "0.1.3" });
    const event: ActorStreamEvent = {
      actor_id: "local",
      conversation_id: "",
      message_id: "push-1",
      event: { type: "desktop_update_check_requested", published_version: "0.2.0" },
    };

    act(() => actorStreamHandlers.forEach((handler) => handler(event)));

    await waitFor(() => expect(tauri.checkDesktopUpdate).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(sendNotification).toHaveBeenCalledWith({
      title: "CoWorker Desktop update available",
      body: "Version 0.2.0 is ready. Open CoWorker Desktop to install it.",
    }));
    expect(await screen.findByText("Desktop update 0.2.0 is available.")).toBeInTheDocument();

    act(() => actorStreamHandlers.forEach((handler) => handler(event)));
    expect(tauri.checkDesktopUpdate).toHaveBeenCalledTimes(1);
    expect(sendNotification).toHaveBeenCalledTimes(1);
  });

  it("keeps the in-app update notice when native notification permission is denied", async () => {
    const user = await renderApp();
    vi.mocked(tauri.checkDesktopUpdate).mockClear();
    vi.mocked(tauri.checkDesktopUpdate).mockResolvedValue({ version: "0.2.0", currentVersion: "0.1.3" });
    vi.mocked(isPermissionGranted).mockResolvedValue(false);
    vi.mocked(requestPermission).mockResolvedValue("denied");

    await user.click(screen.getByRole("button", { name: "Check for desktop updates" }));

    expect(await screen.findByText("Desktop update 0.2.0 is available.")).toBeInTheDocument();
    expect(sendNotification).not.toHaveBeenCalled();
  });

  it("surfaces update check errors from backend failures", async () => {
    const user = await renderApp(stoppedStatus, { ...baseConfig, desktop_update_url: "https://coworker.example.com" });
    vi.mocked(tauri.checkDesktopUpdate).mockClear();
    vi.mocked(tauri.checkDesktopUpdate).mockRejectedValue(new Error("failed to resolve coworker.example.com"));

    await user.click(screen.getByRole("button", { name: "Check for desktop updates" }));

    expect(await screen.findByText(/the update subscription URL is still the example address/)).toBeInTheDocument();
  });

  it("uses the packaged default update URL when classifying blank-config update errors", async () => {
    const user = await renderApp(stoppedStatus, { ...baseConfig, desktop_update_url: "" });
    vi.mocked(tauri.checkDesktopUpdate).mockClear();
    vi.mocked(tauri.checkDesktopUpdate).mockRejectedValue(new Error("connection refused"));

    await user.click(screen.getByRole("button", { name: "Check for desktop updates" }));

    await waitFor(() => expect(tauri.checkDesktopUpdate).toHaveBeenCalledWith(""));
    expect(await screen.findByText(/confirm the update subscription URL is reachable/)).toBeInTheDocument();
    expect(screen.queryByText(/the update subscription URL is still the example address/)).not.toBeInTheDocument();
  });

  it("refreshes and deletes communicate registrations", async () => {
    const user = await renderApp();
    vi.mocked(tauri.listCommunicateRegistrations).mockResolvedValue([registration("reg-old")]);
    await openConfig(user);

    const registry = screen.getByRole("heading", { name: "Communication registrations" }).closest(".registrySubsection") as HTMLElement;
    await user.click(within(registry).getByRole("button", { name: /Refresh/ }));

    await waitFor(() => expect(tauri.listCommunicateRegistrations).toHaveBeenCalledWith("http://localhost:8001", undefined));
    expect(await screen.findByText("coworker-desktop:desktop-local:codex:cw_01:abc123")).toBeInTheDocument();

    await user.click(within(registry).getByRole("button", { name: /Delete/ }));

    await waitFor(() => expect(tauri.deleteCommunicateRegistration).toHaveBeenCalledWith("http://localhost:8001", "reg-old", undefined));
    expect(tauri.listCommunicateRegistrations).toHaveBeenCalledTimes(2);
  });

  it("loads sessions, sends messages, changes mode, renames, and downloads attachments", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);

    await user.click(await screen.findByRole("button", { name: /Bridge thread/ }));
    await waitFor(() => expect(tauri.loadDesktopMessages).toHaveBeenCalledWith("coworker_desktop.json", "codex", "thread-1", null, 50));
    expect(screen.getByLabelText("Session ID")).toHaveTextContent("thread-1");

    vi.mocked(tauri.loadDesktopMessages).mockClear();
    await user.type(screen.getByLabelText("Session message"), "Hello Codex");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(tauri.sendDesktopMessage).toHaveBeenCalledWith("codex", null, "thread-1", "Hello Codex", null, "default", []));
    expect(tauri.loadDesktopMessages).not.toHaveBeenCalled();

    await user.selectOptions(screen.getByLabelText("Session mode"), "plan");
    await waitFor(() => expect(tauri.setDesktopConversationMode).toHaveBeenCalledWith("codex", "thread-1", "plan"));

    await user.click(screen.getByRole("button", { name: "Rename conversation" }));
    const titleInput = await screen.findByLabelText("Session title");
    await user.clear(titleInput);
    await user.type(titleInput, "Renamed thread");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(tauri.renameDesktopConversation).toHaveBeenCalledWith("codex", "thread-1", "Renamed thread"));

    const attachment = screen.getByRole("button", { name: /summary.md/ });
    fireEvent.dragStart(attachment);
    await waitFor(() => expect(tauri.startDesktopFileDrag).toHaveBeenCalledWith("D:\\tmp\\summary.md"));

    vi.mocked(saveDialog).mockResolvedValue("D:\\downloads\\summary.md");
    await user.click(attachment);
    await waitFor(() => expect(tauri.copyDesktopAttachment).toHaveBeenCalledWith("D:\\tmp\\summary.md", "D:\\downloads\\summary.md"));
  });

  it("copies and quotes conversation messages", async () => {
    const user = await renderApp(runningStatus);
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await openSessions(user);

    await user.click(await screen.findByRole("button", { name: "Copy message" }));
    expect(writeText).toHaveBeenCalledWith("Ready");

    await user.click(screen.getByRole("button", { name: "Quote message" }));
    expect(screen.getByLabelText("Session message")).toHaveValue("> Ready\n\n");

    writeText.mockRejectedValueOnce(new Error("clipboard permission denied"));
    await user.click(screen.getByRole("button", { name: "Copy message" }));
    expect(await screen.findByText("Couldn't copy the message. Check the system clipboard permission and try again.")).toBeInTheDocument();
  });

  it("localizes empty Codex responses in the conversation", async () => {
    const user = await renderApp(runningStatus);
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({
      messages: [{
        ...assistantMessage,
        id: "empty-response",
        author_kind: "system",
        content: "",
        metadata: {
          ...assistantMessage.metadata,
          author_label: "System",
          kind: "empty_response",
        },
      }],
      next_before_cursor: null,
    });
    await openSessions(user);

    expect(await screen.findByText(/Codex finished this turn without returning a message/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Toggle language" }));
    expect(await screen.findByText(/Codex 已结束本轮，但没有返回任何消息/)).toBeInTheDocument();
  });

  it("adds dropped files to the active session composer", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    await waitFor(() => expect(desktopFileDropHandlers.length).toBeGreaterThan(0));

    act(() => desktopFileDropHandlers.forEach((handler) => handler({ type: "enter" })));
    expect(screen.getByText("Drop files to attach")).toBeInTheDocument();

    act(() => desktopFileDropHandlers.forEach((handler) => handler({
      type: "drop",
      paths: ["D:\\tmp\\brief.pdf", "D:\\tmp\\brief.pdf", "D:\\tmp\\notes.txt"],
    })));
    expect(screen.getByRole("button", { name: /brief.pdf/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /notes.txt/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(tauri.sendDesktopMessage).toHaveBeenCalledWith(
      "codex",
      null,
      "thread-1",
      "",
      null,
      "default",
      ["D:\\tmp\\brief.pdf", "D:\\tmp\\notes.txt"],
    ));
  });

  it("keeps a conversation loaded when switching actor tabs", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    const codexTimeline = screen.getByRole("region", { name: "Codex conversations" })
      .querySelector<HTMLElement>(".sessionTimeline");
    expect(codexTimeline).not.toBeNull();
    if (codexTimeline) {
      codexTimeline.scrollTop = 120;
      fireEvent.scroll(codexTimeline);
    }
    vi.mocked(tauri.loadDesktopMessages).mockClear();

    await user.click(screen.getByRole("button", { name: "Local" }));
    await user.click(screen.getByRole("button", { name: "Codex" }));

    expect(screen.getByText("Ready")).toBeInTheDocument();
    const restoredTimeline = screen.getByRole("region", { name: "Codex conversations" })
      .querySelector<HTMLElement>(".sessionTimeline");
    await waitFor(() => expect(restoredTimeline?.scrollTop).toBe(120));
    expect(tauri.loadDesktopMessages).not.toHaveBeenCalled();
  });

  it("manually refreshes the selected conversation", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    const externalMessage = { ...assistantMessage, id: "msg-external", content: "Continued externally" };
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({
      messages: [assistantMessage, externalMessage],
      next_before_cursor: null,
    });
    vi.mocked(tauri.loadDesktopMessages).mockClear();

    await user.click(screen.getByRole("button", { name: "Refresh conversations" }));

    await waitFor(() => expect(tauri.loadDesktopMessages).toHaveBeenCalledWith("coworker_desktop.json", "codex", "thread-1", null, 50));
    expect(await screen.findByText("Continued externally")).toBeInTheDocument();
  });

  it("loads Claude history once without polling", async () => {
    const user = await renderApp({
      ...runningStatus,
      actors: [{ actor_id: "claude", available: true, message: "Claude ready" }],
    });
    await openSessions(user);
    vi.mocked(tauri.listDesktopConversations).mockResolvedValue([{
      actor_id: "claude",
      conversation_id: "claude-1",
      title: "Claude thread",
      project_path: "D:\\Projects\\coworker",
      writable: true,
      updated_at: "2026-07-13T12:00:00Z",
      mode: "default",
    }]);
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({
      messages: [{
        id: "claude-message-1",
        actor_id: "claude",
        conversation_id: "claude-1",
        author_kind: "assistant",
        content: "Claude ready",
        created_at: "2026-07-13T12:00:00Z",
        metadata: {},
      }],
      next_before_cursor: null,
    });
    vi.mocked(tauri.loadDesktopMessages).mockClear();

    await user.click(screen.getByRole("button", { name: "Claude Code" }));

    expect(await screen.findByText("Claude ready")).toBeInTheDocument();
    expect(tauri.loadDesktopMessages).toHaveBeenCalledTimes(1);
  });

  it("defaults a new Claude conversation to the current project and lets it be changed", async () => {
    const claudeSession: ActorConversation = {
      ...session,
      actor_id: "claude",
      conversation_id: "claude-project",
      title: "Claude project",
    };
    const user = await renderApp({
      ...runningStatus,
      actors: [{ actor_id: "claude", available: true, message: "Claude ready" }],
    });
    vi.mocked(tauri.listDesktopConversations).mockImplementation(async (actorId) =>
      actorId === "claude" ? [claudeSession] : [session],
    );
    await openSessions(user);
    await user.click(screen.getByRole("button", { name: "Claude Code" }));
    await user.click(await screen.findByRole("button", { name: /Claude project/ }));
    await user.click(screen.getByRole("button", { name: /New conversation/ }));

    expect(screen.getByText("D:\\Projects\\coworker")).toBeInTheDocument();
    vi.mocked(openDialog).mockResolvedValue("D:\\Projects\\chosen");
    await user.click(screen.getByRole("button", { name: "Change project" }));
    expect(await screen.findByText("D:\\Projects\\chosen")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Message to Claude Code"), "Start here");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(tauri.sendDesktopMessage).toHaveBeenCalledWith(
      "claude", null, null, "Start here", "D:\\Projects\\chosen", "default", [],
    ));
  });

  it("restores a previously opened conversation without loading it again", async () => {
    const secondSession = { ...session, conversation_id: "thread-2", title: "Second thread" };
    const secondMessage = { ...assistantMessage, id: "msg-2", conversation_id: "thread-2", content: "Second ready" };
    const user = await renderApp(runningStatus);
    vi.mocked(tauri.listDesktopConversations).mockResolvedValue([session, secondSession]);
    vi.mocked(tauri.loadDesktopMessages).mockImplementation(async (_path, _actorId, conversationId) => ({
      messages: [conversationId === "thread-2" ? secondMessage : assistantMessage],
      next_before_cursor: null,
    }));
    await openSessions(user);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    const timeline = screen.getByRole("region", { name: "Codex conversations" })
      .querySelector<HTMLElement>(".sessionTimeline");
    expect(timeline).not.toBeNull();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    if (timeline) {
      timeline.scrollTop = 120;
      fireEvent.scroll(timeline);
    }
    vi.mocked(tauri.loadDesktopMessages).mockClear();

    await user.click(screen.getByRole("button", { name: /Second thread/ }));
    expect(await screen.findByText("Second ready")).toBeInTheDocument();
    const secondTimeline = screen.getByRole("region", { name: "Codex conversations" })
      .querySelector<HTMLElement>(".sessionTimeline");
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    if (secondTimeline) {
      secondTimeline.scrollTop = 40;
      fireEvent.scroll(secondTimeline);
    }
    await user.click(screen.getByRole("button", { name: /Bridge thread/ }));

    expect(screen.getByText("Ready")).toBeInTheDocument();
    await waitFor(() => expect(timeline?.scrollTop).toBe(120));
    await user.click(screen.getByRole("button", { name: /Second thread/ }));
    await waitFor(() => expect(secondTimeline?.scrollTop).toBe(40));
    expect(tauri.loadDesktopMessages).toHaveBeenCalledTimes(1);
    expect(tauri.loadDesktopMessages).not.toHaveBeenCalledWith("coworker_desktop.json", "codex", "thread-1", null, 50);
  });

  it("keeps the original title when sending later messages in an existing session", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    await user.click(await screen.findByRole("button", { name: /Bridge thread/ }));
    let finishRefresh!: (sessions: ActorConversation[]) => void;
    vi.mocked(tauri.listDesktopConversations).mockImplementationOnce(() => new Promise((resolve) => {
      finishRefresh = resolve;
    }));

    await user.type(screen.getByLabelText("Session message"), "This is a later message");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(tauri.sendDesktopMessage).toHaveBeenCalledWith(
      "codex",
      null,
      "thread-1",
      "This is a later message",
      null,
      "default",
      [],
    ));

    expect(screen.getByRole("heading", { name: "Bridge thread" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "This is a later message" })).not.toBeInTheDocument();

    await act(async () => {
      finishRefresh([session]);
    });
  });

  it("keeps session history readable while stopped and disables every write action", async () => {
    const user = await renderApp(stoppedStatus);
    await openSessions(user);

    await user.click(await screen.findByRole("button", { name: /Bridge thread/ }));
    await waitFor(() => expect(tauri.loadDesktopMessages).toHaveBeenCalledWith("coworker_desktop.json", "codex", "thread-1", null, 50));

    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("history available")).toBeInTheDocument();
    expect(screen.getByText("History remains available offline. Start the Bridge to continue this conversation.")).toBeInTheDocument();
    expect(screen.queryByText(/comes from local history/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Session message")).toBeDisabled();
    expect(screen.getByRole("button", { name: /New conversation/ })).toBeDisabled();
    expect(tauri.listDesktopConversations).toHaveBeenCalledWith("codex", "coworker_desktop.json", 120);
    expect(tauri.sendDesktopMessage).not.toHaveBeenCalled();
  });

  it("does not let an older conversation refresh hide a newly loaded local conversation", async () => {
    const user = await renderApp(runningStatus);
    let resolveOlderRefresh!: (items: ActorConversation[]) => void;
    let localRefreshCount = 0;
    const newConversation: ActorConversation = {
      actor_id: "local",
      conversation_id: "local-new",
      title: "First local message",
      project_path: null,
      writable: true,
      updated_at: "2026-07-13T12:00:00Z",
      mode: null,
    };
    vi.mocked(tauri.listDesktopConversations).mockImplementation(async (actorId) => {
      if (actorId === "codex") return [session];
      localRefreshCount += 1;
      if (localRefreshCount === 1) {
        return new Promise((resolve) => {
          resolveOlderRefresh = resolve;
        });
      }
      return [newConversation];
    });

    await openSessions(user);
    await user.click(screen.getByRole("button", { name: "Local" }));
    await user.click(screen.getByRole("button", { name: "Refresh conversations" }));
    expect(await screen.findByRole("button", { name: /First local message/ })).toBeInTheDocument();

    await act(async () => {
      resolveOlderRefresh([]);
    });

    expect(screen.getByRole("button", { name: /First local message/ })).toBeInTheDocument();
  });

  it("refreshes Local messages when an incoming conversation event arrives", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    vi.mocked(tauri.listDesktopConversations).mockResolvedValue([{
      actor_id: "local",
      conversation_id: "local-event-thread",
      title: "Local thread",
      project_path: null,
      writable: true,
      updated_at: "2026-07-13T12:00:00Z",
      mode: null,
    }]);
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({ messages: [], next_before_cursor: null });
    let actorHandler!: (event: ActorStreamEvent) => void;
    vi.mocked(tauri.listenActorStreamEvents).mockImplementation(async (handler) => {
      actorHandler = handler;
      return () => undefined;
    });

    vi.mocked(tauri.loadDesktopMessages).mockClear();
    await user.click(screen.getByRole("button", { name: "Local" }));
    expect(await screen.findByRole("button", { name: /Local thread/ })).toBeInTheDocument();
    await waitFor(() => expect(tauri.loadDesktopMessages).toHaveBeenCalledTimes(1));
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({
      messages: [{
        id: "local-reply-1",
        actor_id: "local",
        conversation_id: "local-event-thread",
        author_kind: "coworker",
        content: "Coworker replied",
        created_at: "2026-07-13T12:01:00Z",
        metadata: {},
      }],
      next_before_cursor: null,
    });
    vi.mocked(tauri.loadDesktopMessages).mockClear();
    vi.mocked(tauri.listDesktopConversations).mockClear();

    act(() => actorHandler({
      actor_id: "local",
      conversation_id: "local-event-thread",
      message_id: "local-reply-1",
      event: { type: "conversation_updated" },
    }));

    expect(await screen.findByText("Coworker replied")).toBeInTheDocument();
    expect(tauri.loadDesktopMessages).toHaveBeenCalledTimes(1);
    expect(tauri.listDesktopConversations).toHaveBeenCalledTimes(1);
  });

  it("optimistically sends Local messages and restores the draft when delivery fails", async () => {
    const user = await renderApp(runningStatus);
    let rejectSend!: (error: Error) => void;
    vi.mocked(tauri.sendDesktopMessage).mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectSend = reject;
    }));

    await openSessions(user);
    await user.click(screen.getByRole("button", { name: "Local" }));
    await user.click(screen.getByRole("button", { name: /New conversation/ }));
    const composer = screen.getByPlaceholderText("Send directly to your Coworker without an AI intermediary");
    await user.type(composer, "Retry this local message");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));

    expect(await screen.findByText("Retry this local message")).toBeInTheDocument();
    expect(composer).toHaveValue("");
    expect(tauri.sendDesktopMessage).toHaveBeenCalledWith(
      "local",
      "cw_01",
      null,
      "Retry this local message",
      null,
      null,
      [],
    );

    await act(async () => rejectSend(new Error("delivery failed")));
    await waitFor(() => expect(composer).toHaveValue("Retry this local message"));
    expect(document.querySelector(".sessionTimeline")).not.toHaveTextContent("Retry this local message");
  });

  it("waits for macOS IME composition to finish before Enter sends", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    await user.click(screen.getByRole("button", { name: /New conversation/ }));
    const composer = screen.getByLabelText("Session message");
    await user.type(composer, "unfinished composition");
    vi.mocked(tauri.sendDesktopMessage).mockClear();

    fireEvent.keyDown(composer, { key: "Enter", code: "Enter", keyCode: 229 });

    expect(tauri.sendDesktopMessage).not.toHaveBeenCalled();
    expect(composer).toHaveValue("unfinished composition");

    fireEvent.keyDown(composer, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(tauri.sendDesktopMessage).toHaveBeenCalledTimes(1));
  });

  it("grows and shrinks the local composer with its content", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);
    await user.click(screen.getByRole("button", { name: "Local" }));
    const composer = screen.getByPlaceholderText("Send directly to your Coworker without an AI intermediary");
    Object.defineProperty(composer, "scrollHeight", { configurable: true, value: 180 });

    fireEvent.change(composer, { target: { value: "A long local draft" } });
    expect(composer).toHaveStyle({ height: "180px" });

    Object.defineProperty(composer, "scrollHeight", { configurable: true, value: 70 });
    fireEvent.change(composer, { target: { value: "" } });
    expect(composer).toHaveStyle({ height: "70px" });
  });

  it("sends new-session messages and attaches files selected from the dialog", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);

    await user.click(screen.getByRole("button", { name: /New conversation/ }));
    vi.mocked(openDialog).mockResolvedValue(["D:\\tmp\\input.txt"]);
    await user.click(screen.getByRole("button", { name: "Attach files" }));
    expect(await screen.findByText("input.txt")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Session message"), "Start a new thread");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));

    await waitFor(() =>
      expect(tauri.sendDesktopMessage).toHaveBeenCalledWith(
        "codex", null, null, "Start a new thread", null, "default", ["D:\\tmp\\input.txt"],
      ),
    );
  });

  it("shows and renames a newly created Codex session before it reaches the persisted session index", async () => {
    const user = await renderApp(runningStatus);
    vi.mocked(tauri.sendDesktopMessage).mockResolvedValueOnce({ conversation_id: "thread-new" });
    vi.mocked(tauri.loadDesktopMessages).mockResolvedValue({
      messages: [],
      next_before_cursor: null,
    });
    vi.mocked(tauri.listDesktopConversations).mockResolvedValue([session]);
    await openSessions(user);

    await user.click(screen.getByRole("button", { name: /New conversation/ }));
    vi.mocked(tauri.loadDesktopMessages).mockClear();
    await user.type(screen.getByLabelText("Session message"), "Investigate the indexing delay");
    await user.click(screen.getByRole("button", { name: /^Send$/ }));

    expect(await screen.findByRole("button", { name: /Investigate the indexing delay/ })).toBeInTheDocument();
    expect(document.querySelector(".sessionTimeline")).toHaveTextContent("Investigate the indexing delay");
    expect(screen.queryByText("Loading messages...")).not.toBeInTheDocument();
    expect(tauri.loadDesktopMessages).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Bridge thread/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Rename conversation" }));
    const titleInput = screen.getByLabelText("Session title");
    await user.clear(titleInput);
    await user.type(titleInput, "Index delay investigation");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(tauri.renameDesktopConversation).toHaveBeenCalledWith(
      "codex",
      "thread-new",
      "Index delay investigation",
    ));
    expect(await screen.findByRole("button", { name: /Index delay investigation/ })).toBeInTheDocument();
  });

  it("routes the explicit Coworker action through the picker when multiple coworkers exist", async () => {
    const user = await renderApp(runningStatus);
    await openSessions(user);

    await user.click(await screen.findByRole("button", { name: /Bridge thread/ }));
    await user.type(screen.getByLabelText("Session message"), "please check this");
    await user.click(screen.getByRole("button", { name: "Send to Coworker" }));

    const picker = await screen.findByRole("dialog", { name: "Choose Coworker" });
    expect(picker).toBeInTheDocument();
    await user.click(within(picker).getByRole("button", { name: /Partner Two/ }));

    await waitFor(() =>
      expect(tauri.sendDesktopCoworkerMessage).toHaveBeenCalledWith("codex", "cw_02", "thread-1", "please check this", []),
    );
  });
});

describe("Onboarding tutorial", () => {
  beforeEach(() => {
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
  });

  it("opens the tutorial on first launch", async () => {
    setDefaultMocks();
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    await waitFor(() => expect(tauri.getConfigInfo).toHaveBeenCalledWith("coworker_desktop.json"));
    expect(await screen.findByRole("dialog", { name: "Setup wizard" })).toBeInTheDocument();
  });

  it("closes the tutorial with Escape and marks it complete", async () => {
    const user = userEvent.setup();
    setDefaultMocks();
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    await screen.findByRole("dialog", { name: "Setup wizard" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Setup wizard" })).not.toBeInTheDocument());
    expect(window.localStorage.getItem("coworker-desktop-onboarding-completed")).toBe("true");
  });

  it("does not auto-open the tutorial once completed", async () => {
    setDefaultMocks();
    window.localStorage.setItem("coworker-desktop-onboarding-completed", "true");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    await waitFor(() => expect(screen.getAllByText("codex-local").length).toBeGreaterThan(0));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByRole("dialog", { name: "Setup wizard" })).not.toBeInTheDocument();
  });

  it("saves and starts the bridge from the final step", async () => {
    const user = userEvent.setup();
    setDefaultMocks();
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Setup wizard" });
    for (let index = 0; index < 4; index += 1) {
      await user.click(within(dialog).getByRole("button", { name: "Next" }));
    }
    vi.mocked(tauri.saveConfig).mockClear();
    vi.mocked(tauri.startBridge).mockClear();
    await user.click(within(dialog).getByRole("button", { name: "Save and start" }));
    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(tauri.startBridge).toHaveBeenCalledWith("coworker_desktop.json"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Setup wizard" })).not.toBeInTheDocument());
    expect(window.localStorage.getItem("coworker-desktop-onboarding-completed")).toBe("true");
  });

  it("edits the Desktop ID within the wizard and saves it", async () => {
    const user = userEvent.setup();
    setDefaultMocks();
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Setup wizard" });
    await user.click(within(dialog).getByRole("button", { name: "Next" }));
    const codexInput = within(dialog).getByLabelText("Desktop ID");
    await user.clear(codexInput);
    await user.type(codexInput, "codex-from-wizard");
    for (let index = 0; index < 3; index += 1) {
      await user.click(within(dialog).getByRole("button", { name: "Next" }));
    }
    vi.mocked(tauri.saveConfig).mockClear();
    await user.click(within(dialog).getByRole("button", { name: "Save configuration" }));
    await waitFor(() => expect(tauri.saveConfig).toHaveBeenCalledTimes(1));
    expect(tauri.saveConfig).toHaveBeenCalledWith(
      "coworker_desktop.json",
      expect.objectContaining({ codex_id: "codex-from-wizard" }),
    );
  });

  it("reopens the tutorial from the resident panel entry", async () => {
    const user = userEvent.setup();
    setDefaultMocks();
    window.localStorage.setItem("coworker-desktop-onboarding-completed", "true");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    await waitFor(() => expect(screen.getAllByText("codex-local").length).toBeGreaterThan(0));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByRole("dialog", { name: "Setup wizard" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open setup" }));
    expect(await screen.findByRole("dialog", { name: "Setup wizard" })).toBeInTheDocument();
  });

  it("defaults the desktop update URL to the Coworker base URL", async () => {
    const user = userEvent.setup();
    setDefaultMocks(stoppedStatus, { ...baseConfig, desktop_update_url: "" });
    window.localStorage.removeItem("coworker-desktop-onboarding-completed");
    render(
      <LanguageProvider>
        <App />
      </LanguageProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Setup wizard" });
    for (let index = 0; index < 2; index += 1) {
      await user.click(within(dialog).getByRole("button", { name: "Next" }));
    }
    const baseUrlInput = within(dialog).getByLabelText("Coworker base URL");
    await user.clear(baseUrlInput);
    await user.type(baseUrlInput, "http://partner.local");
    expect(dialog.querySelector<HTMLInputElement>("#onboarding-desktop-update-url")).toHaveValue("http://partner.local");
  });
});
