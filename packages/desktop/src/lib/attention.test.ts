import { describe, expect, it } from "vitest";
import { attentionCopy } from "./attention";
import type { AttentionItem } from "./types";

function item(overrides: Partial<AttentionItem>): AttentionItem {
  return {
    item_id: "item-1",
    source: "item",
    workflow_id: "wf",
    task_name: "Add a claim note",
    label: "M-1",
    status: "open",
    result: "stopped_before_saving",
    nothing_written: true,
    execution_outcome: "HALTED",
    transaction_outcome: "HALTED_BEFORE_EFFECT",
    halt_kind: "wrong_record",
    halt_step: "click 'Save'",
    halt_reason: "",
    expected: "M-1",
    observed: "M-9",
    actions: ["retry", "skip", "decline", "teach"],
    ...overrides,
  };
}

const NO_WRITE_CLAIM = /nothing was written|nothing changed|safe to (re-?run|try again)/i;

function text(copy: ReturnType<typeof attentionCopy>): string {
  return [
    copy.question,
    copy.sentence,
    copy.prevented,
    ...copy.choices.flatMap((choice) => [choice.title, choice.sub]),
  ].join(" ");
}

describe("Needs-you card words", () => {
  it("asks about the record and names what was prevented", () => {
    const copy = attentionCopy(item({}));
    expect(copy.question).toBe("Is M-1 the record that should change?");
    expect(copy.sentence).toContain("the screen showed M-9");
    expect(copy.prevented).toContain("Prevented a save on the wrong record");
    expect(copy.choices.map((choice) => choice.title)).toEqual([
      "I fixed it. Check and continue",
      "Skip this one",
      "Don't do it",
      "Show OpenAdapt the right way",
    ]);
  });

  it("never claims nothing was written unless the item proves it", () => {
    for (const halt_kind of ["wrong_record", "screen_changed", "save_not_confirmed", "other", null] as const) {
      for (const result of [
        "check_record",
        "finished_unchecked",
        "did_not_finish",
        "stopped_before_saving",
        null,
      ] as const) {
        const copy = attentionCopy(
          item({
            halt_kind,
            result,
            nothing_written: false,
            actions: ["confirmed_saved", "retry_after_check", "done_by_person"],
          }),
        );
        expect(text(copy)).not.toMatch(NO_WRITE_CLAIM);
        expect(copy.question).toBe("Did the save for M-1 go through?");
        expect(copy.choices.every((choice) => choice.needsRecordCheck)).toBe(true);
      }
    }
  });

  it("asks a person to check the record for a run Cloud sent here", () => {
    const copy = attentionCopy(
      item({ source: "hosted", nothing_written: false, result: "check_record", actions: ["handled"] }),
    );
    expect(text(copy)).not.toMatch(NO_WRITE_CLAIM);
    expect(copy.choices[0].title).toBe("I checked the record");
  });

  it("leads with teaching when the screen changed", () => {
    const copy = attentionCopy(item({ halt_kind: "screen_changed" }));
    expect(copy.question).toBe("OpenAdapt couldn't find “Save” on the screen");
    expect(copy.choices[0].action).toBe("teach");
  });
});

describe("A person approves each save", () => {
  const block = {
    validated: true,
    mode: "each_item" as const,
    reason: null,
    needs_person: true,
    counts_toward_problems: false,
    switch_off_reason: null,
    failed_check: null,
    post_checks: { verified: false, whole_run_clean: false },
    saves_without_person: 0,
  };

  it("says nobody approved in time and offers to run it again", () => {
    const copy = attentionCopy(
      item({
        halt_kind: "other",
        approval: { ...block, decision: "approval_required" },
        actions: ["retry", "skip", "decline", "teach"],
      }),
    );
    expect(copy.question).toBe("M-1 waited for your approval");
    expect(copy.sentence).toContain("Nobody approved the save in time");
    expect(copy.prevented).toBe("It stopped before saving. Nothing was written.");
    expect(copy.choices[0].action).toBe("retry");
  });

  it("repeats the reason a person gave for Don't save", () => {
    const copy = attentionCopy(
      item({
        halt_kind: "other",
        approval: { ...block, decision: "declined", reason: "wrong_values", needs_person: false },
      }),
    );
    expect(copy.question).toBe("M-1 wasn't saved");
    expect(copy.sentence).toBe(
      "You chose Don't save because the values are wrong. OpenAdapt didn't save it.",
    );
  });

  it("never says nothing was written when the item may have saved", () => {
    const copy = attentionCopy(
      item({
        nothing_written: false,
        result: "check_record",
        transaction_outcome: "RECONCILIATION_REQUIRED",
        approval: { ...block, decision: "declined", reason: "wrong_record" },
      }),
    );
    expect(text(copy)).not.toMatch(NO_WRITE_CLAIM);
  });
});
