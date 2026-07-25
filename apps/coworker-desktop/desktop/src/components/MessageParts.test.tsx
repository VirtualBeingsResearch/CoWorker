import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { markdownLocalImagePath, MessageText } from "./MessageParts";
import { readDesktopImagePreview } from "../tauri";

vi.mock("../tauri", () => ({
  readDesktopImagePreview: vi.fn(),
}));

describe("message markdown images", () => {
  beforeEach(() => {
    vi.mocked(readDesktopImagePreview).mockResolvedValue(
      new Uint8Array([137, 80, 78, 71]).buffer,
    );
  });

  it("normalizes Codex markdown paths with a leading slash before the drive", () => {
    expect(markdownLocalImagePath("/C:/Users/Test/image.png"))
      .toBe("C:/Users/Test/image.png");
  });

  it("renders a local markdown image through the desktop preview command", async () => {
    render(
      <MessageText text="![本地生成图片](/C:/Users/Example/.codex/generated_images/example.png)" />,
    );

    expect(await screen.findByRole("img", { name: "本地生成图片" }))
      .toHaveAttribute("src", "blob:coworker-image-preview");
    expect(readDesktopImagePreview).toHaveBeenCalledWith(
      "C:/Users/Example/.codex/generated_images/example.png",
    );
  });
});
