import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// The updater plugin is registered only when the build carries an update
// feed, and no build does until signed releases ship. A "Check for updates"
// button would then always fail, so Settings asks the shell first.

const updater = vi.hoisted(() => ({ check: vi.fn() }));

vi.mock("@tauri-apps/plugin-updater", () => ({ check: updater.check }));

vi.mock("../lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(async (_command: string, _params: unknown, fallback: unknown) => fallback),
    openExternal: vi.fn(async () => {}),
    updaterAvailable: vi.fn(async () => false),
  };
});

import { openExternal, updaterAvailable } from "../lib/engine";
import { Settings } from "./Settings";

const RELEASES = "https://github.com/OpenAdaptAI/openadapt-desktop/releases";

function renderSettings() {
  return render(
    <Settings
      auth={{ authenticated: false }}
      onConnectCloud={() => {}}
      onSignedOut={() => {}}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("offers the releases page, not a check that can only fail, when updates are off", async () => {
  vi.mocked(updaterAvailable).mockResolvedValue(false);
  renderSettings();

  const releases = await screen.findByRole("button", { name: "Open the releases page" });
  expect(screen.queryByRole("button", { name: /check for updates/i })).toBeNull();
  expect(screen.getByText(/Each new release has to be installed by hand/)).toBeTruthy();

  fireEvent.click(releases);
  expect(openExternal).toHaveBeenCalledWith(RELEASES);
  expect(updater.check).not.toHaveBeenCalled();
});

it("keeps the update check when the build has an update feed, without raw errors", async () => {
  vi.mocked(updaterAvailable).mockResolvedValue(true);
  updater.check.mockRejectedValue(new Error("plugin updater not found: ipc://internal"));
  renderSettings();

  fireEvent.click(await screen.findByRole("button", { name: "Check for updates" }));

  expect(
    await screen.findByText(/OpenAdapt couldn't check for updates/),
  ).toBeTruthy();
  expect(screen.queryByText(/ipc:\/\/internal|plugin updater not found/)).toBeNull();
  expect(updater.check).toHaveBeenCalledTimes(1);
});
