// No notification carries protected content, whatever the engine emitted.
import { describe, expect, it } from "vitest";
import {
  NOTIFICATION_FIELDS,
  NOTIFICATION_TITLE,
  genericNotification,
} from "./attentionNotification";

// Everything a lock screen must never show, in shapes the engine event could
// plausibly deliver it.
const PROTECTED = [
  "Coverage: active",
  "MRN 4417092",
  "Jane Q. Patient",
  "Is the intended result present in the destination record?",
  "openIMIS claim 88213",
  "identity signal 2 of 3 refuted",
];

describe("genericNotification", () => {
  it("carries only a fixed title, a derived body, and a count", () => {
    const payload = genericNotification({ open_count: 3 });
    expect(Object.keys(payload).sort()).toEqual([...NOTIFICATION_FIELDS].sort());
    expect(payload.title).toBe(NOTIFICATION_TITLE);
    expect(payload.openCount).toBe(3);
    expect(payload.body).toBe(
      "3 decisions waiting on this computer. Open OpenAdapt to review.",
    );
  });

  it("says nothing about the workflow, person, or application", () => {
    for (const count of [0, 1, 2, 40]) {
      const { body } = genericNotification({ open_count: count });
      expect(body).toMatch(
        /^(\d+ decisions? waiting on this computer\. )?Open OpenAdapt to review\.$/,
      );
    }
  });

  it.each(PROTECTED)("never forwards engine text (%s)", (protectedText) => {
    const payload = genericNotification({
      open_count: 2,
      title: protectedText,
      body: protectedText,
      question: protectedText,
      observed: protectedText,
      route: `/tasks/${protectedText}`,
    });
    expect(JSON.stringify(payload)).not.toContain(protectedText);
    expect(payload.openCount).toBe(2);
    expect(payload.title).toBe(NOTIFICATION_TITLE);
  });

  it("coerces a malformed count instead of propagating it", () => {
    for (const hostile of ["3 records for Jane", null, undefined, {}, [], 1.5, NaN, true]) {
      const payload = genericNotification({ open_count: hostile });
      expect(payload.openCount).toBe(0);
      expect(JSON.stringify(payload)).not.toContain("Jane");
    }
  });

  it("survives a malformed event entirely", () => {
    for (const hostile of [null, undefined, "", 7, [], "MRN 4417092"]) {
      const payload = genericNotification(hostile);
      expect(payload.openCount).toBe(0);
      expect(payload.title).toBe(NOTIFICATION_TITLE);
    }
  });

  it("bounds the count", () => {
    expect(genericNotification({ open_count: 10 ** 9 }).openCount).toBe(9999);
    expect(genericNotification({ open_count: -5 }).openCount).toBe(0);
  });
});
