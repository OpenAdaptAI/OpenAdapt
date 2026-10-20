import { describe, expect, it } from "vitest";
import { stopRuleWords, type GraphNode } from "../src/index.js";
import { referral } from "./fixtures.js";

const node = (over: Partial<GraphNode>): GraphNode => ({ id: "n", index: 0, title: "t", ...over });

describe("stopRuleWords", () => {
  it("words every stop rule in the public referral spec without falling back", () => {
    for (const n of referral.spec.nodes ?? []) {
      const words = stopRuleWords(n);
      expect(words).toHaveLength((n.halts ?? []).length);
      for (const w of words) expect(w.text).not.toBe("Stops if a declared check fails.");
    }
    const save = (referral.spec.nodes ?? []).find((n) => n.id === "for_each::s::step_040")!;
    expect(stopRuleWords(save)).toEqual([
      { kind: "run", text: "Stops if reading the saved record back doesn’t confirm it." },
    ]);
    const key = (referral.spec.nodes ?? []).find((n) => n.id === "for_each::s::step_002")!;
    expect(stopRuleWords(key)[0].kind).toBe("review");
  });

  it("maps Flow's full rule names, in order", () => {
    expect(
      stopRuleWords(node({ halts: ["halts if the system-of-record effect is not confirmed", "halts if not ready in time (x)"] })).map((w) => w.kind),
    ).toEqual(["run", "run"]);
  });

  it("places a public wait rule where Flow's builder puts it", () => {
    const words = stopRuleWords(
      node({
        halts: ["declared stop rule 1", "declared stop rule 2", "declared stop rule 3"],
        effects: [{ kind: "record_written", summary: "x" }],
        risk_review_required: true,
        wait_until: null,
      }),
    );
    expect(words.map((w) => w.text)).toEqual([
      "Stops if reading the saved record back doesn’t confirm it.",
      "Stops if the screen isn’t ready in time.",
      "A person reviews this action before the program is approved to run on its own.",
    ]);
  });

  it("stays generic when the public fields cannot explain the count", () => {
    expect(stopRuleWords(node({ halts: ["declared stop rule 1", "declared stop rule 2", "declared stop rule 3"] }))).toHaveLength(3);
  });
});
