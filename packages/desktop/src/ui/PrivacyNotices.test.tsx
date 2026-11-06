import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const events = vi.hoisted(() => ({ handlers: new Map<string, (payload: unknown) => void>() }));

vi.mock("../lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    engineInvoke: vi.fn(),
    openExternal: vi.fn(async () => {}),
    onEngineEvent: vi.fn((event: string, handler: (payload: unknown) => void) => {
      events.handlers.set(event, handler);
      return Promise.resolve(() => events.handlers.delete(event));
    }),
  };
});

import { CMD, EVT, engineInvoke, engineTry, openExternal } from "../lib/engine";
import type { PrivacySettings, UpdateStatus } from "../lib/types";
import { UpdateNotice, UsageStatsPrompt } from "./PrivacyNotices";

const UNANSWERED: PrivacySettings = {
  usage_stats: null,
  ask_usage_stats: true,
  usage_stats_managed_by_env: false,
  update_check: true,
  update_check_managed_by_env: false,
};

const RELEASE_URL =
  "https://github.com/OpenAdaptAI/OpenAdapt/releases/tag/openadapt-desktop-v0.26.0";

function update(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    enabled: true,
    managed_by_env: false,
    current_version: "0.25.0",
    latest_version: "0.26.0",
    available: true,
    url: RELEASE_URL,
    checked_at: 1,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  events.handlers.clear();
});

it("asks about usage stats before anything is saved, and saves Share", async () => {
  vi.mocked(engineTry).mockResolvedValue(UNANSWERED);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true });
  render(<UsageStatsPrompt engineUp />);

  expect(await screen.findByText("Help improve OpenAdapt")).toBeTruthy();
  expect(screen.getByText(/No screen content, keystrokes, file paths, or task text/)).toBeTruthy();
  expect(engineInvoke).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.SET_USAGE_STATS, { share: true }),
  );
  await waitFor(() => expect(screen.queryByText("Help improve OpenAdapt")).toBeNull());
});

it("saves No thanks", async () => {
  vi.mocked(engineTry).mockResolvedValue(UNANSWERED);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true });
  render(<UsageStatsPrompt engineUp />);

  fireEvent.click(await screen.findByRole("button", { name: "No thanks" }));
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.SET_USAGE_STATS, { share: false }),
  );
});

it("doesn't ask once answered, when the environment decides, or with no engine", async () => {
  vi.mocked(engineTry).mockResolvedValue({ ...UNANSWERED, ask_usage_stats: false });
  render(<UsageStatsPrompt engineUp />);
  await waitFor(() => expect(engineTry).toHaveBeenCalled());
  expect(screen.queryByText("Help improve OpenAdapt")).toBeNull();
  cleanup();

  vi.mocked(engineTry).mockResolvedValue(null);
  render(<UsageStatsPrompt engineUp={false} />);
  await waitFor(() => expect(engineTry).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("Help improve OpenAdapt")).toBeNull();
});

it("shows an available update with a download link, and can be dismissed", async () => {
  vi.mocked(engineTry).mockResolvedValue(update());
  render(<UpdateNotice engineUp />);

  expect(await screen.findByText(/OpenAdapt 0.26.0 is available/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Download" }));
  expect(openExternal).toHaveBeenCalledWith(RELEASE_URL);

  fireEvent.click(screen.getByRole("button", { name: "Dismiss update notice" }));
  expect(screen.queryByText(/is available/)).toBeNull();
});

it("stays quiet when up to date or turned off, and follows the check event", async () => {
  vi.mocked(engineTry).mockResolvedValue(update({ available: false, latest_version: "0.25.0" }));
  render(<UpdateNotice engineUp />);
  await waitFor(() => expect(engineTry).toHaveBeenCalledWith(CMD.GET_UPDATE_STATUS, {}, null));
  expect(screen.queryByText(/is available/)).toBeNull();

  act(() => events.handlers.get(EVT.UPDATE_STATUS)?.(update({ enabled: false })));
  expect(screen.queryByText(/is available/)).toBeNull();

  act(() => events.handlers.get(EVT.UPDATE_STATUS)?.(update()));
  expect(await screen.findByText(/OpenAdapt 0.26.0 is available/)).toBeTruthy();
});

it("keeps a newer available-release event when an older initial GET resolves later", async () => {
  let resolveInitial!: (status: UpdateStatus) => void;
  vi.mocked(engineTry).mockReturnValue(
    new Promise<UpdateStatus>((resolve) => {
      resolveInitial = resolve;
    }) as ReturnType<typeof engineTry>,
  );
  render(<UpdateNotice engineUp />);

  const handler = events.handlers.get(EVT.UPDATE_STATUS);
  expect(handler).toBeDefined();
  act(() => handler?.(update({ latest_version: "0.26.0", available: true, checked_at: 200 })));
  expect(await screen.findByText(/OpenAdapt 0.26.0 is available/)).toBeTruthy();

  await act(async () => {
    resolveInitial(update({ latest_version: null, available: false, checked_at: 100 }));
  });

  // A stale initial GET must not hide the newer event-driven notice.
  expect(screen.getByText(/OpenAdapt 0.26.0 is available/)).toBeTruthy();
});
