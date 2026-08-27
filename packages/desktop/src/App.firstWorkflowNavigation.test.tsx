import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, expect, it, vi } from "vitest";

const appEventMocks = vi.hoisted(() => ({
  handlers: new Map<string, (payload: unknown) => void>(),
}));

vi.mock("./lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(
      (event: string, handler: (payload: unknown) => void) => {
        appEventMocks.handlers.set(event, handler);
        return Promise.resolve(() => appEventMocks.handlers.delete(event));
      },
    ),
    sidecarRunning: vi.fn(async () => true),
  };
});

vi.mock("./screens/Onboarding", () => ({
  Onboarding: ({ onStart }: { onStart: () => void }) => (
    <button type="button" onClick={onStart}>
      Start first workflow
    </button>
  ),
}));

vi.mock("./screens/RecordReview", () => ({
  RecordReview: () => <div>First workflow recording</div>,
}));

vi.mock("./screens/WatchRun", () => ({
  WatchRun: ({
    firstWorkflow,
    onRunningChange,
  }: {
    firstWorkflow?: boolean;
    onRunningChange?: (running: boolean) => void;
  }) => (
    <div>
      <span>{firstWorkflow ? "First supervised replay" : "Normal replay"}</span>
      <button type="button" onClick={() => onRunningChange?.(true)}>
        Start supervised execution
      </button>
    </div>
  ),
}));

vi.mock("./screens/Qualification", () => ({
  Qualification: ({
    onBack,
    backLabel,
  }: {
    onBack: () => void;
    backLabel?: string;
  }) => (
    <button type="button" onClick={onBack}>
      {backLabel || "Back to workflows"}
    </button>
  ),
}));

import App from "./App";
import { CMD, engineTry, EVT } from "./lib/engine";
import type { FirstWorkflowState } from "./lib/types";

afterEach(() => {
  cleanup();
  appEventMocks.handlers.clear();
  vi.clearAllMocks();
});

it("stays in onboarding when its durable stage cannot be saved", async () => {
  let stageAttempts = 0;
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: null };
    }
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      stageAttempts += 1;
      return { ok: stageAttempts > 1 };
    }
    return null;
  });

  render(<App />);
  const start = await screen.findByRole("button", {
    name: "Start first workflow",
  });
  fireEvent.click(start);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(
    "Desktop couldn't save your place in setup. Check the local engine, then try again.",
  );
  expect(screen.getByRole("button", { name: "Start first workflow" })).toBeTruthy();
  expect(screen.queryByText("First workflow recording")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Start first workflow" }));
  await waitFor(() =>
    expect(screen.getByText("First workflow recording")).toBeTruthy(),
  );
});

it("returns the pre-run action review to the supervised replay", async () => {
  const state = {
    stage: "qualification" as const,
    capture_id: "capture-1",
    workflow_id: "workflow-1",
    target: { backend: "web" as const, url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-27T00:00:00Z",
  };
  vi.mocked(engineTry).mockImplementation(async (command, params) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state };
    }
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      expect(params).toEqual({ stage: "review", workflow_id: "workflow-1" });
      return { ok: true, state: { ...state, stage: "review" } };
    }
    return null;
  });

  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Back to supervised run" }),
  );

  expect(await screen.findByText("First supervised replay")).toBeTruthy();
});

it("keeps the first-workflow context after a library visit", async () => {
  const state = {
    stage: "review" as const,
    capture_id: "capture-1",
    workflow_id: "workflow-1",
    target: { backend: "web" as const, url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-27T00:00:00Z",
  };
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test workflow", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state };
    }
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    return null;
  });

  render(<App />);
  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Workflows" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Watch run" }),
  );

  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  expect(screen.queryByText("Normal replay")).toBeNull();
});

it("keeps navigation locked while the supervised replay is running", async () => {
  const state = {
    stage: "review" as const,
    capture_id: "capture-1",
    workflow_id: "workflow-1",
    target: { backend: "web" as const, url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-27T00:00:00Z",
  };
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test workflow", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state };
    }
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    return null;
  });

  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Start supervised execution" }),
  );

  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "Workflows" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true),
  );
});

it("keeps navigation locked from recording stop through review initialization", async () => {
  let firstState: FirstWorkflowState = {
    stage: "record",
    capture_id: "capture-1",
    workflow_id: null,
    target: { backend: "web", url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-27T00:00:00Z",
  };
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: firstState };
    }
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    return null;
  });

  render(<App />);
  expect(await screen.findByText("First workflow recording")).toBeTruthy();
  await waitFor(() => {
    expect(appEventMocks.handlers.has(EVT.RECORDING_STOPPED)).toBe(true);
    expect(appEventMocks.handlers.has(EVT.COMPILE_PROGRESS)).toBe(true);
  });

  act(() => {
    appEventMocks.handlers.get(EVT.RECORDING_STOPPED)?.({
      capture_id: "capture-1",
    });
  });
  const workflows = screen.getByRole("button", { name: "Workflows" });
  expect((workflows as HTMLButtonElement).disabled).toBe(true);

  firstState = {
    ...firstState,
    stage: "review",
    workflow_id: "workflow-1",
  };
  act(() => {
    appEventMocks.handlers.get(EVT.COMPILE_PROGRESS)?.({
      capture_id: "capture-1",
      state: "compiled",
      bundle_id: "workflow-1",
    });
  });

  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  await waitFor(() =>
    expect((workflows as HTMLButtonElement).disabled).toBe(false),
  );
});
