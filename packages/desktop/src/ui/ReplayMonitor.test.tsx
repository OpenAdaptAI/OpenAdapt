import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { RunStep } from "../lib/types";
import { ReplayMonitor } from "./ReplayMonitor";

afterEach(() => {
  cleanup();
});

const SAMPLE_BEFORE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
const SAMPLE_AFTER = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

const STEPS_WITH_FRAMES: RunStep[] = [
  { index: 0, action: "open", target: "Practice portal", state: "verified", latency_ms: 412, effect: null },
  {
    index: 1,
    action: "click",
    target: "Save",
    state: "verified",
    latency_ms: 288,
    effect: "verified",
    before_screenshot: { data_url: SAMPLE_BEFORE, width: 1280, height: 800, focus: { x: 104, y: 350 } },
    after_screenshot: { data_url: SAMPLE_AFTER, width: 1280, height: 800, focus: null },
  },
  { index: 2, action: "click", target: "Close", state: "verified", latency_ms: 154, effect: null },
];

describe("ReplayMonitor local step frame preview", () => {
  it("defaults to the verified step with local frames and switches between Side by side, Before, and After", () => {
    render(<ReplayMonitor workflowName="Invoice review" steps={STEPS_WITH_FRAMES} total={3} />);

    expect(screen.getByText(/Step 02 · click Save/)).toBeTruthy();
    expect(screen.getByAltText("Before frame for step 2: click Save")).toBeTruthy();
    expect(screen.getByAltText("After frame for step 2: click Save")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Before" }));
    expect(screen.getByAltText("Before frame for step 2: click Save")).toBeTruthy();
    expect(screen.queryByAltText("After frame for step 2: click Save")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "After" }));
    expect(screen.queryByAltText("Before frame for step 2: click Save")).toBeNull();
    expect(screen.getByAltText("After frame for step 2: click Save")).toBeTruthy();
  });

  it("toggles local CSS blur for sensitive text without mutating the source frame", () => {
    render(<ReplayMonitor workflowName="Invoice review" steps={STEPS_WITH_FRAMES} total={3} />);

    const beforeImg = screen.getByAltText("Before frame for step 2: click Save");
    expect(beforeImg.className).not.toContain("frame-img-blurred");

    const toggle = screen.getByRole("button", { name: "Blur sensitive text" });
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(beforeImg.className).toContain("frame-img-blurred");
  });

  it("supports keyboard navigation and shows a clean empty state for steps without retained frames", () => {
    render(<ReplayMonitor workflowName="Invoice review" steps={STEPS_WITH_FRAMES} total={3} />);

    const step2 = screen.getByRole("button", { name: /02.*click.*Save/i });
    fireEvent.keyDown(step2, { key: "ArrowDown" });

    expect(screen.getByText(/Step 03 · click Close/)).toBeTruthy();
    expect(screen.getByText(/No local frames were retained on disk for this step/)).toBeTruthy();
  });

  it("rejects remote URLs and only renders inert local data:image/* URIs", () => {
    const unsafeSteps: RunStep[] = [
      {
        index: 0,
        action: "click",
        target: "Save",
        state: "verified",
        before_screenshot: "https://evil.example/pixel.png",
        after_screenshot: "javascript:alert(1)",
      },
    ];
    render(<ReplayMonitor workflowName="Unsafe test" steps={unsafeSteps} total={1} />);

    expect(screen.queryByRole("img")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /01.*click.*Save/i }));
    expect(screen.getByText(/No local frames were retained on disk for this step/)).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
