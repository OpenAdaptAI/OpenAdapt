import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineInvoke, engineTry, EVT } from "../lib/engine";
import { RecordReview } from "./RecordReview";

const eventMocks = vi.hoisted(() => ({
  handlers: new Map<string, (payload: unknown) => void>(),
}));

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(
      (event: string, handler: (payload: unknown) => void) => {
        eventMocks.handlers.set(event, handler);
        return Promise.resolve(() => eventMocks.handlers.delete(event));
      },
    ),
  };
});

vi.mock("../overlay/preferences", () => ({
  overlayPresentationEnabled: () => false,
}));

afterEach(() => {
  cleanup();
  eventMocks.handlers.clear();
  vi.clearAllMocks();
});

it("shows automatic compile progress and retries a retained recording", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: false,
    paused: false,
    duration_secs: 0,
    capture_id: null,
    controls: { pause: false, resume: false, stop: false },
  });
  const onCompiled = vi.fn();
  render(<RecordReview onCompiled={onCompiled} />);

  await waitFor(() => {
    expect(eventMocks.handlers.has(EVT.RECORDING_STOPPED)).toBe(true);
    expect(eventMocks.handlers.has(EVT.COMPILE_PROGRESS)).toBe(true);
  });

  act(() => {
    eventMocks.handlers.get(EVT.RECORDING_STOPPED)?.({ capture_id: "cap-1" });
  });
  expect(await screen.findByText("Building your workflow")).toBeTruthy();
  expect(screen.getByText(/raw recording stays unchanged/i)).toBeTruthy();

  act(() => {
    eventMocks.handlers.get(EVT.COMPILE_PROGRESS)?.({
      capture_id: "cap-1",
      state: "failed",
      error: "The raw recording was retained and is ready for another attempt.",
      recording_retained: true,
    });
  });
  expect(await screen.findByText("Compilation needs attention")).toBeTruthy();
  expect(screen.getByText(/raw recording was retained/i)).toBeTruthy();

  vi.mocked(engineInvoke).mockResolvedValue({
    ok: true,
    workflow_id: "workflow-1",
    recording_retained: true,
  });
  fireEvent.click(screen.getByRole("button", { name: "Retry compilation" }));

  await waitFor(() => {
    expect(engineInvoke).toHaveBeenCalledWith("compile_recording", {
      capture_id: "cap-1",
    });
    expect(onCompiled).toHaveBeenCalledWith("workflow-1", { backend: "web" });
  });
});

it("opens one workflow when the event and command return report the same compile", async () => {
  vi.mocked(engineTry).mockResolvedValue({
    recording: false,
    paused: false,
    duration_secs: 0,
    capture_id: null,
    controls: { pause: false, resume: false, stop: false },
  });
  vi.mocked(engineInvoke).mockImplementation(async (command) => {
    if (command === "start_recording") {
      return { capture_id: "cap-1", recording: true };
    }
    if (command === "stop_recording") {
      act(() => {
        eventMocks.handlers.get(EVT.COMPILE_PROGRESS)?.({
          capture_id: "cap-1",
          state: "compiled",
          bundle_id: "workflow-1",
          recording_retained: true,
        });
      });
      return {
        capture_id: "cap-1",
        compile: {
          ok: true,
          workflow_id: "workflow-1",
          recording_retained: true,
        },
      };
    }
    return {};
  });
  const onCompiled = vi.fn();
  render(<RecordReview onCompiled={onCompiled} />);

  await waitFor(() => {
    expect(eventMocks.handlers.has(EVT.COMPILE_PROGRESS)).toBe(true);
  });
  fireEvent.click(screen.getByRole("button", { name: "Start recording" }));
  act(() => {
    eventMocks.handlers.get(EVT.STATUS_UPDATE)?.({
      recording: true,
      paused: false,
      duration_secs: 1,
      capture_id: "cap-1",
      controls: { pause: false, resume: false, stop: true },
    });
  });
  fireEvent.click(await screen.findByRole("button", { name: "Stop" }));

  await waitFor(() => {
    expect(onCompiled).toHaveBeenCalledTimes(1);
    expect(onCompiled).toHaveBeenCalledWith("workflow-1", { backend: "web" });
  });
});
