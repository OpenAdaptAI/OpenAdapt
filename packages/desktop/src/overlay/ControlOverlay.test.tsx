import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  engineInvoke,
  engineTry,
  ensureControlOverlayCaptureExcluded,
  onEngineEvent,
  setControlOverlayInteractive,
  setControlOverlayLayout,
  setControlOverlayVisible,
} from "../lib/engine";
import { AUTH_PAUSE_COPY } from "./coach";
import { ControlOverlay } from "./ControlOverlay";

const engineHandlers: Record<string, (payload: unknown) => void> = {};

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    ensureControlOverlayCaptureExcluded: vi.fn(() => Promise.resolve()),
    inTauri: vi.fn(() => false),
    onEngineEvent: vi.fn((event, handler) => {
      engineHandlers[event] = handler;
      return Promise.resolve(() => {});
    }),
    setControlOverlayInteractive: vi.fn(() => Promise.resolve()),
    setControlOverlayLayout: vi.fn(() => Promise.resolve()),
    setControlOverlayVisible: vi.fn(() => Promise.resolve()),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
  for (const key of Object.keys(engineHandlers)) delete engineHandlers[key];
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

it("paints a local coach hint during recording without taking clicks", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });

  render(<ControlOverlay />);
  expect(await screen.findByText("Recording demonstration")).toBeTruthy();
  await waitFor(() => expect(onEngineEvent).toHaveBeenCalled());

  engineHandlers.coach?.({
    hint: "Open the claim screen",
    turn: "your_turn",
  });

  expect(await screen.findByText("Open the claim screen")).toBeTruthy();
  expect(screen.getByText("Your turn")).toBeTruthy();
  expect(screen.getByText("LOCAL CAPTURE")).toBeTruthy();
  expect(screen.queryByText("VERIFIED")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
  await waitFor(() =>
    expect(setControlOverlayInteractive).toHaveBeenCalledWith(false),
  );
  expect(setControlOverlayInteractive).not.toHaveBeenCalledWith(true);
  expect(document.querySelector(".overlay-target-ring")).toBeNull();
});

it("opens auth pause controls only at a pause boundary", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });
  vi.mocked(engineInvoke).mockResolvedValue({
    schema_version: "openadapt.control-overlay-coach/v1",
    hint: null,
    turn: "your_turn",
    pause_reason: null,
    target: null,
    operator_response: "continue",
  });

  render(<ControlOverlay />);
  expect(await screen.findByText("Recording demonstration")).toBeTruthy();
  await waitFor(() => expect(onEngineEvent).toHaveBeenCalled());

  engineHandlers.coach?.({
    turn: "auth",
    pause_reason: "auth",
  });

  expect(await screen.findByText(AUTH_PAUSE_COPY)).toBeTruthy();
  await waitFor(() =>
    expect(setControlOverlayInteractive).toHaveBeenCalledWith(true),
  );
  expect(screen.getByRole("button", { name: "Continue OpenAdapt" })).toBeTruthy();
  expect(setControlOverlayLayout).toHaveBeenCalledWith("paused");
});

it("does not draw a ghost ring without an exact observation binding", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });

  render(<ControlOverlay />);
  expect(await screen.findByText("Recording demonstration")).toBeTruthy();
  await waitFor(() => expect(onEngineEvent).toHaveBeenCalled());

  engineHandlers.coach?.({
    hint: "Open the claim screen",
    turn: "your_turn",
    target: {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x: 0.2, y: 0.2, width: 0.15, height: 0.1 },
    },
  });

  expect(await screen.findByText("Open the claim screen")).toBeTruthy();
  expect(document.querySelector(".overlay-target-ring")).toBeNull();
  expect(document.querySelector(".control-overlay-host.stage")).toBeNull();
});

it("draws a click-through ring only when the rect is bound to an observation", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: true,
    paused: false,
    controls: { pause: false, resume: false, stop: true },
  });

  render(<ControlOverlay />);
  expect(await screen.findByText("Recording demonstration")).toBeTruthy();
  await waitFor(() => expect(onEngineEvent).toHaveBeenCalled());

  engineHandlers.coach?.({
    hint: "Open the claim screen",
    turn: "your_turn",
    target: {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x: 0.2, y: 0.25, width: 0.15, height: 0.1 },
      binding: {
        kind: "observation_hmac_sha256",
        observation_hmac_sha256: "ab".repeat(32),
      },
    },
  });

  expect(await screen.findByText("Open the claim screen")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
  const ring = document.querySelector(".overlay-target-ring");
  expect(ring).toBeTruthy();
  expect(document.querySelector(".control-overlay-host.stage")).toBeTruthy();
  await waitFor(() =>
    expect(setControlOverlayLayout).toHaveBeenCalledWith("stage"),
  );
  await waitFor(() =>
    expect(setControlOverlayInteractive).toHaveBeenCalledWith(false),
  );
});
