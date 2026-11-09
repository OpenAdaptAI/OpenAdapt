import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const events = vi.hoisted(() => ({ handlers: new Map<string, (payload: unknown) => void>() }));

vi.mock("../lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    engineInvoke: vi.fn(),
    setShadowTrayStatus: vi.fn(async () => {}),
    onEngineEvent: vi.fn((event: string, handler: (payload: unknown) => void) => {
      events.handlers.set(event, handler);
      return Promise.resolve(() => events.handlers.delete(event));
    }),
  };
});

import { CMD, EVT, engineInvoke, engineTry, setShadowTrayStatus } from "../lib/engine";
import { parseDomains, readShadowStatus } from "../lib/shadowMode";
import {
  ShadowModeCard,
  ShadowModeIndicator,
  ShadowPromptBanner,
  useShadowMode,
} from "./ShadowMode";

const NOTICE = {
  version: "2026-10-17.4",
  sha256: "a".repeat(64),
  title: "Turn on shadow mode?",
  intro: "Shadow mode watches for a task you repeat.",
  sections: [
    {
      heading: "Who can access it",
      items: ["OpenAdapt never sends shadow mode data off this computer."],
    },
  ],
  acknowledge_label: "Turn on shadow mode",
  decline_label: "Not now",
  purge_and_disable_label: "Delete everything and turn off",
  text: "",
};

const OFF = { ...readShadowStatus({ available: true }), available: true };
const ON = {
  ...OFF,
  active: true,
  acknowledged_at: 1_000_000,
  epoch_ends_at: 1_086_400,
  keys_in_keychain: true,
  watches: "recordings" as const,
};
const PROMPT = {
  motif_id: "motif-abc",
  count: 6,
  title: "OpenAdapt noticed a repeated task",
  body: "You did this 6 times today.",
  actions: [
    { id: "review_draft" as const, label: "Review draft" },
    { id: "ignore" as const, label: "Ignore" },
  ],
};

let status: unknown = OFF;

beforeEach(() => {
  status = OFF;
  events.handlers.clear();
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) =>
    command === CMD.SHADOW_STATUS ? status : fallback,
  );
  vi.mocked(engineInvoke).mockReset();
  vi.mocked(setShadowTrayStatus).mockClear();
});

afterEach(cleanup);

