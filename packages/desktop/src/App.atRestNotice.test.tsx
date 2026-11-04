import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// Flow records each write of patient data it can't encrypt yet in a local
// ledger; the window must say so (openadapt_desktop/at_rest_notice.py).

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

function engineWith(atRest: Record<string, unknown> | null) {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_AUTH_STATUS) return { authenticated: true };
    if (command === CMD.GET_WORKFLOWS) return [];
    if (command === CMD.GET_FIRST_WORKFLOW_STATE) return { ok: true, state: null };
    if (command === CMD.GET_NEEDS_ATTENTION) {
      return { count: 0, open_halts: 0, failed_runs: 0 };
    }
    if (command === CMD.GET_SYNC_STATE) return { state: "synced", queued: 0 };
    if (command === CMD.AUTHORING_STATUS) return { status: "idle" };
    if (command === CMD.AT_REST_STATUS) return atRest;
    return fallback ?? null;
  });
}

it("names unencrypted writes and where they are listed", async () => {
  engineWith({
    strict: false,
    unencrypted_writes: 3,
    latest_purpose: "a run directory at /tmp/run",
    ledger: "/Users/me/.openadapt/plaintext-at-rest.jsonl",
  });
  render(<App />);
  const [notice, ...others] = await screen.findAllByText(/without encryption/);
  expect(others).toHaveLength(0);
  expect(notice.textContent).toContain("3 recordings or run records");
  expect(notice.textContent).toContain("/Users/me/.openadapt/plaintext-at-rest.jsonl");
});

it("shows nothing when nothing was written unencrypted", async () => {
  engineWith({ strict: false, unencrypted_writes: 0, latest_purpose: "", ledger: "x" });
  render(<App />);
  await waitFor(() =>
    expect(vi.mocked(engineTry)).toHaveBeenCalledWith(CMD.AT_REST_STATUS, {}, null),
  );
  expect(screen.queryAllByText(/without encryption/)).toHaveLength(0);
});
