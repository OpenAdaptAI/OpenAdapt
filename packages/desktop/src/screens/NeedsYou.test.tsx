import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { AttentionItem } from "../lib/types";
import { NeedsYou, fieldLabel } from "./NeedsYou";

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

function engine(items: AttentionItem[], switches: unknown[] = []) {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.LIST_ATTENTION_ITEMS) return { ok: true, items, switches };
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
  // Click only once the button is enabled, queried again after the re-render:
  // on a loaded CI runner a click on the stale, still-disabled button was lost.
  const enabled = await waitFor(() => {
    const button = screen.getByRole("button", {
      name: /It saved. Mark it done/,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    return button;
  });
  fireEvent.click(enabled);

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

const WORKAROUND: AttentionItem = {
  ...WRONG_RECORD,
  item_id: "item-3",
  workflow_id: "wf-ref",
  task_name: "Referral intake",
  label: "M-4822",
  result: "done_checked",
  nothing_written: false,
  execution_outcome: "VERIFIED",
  transaction_outcome: "VERIFIED",
  halt_kind: null,
  halt_step: "",
  expected: "",
  observed: "",
  actions: ["confirmed_saved", "done_by_person"],
  approval: {
    validated: true,
    mode: "problems_only",
    decision: "problem",
    reason: "screen_changed",
    needs_person: true,
    counts_toward_problems: true,
    switch_off_reason: "screen_changed",
    failed_check: { code: "whole_run_clean", passed: false, detail: "repair_applied" },
    post_checks: { verified: true, whole_run_clean: false },
    saves_without_person: 1,
  },
};

it("says only problems come here while a switch is on", async () => {
  engine([WRONG_RECORD], [
    { workflow_id: "wf-1", task_name: "Add a claim note", state: "on", off_reason: null, off_at: null, off_by_this_computer: false, turned_on: null, off_step: "" },
  ]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);
  expect(await screen.findByText(/only problems come here/)).toBeTruthy();
});

it("keeps today's words when no switch is on here", async () => {
  engine([WRONG_RECORD], [
    { workflow_id: "wf-1", task_name: "Add a claim note", state: "not_used_here", off_reason: null, off_at: null, off_by_this_computer: false, turned_on: null, off_step: "" },
  ]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);
  expect(await screen.findByText(/OpenAdapt stopped on these items/)).toBeTruthy();
  expect(screen.queryByText(/only problems come here/)).toBeNull();
});

it("explains an automatic switch-off once, above the items", async () => {
  engine([WORKAROUND], [
    { workflow_id: "wf-ref", task_name: "Referral intake", state: "off", off_reason: "screen_changed", off_at: "2026-10-09T10:00:00Z", off_by_this_computer: false, turned_on: null, off_step: "click 'Save referral'" },
    { workflow_id: "wf-1", task_name: "Add a claim note", state: "off", off_reason: "turned_off", off_at: "2026-10-09T10:00:00Z", off_by_this_computer: true, turned_on: null, off_step: "" },
  ]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);
  const banners = await screen.findAllByText(/Only ask a person about problems turned off/);
  // A person's own switch-off needs no banner.
  expect(banners).toHaveLength(1);
  expect(screen.getByText(/The screen changed at “Save referral”. An admin can turn it on again/)).toBeTruthy();
});

it("keeps Done and checked for a confirmed save that still needs a look, with no retry", async () => {
  engine([WORKAROUND]);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true });
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);

  expect(await screen.findByText("Is the record for M-4822 right?")).toBeTruthy();
  expect(screen.getByText(/Done and checked. Look at the record/)).toBeTruthy();
  expect(
    screen.getByText(
      "Saved and checked, but OpenAdapt had to work around a changed screen. Look at the record.",
    ),
  ).toBeTruthy();
  expect(screen.getByText(/From a list with “Only ask a person about problems” on/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Run it again/ })).toBeNull();
  const closeButton = () =>
    screen.getByRole("button", { name: /It's right. Close it/ }) as HTMLButtonElement;
  expect(closeButton().disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("I looked at the record in the app."));
  // Re-query: a late re-render can replace the button node.
  await waitFor(() => expect(closeButton().disabled).toBe(false));
  fireEvent.click(closeButton());
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(
      CMD.RESOLVE_ATTENTION_ITEM,
      expect.objectContaining({ item_id: "item-3", action: "confirmed_saved", record_checked: true }),
    ),
  );
});

it("says which check held an item for a person, in words, and offers no re-run", async () => {
  const held: AttentionItem = {
    ...WRONG_RECORD,
    item_id: "item-4",
    label: "M-4851",
    halt_kind: "other",
    halt_reason:
      "Approval mode: this item is waiting for a person to approve it before saving (record_key_not_recent: seen_recently). Nothing was retried.",
    expected: "",
    observed: "",
    actions: ["skip", "decline", "done_by_person"],
    approval: {
      validated: true,
      mode: "problems_only",
      decision: "approval_required",
      reason: "record_key_not_recent",
      needs_person: true,
      counts_toward_problems: false,
      switch_off_reason: null,
      failed_check: { code: "record_key_not_recent", passed: false, detail: "seen_recently" },
      post_checks: { verified: false, whole_run_clean: false },
      saves_without_person: 0,
    },
  };
  engine([held]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);

  expect(await screen.findByText("M-4851 is waiting for a person before saving")).toBeTruthy();
  const sentence = screen.getByText(/because this record was entered through this task recently/);
  // Engine codes stay in Technical details, out of the sentence a person reads.
  expect(sentence.className).toBe("attention-sentence");
  expect(sentence.textContent).not.toMatch(/record_key_not_recent/);
  expect(screen.queryByRole("button", { name: /Check and continue/ })).toBeNull();
  expect(screen.getByText("It stopped before saving. Nothing was written.")).toBeTruthy();
});

it("shows what was typed and what the record has on a Check the record stop", async () => {
  engine([
    {
      ...UNCERTAIN,
      field_diffs: [{ field: "dob", typed: "1949-12-11", on_file: "1960-01-01" }],
    },
  ]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);

  const box = await screen.findByLabelText("What the record check found");
  expect(box.textContent).toContain("DOB.");
  expect(box.textContent).toContain("Typed: 1949-12-11.");
  expect(box.textContent).toContain("Record on file: 1960-01-01");
  expect(box.textContent).toContain("Shown only in this window. OpenAdapt doesn't send it anywhere.");
});

it("shows no field diff when the record check recorded none", async () => {
  engine([UNCERTAIN]);
  render(<NeedsYou onTeach={() => {}} onChanged={() => {}} />);
  await screen.findAllByText(/Check the record/);
  expect(screen.queryByLabelText("What the record check found")).toBeNull();
});

it("labels fields in plain words", () => {
  expect(fieldLabel("dob")).toBe("DOB");
  expect(fieldLabel("first_name")).toBe("First name");
  expect(fieldLabel("patient_mrn")).toBe("Patient MRN");
});
