// The words on a Needs-you card: a plain question, what OpenAdapt prevented,
// and the choices the evidence allows.
//
// The choices come from the engine (WorkItem.actions), which offers a retry
// only when the run proved nothing was written. This file only words them.
// A card may say "Nothing was written" only when `nothing_written` is true.

import {
  confirmedButNeedsLook,
  confirmedButNeedsLookSentence,
  whyItCameToYou,
} from "./approvalMode";
import { humanizeInput, plainStep, sentenceCase } from "./outcomes";
import type { AttentionAction, AttentionItem } from "./types";

export interface AttentionChoice {
  action: AttentionAction;
  title: string;
  sub: string;
  primary?: boolean;
  /** The person must confirm they looked at the record first. */
  needsRecordCheck?: boolean;
}

export interface AttentionCopy {
  question: string;
  sentence: string;
  prevented: string;
  preventedTone: "ok" | "warn";
  choices: AttentionChoice[];
}

/** "Click “Save”", or null when the stop has no recorded step. */
function stepCaption(item: AttentionItem): string | null {
  const caption = plainStep(item.halt_step);
  return caption === "A step" ? null : caption;
}

/** The button or field the step acted on, such as "Save". */
function targetName(item: AttentionItem): string | null {
  const match = /['"‘’“”](.+)['"‘’“”]/.exec(String(item.halt_step ?? ""));
  return match ? match[1] : null;
}

function stepPhrase(item: AttentionItem): string {
  const caption = stepCaption(item);
  return caption ? `this step: ${caption}` : "a step";
}

function subject(item: AttentionItem): string {
  return item.label ? item.label : "this item";
}

const CHOICES: Record<AttentionAction, Omit<AttentionChoice, "action">> = {
  retry: {
    title: "I fixed it. Check and continue",
    sub: "Runs this item again. It checks the record before it saves.",
    primary: true,
  },
  skip: {
    title: "Skip this one",
    sub: "Leaves this item undone. Nothing else changes.",
  },
  decline: {
    title: "Don't do it",
    sub: "Marks this item as one OpenAdapt should not do.",
  },
  teach: {
    title: "Show OpenAdapt the right way",
    sub: "Teach the fix once. A person approves it before it's used.",
  },
  confirmed_saved: {
    title: "It saved. Mark it done",
    sub: "You found the entry in the record.",
    primary: true,
    needsRecordCheck: true,
  },
  retry_after_check: {
    title: "It didn't save. Run it again",
    sub: "Only after you confirmed nothing was saved.",
    needsRecordCheck: true,
  },
  done_by_person: {
    title: "I finished it by hand",
    sub: "You made or fixed the entry yourself.",
    needsRecordCheck: true,
  },
  handled: {
    title: "I checked the record",
    sub: "Closes this stop. OpenAdapt won't retry it.",
    primary: true,
  },
};

export function attentionCopy(item: AttentionItem): AttentionCopy {
  const choices = item.actions.map((action) => ({ action, ...CHOICES[action] }));
  const who = subject(item);

  if (item.source === "hosted") {
    return item.nothing_written
      ? {
          question: `OpenAdapt stopped a run that Cloud sent to this computer`,
          sentence: item.halt_reason || "A check didn't match, so it stopped.",
          prevented: "It stopped before saving. Nothing was written.",
          preventedTone: "ok",
          choices: choices.map((choice) => ({
            ...choice,
            title: "Mark as handled",
            sub: "Closes this stop on this computer.",
          })),
        }
      : {
          question: "Did the save go through?",
          sentence:
            "A run that Cloud sent here stopped after it may have saved. Check the record before anything is retried.",
          prevented: "A save may have gone through.",
          preventedTone: "warn",
          choices,
        };
  }

  // Saved, and the record check confirmed it, but "Only ask a person about
  // problems" sent it here (for example, the run had to work around a changed
  // screen). The result stays "Done and checked"; a person looks first.
  if (confirmedButNeedsLook(item)) {
    return {
      question: `Is the record for ${who} right?`,
      sentence: confirmedButNeedsLookSentence(item.approval),
      prevented: "The record check confirmed the save. OpenAdapt won't enter this item again.",
      preventedTone: "warn",
      choices: choices.map((choice) =>
        choice.action === "confirmed_saved"
          ? { ...choice, title: "It's right. Close it", sub: "You looked, and the record is right." }
          : choice.action === "done_by_person"
            ? { ...choice, title: "I fixed it by hand", sub: "The record needed a change, and you made it." }
            : choice,
      ),
    };
  }

  // A person approves each save. Nobody answered in time, or the person
  // chose Don't save. Nothing was written.
  if (
    item.nothing_written &&
    item.approval &&
    ((item.approval.mode === "each_item" && item.approval.decision === "approval_required") ||
      item.approval.decision === "declined")
  ) {
    return {
      question:
        item.approval.decision === "declined"
          ? `${sentenceCase(who)} wasn't saved`
          : `${sentenceCase(who)} waited for your approval`,
      sentence: whyItCameToYou(item) ?? "OpenAdapt stopped before saving.",
      prevented: "It stopped before saving. Nothing was written.",
      preventedTone: "ok",
      choices,
    };
  }

  // "Only ask a person about problems" held the save for a person. Nothing
  // was written; the sentence says which check sent it here.
  if (item.nothing_written && item.approval?.decision === "approval_required") {
    return {
      question: `${sentenceCase(who)} is waiting for a person before saving`,
      sentence: whyItCameToYou(item) ?? "This item waits for a person before saving.",
      prevented: "It stopped before saving. Nothing was written.",
      preventedTone: "ok",
      choices,
    };
  }

  // Anything the evidence doesn't prove effect-free is a record to check.
  if (!item.nothing_written) {
    return {
      question: `Did the save for ${who} go through?`,
      sentence:
        item.halt_kind === "save_not_confirmed"
          ? "OpenAdapt saved, but when it read the record back it couldn't confirm the entry. A person checks the record before anything is retried."
          : `OpenAdapt stopped at ${stepPhrase(item)}. It can't tell whether the save went through, so a person checks the record before anything is retried.`,
      prevented: "A save may have gone through. OpenAdapt won't retry this item on its own.",
      preventedTone: "warn",
      choices,
    };
  }

  if (item.result === "did_not_finish") {
    return {
      question: `OpenAdapt had a problem with ${who}`,
      sentence:
        "It stopped because of a problem on this computer, before it could change anything.",
      prevented: "It didn't change anything. Nothing was written.",
      preventedTone: "ok",
      choices,
    };
  }

  if (item.result === "did_not_start") {
    return {
      question: `OpenAdapt didn't start ${who}`,
      sentence: item.halt_reason
        ? sentenceCase(item.halt_reason)
        : "It refused before it touched the app.",
      prevented: "It didn't touch the app. Nothing was written.",
      preventedTone: "ok",
      choices,
    };
  }

  switch (item.halt_kind) {
    case "wrong_record":
      return {
        question: item.expected
          ? `Is ${item.expected} the record that should change?`
          : "Is the right record open?",
        sentence: item.observed
          ? `OpenAdapt was given ${item.expected || "one record"}, but the screen showed ${item.observed}. It stopped before saving.`
          : "The record on screen didn't match the one OpenAdapt was given. It stopped before saving.",
        prevented: "Prevented a save on the wrong record. Nothing was written.",
        preventedTone: "ok",
        choices,
      };
    case "screen_changed":
      return {
        question: targetName(item)
          ? `OpenAdapt couldn't find “${targetName(item)}” on the screen`
          : "OpenAdapt couldn't find the next thing to click",
        sentence:
          "The app may look different from when you showed the workflow. It stopped instead of guessing where to click.",
        prevented: "It stopped before saving. Nothing was written.",
        preventedTone: "ok",
        choices: [...choices].sort((left, right) =>
          left.action === "teach" ? -1 : right.action === "teach" ? 1 : 0,
        ),
      };
    default:
      return {
        question: stepCaption(item)
          ? `OpenAdapt stopped at this step: ${stepCaption(item)}`
          : "OpenAdapt stopped before finishing",
        // An approval-mode stop names its check in words, not engine codes.
        sentence:
          whyItCameToYou(item) ??
          (item.halt_reason
            ? sentenceCase(item.halt_reason)
            : "Something didn't match, so it stopped instead of guessing."),
        prevented: "It stopped before saving. Nothing was written.",
        preventedTone: "ok",
        choices,
      };
  }
}

/** The input values of an item, in plain words, for the card's detail line. */
export function describeInputs(values: Record<string, unknown> | undefined): string {
  if (!values) return "";
  return Object.entries(values)
    .map(([name, value]) => `${sentenceCase(humanizeInput(name))}: ${String(value)}`)
    .join(", ");
}
