import { expect, it } from "vitest";

import { describeEffect } from "./checkPlan";
import type { QualificationEditableEffect } from "./types";

// What "Check it" says each save changes. The lines name the field in plain
// words and say where its value comes from. They don't repeat the field that
// finds the record on every line ("on the record for the email you give it").

function effect(
  overrides: Partial<QualificationEditableEffect>,
): QualificationEditableEffect {
  return {
    index: 0,
    kind: "field_equals",
    match: { email: { source: "parameter", value: "email" } },
    field: null,
    value: null,
    expected_count: 1,
    key_field: "key",
    count_new_only: false,
    risk: "reversible",
    needs_operator_confirmation: false,
    verification_tier: null,
    effect_contract_hash: "sha256:0",
    ...overrides,
  };
}

const param = (value: string) => ({ source: "parameter" as const, value });

it.each([
  // The referral task: register a patient, found by the email you give it.
  [effect({ kind: "record_written" }), "Saves a record with the email you give it."],
  [effect({ field: "title", value: param("title") }), "Sets the title you give it."],
  [effect({ field: "fname", value: param("fname") }), "Sets the first name you give it."],
  [effect({ field: "DOB", value: param("DOB") }), "Sets the date of birth you give it."],
  [effect({ field: "phone_home", value: param("phone_home") }), "Sets the home phone you give it."],
  [effect({ field: "postal_code", value: param("postal_code") }), "Sets the postal code you give it."],
  [
    effect({ field: "state", value: { source: "literal", value: "CA" } }),
    "Sets the state to the same value every time.",
  ],
  // A value from a differently named input says which input.
  [
    effect({
      match: { member_id: param("member_id") },
      field: "status_note",
      value: param("note"),
    }),
    "Sets the status note to the note you give it.",
  ],
  [effect({ kind: "record_written", match: {} }), "Saves a record."],
  [
    effect({ kind: "record_written", expected_count: 0 }),
    "Checks that no record with the email you give it is saved.",
  ],
])("%#: %j", (input, expected) => {
  expect(describeEffect(input)).toBe(expected);
});

it("never keys a line on the field that finds the record", () => {
  const line = describeEffect(effect({ field: "title", value: param("title") }));
  expect(line).not.toMatch(/on the record for/);
  expect(line).not.toContain("email");
});
