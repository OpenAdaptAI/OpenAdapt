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

it("counts items saved without a person inside Done and checked, and never calls them approved", async () => {
  const counts = { ...COUNTS, saved_without_person: 26 };
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_RESULTS_SUMMARY) {
      return {
        ok: true,
        total: counts,
        tasks: { "wf-1": counts },
        names: { "wf-1": "Claim note" },
        recent: [
          { item_id: "r1", batch_id: "b", workflow_id: "wf-1", position: 0, label: "M-1", status: "done", result: "done_checked", nothing_written: false, execution_outcome: "VERIFIED", transaction_outcome: "VERIFIED", actions: [], task_name: "Claim note", saved_without_person: true },
          { item_id: "r2", batch_id: "b", workflow_id: "wf-1", position: 1, label: "M-2", status: "done", result: "done_checked", nothing_written: false, execution_outcome: "VERIFIED", transaction_outcome: "VERIFIED", actions: [], task_name: "Claim note", saved_without_person: false },
        ],
        switches: [
          { workflow_id: "wf-1", task_name: "Claim note", state: "on", off_reason: null, off_at: null, off_by_this_computer: false, turned_on: { name: "Sam Ortiz", role: "admin", at: "2026-10-04T15:02:00Z" }, off_step: "" },
        ],
      };
    }
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "wf-1", name: "Claim note", steps: 4, capture_id: "cap-1", demo_seconds: 90 }];
    }
    return fallback;
  });
  render(<Results onNeedsYou={() => {}} />);

  expect(await screen.findByText("26 of them without a person: every check passed.")).toBeTruthy();
  expect(screen.getByText(/\(26 without a person\)/)).toBeTruthy();
  expect(screen.getAllByText("No person")).toHaveLength(1);
  expect(screen.getByText("Since Oct 4.")).toBeTruthy();
  // Nobody approved these items, so no label says they were approved.
  expect(screen.queryByText(/approved by a person/i)).toBeNull();
});
