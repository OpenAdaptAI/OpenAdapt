import { describe, expect, it } from "vitest";

import {
  approverSentence,
  confirmedButNeedsLook,
  notAvailableLine,
  offAutomatically,
  offReasonSentence,
  onlyProblemsComeToYou,
  turnedOnLine,
  whyItCameToYou,
} from "./approvalMode";
import { plainResult } from "./outcomes";
import cases from "./outcome-cases.json";
import type { ApprovalModeState, ItemApproval, OffReason, SwitchState } from "./types";

const SENTENCES: Record<OffReason, string> = {
  uncertain_result: "A result was uncertain or wrong.",
  wrong_item_found: "A result was uncertain or wrong.",
  too_many_problems: "Too many items needed a person.",
  screen_changed: "The screen changed.",
  record_check_lost: "The record check isn't working.",
  spot_checks_overdue: "Spot checks are overdue.",
  version_changed: "Something changed: the version, the computer, the application or its setup.",
  environment_changed:
    "Something changed: the version, the computer, the application or its setup.",
  admission_lost: "Something changed: the version, the computer, the application or its setup.",
  setup_lapsed: "Something changed: the version, the computer, the application or its setup.",
  turned_off: "Turned off by a person.",
};

describe("switch-off reasons", () => {
  it.each(Object.entries(SENTENCES))("%s renders its customer sentence", (reason, sentence) => {
    expect(offReasonSentence(reason as OffReason)).toBe(sentence);
  });

  it("names the step where the screen changed", () => {
    expect(offReasonSentence("screen_changed", { step: "click 'Save referral'" })).toBe(
      "The screen changed at “Save referral”.",
    );
  });

  it("says when this computer turned it off", () => {
    expect(offReasonSentence("turned_off", { byThisComputer: true })).toBe(
      "Turned off on this computer.",
    );
    expect(offAutomatically("turned_off")).toBe(false);
    expect(offAutomatically("screen_changed")).toBe(true);
  });
});

describe("the switch state", () => {
  it("says only problems come to you only when this computer applies the switch", () => {
    const states: SwitchState[] = ["none", "on", "waiting_for_cloud", "not_used_here", "off"];
    expect(states.filter((state) => onlyProblemsComeToYou({ state }))).toEqual(["on"]);
    expect(onlyProblemsComeToYou(null)).toBe(false);
  });

  it("names who turned it on and who confirmed", () => {
    expect(
      turnedOnLine({
        turned_on: { name: "Sam Ortiz", role: "admin", at: "2026-10-04T15:02:00Z" },
        confirmed: { name: "Lee Park", at: "2026-10-04T15:20:00Z" },
      }),
    ).toBe("Turned on Oct 4 by Sam Ortiz (Admin). Confirmed by Lee Park (privacy contact).");
  });

  it("explains why it isn't available from the first unmet requirement, then the first blocker", () => {
    const base: ApprovalModeState = {
      ok: true,
      workflow_id: "wf-1",
      state: "none",
      blockers: ["field_change_check"],
    };
    expect(
      notAvailableLine({
        ...base,
        requirements: {
          available: true,
          items: [
            { code: "E1", met: false, reason: "no_active_admission", checked_by: "software" },
            { code: "E2", met: false, reason: "no_record_check", checked_by: "software" },
          ],
        },
      }),
    ).toBe("Not available yet: this version needs an active admission in OpenAdapt Cloud.");
    expect(notAvailableLine(base)).toBe(
      "Not available yet: OpenAdapt can't check yet that a save changed only the fields it was given.",
    );
  });
});

function approval(overrides: Partial<ItemApproval>): ItemApproval {
  return {
    validated: true,
    mode: "problems_only",
    decision: "problem",
    reason: null,
    needs_person: true,
    counts_toward_problems: true,
    switch_off_reason: null,
    failed_check: null,
    post_checks: { verified: false, whole_run_clean: false },
    saves_without_person: 0,
    ...overrides,
  };
}

describe("why an item came to a person", () => {
  it("keeps the design's words for a confirmed save that needed a workaround", () => {
    const item = {
      result: "done_checked",
      approval: approval({
        decision: "problem",
        failed_check: { code: "whole_run_clean", passed: false, detail: "repair_applied" },
        post_checks: { verified: true, whole_run_clean: false },
      }),
    };
    expect(confirmedButNeedsLook(item)).toBe(true);
    expect(whyItCameToYou(item)).toBe(
      "Saved and checked, but OpenAdapt had to work around a changed screen. Look at the record.",
    );
  });

  it("names the check that sent an item to a person before saving", () => {
    expect(
      whyItCameToYou({
        result: "stopped_before_saving",
        approval: approval({
          decision: "approval_required",
          failed_check: { code: "channel", passed: false, detail: "not_a_list_with_header" },
        }),
      }),
    ).toBe(
      "This item waits for a person before saving, because the item didn't come from a list with column names. OpenAdapt can't approve and save it from here yet, so skip it or enter it by hand.",
    );
  });

  it("says nothing for an item saved without a person", () => {
    expect(
      whyItCameToYou({
        result: "done_checked",
        approval: approval({ decision: "no_person", needs_person: false }),
      }),
    ).toBeNull();
    expect(whyItCameToYou({ result: "done_checked", approval: null })).toBeNull();
  });
});

describe("shared switch cases (openadapt_desktop/work_items.py reads the same file)", () => {
  it.each(cases.switch_cases.map((c) => [c.name, c] as const))("%s", (_name, item) => {
    // The outcome word comes from the transaction outcome alone.
    expect(plainResult(item.execution, item.transaction).key).toBe(item.key);
    expect(
      confirmedButNeedsLook({ result: item.key, approval: item.approval as ItemApproval }),
    ).toBe(item.confirmed_but_needs_look);
  });
});

describe("who decided each save", () => {
  const person = {
    decision: "approved" as const,
    route: "desktop_run_it" as const,
    kind: "person" as const,
    id: "aaaaaaaa-0000-4000-8000-000000000003",
    policy_ref: null,
    signed_in: true,
    verified: true,
  };

  it("names a person in Run it or on their phone", () => {
    expect(approverSentence(person)).toBe("Approved by a signed-in person in Run it");
    expect(approverSentence({ ...person, route: "decision_portal" })).toBe(
      "Approved by a signed-in person on their phone",
    );
    expect(approverSentence({ ...person, decision: "declined", signed_in: false })).toBe(
      "Declined by a person signed out of Cloud in Run it",
    );
  });

  it("names a delegate and the grant a person signed", () => {
    const agent = {
      ...person,
      route: "local_cli" as const,
      kind: "agent" as const,
      id: "agent:approver-bot",
      policy_ref: `sha256:${"e".repeat(64)}`,
      signed_in: false,
    };
    expect(approverSentence(agent)).toBe(
      "Approved by agent agent:approver-bot, under a grant a person signed (eeeeeeeeeeee…)",
    );
    expect(approverSentence({ ...agent, policy_ref: null, verified: false })).toMatch(
      /can't verify, so a person should look/,
    );
  });
});
