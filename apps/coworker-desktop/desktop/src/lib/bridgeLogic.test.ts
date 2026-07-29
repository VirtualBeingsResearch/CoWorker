import { describe, expect, it } from "vitest";
import {
  classifyDesktopUpdateErrorKey,
  applyActorStreamEvent,
  clampLogText,
  conversationTitleFromMessage,
  diagnosticsForCoworker,
  enabledCoworkers,
  fileName,
  formatSessionTime,
  groupToolMessages,
  isRelayBaseUrl,
  maxLogTextChars,
  mergeMessages,
  nextCoworker,
  normalizeApprovalsReviewer,
  normalizeCoworkers,
  normalizePermissionsMode,
  normalizeTimeoutSeconds,
  overviewReadiness,
  parseLog,
  resolvedApprovalConfig,
  runtimeMood,
  validateConfig,
  type TimelineMessage,
} from "./bridgeLogic";
import type { ActorStreamEvent } from "../tauri";

function message(overrides: Partial<TimelineMessage>): TimelineMessage {
  return {
    id: overrides.id ?? "msg",
    timestamp: overrides.timestamp ?? "2026-07-06T10:00:00Z",
    author_kind: overrides.author_kind ?? "assistant",
    author_id: overrides.author_id ?? null,
    author_label: overrides.author_label ?? "Codex",
    kind: overrides.kind ?? "message",
    text: overrides.text ?? "",
    attachments: overrides.attachments ?? [],
    turn_id: overrides.turn_id ?? null,
    item_id: overrides.item_id ?? null,
    streaming: overrides.streaming ?? false,
  };
}

describe("bridgeLogic config helpers", () => {
  it("builds a single-line conversation title capped at 60 characters", () => {
    expect(conversationTitleFromMessage("  First\n  local message  ", "Fallback")).toBe("First local message");
    expect(conversationTitleFromMessage("你".repeat(70), "Fallback")).toBe(`${"你".repeat(59)}…`);
    expect(conversationTitleFromMessage("  ", "Fallback")).toBe("Fallback");
  });

  it("normalizes coworker arrays and ignores removed top-level fields", () => {
    expect(
      normalizeCoworkers({
        coworkers: [
          { coworker_id: "", display_name: "", base_url: "http://localhost:8001", bearer_token: "secret" },
          { coworker_id: "cw_ops", display_name: "Ops", base_url: "", enabled: false },
        ],
      }),
    ).toEqual([
      { coworker_id: "cw_01", display_name: "cw_01", base_url: "http://localhost:8001", bearer_token: "secret", enabled: true },
      { coworker_id: "cw_ops", display_name: "Ops", base_url: "", enabled: false },
    ]);

    expect(
      normalizeCoworkers({
        coworker_id: "legacy",
        coworker_display_name: "Legacy Partner",
        coworker_base_url: "http://legacy.local",
      }),
    ).toEqual([]);

    expect(enabledCoworkers([
      { coworker_id: "cw_01", display_name: "One", base_url: "http://localhost:8001", enabled: true },
      { coworker_id: "cw_02", display_name: "Two", base_url: "http://localhost:8002", enabled: false },
    ])).toEqual([
      { coworker_id: "cw_01", display_name: "One", base_url: "http://localhost:8001", enabled: true },
    ]);
  });

  it("resolves approval config with safe defaults", () => {
    expect(
      resolvedApprovalConfig({
        permissions_mode: "danger-full-access",
        approvals_reviewer: "coworker",
        approval_timeout_seconds: "42" as unknown as number,
      }),
    ).toEqual({
      permissionsMode: "danger-full-access",
      approvalsReviewer: "coworker",
      approvalTimeoutSeconds: 42,
    });
    expect(normalizePermissionsMode("root", "read-only")).toBe("read-only");
    expect(normalizeApprovalsReviewer("team", "none")).toBe("none");
    expect(normalizeTimeoutSeconds(-1)).toBe(300);
  });

  it("validates required identity, coworker uniqueness, base urls, and update urls", () => {
    const issues = validateConfig({
      codex_id: " ",
      desktop_update_url: "coworker.local",
      coworkers: [
        { coworker_id: "cw_01", display_name: "One", base_url: "" },
        { coworker_id: "cw_01", display_name: "Two", base_url: "http://localhost:8002" },
      ],
    });

    expect(issues.map((issue) => issue.key)).toEqual(
      expect.arrayContaining([
        "validation.codexIdRequired",
        "validation.updateUrlFormat",
        "validation.baseUrlRequired",
        "validation.coworkerIdUnique",
      ]),
    );
  });

  it("creates the next available coworker id", () => {
    expect(
      nextCoworker(
        [
          { coworker_id: "cw_01", display_name: "One", base_url: "http://localhost:8001" },
          { coworker_id: "cw_02", display_name: "Two", base_url: "http://localhost:8002" },
        ],
        (index) => `Partner ${index}`,
      ),
    ).toEqual({ coworker_id: "cw_03", display_name: "Partner 3", base_url: "http://localhost:8002", enabled: true });
  });

  it("requires at least one enabled coworker", () => {
    const issues = validateConfig({
      codex_id: "codex-local",
      coworkers: [
        { coworker_id: "cw_01", display_name: "One", base_url: "http://localhost:8001", enabled: false },
      ],
    });

    expect(issues.map((issue) => issue.key)).toContain("validation.atLeastOneEnabledCoworker");
  });
});

