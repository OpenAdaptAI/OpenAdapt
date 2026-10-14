import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { AttentionItem } from "../lib/types";
import { NeedsYou } from "./NeedsYou";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const WRONG_RECORD: AttentionItem = {
  item_id: "item-1",
  source: "item",
  workflow_id: "wf-1",
  task_name: "Add a claim note",
  label: "M-4821",
  status: "open",
  result: "stopped_before_saving",
  nothing_written: true,
  execution_outcome: "HALTED",
  transaction_outcome: "HALTED_BEFORE_EFFECT",
  halt_kind: "wrong_record",
  halt_step: "click 'Save'",
  expected: "M-4821",
  observed: "M-4822",
  created_at: "2026-10-01T10:00:00Z",
  actions: ["retry", "skip", "decline", "teach"],
};

const UNCERTAIN: AttentionItem = {
  ...WRONG_RECORD,
  item_id: "item-2",
  label: "M-5530",
  result: "check_record",
  nothing_written: false,
  transaction_outcome: "RECONCILIATION_REQUIRED",
  halt_kind: "save_not_confirmed",
  expected: "",
  observed: "",
  actions: ["confirmed_saved", "retry_after_check", "done_by_person"],
};

function engine(items: AttentionItem[]) {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.LIST_ATTENTION_ITEMS) return { ok: true, items };
    if (command === CMD.GET_ATTENTION_SCREENSHOT) return { ok: false };
    if (command === CMD.GET_TASK_INPUTS) {
      return { ok: true, workflow_id: "wf-1", inputs: [], target: { backend: "web", url: "https://emr.example.test" } };
    }
    return fallback;
  });
}

it("asks one plain question and says nothing was written only when proven", async () => {
  engine([WRONG_RECORD]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);

  expect(
    await screen.findByRole("heading", { name: "Is M-4821 the record that should change?" }),
  ).toBeTruthy();
  expect(screen.getByText("Prevented a save on the wrong record. Nothing was written.")).toBeTruthy();
  expect(screen.getByText("M-4822")).toBeTruthy();
});

it("reruns a proven no-write stop on the recorded app and reports the new result", async () => {
  engine([WRONG_RECORD]);
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: true,
    item: {
      ...WRONG_RECORD,
      item_id: "item-3",
      status: "done",
      result: "done_checked",
      nothing_written: false,
      execution_outcome: "VERIFIED",
      transaction_outcome: "VERIFIED",
      actions: [],
    },
  });
  const onChanged = vi.fn();
  render(<NeedsYou onTeach={() => {}} onChanged={onChanged} />);

  fireEvent.click(
    await screen.findByRole("button", { name: /I fixed it. Check and continue/ }),
  );

  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.RESOLVE_ATTENTION_ITEM, {
      item_id: "item-1",
      action: "retry",
      record_checked: false,
      target: { backend: "web", url: "https://emr.example.test" },
    }),
  );
  expect(await screen.findByText(/Ran it again: Done and checked/)).toBeTruthy();
  expect(onChanged).toHaveBeenCalled();
});

it("needs the person to look at the record before any choice on a possible save", async () => {
  engine([UNCERTAIN]);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true });
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);

  const done = await screen.findByRole("button", { name: /It saved. Mark it done/ });
  expect(screen.queryByText(/Nothing was written/)).toBeNull();
  expect(screen.queryByRole("button", { name: /I fixed it/ })).toBeNull();
  expect((done as HTMLButtonElement).disabled).toBe(true);
  expect(
    (screen.getByRole("button", { name: /It didn't save. Run it again/ }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);

  fireEvent.click(screen.getByRole("checkbox", { name: "I looked at the record in the app." }));
  fireEvent.click(done);

  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.RESOLVE_ATTENTION_ITEM, {
      item_id: "item-2",
      action: "confirmed_saved",
      record_checked: true,
    }),
  );
});

it("sends a person to teach the fix", async () => {
  engine([WRONG_RECORD]);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true });
  const onTeach = vi.fn();
  render(<NeedsYou onTeach={onTeach} onChanged={() => {}} />);

  fireEvent.click(await screen.findByRole("button", { name: /Show OpenAdapt the right way/ }));

  await waitFor(() => expect(onTeach).toHaveBeenCalledWith("wf-1"));
});

it("shows a calm empty inbox", async () => {
  engine([]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);
  expect(await screen.findByText("Nothing needs you right now")).toBeTruthy();
});
