import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, expect, it, vi } from "vitest";

// With the local engine down, every engine call falls back. A fallback must
// not read as a real answer: no workflows is not the same as "this person has
// never shown a task", so Desktop must not send them back to first run.

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
    sidecarRunning: vi.fn(async () => false),
  };
});

vi.mock("./screens/Tasks", () => ({
  Tasks: () => <h1>Tasks</h1>,
}));

import App from "./App";
import { CMD, EVT, engineTry } from "./lib/engine";

let engineAnswers = false;

function engine(): void {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (!engineAnswers) return fallback;
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: false };
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "workflow-1", name: "Enter a referral", steps: 4 }];
    }
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: null };
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.AUTHORING_STATUS) return { status: "idle" };
    return fallback;
  });
}

afterEach(() => {
  cleanup();
  appEventMocks.handlers.clear();
  vi.clearAllMocks();
  engineAnswers = false;
});

it("says the engine is offline instead of showing first run", async () => {
  engine();
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "Waiting for the local engine" }),
  ).toBeTruthy();
  expect(screen.queryByText("Hand off the last step of a task")).toBeNull();
  expect(screen.queryByRole("button", { name: /Show OpenAdapt your task/ })).toBeNull();
  expect(screen.queryByText(/needed/)).toBeNull();
  expect(screen.queryByText(/Grant the permissions/)).toBeNull();
  expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
});

it("opens the existing tasks once the engine answers", async () => {
  engine();
  render(<App />);
  await screen.findByRole("heading", { name: "Waiting for the local engine" });

  engineAnswers = true;
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  expect(await screen.findByRole("heading", { name: "Tasks" })).toBeTruthy();
  expect(screen.queryByText("Hand off the last step of a task")).toBeNull();
});

it("reconnects on its own when the engine starts", async () => {
  engine();
  render(<App />);
  await screen.findByRole("heading", { name: "Waiting for the local engine" });

  engineAnswers = true;
  await act(async () => {
    appEventMocks.handlers.get(EVT.SIDECAR_STATE)?.({ running: true });
  });

  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "Tasks" })).toBeTruthy(),
  );
});
