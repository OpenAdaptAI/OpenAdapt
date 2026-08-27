import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CMD, engineInvoke, engineTry, EVT } from "../lib/engine";
import type { CapabilityReport } from "../lib/types";
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

function capabilityReport(
  state: "available" | "permission_required" = "available",
): CapabilityReport {
  return {
    schema: "openadapt-desktop.capability-report/v1",
    generated_at: "2026-08-26T00:00:00Z",
    host: {
      os: "test",
      os_version: "1",
      arch: "arm64",
      app_version: "0.15.0",
    },
    surfaces: {
      web: {
        state,
        detail:
          state === "available"
            ? "The browser recorder is ready."
            : "Browser capture permission is required.",
        remediation:
          state === "available" ? null : "Grant permission, then check again.",
        driver: null,
      },
    },
  };
}

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

it("requires a real target and a named task for the first workflow", async () => {
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_CAPABILITIES) return capabilityReport();
    return {
      recording: false,
      paused: false,
      duration_secs: 0,
      capture_id: null,
      controls: { pause: false, resume: false, stop: false },
    };
  });
  vi.mocked(engineInvoke).mockResolvedValue({
    capture_id: "cap-1",
    recording: true,
  });

  render(<RecordReview firstWorkflow onCompiled={() => {}} />);

  const recordButton = screen.getByRole("button", {
    name: "Record this task",
  }) as HTMLButtonElement;
  expect(recordButton.disabled).toBe(true);
  expect(screen.getByText("Describe the task you want to record.")).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Task to record"), {
    target: { value: "Save one test value" },
  });
  expect(
    screen.getByText("Enter the page URL for the app you want to record."),
  ).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Page URL"), {
    target: { value: "https://example.test/form" },
  });

  await waitFor(() => expect(recordButton.disabled).toBe(false));
  fireEvent.click(recordButton);

  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith("start_recording", {
      target: { backend: "web", url: "https://example.test/form" },
      first_workflow: true,
      purpose: "Save one test value",
    }),
  );
});

it("keeps an invalid or unavailable target out of recording and rechecks it", async () => {
  let capabilityChecks = 0;
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_CAPABILITIES) {
      capabilityChecks += 1;
      return capabilityReport(
        capabilityChecks === 1 ? "permission_required" : "available",
      );
    }
    return {
      recording: false,
      paused: false,
      duration_secs: 0,
      capture_id: null,
      controls: { pause: false, resume: false, stop: false },
    };
  });

  render(<RecordReview firstWorkflow onCompiled={() => {}} />);
  fireEvent.change(screen.getByLabelText("Task to record"), {
    target: { value: "Read one test record" },
  });
  fireEvent.change(screen.getByLabelText("Page URL"), {
    target: { value: "not a URL" },
  });
  expect(screen.getByText("Enter a complete HTTP or HTTPS page URL.")).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Page URL"), {
    target: { value: "https://example.test" },
  });
  expect(
    (await screen.findAllByText(/Browser capture permission is required/)).length,
  ).toBeGreaterThan(0);
  expect(
    (screen.getByRole("button", { name: "Record this task" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() =>
    expect(
      (screen.getByRole("button", {
        name: "Record this task",
      }) as HTMLButtonElement).disabled,
    ).toBe(false),
  );
  expect(capabilityChecks).toBe(2);
  expect(engineInvoke).not.toHaveBeenCalled();
});

it("shows a recording failure and lets the user retry", async () => {
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_CAPABILITIES) return capabilityReport();
    return {
      recording: false,
      paused: false,
      duration_secs: 0,
      capture_id: null,
      controls: { pause: false, resume: false, stop: false },
    };
  });
  vi.mocked(engineInvoke).mockResolvedValue({
    capture_id: "cap-1",
    recording: true,
  });

  render(<RecordReview firstWorkflow onCompiled={() => {}} />);
  fireEvent.change(screen.getByLabelText("Task to record"), {
    target: { value: "Read one test record" },
  });
  fireEvent.change(screen.getByLabelText("Page URL"), {
    target: { value: "https://example.test" },
  });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", {
        name: "Record this task",
      }) as HTMLButtonElement).disabled,
    ).toBe(false),
  );

  await waitFor(() => expect(eventMocks.handlers.has(EVT.RECORDING_ERROR)).toBe(true));
  act(() => {
    eventMocks.handlers.get(EVT.RECORDING_ERROR)?.({
      error: "Browser setup failed before recording began",
    });
  });
  expect(screen.getByRole("alert").textContent).toContain(
    "Browser setup failed before recording began",
  );
  fireEvent.click(screen.getByRole("button", { name: "Try recording again" }));
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.START_RECORDING, {
      target: { backend: "web", url: "https://example.test" },
      first_workflow: true,
      purpose: "Read one test record",
    }),
  );
});
