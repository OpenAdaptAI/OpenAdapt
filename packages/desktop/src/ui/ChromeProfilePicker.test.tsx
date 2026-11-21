import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { ChromeProfilePicker } from "./ChromeProfilePicker";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const NOTICE = { title: "Use a Chrome profile for OpenAdapt", text: "Notice body.", sha256: "a".repeat(64) };

function profile(overrides: Record<string, unknown> = {}) {
  return {
    profile_id: "cp-12345678",
    name: "Work portals",
    acknowledged_at: 1,
    consent_current: true,
    open: false,
    browser_cdp_endpoint: null,
    ...overrides,
  };
}

function engine(profiles: unknown[]) {
  vi.mocked(engineTry).mockImplementation(async (cmd: string) => {
    if (cmd === CMD.CHROME_PROFILE_LIST) return { ok: true, profiles } as never;
    if (cmd === CMD.CHROME_PROFILE_NOTICE) return NOTICE as never;
    return null as never;
  });
}

it("creates a profile only after the person agrees to the notice", async () => {
  engine([]);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, profile_id: "cp-1", name: "Work" });
  render(<ChromeProfilePicker url="" endpoint="" disabled={false} idPrefix="t" onEndpoint={() => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "Use a Chrome profile" }));
  expect(await screen.findByText("Notice body.")).toBeTruthy();
  const agree = screen.getByRole("button", { name: "Agree" }) as HTMLButtonElement;
  fireEvent.change(screen.getByLabelText("Profile name"), { target: { value: "Work" } });
  expect(agree.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("I've read this and agree"));
  expect(agree.disabled).toBe(false);
  fireEvent.click(agree);
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.CHROME_PROFILE_CREATE, {
      name: "Work",
      notice_sha256: NOTICE.sha256,
      acknowledged: true,
    }),
  );
});

it("fills the signed-in browser address with the endpoint the engine verified", async () => {
  engine([profile()]);
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: true,
    profile_id: "cp-12345678",
    open: true,
    browser_cdp_endpoint: "http://127.0.0.1:51234",
  });
  const onEndpoint = vi.fn();
  render(
    <ChromeProfilePicker url="https://portal.example/" endpoint="" disabled={false} idPrefix="t" onEndpoint={onEndpoint} />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open in Chrome" }));
  await waitFor(() => expect(onEndpoint).toHaveBeenCalledWith("http://127.0.0.1:51234"));
  expect(engineInvoke).toHaveBeenCalledWith(CMD.CHROME_PROFILE_OPEN, {
    profile_id: "cp-12345678",
    url: "https://portal.example/",
  });
});

it("shows the engine's refusal and keeps the address unchanged", async () => {
  engine([profile()]);
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: false,
    code: "profile_in_use",
    error: "This profile is already open in a Chrome window that OpenAdapt didn't open.",
  });
  const onEndpoint = vi.fn();
  render(<ChromeProfilePicker url="" endpoint="" disabled={false} idPrefix="t" onEndpoint={onEndpoint} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open in Chrome" }));
  expect(await screen.findByText(/already open in a Chrome window/)).toBeTruthy();
  expect(onEndpoint).not.toHaveBeenCalled();
});

it("asks again when the notice changed instead of offering to open", async () => {
  engine([profile({ consent_current: false })]);
  render(<ChromeProfilePicker url="" endpoint="" disabled={false} idPrefix="t" onEndpoint={() => {}} />);
  expect(await screen.findByRole("button", { name: "Review the notice again" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Open in Chrome" })).toBeNull();
});
