import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  engineInvoke,
  engineTry,
  setControlOverlayInteractive,
  setControlOverlayPresentation,
  setControlOverlayVisible,
} from "../lib/engine";
import { ControlOverlay } from "./ControlOverlay";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    inTauri: vi.fn(() => false),
    onEngineEvent: vi.fn(() => Promise.resolve(() => {})),
    setControlOverlayInteractive: vi.fn(() => Promise.resolve()),
    setControlOverlayPresentation: vi.fn(() => Promise.resolve()),
    setControlOverlayVisible: vi.fn(() => Promise.resolve()),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

it("keeps an active presentation overlay click-through and hides its controls", async () => {
  window.localStorage.setItem(
    "openadapt.control-overlay.include-in-recordings.v1",
    "true",
  );
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });

  render(<ControlOverlay />);

  expect(await screen.findByText("Watching your demonstration")).toBeTruthy();
  await waitFor(() =>
    expect(setControlOverlayPresentation).toHaveBeenCalledWith(true),
  );
  await waitFor(() =>
    expect(setControlOverlayInteractive).toHaveBeenCalledWith(false),
  );
  expect(setControlOverlayInteractive).not.toHaveBeenCalledWith(true);
  expect(setControlOverlayVisible).toHaveBeenCalledWith(true);
  expect(screen.queryByRole("button")).toBeNull();
  expect(engineInvoke).not.toHaveBeenCalled();
});
