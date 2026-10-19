import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn(), onEngineEvent: vi.fn() };
});

import { CMD, engineInvoke } from "../lib/engine";
import type { ItemApprovalState } from "../lib/types";
import { ItemApprovalPanel } from "./ItemApproval";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Made-up values; the screenshot is a placeholder data URL.
function waiting(overrides: Partial<NonNullable<ItemApprovalState["request"]>> = {}, kind: "item" | "practice" = "item"): ItemApprovalState {
  return {
    ok: true,
    running: true,
    waiting: true,
    workflow_id: "wf-1",
    kind,
    request: {
      request_id: "0".repeat(31) + "1",
      request_sha256: "sha256:" + "a".repeat(64),
      step_id: "step_012",
      step_intent: "click recorded visual target",
      answer_by: "2026-10-14T12:10:00+00:00",
      screenshot: "data:image/png;base64,iVBORw0KGgo=",
      values: [
        { name: "claim_no", value: "VD265283" },
        { name: "explanation", value: "" },
      ],
      ...overrides,
    },
  };
}

it("shows the item's values next to the filled form and approves that exact request", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, decision: "approved" });
  const answered = vi.fn();
  render(<ItemApprovalPanel state={waiting()} onAnswered={answered} />);

  expect(screen.getByRole("dialog", { name: "Approve this save?" })).toBeTruthy();
  expect(screen.getByText("Claim no")).toBeTruthy();
  expect(screen.getByText("VD265283")).toBeTruthy();
  expect(screen.getByText("Left empty")).toBeTruthy();
  expect(screen.getByAltText("The filled form, before saving")).toBeTruthy();
  expect(screen.getByText(/records this approval without your name/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Approve and save" }));

  await waitFor(() => expect(answered).toHaveBeenCalled());
  expect(engineInvoke).toHaveBeenCalledWith(CMD.ANSWER_ITEM_APPROVAL, {
    workflow_id: "wf-1",
    decision: "approve",
    request_id: "0".repeat(31) + "1",
    request_sha256: "sha256:" + "a".repeat(64),
  });
});

it("never offers Approve without the filled form to compare", () => {
  render(<ItemApprovalPanel state={waiting({ screenshot: null })} onAnswered={() => {}} />);

  const approve = screen.getByRole("button", { name: "Approve and save" }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true);
  expect(screen.getByRole("alert").textContent).toMatch(/couldn't show the filled form/);
});

it("asks why before it records Don't save", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, decision: "declined" });
  render(<ItemApprovalPanel state={waiting({}, "practice")} onAnswered={() => {}} />);

  expect(screen.getByRole("dialog", { name: "Approve the practice save?" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Don't save" }));
  const send = screen.getByRole("button", { name: "Don't save" }) as HTMLButtonElement;
  expect(send.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("It's the wrong record"));
  fireEvent.click(send);

  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith(CMD.ANSWER_ITEM_APPROVAL, {
      workflow_id: "wf-1",
      decision: "decline",
      request_id: "0".repeat(31) + "1",
      request_sha256: "sha256:" + "a".repeat(64),
      reason: "wrong_record",
    }),
  );
  expect(await screen.findByText("OpenAdapt won't save it.")).toBeTruthy();
});

it("says plainly when the engine refused the answer", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: false,
    error: "That request isn't waiting any more. Nothing was approved.",
  });
  const answered = vi.fn();
  render(<ItemApprovalPanel state={waiting()} onAnswered={answered} />);

  fireEvent.click(screen.getByRole("button", { name: "Approve and save" }));

  expect(await screen.findByText(/isn't waiting any more/)).toBeTruthy();
  expect(answered).not.toHaveBeenCalled();
});
