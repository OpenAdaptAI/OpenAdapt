import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    openExternal: vi.fn(),
    ffmpegRuntimeStatus: vi.fn(async () => ({
      phase: "ready",
      source: "managed",
      runtime_version: "8.1.2-r1",
      target: "aarch64-apple-darwin",
    })),
    onFfmpegRuntimeStatus: vi.fn(async () => () => {}),
  };
});

import { CMD, engineTry, openExternal } from "../lib/engine";
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
  const start = screen.getByRole("button", { name: "Choose an app and workflow" });
  expect((start as HTMLButtonElement).disabled).toBe(true);
});

it("still says a permission is needed when the engine reports it missing", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.CHECK_PERMISSIONS) {
      return {
        screen_recording: true, accessibility: false, input_monitoring: true,
        control_permission: {
          setup_mode: "legacy", label: "Accessibility",
          settings_url: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
          instructions: "Use Accessibility on earlier macOS.",
        },
      };
    }
    return fallback;
  });

  render(<Onboarding onStart={() => {}} />);

  expect(await screen.findByText("needed")).toBeTruthy();
  expect(screen.getAllByText("granted")).toHaveLength(2);
  expect(screen.getByText(/Grant the permissions below/)).toBeTruthy();
});


it.each([true, false, null])("shows scoped control guidance without claiming a global grant (%s)", async (trusted) => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.CHECK_PERMISSIONS) {
      return {
        screen_recording: true, accessibility: trusted, input_monitoring: true,
        control_permission: {
          setup_mode: "app_selection", label: "System & App Protection Override",
          settings_url: "x-apple.systempreferences:com.apple.preference.security",
          instructions: "Click +, Choose Apps, then select Ask First or At Any Time.",
        },
      };
    }
    return fallback;
  });
  render(<Onboarding onStart={() => {}} />);
  expect(await screen.findByText("System & App Protection Override")).toBeTruthy();
  expect(screen.getByText(trusted ? "check target" : "not confirmed")).toBeTruthy();
  expect(screen.getAllByText("granted")).toHaveLength(2);
  expect(screen.queryByText("Accessibility")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  expect(openExternal).toHaveBeenCalledWith("x-apple.systempreferences:com.apple.preference.security");
  const start = screen.getByRole("button", { name: "Choose an app and workflow" });
  expect((start as HTMLButtonElement).disabled).toBe(!trusted);
});

it("keeps an unavailable native API unknown on the legacy path", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.CHECK_PERMISSIONS) {
      return {
        screen_recording: null, accessibility: null, input_monitoring: true,
        control_permission: {
          setup_mode: "legacy", label: "Accessibility", settings_url: "legacy-settings",
          instructions: "Use Accessibility on earlier macOS.",
        },
      };
    }
    return fallback;
  });
  render(<Onboarding onStart={() => {}} />);
  await waitFor(() => expect(screen.getAllByText("unknown")).toHaveLength(2));
  expect(screen.queryByText("needed")).toBeNull();
  expect((screen.getByRole("button", { name: "Choose an app and workflow" }) as HTMLButtonElement).disabled).toBe(true);
});
