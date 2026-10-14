// "Only ask a person about problems": the words for the switch.
//
// The engine (engine/approval_mode.py) returns closed codes. This file turns
// them into the words a person reads. The rule it keeps: nothing says that
// only problems come to the person unless the engine reports the switch as
// "on", which it does only when this computer checks every item before and
// after each save. The outcome words never change; they come from outcomes.ts.

import type {
  ApprovalModeState,
  ItemApproval,
  OffReason,
  RequirementResult,
  SwitchState,
} from "./types";
import type { Tone } from "./outcomes";

export const SWITCH_LABEL = "Only ask a person about problems";

/** The sentence shown next to the switch (design section 1). */
export const SWITCH_SENTENCE =
  "With this on, OpenAdapt saves items without asking first, then reads each saved record back from your records system. If the record doesn't show exactly the values it was given, or any other check fails, the list stops and the item waits for you in Needs you. OpenAdapt checks the record against the values it was given, not whether those values are right.";

const CHANGED = "Something changed: the version, the computer, the application or its setup.";

/** The button or field a step acted on, such as "Save referral". */
export function stepTarget(step: string | null | undefined): string | null {
  const match = /['"‘’“”](.+)['"‘’“”]/.exec(String(step ?? ""));
  return match ? match[1] : null;
}

/** Why the switch turned off, in the customer's words (design 7.6). */
export function offReasonSentence(
  reason: OffReason | null | undefined,
  { step, byThisComputer = false }: { step?: string | null; byThisComputer?: boolean } = {},
): string {
  switch (reason) {
    case "uncertain_result":
    case "wrong_item_found":
      return "A result was uncertain or wrong.";
    case "too_many_problems":
      return "Too many items needed a person.";
    case "screen_changed": {
      const target = stepTarget(step);
      return target ? `The screen changed at “${target}”.` : "The screen changed.";
    }
    case "record_check_lost":
      return "The record check isn't working.";
    case "spot_checks_overdue":
      return "Spot checks are overdue.";
    case "version_changed":
    case "environment_changed":
    case "admission_lost":
    case "setup_lapsed":
      return CHANGED;
    case "turned_off":
      return byThisComputer ? "Turned off on this computer." : "Turned off by a person.";
    default:
      return "A result was uncertain or wrong.";
  }
}

/** True when the switch turned itself off, not a person. */
export function offAutomatically(reason: OffReason | null | undefined): boolean {
  return Boolean(reason) && reason !== "turned_off";
}

/** The reasons a person can give for Don't save, as Flow records them. */
export const DECLINED: Record<string, string> = {
  wrong_values: "the values are wrong",
  wrong_record: "it's the wrong record",
  screen_problem: "something on the screen is wrong",
};

const BLOCKERS: Record<string, string> = {
  engine_without_switch: "The OpenAdapt engine on this computer doesn't have this switch yet.",
  field_change_check:
    "OpenAdapt can't check yet that a save changed only the fields it was given.",
  cloud_status: "This computer can't confirm the switch with OpenAdapt Cloud yet.",
  spot_checks: "Spot checks aren't available on this computer yet.",
  list_queue: "A paused list can't continue safely on this computer yet.",
};

export function blockerWords(code: string): string {
  return BLOCKERS[code] ?? "A piece this switch needs is missing on this computer.";
}

const TITLES: Record<RequirementResult["code"], string> = {
  E1: "A locked version under the Regulated profile, with model help off",
  E2: "A record check that reads back every value",
  E3: "No unchecked saves, and nothing that can't be taken back",
  E4: "The readiness test on this computer",
  E5: "Recent items approved one by one by at least two people",
  E6: "This computer set up by your IT as a runner",
  E7: "Where the values in your lists come from",
  E8: "Someone who answers problems",
};

const DEFAULT_NEXT: Record<RequirementResult["code"], string> = {
  E1: "Check this task and approve this version, then admit it in OpenAdapt Cloud.",
  E2: "Set up a record check that reads back every value from your records system.",
  E3: "Make sure every save has a record check and nothing deletes, sends, signs or orders.",
  E4: "Run the readiness test on this computer, then a person at the clinic signs it off.",
  E5: "Run recent items with a person approving each one.",
  E6: "Your IT sets up this computer as a runner with a dedicated OpenAdapt user.",
  E7: "Record in OpenAdapt Cloud where the values in your lists come from.",
  E8: "Name a privacy contact, and answer a stop in Needs you while signed in.",
};

const NEXT: Record<string, string> = {
  not_certified_and_sealed: "Check this task and approve this version first.",
  no_active_admission: "This version needs an active admission in OpenAdapt Cloud.",
  profile_not_regulated: "Run this task under the Regulated profile.",
  model_egress_allowed: "Turn model help off for this task.",
  no_environment_observer: "Set up the check that reads the application's version.",
  no_record_check: "Set up a record check that reads your records system.",
  update_needs_field_delta_check: "OpenAdapt can't check changes to existing records yet.",
  program_graph: "Tasks with branches or loops can't use this switch yet.",
  unchecked_step_classified_harmless:
    "A step marked as not changing anything has no record check.",
  write_classification_lowered: "A step that saves was marked as lower risk.",
  irreversible_write:
    "The task deletes, sends, signs or orders something. That always needs a person.",
  production_write_authorization_missing:
    "Record the clinic's written permission for test writes in the application.",
  runner_setup_not_current: "Your IT sets up this computer as a runner with a dedicated OpenAdapt user.",
  privacy_contact_not_named: "Name the clinic's privacy contact in OpenAdapt Cloud.",
};

export function requirementTitle(code: RequirementResult["code"]): string {
  return TITLES[code] ?? code;
}

/** What to do next for one unmet requirement. */
export function requirementNext(item: RequirementResult): string {
  return NEXT[item.reason] ?? DEFAULT_NEXT[item.code] ?? "Open OpenAdapt Cloud to see what's needed.";
}

export function firstUnmet(mode: ApprovalModeState | null): RequirementResult | null {
  return mode?.requirements?.items.find((item) => !item.met) ?? null;
}

/** "Not available yet: …", from the first unmet requirement or blocker. */
export function notAvailableLine(mode: ApprovalModeState): string {
  const unmet = firstUnmet(mode);
  if (unmet) return `Not available yet: ${lowerFirst(requirementNext(unmet))}`;
  const blocker = mode.blockers[0];
  if (blocker) return `Not available yet: ${lowerFirst(blockerWords(blocker))}`;
  return "Not available yet.";
}

function lowerFirst(text: string): string {
  // Keep "OpenAdapt" and similar names capitalized.
  return /^[A-Z][a-z]/.test(text) && !/^OpenAdapt/.test(text)
    ? text.charAt(0).toLowerCase() + text.slice(1)
    : text;
}

export interface SwitchStatusWords {
  word: string;
  tone: Tone;
  icon: "check" | "half" | "minus" | "ring";
}

export function switchStatus(state: SwitchState): SwitchStatusWords {
  switch (state) {
    case "on":
      return { word: "On", tone: "ok", icon: "check" };
    case "waiting_for_cloud":
      return { word: "On, waiting for Cloud", tone: "warn", icon: "half" };
    case "not_used_here":
      return { word: "Not used on this computer", tone: "neutral", icon: "minus" };
    default:
      return { word: "Off", tone: "neutral", icon: "ring" };
  }
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** "Turned on Oct 4 by Sam Ortiz (Admin). Confirmed by Lee Park (privacy contact)." */
export function turnedOnLine(mode: Pick<ApprovalModeState, "turned_on" | "confirmed">): string {
  const on = mode.turned_on;
  if (!on) return "";
  const role = on.role === "owner" ? "Owner" : "Admin";
  const confirmed = mode.confirmed
    ? ` Confirmed by ${mode.confirmed.name} (privacy contact).`
    : "";
  return `Turned on ${shortDate(on.at)} by ${on.name} (${role}).${confirmed}`;
}

/** True only when this computer applies the switch to every item. */
export function onlyProblemsComeToYou(mode: Pick<ApprovalModeState, "state"> | null): boolean {
  return mode?.state === "on";
}

const CHECKS: Record<string, string> = {
  channel: "the item didn't come from a list with column names",
  inputs_strict: "a value didn't fit its column",
  inputs_supplied: "a value was missing",
  not_a_retry: "the item is a retry or a repeat",
  record_key_not_recent: "this record was entered through this task recently",
  deployment_config: "the run used settings other than the task's own",
  switch_lease: "another list of this task is running",
  not_resumed: "the run was resumed",
  switch_on: "OpenAdapt couldn't confirm the switch",
  signed_in_user: "the application didn't show the OpenAdapt user as signed in",
  right_record: "the right-record check didn't confirm the record",
  same_screens: "the screen changed",
  record_check_ready: "the record check wasn't ready",
  verified: "the record check didn't confirm the save",
  whole_run_clean: "OpenAdapt had to work around a changed screen",
};

/**
 * True for a save the record check confirmed that still went to a person.
 * The result word stays "Done and checked"; the item waits in Needs you.
 */
export function confirmedButNeedsLook(
  item: { result: string | null; approval?: ItemApproval | null },
): boolean {
  return item.result === "done_checked" && item.approval?.needs_person === true;
}

/** Why a confirmed save still needs a look (the Needs-you sentence). */
export function confirmedButNeedsLookSentence(approval: ItemApproval | null | undefined): string {
  if (approval?.validated && approval.failed_check?.code === "whole_run_clean") {
    return "Saved and checked, but OpenAdapt had to work around a changed screen. Look at the record.";
  }
  return "Saved and checked, but OpenAdapt couldn't confirm its other checks for this item. Look at the record.";
}

/**
 * One line on why an item under the switch came to a person, or null when the
 * item's own result already says it (a wrong record, a save to check).
 */
export function whyItCameToYou(
  item: { result: string | null; approval?: ItemApproval | null },
): string | null {
  const approval = item.approval;
  if (!approval) return null;
  // A person already answered this save (or nobody did in time). Say so
  // whether or not Flow sends the item to a person.
  if (approval.mode === "each_item" && approval.decision === "approval_required") {
    return "Nobody approved the save in time, so OpenAdapt stopped before saving. Run it again and approve the save when OpenAdapt asks, or skip it.";
  }
  if (approval.decision === "declined") {
    const why = approval.reason ? DECLINED[approval.reason] : undefined;
    return why
      ? `You chose Don't save because ${why}. OpenAdapt didn't save it.`
      : "You chose Don't save. OpenAdapt didn't save it.";
  }
  if (!approval.needs_person) return null;
  if (confirmedButNeedsLook(item)) return confirmedButNeedsLookSentence(approval);
  if (approval.decision === "unverified") {
    return "OpenAdapt couldn't confirm this item's checks, so a person looks at it.";
  }
  const code = approval.failed_check?.code;
  const check = code ? CHECKS[code] : undefined;
  if (approval.decision === "approval_required") {
    const why = check
      ? `This item waits for a person before saving, because ${check}.`
      : "This item waits for a person before saving.";
    return `${why} OpenAdapt can't approve and save it from here yet, so skip it or enter it by hand.`;
  }
  if (approval.decision === "stop_before_saving" && code !== "right_record") {
    return check
      ? `OpenAdapt stopped before saving, because ${check}.`
      : "OpenAdapt stopped before saving.";
  }
  return null;
}
