import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn() };
});

import { CMD, engineTry } from "../lib/engine";
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
