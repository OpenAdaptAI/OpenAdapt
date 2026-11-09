import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { ApprovalModeState } from "../lib/types";
import { ApprovalSwitch } from "./ApprovalSwitch";

afterEach(cleanup);

const PEOPLE = {
  turned_on: { name: "Sam Ortiz", role: "admin" as const, at: "2026-10-04T15:02:00Z" },
  confirmed: { name: "Lee Park", at: "2026-10-04T15:20:00Z" },
};

const EVIDENCE = {
  switch_id: "sw-1",
  record_sha256: "sha256:" + "1".repeat(64),
  bundle_digest: "2".repeat(64),
  runner_id: "runner-clinic-1",
  verifiers: 1,
  profile: "regulated",
  log_intact: true,
  log_events: 41,
  items_finished: 38,
  items_without_person: 36,
};

function state(overrides: Partial<ApprovalModeState>): ApprovalModeState {
  return { ok: true, workflow_id: "wf-1", state: "none", blockers: [], ...overrides };
}

it("shows an off switch with the first unmet requirement and a way to Cloud, never Turn on", () => {
  const onOpenCloud = vi.fn();
  render(
    <ApprovalSwitch
      mode={state({
        blockers: ["field_change_check"],
        requirements: {
          available: true,
          items: [{ code: "E1", met: false, reason: "no_active_admission", checked_by: "software" }],
        },
      })}
      onTurnOff={() => {}}
      onOpenCloud={onOpenCloud}
    />,
  );
  expect(screen.getByRole("heading", { name: "Only ask a person about problems" })).toBeTruthy();
  expect(screen.getByText("Off")).toBeTruthy();
  expect(
    screen.getByText("Not available yet: this version needs an active admission in OpenAdapt Cloud."),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: /turn on/i })).toBeNull();
  expect(screen.queryByRole("button", { name: "Turn off" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open Cloud" }));
  expect(onOpenCloud).toHaveBeenCalled();
});

it("shows who turned it on and confirmed, and turns off after a confirmation", () => {
  const onTurnOff = vi.fn();
  render(
    <ApprovalSwitch
      mode={state({ state: "on", can_turn_off: true, evidence: EVIDENCE, ...PEOPLE })}
      onTurnOff={onTurnOff}
      onOpenCloud={() => {}}
    />,
  );
  expect(screen.getByText("On")).toBeTruthy();
  expect(
    screen.getByText(
      "Turned on Oct 4 by Sam Ortiz (Admin). Confirmed by Lee Park (privacy contact).",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
  expect(onTurnOff).not.toHaveBeenCalled();
  expect(screen.getByText(/It applies before the next save, even offline/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Turn it off" }));
  expect(onTurnOff).toHaveBeenCalledTimes(1);
});

it("never reads as on when this computer can't apply it", () => {
  render(
    <ApprovalSwitch
      mode={state({
        state: "not_used_here",
        recorded_state: "on",
        can_turn_off: true,
        blockers: ["field_change_check", "cloud_status"],
        evidence: EVIDENCE,
        ...PEOPLE,
      })}
      onTurnOff={() => {}}
      onOpenCloud={() => {}}
    />,
  );
  expect(screen.queryByText("On")).toBeNull();
  expect(screen.getByText("Not used on this computer")).toBeTruthy();
  expect(
    screen.getByText(/^On in OpenAdapt Cloud, but not used on this computer: .*can't check yet that a save changed only the fields it was given/),
  ).toBeTruthy();
  expect(screen.queryByText(/Only problems come to you/)).toBeNull();
  // Turning off still works: it reaches the local switch log.
  expect(screen.getByRole("button", { name: "Turn off" })).toBeTruthy();
});

it("explains an automatic switch-off and who can turn it on again", () => {
  render(
    <ApprovalSwitch
      mode={state({
        state: "off",
        off_reason: "screen_changed",
        off_step: "click 'Save referral'",
      })}
      onTurnOff={() => {}}
      onOpenCloud={() => {}}
    />,
  );
  expect(screen.getByText("The screen changed at “Save referral”.")).toBeTruthy();
  expect(
    screen.getByText(
      "An admin can turn it on again in OpenAdapt Cloud once the requirements are met.",
    ),
  ).toBeTruthy();
});

it("shows its evidence: the binding, the switch log and the requirements", () => {
  render(
    <ApprovalSwitch
      mode={state({
        state: "on",
        can_turn_off: true,
        evidence: EVIDENCE,
        ...PEOPLE,
        requirements: {
          available: true,
          source: "this_computer",
          items: [
            { code: "E1", met: true, reason: "ok", checked_by: "software" },
            { code: "E4", met: false, reason: "clinic_sign_off_missing", checked_by: "person" },
          ],
        },
      })}
      onTurnOff={() => {}}
      onOpenCloud={() => {}}
    />,
  );
  expect(screen.getByText("runner-clinic-1")).toBeTruthy();
  expect(screen.getByText(/Intact on this computer, 41 events. 36 items saved without a person./)).toBeTruthy();
  expect(screen.getByText("Met")).toBeTruthy();
  expect(screen.getByText("Not confirmed here")).toBeTruthy();
  expect(screen.getByText(/A person at the clinic decides this/)).toBeTruthy();
  expect(screen.getByText(/OpenAdapt checks the record against the values it was given/)).toBeTruthy();
});
