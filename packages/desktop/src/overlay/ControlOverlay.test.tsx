import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  engineInvoke,
  engineTry,
  ensureControlOverlayCaptureExcluded,
  setControlOverlayInteractive,
  setControlOverlayVisible,
} from "../lib/engine";
import { ControlOverlay } from "./ControlOverlay";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    ensureControlOverlayCaptureExcluded: vi.fn(() => Promise.resolve()),
    inTauri: vi.fn(() => false),
    onEngineEvent: vi.fn(() => Promise.resolve(() => {})),
    setControlOverlayInteractive: vi.fn(() => Promise.resolve()),
    setControlOverlayVisible: vi.fn(() => Promise.resolve()),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

it("keeps an active overlay capture-excluded and hides its controls", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });

  render(<ControlOverlay />);

  expect(await screen.findByText("Recording demonstration")).toBeTruthy();
  expect(screen.getByText("Step pending")).toBeTruthy();
  expect(screen.getByText("LOCAL CAPTURE")).toBeTruthy();
  await waitFor(() =>
    expect(ensureControlOverlayCaptureExcluded).toHaveBeenCalledOnce(),
  );
  await waitFor(() =>
    expect(setControlOverlayInteractive).toHaveBeenCalledWith(false),
  );
  expect(setControlOverlayInteractive).not.toHaveBeenCalledWith(true);
  expect(setControlOverlayVisible).toHaveBeenCalledWith(true);
  expect(screen.queryByRole("button")).toBeNull();
  expect(engineInvoke).not.toHaveBeenCalled();
});