describe("bridgeLogic runtime and update helpers", () => {
  it("derives runtime mood from status", () => {
    expect(runtimeMood(null)).toBe("loading");
    expect(runtimeMood({ state: "running", config_path: null, codex_id: null, coworkers: [], last_error: null })).toBe("running");
    expect(runtimeMood({ state: "stopped", config_path: null, codex_id: null, coworkers: [], last_error: "boom" })).toBe("error");
  });

  it("derives overview readiness from runtime and actor health", () => {
    expect(overviewReadiness(null)).toBe("loading");
    expect(
      overviewReadiness({ state: "running", config_path: null, codex_id: null, coworkers: [], actors: [], last_error: null }),
    ).toBe("ready");
    expect(
      overviewReadiness({
        state: "running",
        config_path: null,
        codex_id: null,
        coworkers: [],
        actors: [{ actor_id: "claude", available: false, message: "not installed" }],
        last_error: null,
      }),
    ).toBe("partial");
  });

  it("matches Coworker diagnostics exactly", () => {
    const diagnostics = [
      { name: "Codex command", ok: true, message: "ready" },
      { name: "Coworker Codex", ok: false, message: "offline" },
    ];
    expect(
      diagnosticsForCoworker(diagnostics, {
        coworker_id: "cw_codex",
        display_name: "Codex",
        base_url: "http://localhost:8000",
      }),
    ).toEqual(diagnostics[1]);
  });

  it("classifies desktop update errors", () => {
    expect(classifyDesktopUpdateErrorKey(new Error("failed to resolve coworker.example.com"), "")).toBe("update.error.exampleUrl");
    expect(classifyDesktopUpdateErrorKey(new Error("connection refused"), "")).toBe("update.error.generic");
    expect(classifyDesktopUpdateErrorKey(new Error("connection refused"), "https://coworker.example.com")).toBe("update.error.exampleUrl");
    expect(classifyDesktopUpdateErrorKey(new Error("relative URL without a base"), "localhost:8000")).toBe("update.error.badFormat");
    expect(classifyDesktopUpdateErrorKey(new Error("connection refused"), "http://localhost:8000")).toBe("update.error.generic");
  });
});

describe("bridgeLogic log helpers", () => {
  it("parses log levels, sources, messages, and coworker targets", () => {
    const entries = parseLog(
      [
        "2026-07-06T10:00:00.123+08:00 INFO coworker_desktop_app: started cw_ops",
        "2026-07-06 10:00:01 WARN coworker.client: failed http://localhost:8002",
        "plain line",
      ].join("\n"),
      [{ coworker_id: "cw_ops", display_name: "Ops", base_url: "http://localhost:8002" }],
    );

    expect(entries).toHaveLength(3);
    expect(entries[0]).toMatchObject({ level: "info", target: "Ops", source: "coworker_desktop_app", message: "started cw_ops" });
    expect(entries[1]).toMatchObject({ level: "warn", target: "Ops", source: "coworker.client", message: "failed http://localhost:8002" });
    expect(entries[2]).toMatchObject({ level: "raw", target: "Bridge", message: "plain line" });
  });

  it("redacts verbose updater signatures from parsed log messages", () => {
    const entries = parseLog(
      '2026-07-06T10:12:15Z DEBUG tauri_plugin_updater::updater update response: Object {"signature": String("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/abcdefghijklmnopqrstuvwxyz")}',
      [],
    );

    expect(entries[0].source).toBe("tauri_plugin_updater::updater");
    expect(entries[0].message).toContain('"signature": String("<redacted>")');
    expect(entries[0].message).not.toContain("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ");
  });

  it("clamps long log text to a tail that starts after a newline", () => {
    const text = `prefix without keeping\n${"a".repeat(maxLogTextChars + 64)}`;
    const clamped = clampLogText(text);
    expect(clamped.length).toBeLessThanOrEqual(maxLogTextChars);
    expect(clamped.startsWith("prefix")).toBe(false);
  });
});

