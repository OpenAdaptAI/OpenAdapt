import { describe, expect, it } from "vitest";
import cases from "./outcome-cases.json";
import {
  humanizeInput,
  plainResult,
  plainResultForReport,
  plainStep,
  resultCopy,
  type PlainResultKey,
} from "./outcomes";

type Case = {
  execution: string;
  transaction: string | null;
  pre_action_refusal?: boolean;
  key: PlainResultKey;
  nothing_written: boolean;
  safe_to_retry: boolean;
};

describe("plain results", () => {
  it.each(cases.cases as Case[])(
    "maps $execution / $transaction like the engine",
    (entry) => {
      const result = plainResult(entry.execution, entry.transaction, {
        preActionRefusal: Boolean(entry.pre_action_refusal),
      });
      expect(result.key).toBe(entry.key);
      expect(result.nothingWritten).toBe(entry.nothing_written);
      expect(result.safeToRetry).toBe(entry.safe_to_retry);
    },
  );

  it("never says nothing was written unless the transaction outcome proves it", () => {
    const claims = /nothing was written|safe to (re-?run|try again)/i;
    for (const entry of cases.cases as Case[]) {
      const result = plainResult(entry.execution, entry.transaction, {
        preActionRefusal: Boolean(entry.pre_action_refusal),
      });
      const copy = resultCopy(result);
      const text = `${copy.label} ${copy.sentence} ${copy.next}`;
      if (!result.nothingWritten) {
        expect(text).not.toMatch(claims);
      }
    }
  });

  it("treats a coarse halt as a record to check", () => {
    const halted = plainResultForReport({ outcome: "HALTED" });
    expect(halted.key).toBe("check_record");
    expect(resultCopy(halted).label).toBe("Check the record");
  });

  it("reads the engine's run report fields", () => {
    expect(
      plainResultForReport({
        outcome: "HALTED",
        transaction_outcome: "HALTED_BEFORE_EFFECT",
      }).key,
    ).toBe("stopped_before_saving");
    expect(
      plainResultForReport({ outcome: "refused", pre_action_refusal: true }).key,
    ).toBe("did_not_start");
  });
});

describe("plain words", () => {
  it("turns input names into words", () => {
    expect(humanizeInput("member_id")).toBe("member ID");
    expect(humanizeInput("patientDob")).toBe("patient date of birth");
    expect(humanizeInput("note")).toBe("note");
  });

  it("turns compiled step intents into captions", () => {
    expect(plainStep("click 'Save Encounter'")).toBe("Click “Save Encounter”");
    expect(plainStep("type <note>")).toBe("Type the note");
    expect(plainStep("click recorded visual target")).toBe(
      "Click the spot you clicked when you showed it",
    );
    expect(plainStep("")).toBe("A step");
  });
});

describe("transaction-only records", () => {
  it("maps a Cloud run's transaction outcome conservatively", async () => {
    const { plainResultForTransaction } = await import("./outcomes");
    expect(plainResultForTransaction("HALTED_BEFORE_EFFECT").nothingWritten).toBe(true);
    expect(plainResultForTransaction("RECONCILIATION_REQUIRED").key).toBe("check_record");
    expect(plainResultForTransaction("SOMETHING_NEW").key).toBe("check_record");
    expect(plainResultForTransaction(null).nothingWritten).toBe(false);
  });
});
