import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { RunIt } from "./RunIt";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const TARGET = { backend: "web", url: "https://emr.example.test" };

function engine(readiness: "ready" | "checking" = "ready") {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) {
      return [{ id: "wf-1", name: "Add a claim note", steps: 4, readiness, blocked: false }];
    }
    if (command === CMD.GET_TASK_INPUTS) {
      return {
        ok: true,
        workflow_id: "wf-1",
        inputs: [
          { name: "member_id", type: "string", secret: false, required: true, example: "M-1", choices: [] },
          { name: "note", type: "string", secret: false, required: true, example: "Reviewed", choices: [] },
        ],
        target: TARGET,
      };
    }
    return fallback;
  });
}

function item(position: number, execution: string, transaction: string, result: string, nothingWritten: boolean) {
  return {
    ok: true,
    item: {
      item_id: `item-${position}`,
      batch_id: "b",
      workflow_id: "wf-1",
      position,
      label: "",
      status: result === "done_checked" ? "done" : "open",
      result,
      nothing_written: nothingWritten,
      execution_outcome: execution,
      transaction_outcome: transaction,
      actions: [],
    },
  };
}

async function paste(text: string) {
  const box = await screen.findByLabelText("Paste rows from a spreadsheet or a list");
  fireEvent.change(box, { target: { value: text } });
}

it("runs each item through the engine with its own values and the recorded app", async () => {
  engine();
  vi.mocked(engineInvoke)
    .mockResolvedValueOnce(item(0, "VERIFIED", "VERIFIED", "done_checked", false))
    .mockResolvedValueOnce(item(1, "HALTED", "HALTED_BEFORE_EFFECT", "stopped_before_saving", true))
    .mockResolvedValueOnce(item(2, "VERIFIED", "VERIFIED", "done_checked", false));
  render(<RunIt workflowId="wf-1" onCheck={() => {}} onNeedsYou={() => {}} onResults={() => {}} />);

  await paste("member_id,note\nM-1,Reviewed\nM-2,Reviewed\nM-3,Called\nM-4,");
  expect(await screen.findByText(/4 items. 3 ready/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Run 3 items" }));

  expect(await screen.findByText("Finished this list")).toBeTruthy();
  expect(engineInvoke).toHaveBeenCalledTimes(3);
  expect(vi.mocked(engineInvoke).mock.calls[0]).toEqual([
    CMD.RUN_ITEM,
    expect.objectContaining({
      workflow_id: "wf-1",
      inputs: { member_id: "M-1", note: "Reviewed" },
      target: TARGET,
      position: 0,
    }),
  ]);
  // A stop that wrote nothing goes to Needs you and the list moves on.
  expect(vi.mocked(engineInvoke).mock.calls[2][1]).toEqual(
    expect.objectContaining({ inputs: { member_id: "M-3", note: "Called" } }),
  );
  expect(screen.getByText(/2 items done and checked. 1 item needs you. 1 item didn't run./)).toBeTruthy();
});

it("stops the list when a save may have gone through", async () => {
  engine();
  vi.mocked(engineInvoke)
    .mockResolvedValueOnce(item(0, "HALTED", "RECONCILIATION_REQUIRED", "check_record", false))
    .mockResolvedValue(item(1, "VERIFIED", "VERIFIED", "done_checked", false));
  render(<RunIt workflowId="wf-1" onCheck={() => {}} onNeedsYou={() => {}} onResults={() => {}} />);

  await paste("member_id,note\nM-1,Reviewed\nM-2,Reviewed");
  fireEvent.click(await screen.findByRole("button", { name: "Run 2 items" }));

  expect(await screen.findByText(/may have gone through/)).toBeTruthy();
  expect(engineInvoke).toHaveBeenCalledTimes(1);
  expect(screen.getAllByText("Check the record").length).toBeGreaterThan(0);
});

it("sends an unchecked task to Check it first", async () => {
  engine("checking");
  const onCheck = vi.fn();
  render(<RunIt workflowId="wf-1" onCheck={onCheck} onNeedsYou={() => {}} onResults={() => {}} />);

  await paste("member_id,note\nM-1,Reviewed");
  expect(await screen.findByText("Check this task first")).toBeTruthy();
  expect((screen.getByRole("button", { name: "Run 1 item" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Check it" }));
  await waitFor(() => expect(onCheck).toHaveBeenCalledWith("wf-1"));
});

it("never runs a finished list again; the next step is a new list", async () => {
  engine();
  vi.mocked(engineInvoke)
    .mockResolvedValueOnce(item(0, "VERIFIED", "VERIFIED", "done_checked", false))
    .mockResolvedValueOnce(item(1, "HALTED", "RECONCILIATION_REQUIRED", "check_record", false));
  render(<RunIt workflowId="wf-1" onCheck={() => {}} onNeedsYou={() => {}} onResults={() => {}} />);

  await paste("member_id,note\nM-1,Reviewed\nM-2,Reviewed\nM-3,Reviewed");
  fireEvent.click(await screen.findByRole("button", { name: "Run 3 items" }));
  expect(await screen.findByText("Finished this list")).toBeTruthy();

  // The item the list never reached says so, and nothing offers to run the
  // same items again: item 1 is already saved.
  expect(screen.getByText("Not run")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /^Run \d/ })).toBeNull();
  // The "All ready" line describes a list before it runs, not after.
  expect(screen.queryByText(/All ready/)).toBeNull();
  const box = screen.getByLabelText("Paste rows from a spreadsheet or a list") as HTMLTextAreaElement;
  expect(box.disabled).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: "Start a new list" }));
  expect(box.value).toBe("");
  expect(box.disabled).toBe(false);
  expect(engineInvoke).toHaveBeenCalledTimes(2);
});
