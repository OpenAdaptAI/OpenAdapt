import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { Tasks, type TaskActions } from "./Tasks";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function actions(): TaskActions {
  return {
    onShow: vi.fn(),
    onSample: vi.fn(),
    onCheck: vi.fn(),
    onRun: vi.fn(),
    onNeedsYou: vi.fn(),
    onWatch: vi.fn(),
    onExactChecks: vi.fn(),
    onTeach: vi.fn(),
  };
}

it("gives each task one status and one next step, newest version first", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) {
      return [
        { id: "a-v1", name: "Claim note", steps: 4, capture_id: "cap-a", version: 1, readiness: "checking" },
        { id: "a-v2", name: "Claim note", steps: 4, capture_id: "cap-a", version: 2, readiness: "ready" },
        { id: "b", name: "Referral entry", steps: 9, capture_id: "cap-b", readiness: "new" },
        { id: "c", name: "Eligibility check", steps: 3, capture_id: "cap-c", readiness: "ready", open_items: 1, blocked: true },
      ];
    }
    return fallback;
  });
  const handlers = actions();
  render(<Tasks attention={1} {...handlers} />);

  const ready = await screen.findByRole("article", { name: "Claim note" });
  expect(within(ready).getByText("Ready")).toBeTruthy();
  fireEvent.click(within(ready).getByRole("button", { name: "Run it" }));
  expect(handlers.onRun).toHaveBeenCalledWith("a-v2");

  const fresh = screen.getByRole("article", { name: "Referral entry" });
  fireEvent.click(within(fresh).getByRole("button", { name: "Check it" }));
  expect(handlers.onCheck).toHaveBeenCalledWith("b");

  const blocked = screen.getByRole("article", { name: "Eligibility check" });
  expect(within(blocked).getByText("Check the record")).toBeTruthy();
  fireEvent.click(within(blocked).getByRole("button", { name: "Open Needs you" }));
  expect(handlers.onNeedsYou).toHaveBeenCalled();

  // What needs a person comes first.
  const titles = screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"));
  expect(titles).toEqual(["Eligibility check", "Claim note", "Referral entry"]);
});

it("offers the sample and a first task when there are no tasks", async () => {
  vi.mocked(engineTry).mockImplementation(async (_command, _params, fallback) =>
    _command === CMD.GET_WORKFLOWS ? [] : fallback,
  );
  const handlers = actions();
  render(<Tasks attention={0} {...handlers} />);

  fireEvent.click(await screen.findByRole("button", { name: /See it work on a sample app/ }));
  expect(handlers.onSample).toHaveBeenCalled();
});

it("asks for a new showing when a person chose to teach a fix", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) {
      return [
        { id: "t", name: "Referral entry", steps: 9, capture_id: "cap-t", readiness: "ready", open_items: 1, teaching_items: 1 },
      ];
    }
    return fallback;
  });
  const handlers = actions();
  render(<Tasks attention={1} {...handlers} />);

  const card = await screen.findByRole("article", { name: "Referral entry" });
  expect(within(card).getByText("Needs a fix")).toBeTruthy();
  fireEvent.click(within(card).getByRole("button", { name: "Show it" }));
  expect(handlers.onTeach).toHaveBeenCalledWith("t");
});

const SWITCH_PEOPLE = {
  turned_on: { name: "Sam Ortiz", role: "admin", at: "2026-10-04T15:02:00Z" },
  confirmed: { name: "Lee Park", at: "2026-10-04T15:20:00Z" },
};

function switchEngine(mode: Record<string, unknown>) {
  vi.mocked(engineTry).mockImplementation(async (command, params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) {
      return [
        { id: "r-v2", name: "Referral intake", steps: 7, capture_id: "cap-r", version: 2, readiness: "ready" },
        { id: "n", name: "New task", steps: 3, capture_id: "cap-n", readiness: "new" },
      ];
    }
    if (command === CMD.GET_APPROVAL_MODE) {
      // Only a task that is ready to run asks for its switch.
      expect((params as { workflow_id: string }).workflow_id).toBe("r-v2");
      return { ok: true, workflow_id: "r-v2", blockers: [], visible: true, ...mode };
    }
    return fallback;
  });
}

it("names the mode on the card only when this computer applies the switch", async () => {
  switchEngine({ state: "on", can_turn_off: true, ...SWITCH_PEOPLE });
  render(<Tasks attention={0} {...actions()} />);

  const card = await screen.findByRole("article", { name: "Referral intake" });
  expect(await within(card).findByText("Checked and approved. Only problems come to you.")).toBeTruthy();
  expect(within(card).getByText(/Turned on Oct 4 by Sam Ortiz \(Admin\)/)).toBeTruthy();
  const fresh = screen.getByRole("article", { name: "New task" });
  expect(within(fresh).queryByText("Only ask a person about problems")).toBeNull();
});

it("keeps today's words when the switch is on in Cloud but not used here", async () => {
  switchEngine({
    state: "not_used_here",
    recorded_state: "on",
    can_turn_off: true,
    blockers: ["field_change_check"],
    ...SWITCH_PEOPLE,
  });
  render(<Tasks attention={0} {...actions()} />);

  const card = await screen.findByRole("article", { name: "Referral intake" });
  expect(await within(card).findByText("Not used on this computer")).toBeTruthy();
  expect(within(card).getByText("Checked and approved. Give it a list of items to work through.")).toBeTruthy();
  expect(within(card).queryByText(/Only problems come to you/)).toBeNull();
});

it("turns the switch off from the card", async () => {
  switchEngine({ state: "on", can_turn_off: true, ...SWITCH_PEOPLE });
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: true,
    mode: {
      ok: true,
      workflow_id: "r-v2",
      state: "off",
      off_reason: "turned_off",
      off_by_this_computer: true,
      blockers: [],
    },
  });
  render(<Tasks attention={0} {...actions()} />);

  const card = await screen.findByRole("article", { name: "Referral intake" });
  fireEvent.click(await within(card).findByRole("button", { name: "Turn off" }));
  fireEvent.click(within(card).getByRole("button", { name: "Turn it off" }));
  expect(await within(card).findByText("Turned off on this computer.")).toBeTruthy();
  expect(engineInvoke).toHaveBeenCalledWith(CMD.TURN_OFF_APPROVAL_MODE, { workflow_id: "r-v2" });
  expect(within(card).getByText("Checked and approved. Give it a list of items to work through.")).toBeTruthy();
});

it("shows no switch on a task this computer has no switch for", async () => {
  switchEngine({ state: "none", visible: false, blockers: ["field_change_check"] });
  render(<Tasks attention={0} {...actions()} />);

  const card = await screen.findByRole("article", { name: "Referral intake" });
  await waitFor(() => expect(engineTry).toHaveBeenCalledWith(
    CMD.GET_APPROVAL_MODE, { workflow_id: "r-v2", requirements: true }, null,
  ));
  expect(within(card).queryByText("Only ask a person about problems")).toBeNull();
  expect(within(card).getByText("Checked and approved. Give it a list of items to work through.")).toBeTruthy();
});
