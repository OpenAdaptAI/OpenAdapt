import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn() };
});

import { CMD, engineTry } from "../lib/engine";
import { Results } from "./Results";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

const COUNTS = {
  done_checked: 30, needed_person: 2, waiting: 1, wrong_record_prevented: 1,
  done_by_person: 1, skipped: 0, declined: 0, items: 32,
};

it("labels the time saved as an estimate and shows the baseline it used", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_RESULTS_SUMMARY) {
      return { ok: true, total: COUNTS, tasks: { "wf-1": COUNTS }, names: { "wf-1": "Claim note" }, recent: [] };
    }
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "wf-1", name: "Claim note", steps: 4, capture_id: "cap-1", demo_seconds: 90 }];
    }
    return fallback;
  });
  render(<Results onNeedsYou={() => {}} />);

  expect((await screen.findAllByText("Time saved (estimate)")).length).toBe(2);
  // 30 items done and checked x 90 s from the recording = 45 minutes.
  expect(screen.getAllByText("about 45 min").length).toBeGreaterThan(0);
  expect(screen.getByText("From your recording (90 s)")).toBeTruthy();
  expect(screen.getByText(/is an estimate, not a measurement/)).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Minutes per item by hand for Claim note"), {
    target: { value: "4" },
  });
  expect(screen.getAllByText("about 2 h").length).toBeGreaterThan(0);
  expect(screen.getByText("Your number")).toBeTruthy();
});

it("shows a calm empty state before anything ran", async () => {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_RESULTS_SUMMARY) {
      return { ok: true, total: { ...COUNTS, items: 0, done_checked: 0 }, tasks: {}, names: {}, recent: [] };
    }
    if (command === CMD.GET_WORKFLOWS) return [];
    return fallback;
  });
  render(<Results onNeedsYou={() => {}} />);
  expect(await screen.findByText("No results yet")).toBeTruthy();
});