describe("ShadowModeCard", () => {
  it("turns on only from the notice's acknowledge button, with the notice hash", async () => {
    vi.mocked(engineInvoke).mockImplementation(async (command) => {
      if (command === CMD.SHADOW_NOTICE) return NOTICE;
      if (command === CMD.SHADOW_ENABLE) return ON;
      throw new Error(`unexpected ${command}`);
    });
    render(<ShadowModeCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Read the notice to turn on" }));
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "OpenAdapt never sends shadow mode data off this computer.",
    );
    expect(engineInvoke).not.toHaveBeenCalledWith(CMD.SHADOW_ENABLE, expect.anything());

    fireEvent.click(screen.getByRole("button", { name: "Turn on shadow mode" }));
    await waitFor(() =>
      expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_ENABLE, {
        notice_sha256: NOTICE.sha256,
        acknowledged: true,
      }),
    );
    expect(await screen.findByText("On")).toBeTruthy();
    expect(screen.getByText(/in your system keychain/)).toBeTruthy();
  });

  it("declining the notice changes nothing", async () => {
    vi.mocked(engineInvoke).mockResolvedValue(NOTICE);
    render(<ShadowModeCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Read the notice to turn on" }));
    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(engineInvoke).toHaveBeenCalledTimes(1);
  });

  it("deletes everything and turns off in one click", async () => {
    status = ON;
    vi.mocked(engineInvoke).mockResolvedValue(OFF);
    render(<ShadowModeCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete everything and turn off" }));
    await waitFor(() => expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_PURGE_AND_DISABLE));
    expect(await screen.findByText(/Everything it kept on this computer is erased/)).toBeTruthy();
  });

  it("says so when the keychain kept the key", async () => {
    status = ON;
    vi.mocked(engineInvoke).mockResolvedValue({ ...OFF, key_deleted: false });
    render(<ShadowModeCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete everything and turn off" }));
    expect(await screen.findByText(/didn't delete its key/)).toBeTruthy();
  });

  it("turns off, erasing the buffer, without deleting the ignore list", async () => {
    status = ON;
    vi.mocked(engineInvoke).mockResolvedValue({ ...OFF, key_deleted: true });
    render(<ShadowModeCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_TURN_OFF));
    expect(await screen.findByText(/buffer, key, and drafts are erased/)).toBeTruthy();
  });

  it("pauses and resumes, and advises pausing before sensitive material", async () => {
    status = ON;
    vi.mocked(engineInvoke).mockImplementation(async (command) =>
      command === CMD.SHADOW_PAUSE ? { ...ON, paused: true } : ON,
    );
    render(<ShadowModeCard />);
    expect(await screen.findByText(/Pause shadow mode before you view sensitive material/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_PAUSE));
    expect(await screen.findByText("Paused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_UNPAUSE));
  });

  it("saves the work sites as a cleaned list", async () => {
    vi.mocked(engineInvoke).mockResolvedValue({ ...OFF, work_domains: ["erp.example.com"] });
    render(<ShadowModeCard />);
    const box = await screen.findByLabelText("Work sites");
    fireEvent.change(box, { target: { value: "https://ERP.example.com/x, mail.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Save work sites" }));
    await waitFor(() =>
      expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_SET_WORK_DOMAINS, {
        domains: ["erp.example.com", "mail.example.com"],
      }),
    );
  });

  it("says it's checking until the engine answers, never off", async () => {
    status = null;
    render(<ShadowModeCard />);
    expect(await screen.findByText(/Checking shadow mode/)).toBeTruthy();
    expect(screen.queryByText("Off")).toBeNull();
  });

  it("says so when the engine has no shadow mode", async () => {
    status = { available: false };
    render(<ShadowModeCard />);
    expect(await screen.findByText(/doesn't include shadow mode/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /turn on/i })).toBeNull();
  });
});

describe("indicator and prompts", () => {
  it("shows the indicator only while on", () => {
    const { container, rerender } = render(<ShadowModeIndicator status={OFF} />);
    expect(container.textContent).toBe("");
    rerender(<ShadowModeIndicator status={ON} />);
    expect(screen.getByRole("status").textContent).toContain("Shadow mode on");
  });

  it("shows paused, and its button pauses or resumes", () => {
    const toggle = vi.fn();
    const { rerender } = render(<ShadowModeIndicator status={ON} onTogglePause={toggle} />);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(toggle).toHaveBeenCalledTimes(1);
    rerender(<ShadowModeIndicator status={{ ...ON, paused: true }} onTogglePause={toggle} />);
    expect(screen.getByRole("status").textContent).toContain("Shadow mode paused");
    expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
  });

  it("offers Review draft and Ignore with generic text, and runs nothing", async () => {
    vi.mocked(engineInvoke).mockResolvedValue({
      motif_id: PROMPT.motif_id,
      action: "review_draft",
      next: "This is a draft, not an automation.",
    });
    render(<ShadowPromptBanner status={{ ...ON, pending: [PROMPT] }} />);
    expect(screen.getByText("You did this 6 times today.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Review draft" }));
    await waitFor(() =>
      expect(engineInvoke).toHaveBeenCalledWith(CMD.SHADOW_DECIDE, {
        motif_id: PROMPT.motif_id,
        action: "review_draft",
      }),
    );
    expect(await screen.findByText("This is a draft, not an automation.")).toBeTruthy();
  });

  it("shows no prompt while shadow mode is off", () => {
    const { container } = render(<ShadowPromptBanner status={{ ...OFF, pending: [PROMPT] }} />);
    expect(container.textContent).toBe("");
  });

  it("follows engine events and keeps the tray in step", async () => {
    function Probe() {
      const { status: s } = useShadowMode({ syncTray: true });
      return <span data-testid="probe">{`${s.active}:${s.pending.length}`}</span>;
    }
    render(<Probe />);
    await waitFor(() => expect(events.handlers.has(EVT.SHADOW_STATE)).toBe(true));
    act(() => events.handlers.get(EVT.SHADOW_STATE)?.(ON));
    act(() => events.handlers.get(EVT.SHADOW_PROMPT)?.(PROMPT));
    act(() => events.handlers.get(EVT.SHADOW_PROMPT)?.({ motif_id: "x", leaked: "Doe, John" }));
    expect(screen.getByTestId("probe").textContent).toContain("true:1");
    await waitFor(() => expect(setShadowTrayStatus).toHaveBeenLastCalledWith(true, false));
  });

  it("tells the tray it doesn't know yet instead of saying off", async () => {
    status = null;
    function Probe() {
      useShadowMode({ syncTray: true });
      return null;
    }
    render(<Probe />);
    await waitFor(() => expect(setShadowTrayStatus).toHaveBeenCalledWith(null, false));
    expect(setShadowTrayStatus).not.toHaveBeenCalledWith(false, false);
  });
});

describe("parseDomains", () => {
  it("splits, lowercases, and strips schemes and paths", () => {
    expect(parseDomains(" A.com\nhttps://b.com/path, a.com ")).toEqual(["a.com", "b.com"]);
  });
});
