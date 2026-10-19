import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// The permission card only renders on macOS, and the platform is read once
// when the module loads.
vi.hoisted(() => {
  Object.defineProperty(window.navigator, "platform", {
    value: "MacIntel",
    configurable: true,
  });
});

vi.mock("../lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    ffmpegRuntimeStatus: vi.fn(async () => ({
      phase: "ready",
      source: "managed",
      runtime_version: "8.1.2-r1",
      target: "aarch64-apple-darwin",
    })),
    onFfmpegRuntimeStatus: vi.fn(async () => () => {}),
  };
});

import { CMD, engineTry } from "../lib/engine";
import { Onboarding } from "./Onboarding";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("doesn't call permissions missing when the engine can't check them", async () => {
  // Engine offline: every call falls back.
  vi.mocked(engineTry).mockImplementation(async (_command, _params, fallback) => fallback);

  render(<Onboarding onStart={() => {}} />);

  await waitFor(() => expect(screen.getAllByText("unknown")).toHaveLength(3));
  expect(screen.queryByText("needed")).toBeNull();
  expect(screen.queryByText(/Grant the permissions/)).toBeNull();
  expect(screen.getByText(/can't reach the local engine/)).toBeTruthy();
  const start = screen.getByRole("button", { name: "Choose an app and task" });
  expect((start as HTMLButtonElement).disabled).toBe(true);
});

it("still says a permission is needed when the engine reports it missing", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.CHECK_PERMISSIONS) {
      return { screen_recording: true, accessibility: false, input_monitoring: true };
    }
    return fallback;
  });

  render(<Onboarding onStart={() => {}} />);

  expect(await screen.findByText("needed")).toBeTruthy();
  expect(screen.getAllByText("granted")).toHaveLength(2);
  expect(screen.getByText(/Grant the permissions below/)).toBeTruthy();
});