describe("bridgeLogic session helpers", () => {
  it("merges a normalized actor message delta", () => {
    const messages = applyActorStreamEvent([], {
      actor_id: "codex",
      conversation_id: "codex-thread",
      message_id: "codex-message",
      event: {
        type: "message_delta",
        message: {
          id: "codex-message",
          actor_id: "codex",
          conversation_id: "codex-thread",
          author_kind: "assistant",
          content: "Streaming reply",
          created_at: "2026-07-13T12:00:00Z",
          metadata: { streaming: true, item_id: "item-1" },
        },
      },
    });

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      id: "codex-message",
      actor_id: "codex",
      content: "Streaming reply",
      metadata: { streaming: true, item_id: "item-1" },
    });
  });

  it("renders Claude partial text before the final result arrives", () => {
    const update = (event: Record<string, unknown>): ActorStreamEvent => ({
      actor_id: "claude",
      conversation_id: "claude-session",
      message_id: "message-1",
      event: { type: "stream_event", event },
    });
    let messages = applyActorStreamEvent([], update({
      type: "content_block_start",
      index: 0,
      content_block: { type: "text", text: "" },
    }));
    messages = applyActorStreamEvent(messages, update({
      type: "content_block_delta",
      index: 0,
      delta: { type: "text_delta", text: "Hello" },
    }));
    messages = applyActorStreamEvent(messages, update({
      type: "content_block_delta",
      index: 0,
      delta: { type: "text_delta", text: " Claude" },
    }));

    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("Hello Claude");
    expect(messages[0].metadata?.streaming).toBe(true);

    messages = applyActorStreamEvent(messages, update({ type: "content_block_stop", index: 0 }));
    expect(messages[0].metadata?.streaming).toBe(false);
  });

  it("merges streaming message updates and keeps previous attachments when deltas omit them", () => {
    const merged = mergeMessages([
      message({ id: "m1", text: "Hello", attachments: [{ filename: "a.txt", media_type: "text/plain", size: 1, path: "a.txt", downloadable: true, reason: null }], streaming: true }),
      message({ id: "m1", text: " world", attachments: [], streaming: true }),
      message({ id: "m1", text: "Done", attachments: [], streaming: false }),
    ]);

    expect(merged).toHaveLength(1);
    expect(merged[0].text).toBe("Done");
    expect(merged[0].attachments).toHaveLength(1);
    expect(merged[0].streaming).toBe(false);
  });

  it("groups tool results with their matching call", () => {
    const groups = groupToolMessages([
      message({ id: "call", kind: "tool_call", item_id: "tool_1", text: "run" }),
      message({ id: "result", kind: "tool_result", item_id: "tool_1", text: "ok" }),
      message({ id: "plain", kind: "message", text: "next" }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].message.id).toBe("call");
    expect(groups[0].result?.id).toBe("result");
    expect(groups[1].message.id).toBe("plain");
  });

  it("formats session timestamps and filenames defensively", () => {
    expect(formatSessionTime("not-a-date")).toBe("not-a-date");
    expect(formatSessionTime("2026-07-06T10:00:00Z")).not.toBe("unknown");
    expect(fileName("C:\\Users\\fine\\report.md")).toBe("report.md");
    expect(fileName("/tmp/archive.zip")).toBe("archive.zip");
  });

  it("recognizes only exact Relay instance base URLs", () => {
    expect(isRelayBaseUrl("http://relay.example.com:8443/i/cw_abcdefgh")).toBe(true);
    expect(isRelayBaseUrl("https://relay.example.com/i/cw_abcdefgh")).toBe(true);
    expect(isRelayBaseUrl("http://relay.example.com/i/cw_abcdefgh/status")).toBe(false);
    expect(isRelayBaseUrl("http://relay.example.com/i/not-an-instance")).toBe(false);
    expect(isRelayBaseUrl("ftp://relay.example.com/i/cw_abcdefgh")).toBe(false);
  });
});
