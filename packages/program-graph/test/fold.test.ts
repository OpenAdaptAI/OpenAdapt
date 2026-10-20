import { describe, expect, it } from "vitest";
import { deriveDisplayGraph, foldSequences, STOP_NODE_ID, validateFolds } from "../src/index.js";
import { processRecords, referral } from "./fixtures.js";

describe("folds", () => {
  it("the fixture's folds are sound and cover the whole loop body", () => {
    expect(validateFolds(referral.spec, referral.folds)).toEqual([]);
    const members = referral.folds.groups.flatMap((g) => g.nodes);
    const body = (referral.spec.nodes ?? []).filter((n) => n.kind === "action").map((n) => n.id);
    expect(members).toEqual(body);
  });

  it("folds members into one card each and keeps every edge between cards", () => {
    const display = deriveDisplayGraph(referral.spec, { folds: referral.folds });
    expect((display.spec.nodes ?? []).map((n) => n.id)).toEqual([
      "for_each", "name", "birth", "address", "contact", "save", "__loop_done__",
    ]);
    expect((display.spec.edges ?? []).map((e) => `${e.source}>${e.target}:${e.kind}`)).toEqual([
      "for_each>name:loop_body",
      "name>birth:sequence",
      "birth>address:sequence",
      "address>contact:sequence",
      "contact>save:sequence",
      "save>for_each:sequence",
      "for_each>__loop_done__:sequence",
    ]);
    const save = display.folds.get("save")!;
    expect([save.first, save.last, save.members.length]).toEqual([40, 42, 3]);
    const card = (display.spec.nodes ?? []).find((n) => n.id === "save")!;
    expect(card.effects).toHaveLength(11);
    expect(display.ownerOf.get("for_each::s::step_040")).toBe("save");
  });

  it("an expanded fold shows its exact members again", () => {
    const display = deriveDisplayGraph(referral.spec, { folds: referral.folds, expanded: new Set(["save"]) });
    const ids = (display.spec.nodes ?? []).map((n) => n.id);
    expect(ids).toContain("for_each::s::step_039");
    expect(ids).not.toContain("save");
    expect(display.ownerOf.get("for_each::s::step_040")).toBe("for_each::s::step_040");
  });

  it("adds one stop card with an exception exit per folded card", () => {
    const display = deriveDisplayGraph(referral.spec, { folds: referral.folds, stopExits: referral.folds.stops });
    const exits = (display.spec.edges ?? []).filter((e) => e.target === STOP_NODE_ID);
    expect(exits).toEqual([{ source: "save", target: STOP_NODE_ID, kind: "exception", label: "not confirmed" }]);
  });

  it("reports a fold that names a missing node or reuses an id", () => {
    expect(
      validateFolds(processRecords, { groups: [{ id: "done", nodes: ["open_record", "nope"] }] }),
    ).toEqual(["fold done reuses a spec node id", "fold done names nope, which the spec does not have"]);
  });

  it("foldSequences folds long straight runs only", () => {
    const folds = foldSequences(referral.spec, { minLength: 6, maxLength: 10 });
    expect(folds.groups.length).toBe(5);
    expect(folds.groups.flatMap((g) => g.nodes)).toHaveLength(41);
    expect(validateFolds(referral.spec, folds)).toEqual([]);
    expect(foldSequences(processRecords).groups).toEqual([]);
  });
});
