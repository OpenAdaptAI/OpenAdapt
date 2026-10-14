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

vi.mock("./screens/Welcome", () => ({
  Welcome: ({ onShow, onSample }: { onShow: () => void; onSample: () => void }) => (
    <div>
      <button type="button" onClick={onShow}>
        Show OpenAdapt your task
      </button>
      <button type="button" onClick={onSample}>
        See it work on a sample app
      </button>
    </div>
  ),
}));

vi.mock("./screens/RecordReview", () => ({
  RecordReview: () => <div>First workflow recording</div>,
}));

vi.mock("./screens/Sample", () => ({
  Sample: () => <div>Sample screen</div>,
}));

vi.mock("./screens/CheckIt", () => ({
  CheckIt: ({ workflowId }: { workflowId: string }) => (
    <div>Check it screen for {workflowId}</div>
  ),
}));

vi.mock("./screens/WatchRun", () => ({
  WatchRun: ({
    firstWorkflow,
    workflowId,
    onRunningChange,
    onQualify,
  }: {
    firstWorkflow?: boolean;
    workflowId: string;
    onRunningChange?: (running: boolean) => void;
    onQualify: (id: string) => void;
  }) => (
    <div>
      <span>{firstWorkflow ? "First supervised replay" : "Normal replay"}</span>
      <button type="button" onClick={() => onRunningChange?.(true)}>
        Start supervised execution
      </button>
      <button type="button" onClick={() => onQualify(workflowId)}>
        Go to the next step
      </button>
    </div>
  ),
}));

import App from "./App";
import { CMD, engineTry, EVT } from "./lib/engine";
import type { FirstWorkflowState } from "./lib/types";

function bootDefaults(command: string): unknown {
  if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
  if (command === CMD.GET_NEEDS_ATTENTION) {
    return { count: 0, open_halts: 0, failed_runs: 0 };
  }
  if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
  if (command === CMD.AUTHORING_STATUS) return { status: "idle" };
  return undefined;
}

function first(stage: FirstWorkflowState["stage"]): FirstWorkflowState {
  return {
    stage,
    capture_id: "capture-1",
    workflow_id: "workflow-1",
    target: { backend: "web", url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-27T00:00:00Z",
  };
}

afterEach(() => {
  cleanup();
  appEventMocks.handlers.clear();
  vi.clearAllMocks();
});

it("stays on the welcome screen when its durable stage cannot be saved", async () => {
  let stageAttempts = 0;
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: null };
    }
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      stageAttempts += 1;
      return { ok: stageAttempts > 1 };
    }
    return fallback ?? null;
  });

  render(<App />);
  const start = await screen.findByRole("button", {
    name: "Show OpenAdapt your task",
  });
  fireEvent.click(start);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(
    "Desktop couldn't save your place in setup. Check the local engine, then try again.",
  );
  expect(screen.getByRole("button", { name: "Show OpenAdapt your task" })).toBeTruthy();
  expect(screen.queryByText("First workflow recording")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Show OpenAdapt your task" }));
  await waitFor(() =>
    expect(screen.getByText("First workflow recording")).toBeTruthy(),
  );
});

it("opens the sample without an account, a recording, or a saved stage", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: false };
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: null };
    return fallback ?? null;
  });

  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "See it work on a sample app" }),
  );

  expect(await screen.findByText("Sample screen")).toBeTruthy();
  expect(engineTry).not.toHaveBeenCalledWith(
    CMD.SET_FIRST_WORKFLOW_STAGE,
    expect.anything(),
    expect.anything(),
  );
});

it("opens Check it and leaves the read-only first-run path before anything ran", async () => {
  const stages: unknown[] = [];
  vi.mocked(engineTry).mockImplementation(async (command, params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: first("review") };
    }
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      stages.push(params);
      return { ok: true, state: first("complete") };
    }
    return fallback ?? null;
  });

  render(<App />);

  expect(await screen.findByText("Check it screen for workflow-1")).toBeTruthy();
  expect(stages).toEqual([{ stage: "complete", workflow_id: "workflow-1" }]);
});

