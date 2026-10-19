import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// The authoring notice names the assistant that asked to drive the job. A
// Claude.ai user must never be told to allow ChatGPT, and when the engine
// hasn't named a client the notice must not guess one. The engine passes on
// only the names it knows (openadapt_desktop/authoring_runner.py).

vi.mock("./lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(() => Promise.resolve(() => {})),
    sidecarRunning: vi.fn(async () => true),
  };
});

vi.mock("./screens/Welcome", () => ({
  Welcome: () => <div>Welcome screen</div>,
}));

import App from "./App";
import { CMD, engineTry } from "./lib/engine";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function noticeFor(authoring: Record<string, unknown>): Promise<string> {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: null };
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    if (command === CMD.AUTHORING_STATUS) return authoring;
    return fallback ?? null;
  });
  render(<App />);
  const notice = await screen.findByText(/this job|this computer is bound|account is asking/i);
  return notice.textContent ?? "";
}

it.each([
  [
    { status: "bound", coach_only: true, client_display: "Claude" },
    "This job is coach-only on this window. Claude can suggest; you click.",
  ],
  [
    { status: "bound", allowed: false, client_display: "Claude" },
    "This computer is bound. Pin the window, then Allow Claude to drive this job.",
  ],
  [
    { status: "bound", coach_only: true, client_display: "ChatGPT" },
    "This job is coach-only on this window. ChatGPT can suggest; you click.",
  ],
])("names the client the engine reported: %j", async (authoring, expected) => {
  expect(await noticeFor(authoring)).toBe(expected);
});

it.each([
  [
    { status: "bound", coach_only: true },
    "This job is coach-only on this window. Your assistant can suggest; you click.",
  ],
  [
    { status: "bound", allowed: false },
    "This computer is bound. Pin the window, then Allow your assistant to drive this job.",
  ],
  [{ status: "pending_allow" }, "Allow your assistant to drive this job"],
  [
    { status: "replace_allow" },
    "A different assistant account is asking. Allow it to replace the current one?",
  ],
])("never guesses a client it wasn't told: %j", async (authoring, expected) => {
  const text = await noticeFor(authoring);
  expect(text).toBe(expected);
  expect(text).not.toContain("ChatGPT");
});
