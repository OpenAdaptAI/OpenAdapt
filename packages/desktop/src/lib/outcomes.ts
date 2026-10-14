// Plain results derived from Flow's transaction outcome.
//
// This mirrors openadapt_desktop/outcomes.py. Both read the same cases from
// outcome-cases.json in their tests, so the two copies cannot drift apart.
//
// The safety rule: a label may say "nothing was written" or offer a retry
// only when the transaction outcome proves that no business effect happened.
// A coarse HALTED (or FAILED, or anything unknown) never proves that, so it
// renders as "Check the record", never as "Stopped before saving".

export type PlainResultKey =
  | "done_checked"
  | "stopped_before_saving"
  | "check_record"
  | "finished_unchecked"
  | "did_not_finish"
  | "did_not_start";

export interface PlainResult {
  key: PlainResultKey;
  nothingWritten: boolean;
  mayHaveWritten: boolean;
  safeToRetry: boolean;
  needsPerson: boolean;
  /** The exact engine term, for technical details only. */
  exact: string;
}

const TRANSACTION_OUTCOMES = new Set([
  "VERIFIED",
  "HALTED_BEFORE_EFFECT",
  "RECONCILIATION_REQUIRED",
  "FAILED_PLATFORM",
  "CANCELED",
  "REJECTED_POLICY",
  "COMPLETED_UNVERIFIED",
  "ROLLED_BACK",
]);

// Flow derives the transaction outcome from the execution outcome; any other
// pairing is not internally consistent and proves nothing.
const CONSISTENT_EXECUTION: Record<string, Set<string>> = {
  VERIFIED: new Set(["VERIFIED"]),
  HALTED_BEFORE_EFFECT: new Set(["HALTED"]),
  REJECTED_POLICY: new Set(["HALTED", "FAILED"]),
  CANCELED: new Set(["HALTED", "FAILED"]),
  FAILED_PLATFORM: new Set(["FAILED"]),
  COMPLETED_UNVERIFIED: new Set(["COMPLETED_UNVERIFIED"]),
  ROLLED_BACK: new Set(["ROLLED_BACK"]),
  RECONCILIATION_REQUIRED: new Set([
    "HALTED",
    "FAILED",
    "COMPLETED_UNVERIFIED",
    "ROLLED_BACK",
  ]),
};

export function consistentTransactionOutcome(
  execution: unknown,
  transaction: unknown,
): string | null {
  if (typeof transaction !== "string") return null;
  if (!TRANSACTION_OUTCOMES.has(transaction)) return null;
  if (typeof execution !== "string") return null;
  return CONSISTENT_EXECUTION[transaction].has(execution) ? transaction : null;
}

function result(
  key: PlainResultKey,
  exact: string,
  { nothingWritten = false, mayHaveWritten = false } = {},
): PlainResult {
  return {
    key,
    nothingWritten,
    mayHaveWritten: mayHaveWritten && !nothingWritten,
    safeToRetry: nothingWritten,
    needsPerson: key !== "done_checked",
    exact,
  };
}

/** Map one run's outcome to a plain result and its safety flags. */
export function plainResult(
  execution: unknown,
  transaction: unknown = null,
  { preActionRefusal = false }: { preActionRefusal?: boolean } = {},
): PlainResult {
  if (preActionRefusal) {
    return result("did_not_start", "refused before Flow", {
      nothingWritten: true,
    });
  }
  const tx = consistentTransactionOutcome(execution, transaction);
  if (transaction !== null && transaction !== undefined && tx === null) {
    return result("check_record", `inconsistent ${String(execution)}/${String(transaction)}`, {
      mayHaveWritten: true,
    });
  }
  switch (tx) {
    case "VERIFIED":
      return result("done_checked", tx, { mayHaveWritten: true });
    case "HALTED_BEFORE_EFFECT":
    case "REJECTED_POLICY":
    case "CANCELED":
      return result("stopped_before_saving", tx, { nothingWritten: true });
    case "FAILED_PLATFORM":
      return result("did_not_finish", tx, { nothingWritten: true });
    case "COMPLETED_UNVERIFIED":
      return result("finished_unchecked", tx, { mayHaveWritten: true });
    case "RECONCILIATION_REQUIRED":
    case "ROLLED_BACK":
      return result("check_record", tx, { mayHaveWritten: true });
  }
  // Only the coarse execution outcome is known.
  if (execution === "VERIFIED") {
    return result("done_checked", "VERIFIED", { mayHaveWritten: true });
  }
  if (execution === "COMPLETED_UNVERIFIED" || execution === "success") {
    return result("finished_unchecked", String(execution), {
      mayHaveWritten: true,
    });
  }
  if (execution === "FAILED") {
    return result("did_not_finish", "FAILED", { mayHaveWritten: true });
  }
  return result("check_record", String(execution || "unknown"), {
    mayHaveWritten: true,
  });
}

