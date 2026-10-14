import { describe, expect, it } from "vitest";
import { parseItems, splitRows } from "./parseItems";
import type { QualificationParameter } from "./types";

const input = (name: string, extra: Partial<QualificationParameter> = {}) => ({
  name,
  type: "string",
  secret: false,
  required: true,
  example: null,
  choices: [],
  ...extra,
});

const INPUTS = [input("member_id"), input("note")];

describe("splitRows", () => {
  it("reads quoted CSV cells", () => {
    expect(splitRows('a,"b, c","say ""hi"""\n1,2,3')).toEqual([
      ["a", "b, c", 'say "hi"'],
      ["1", "2", "3"],
    ]);
  });

  it("prefers tabs from a spreadsheet paste", () => {
    expect(splitRows("M-1\tReviewed, ok\nM-2\tCalled")).toEqual([
      ["M-1", "Reviewed, ok"],
      ["M-2", "Called"],
    ]);
  });
});

describe("parseItems", () => {
  it("maps a header row by input name or its plain words", () => {
    const parsed = parseItems("Member ID,note,extra\nM-1,Reviewed,x\nM-2,,y", INPUTS);
    expect(parsed.header).toBe(true);
    expect(parsed.ignored).toEqual(["extra"]);
    expect(parsed.items[0].values).toEqual({ member_id: "M-1", note: "Reviewed" });
    expect(parsed.items[1].problems).toEqual(["No note"]);
  });

  it("takes one value per line for a task with one input", () => {
    const parsed = parseItems("M-1\nM-2\n\nM-3", [input("member_id")]);
    expect(parsed.items.map((item) => item.values.member_id)).toEqual([
      "M-1",
      "M-2",
      "M-3",
    ]);
  });

  it("asks for a header when columns can't be matched", () => {
    const parsed = parseItems("M-1\nM-2,Reviewed,extra", INPUTS);
    expect(parsed.error).toContain("member ID, note");
    expect(parsed.items).toEqual([]);
  });

  it("never asks for a protected input", () => {
    const parsed = parseItems("member_id\nM-1", [
      input("member_id"),
      input("password", { secret: true }),
    ]);
    expect(parsed.items[0]).toEqual({
      position: 0,
      values: { member_id: "M-1" },
      problems: [],
    });
  });
});
