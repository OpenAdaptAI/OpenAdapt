import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn(), onEngineEvent: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { StopCard, StopCardsResponse } from "../lib/types";
import { Stops } from "./Stops";

// Synthetic referral values.
function card(id: string, lname: string, overrides: Partial<StopCard> = {}): StopCard {
  return {
    schema: "openadapt.desktop-stop-card/v1",
    card_id: `sha256:${id}`,
    kind: "same_record",
    source: { type: "item", id: `item-${id}` },
    workflow_id: "wf-ref",
    task_name: "New patient referral",
    label: `${lname} referral`,
    question: "Same person?",
    summary: "OpenAdapt stopped before saving: the app may already have this record.",
    evidence: {
      source: [
        { field: "lname", label: "Lname", value: lname },
        { field: "DOB", label: "DOB", value: "1941-02-27" },
      ],
      record: [{ field: "DOB", value: "1941-07-22" }],
      differences: [
        { field: "DOB", label: "DOB", source: "1941-02-27", record: "1941-07-22", same: false },
      ],
      candidates: [`${lname}, Ana, 1941-07-22`],
      reason: "no_duplicate_candidate",
      step: "",
      expected: "",
      observed: "",
      screenshot: null,
    },
    choices: [
      { id: "same", label: "Same record: don't add it", consequence: "Nothing is saved." },
      { id: "different_by_hand", label: "Different record: I entered it by hand", consequence: "Closes." },
      { id: "leave", label: "Not sure, leave it", consequence: "Nothing changes." },
    ],
    expires_at: null,
    created_at: null,
    batch_key: "same_record:wf-ref:duplicate",
    status: "open",
    ...overrides,
  };
}

function listing(cards: StopCard[], ready = true): StopCardsResponse {
  return { ok: true, cards, approver_key: { ready, in_app: true, touch_id: ready } };
}

beforeEach(() => {
  vi.mocked(engineTry).mockImplementation(async (cmd) =>
    cmd === CMD.LIST_STOP_CARDS ? listing([card("a", "Fairwick"), card("b", "Caldley")]) : null,
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("shows the question, the item next to the record, and numbered choices", async () => {
  render(<Stops />);

  expect(await screen.findByRole("heading", { name: "Same person?" })).toBeTruthy();
  expect(screen.getByText("1 of 2 · New patient referral · Fairwick referral")).toBeTruthy();
  expect(screen.getByText("Fairwick, Ana, 1941-07-22")).toBeTruthy();
  // The differing value is marked, with the record's value beside it.
  expect(screen.getByText("1941-07-22", { selector: "strong" })).toBeTruthy();
  expect(screen.getByRole("button", { name: /1\. Same record: don't add it/ })).toBeTruthy();
});

it("answers the current stop with one key and sends how long it was shown", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, changed: 1 });
  render(<Stops />);
  await screen.findByRole("heading", { name: "Same person?" });

  await act(async () => {
    fireEvent.keyDown(window, { key: "1", code: "Digit1" });
  });

  await waitFor(() => expect(engineInvoke).toHaveBeenCalled());
  const [cmd, params] = vi.mocked(engineInvoke).mock.calls[0];
  expect(cmd).toBe(CMD.ANSWER_STOP_CARDS);
  expect(params).toMatchObject({ card_ids: ["sha256:a"], choice: "same" });
  expect(typeof (params as { shown_ms: Record<string, number> }).shown_ms["sha256:a"]).toBe(
    "number",
  );
});

it("answers a batch of similar stops with Shift and a number", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, changed: 2 });
  render(<Stops />);
  await screen.findByText("2 similar stops");

  for (const box of screen.getAllByRole("checkbox")) fireEvent.click(box);
  await act(async () => {
    fireEvent.keyDown(window, { key: "!", code: "Digit1", shiftKey: true });
  });

  await waitFor(() => expect(engineInvoke).toHaveBeenCalled());
  expect(vi.mocked(engineInvoke).mock.calls[0][1]).toMatchObject({
    card_ids: ["sha256:a", "sha256:b"],
    choice: "same",
  });
  expect(await screen.findByText("Answered 2 stops.")).toBeTruthy();
});

it("says nothing changed when a stop changed before the answer", async () => {
  vi.mocked(engineInvoke).mockResolvedValue({
    ok: false,
    code: "card_changed",
    error: "A stop changed or was answered since you saw it. Nothing changed; look at it again.",
  });
  render(<Stops />);
  await screen.findByRole("heading", { name: "Same person?" });

  fireEvent.click(screen.getByRole("button", { name: /Same record/ }));

  expect((await screen.findByRole("alert")).textContent).toMatch(/Nothing changed/);
});

it("routes a waiting save to the signed item approval", async () => {
  vi.mocked(engineTry).mockImplementation(async (cmd) =>
    cmd === CMD.LIST_STOP_CARDS
      ? listing([
          card("s", "Fairwick", {
            kind: "approve_save",
            question: "Approve this save?",
            batch_key: null,
            choices: [{ id: "approve", label: "Approve and save", consequence: "Saves." }],
          }),
        ])
      : null,
  );
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, changed: 1 });
  render(<Stops />);
  await screen.findByRole("heading", { name: "Approve this save?" });

  fireEvent.click(screen.getByRole("button", { name: /Approve and save/ }));

  await waitFor(() => expect(engineInvoke).toHaveBeenCalled());
  expect(vi.mocked(engineInvoke).mock.calls[0][0]).toBe(CMD.ANSWER_STOP_APPROVAL);
  expect(vi.mocked(engineInvoke).mock.calls[0][1]).toMatchObject({
    card_id: "sha256:s",
    choice: "approve",
  });
});

it("offers the approver key setup when answers can't be signed yet", async () => {
  vi.mocked(engineTry).mockImplementation(async (cmd) =>
    cmd === CMD.LIST_STOP_CARDS ? listing([card("a", "Fairwick")], false) : null,
  );
  render(<Stops />);

  expect(await screen.findByRole("button", { name: "Set up with Touch ID" })).toBeTruthy();
});