it("takes a saved first result to Check it through the allowed stages", async () => {
  const stages: unknown[] = [];
  let current = first("result");
  vi.mocked(engineTry).mockImplementation(async (command, params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: current };
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      stages.push(params);
      current = {
        ...current,
        stage: (params as { stage: FirstWorkflowState["stage"] }).stage,
      };
      return { ok: true, state: current };
    }
    return fallback ?? null;
  });

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Go to the next step" }));

  expect(await screen.findByText("Check it screen for workflow-1")).toBeTruthy();
  expect(stages).toEqual([
    { stage: "qualification_after_result", workflow_id: "workflow-1" },
    { stage: "complete", workflow_id: "workflow-1" },
  ]);
});

it("keeps an uncertain first run on its run screen until the target is checked", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: first("reconciliation") };
    }
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      throw new Error("an uncertain first run must not change stage here");
    }
    return fallback ?? null;
  });

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Go to the next step" }));

  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  expect(screen.queryByText(/Check it screen/)).toBeNull();
});

it("keeps navigation locked while the supervised run is running", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test workflow", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: first("result") };
    }
    return fallback ?? null;
  });

  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Start supervised execution" }),
  );

  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "Tasks" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true),
  );
  expect(
    (
      screen.getByRole("button", {
        name: "Settings and advanced tools",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

it("keeps navigation locked from recording stop until Check it opens", async () => {
  let firstState: FirstWorkflowState = { ...first("record"), workflow_id: null };
  vi.mocked(engineTry).mockImplementation(async (command, params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: firstState };
    }
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      firstState = {
        ...firstState,
        stage: (params as { stage: FirstWorkflowState["stage"] }).stage,
      };
      return { ok: true, state: firstState };
    }
    return fallback ?? null;
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
  const tasks = screen.getByRole("button", { name: "Tasks" });
  expect((tasks as HTMLButtonElement).disabled).toBe(true);

  firstState = { ...firstState, stage: "review", workflow_id: "workflow-1" };
  act(() => {
    appEventMocks.handlers.get(EVT.COMPILE_PROGRESS)?.({
      capture_id: "capture-1",
      state: "compiled",
      bundle_id: "workflow-1",
    });
  });

  expect(await screen.findByText("Check it screen for workflow-1")).toBeTruthy();
  await waitFor(() =>
    expect((tasks as HTMLButtonElement).disabled).toBe(false),
  );
});

it("puts Settings and every expert tool behind the gear", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test workflow", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: null };
    return fallback ?? null;
  });

  render(<App />);
  expect(await screen.findByRole("heading", { name: "Tasks" })).toBeTruthy();
  const nav = screen.getByRole("navigation", { name: "Desktop navigation" });
  expect(nav.textContent).toBe("TasksNeeds youResults");

  fireEvent.click(screen.getByRole("button", { name: "Settings and advanced tools" }));
  expect(screen.getByRole("menuitem", { name: /^Settings/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("menuitem", { name: /^Advanced/ }));

  expect(await screen.findByRole("heading", { name: "Advanced" })).toBeTruthy();
  for (const tool of [
    "Open exact checks",
    "Open all versions",
    "Open recording",
    "Open runner",
    "Open phone pairing",
    "Open setup",
  ]) {
    expect(screen.getByRole("button", { name: tool })).toBeTruthy();
  }
});

it("leaves loading when authoring status is missing", async () => {
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
    if (command === CMD.AUTHORING_STATUS) return null;
    return null;
  });

  render(<App />);
  expect(
    await screen.findByRole("button", { name: "Show OpenAdapt your task" }),
  ).toBeTruthy();
  expect(screen.queryByText("Loading…")).toBeNull();
});

it("won't start another task while an uncertain first run is unchecked", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    const boot = bootDefaults(command);
    if (boot !== undefined) return boot;
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Test", steps: 1 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) {
      return { ok: true, state: first("reconciliation") };
    }
    if (command === CMD.SET_FIRST_WORKFLOW_STAGE) {
      throw new Error("the uncertain first run must keep its lock");
    }
    return fallback ?? null;
  });

  render(<App />);
  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Tasks" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Show OpenAdapt a new task" }),
  );

  expect((await screen.findByRole("alert")).textContent).toContain(
    "Check the target of your last run before you record another task.",
  );
  expect(await screen.findByText("First supervised replay")).toBeTruthy();
  expect(screen.queryByText("First workflow recording")).toBeNull();
});