export type Tone = "ok" | "warn" | "crit" | "neutral";

export interface ResultCopy {
  label: string;
  sentence: string;
  next: string;
  tone: Tone;
}

/**
 * The words for one plain result. Every sentence that says nothing was
 * written is reachable only through a result whose `nothingWritten` is true.
 */
export function resultCopy(plain: Pick<PlainResult, "key" | "nothingWritten">): ResultCopy {
  switch (plain.key) {
    case "done_checked":
      return {
        label: "Done and checked",
        sentence:
          "It saved the entry and read the record back to confirm it.",
        next: "",
        tone: "ok",
      };
    case "stopped_before_saving":
      return {
        label: "Stopped before saving",
        sentence:
          "Something didn't match, so it stopped before changing anything. Nothing was written.",
        next: "Decide what happens next in Needs you.",
        tone: "warn",
      };
    case "check_record":
      return {
        label: "Check the record",
        sentence:
          "A save may have gone through. A person checks the record before anything is retried.",
        next: "Look at the record, then tell OpenAdapt what you found.",
        tone: "warn",
      };
    case "finished_unchecked":
      return {
        label: "Finished, not checked",
        sentence:
          "It did the steps but didn't confirm the saved result.",
        next: "Check the record yourself before you rely on it.",
        tone: "neutral",
      };
    case "did_not_finish":
      return plain.nothingWritten
        ? {
            label: "Didn't finish",
            sentence:
              "OpenAdapt had a problem before it could change anything. Nothing was written.",
            next: "Try again once the problem is fixed.",
            tone: "crit",
          }
        : {
            label: "Didn't finish",
            sentence:
              "It couldn't complete the task, and it can't tell whether anything was saved.",
            next: "Check the record before you try again.",
            tone: "crit",
          };
    case "did_not_start":
      return {
        label: "Didn't start",
        sentence:
          "OpenAdapt refused before it touched the app. Nothing was written.",
        next: "Fix what it asked for, then try again.",
        tone: "neutral",
      };
  }
}

const EXECUTION_FOR_TRANSACTION: Record<string, string> = {
  VERIFIED: "VERIFIED",
  HALTED_BEFORE_EFFECT: "HALTED",
  REJECTED_POLICY: "HALTED",
  CANCELED: "FAILED",
  FAILED_PLATFORM: "FAILED",
  COMPLETED_UNVERIFIED: "COMPLETED_UNVERIFIED",
  ROLLED_BACK: "ROLLED_BACK",
  RECONCILIATION_REQUIRED: "HALTED",
};

/**
 * Plain result for a record that carries only Flow's transaction outcome,
 * such as a run Cloud sent to this computer. An unknown value proves nothing.
 */
export function plainResultForTransaction(transaction: unknown): PlainResult {
  const execution = EXECUTION_FOR_TRANSACTION[String(transaction)];
  if (!execution) return plainResult("unknown");
  return plainResult(execution, transaction);
}

