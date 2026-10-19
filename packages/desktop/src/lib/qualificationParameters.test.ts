import { describe, expect, it } from "vitest";
import type { QualificationParameter } from "./types";
import { serializeQualificationParameters } from "./qualificationParameters";

function parameter(
  overrides: Partial<QualificationParameter> = {},
): QualificationParameter {
  return {
    name: "record_id",
    type: "string",
    secret: false,
    required: true,
    example: null,
    choices: [],
    ...overrides,
  };
}

describe("qualification case parameters", () => {
  it("requires required public inputs", () => {
    expect(serializeQualificationParameters([parameter()], {})).toEqual({
      ok: false,
      error: "record_id is required.",
    });
  });

  it("accepts only declared choices", () => {
    const result = serializeQualificationParameters(
      [parameter({ name: "priority", type: "enum", choices: ["routine", "urgent"] })],
      { priority: "other" },
    );
    expect(result).toEqual({
      ok: false,
      error: "priority must be one of its allowed values.",
    });
  });

  it("converts numbers and omits optional and secret values", () => {
    const result = serializeQualificationParameters(
      [
        parameter({ name: "amount", type: "number" }),
        parameter({ name: "note", required: false }),
        parameter({ name: "api_token", secret: true }),
      ],
      { amount: "75.5", note: "", api_token: "never-send" },
    );
    expect(result).toEqual({ ok: true, json: JSON.stringify({ amount: 75.5 }) });
  });
});