/** Plain result for a run report as Desktop's engine returns it. */
export function plainResultForReport(report: {
  outcome?: unknown;
  transaction_outcome?: unknown;
  pre_action_refusal?: unknown;
}): PlainResult {
  return plainResult(report.outcome, report.transaction_outcome ?? null, {
    preActionRefusal: report.pre_action_refusal === true,
  });
}

// ------------------------------------------------------------ plain words

const INPUT_WORDS: Record<string, string> = {
  id: "ID",
  mrn: "MRN",
  dob: "date of birth",
  npi: "NPI",
  url: "URL",
};

// Whole names that EMR patient tables abbreviate or write in database word
// order, so "fname" reads "first name" and "phone_home" reads "home phone".
const INPUT_PHRASES: Record<string, string> = {
  fname: "first name",
  mname: "middle name",
  lname: "last name",
  "phone home": "home phone",
  "phone cell": "mobile phone",
  "phone biz": "work phone",
};

/** "member_id" -> "member ID", "patientName" -> "patient name". */
export function humanizeInput(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((word) => word.toLowerCase());
  const phrase = INPUT_PHRASES[words.join(" ")];
  if (phrase) return phrase;
  return words.map((word) => INPUT_WORDS[word] ?? word).join(" ") || name;
}

/** Sentence-case a phrase: "member ID" -> "Member ID". */
export function sentenceCase(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

const CLICK_VERBS: Record<string, string> = {
  click: "Click",
  double_click: "Double-click",
  right_click: "Right-click",
};

/**
 * A caption for a click from the control it clicks: its role and accessible
 * name, as the recorder saved them. Returns null when there is no name to go
 * on, so the caller falls back to the step title. A step title quotes OCR
 * text, which can misread a control; the structure doesn't.
 */
export function plainTargetStep(
  action: string | null | undefined,
  target: { role?: string | null; name?: string | null } | null | undefined,
): string | null {
  const verb = CLICK_VERBS[String(action ?? "").toLowerCase()];
  const raw = String(target?.name ?? "").trim().replace(/\s+/g, " ");
  if (!verb || !raw) return null;
  // A name with no spaces is usually an element's id or form name, such as
  // "form_state"; make it readable. A real label is kept as written.
  const name = /\s/.test(raw) ? raw : humanizeInput(raw);
  const quoted = `“${name}”`;
  switch (String(target?.role ?? "").toLowerCase()) {
    case "combobox":
    case "listbox":
      return verb === "Click" ? `Open the ${quoted} list` : `${verb} the ${quoted} list`;
    case "textbox":
    case "searchbox":
    case "spinbutton":
      return `${verb} the ${quoted} field`;
    case "checkbox":
      return `${verb} the ${quoted} checkbox`;
    case "radio":
      return `${verb} the ${quoted} option`;
    case "button":
      return `${verb} the ${quoted} button`;
    case "link":
      return `${verb} the ${quoted} link`;
    case "tab":
      return `${verb} the ${quoted} tab`;
    case "menuitem":
      return `${verb} ${quoted} in the menu`;
    default:
      return `${verb} ${quoted}`;
  }
}

/** One plain caption for a compiled step intent, such as "click 'Save'". */
export function plainStep(intent: string | null | undefined): string {
  const text = String(intent ?? "").trim();
  if (!text) return "A step";
  const click = /^(?:double[_ ])?click\s+['"‘’“”](.+)['"‘’“”]$/i.exec(text);
  if (click) return `Click “${click[1]}”`;
  if (/^click recorded visual target$/i.test(text)) {
    return "Click the spot you clicked when you showed it";
  }
  const typeParam = /^type\s+<([^>]+)>$/i.exec(text);
  if (typeParam) return `Type the ${humanizeInput(typeParam[1])}`;
  const typeText = /^type\s+['"‘’“”](.+)['"‘’“”]$/i.exec(text);
  if (typeText) return `Type “${typeText[1]}”`;
  const select = /^select(?:_option)?\s+['"‘’“”](.+)['"‘’“”]$/i.exec(text);
  if (select) return `Choose “${select[1]}”`;
  return sentenceCase(text);
}
