import { expect, it } from "vitest";
import {
  AUTH_PAUSE_COPY,
  EMPTY_COACH,
  normalizeCoachPayload,
  overlayLayoutFor,
  overlayUsesStage,
  sanitizeCoachHint,
} from "./coach";

const HMAC = "a".repeat(64);

it("keeps a short playbook hint and drops identifying text", () => {
  expect(sanitizeCoachHint("Open the claim screen")).toBe(
    "Open the claim screen",
  );
  expect(sanitizeCoachHint("see https://openadapt.ai/j/1")).toBeNull();
  expect(sanitizeCoachHint("ask jane@clinic.org")).toBeNull();
  expect(sanitizeCoachHint("open record 123456")).toBeNull();
});

it("omits a target rect unless an exact observation binding is present", () => {
  const withRectOnly = normalizeCoachPayload({
    hint: "Open the claim screen",
    target: {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x: 0.1, y: 0.1, width: 0.2, height: 0.2 },
    },
  });
  expect(withRectOnly.target).toBeNull();

  const bound = normalizeCoachPayload({
    target: {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
      binding: {
        kind: "observation_hmac_sha256",
        observation_hmac_sha256: HMAC,
      },
    },
  });
  expect(bound.target?.rect).toEqual({
    x: 0.1,
    y: 0.2,
    width: 0.3,
    height: 0.4,
  });
  expect(bound.target?.binding).toEqual({
    kind: "observation_hmac_sha256",
    observation_hmac_sha256: HMAC,
  });
});

it("drops pack URLs and screenshots from the local coach payload", () => {
  const payload = normalizeCoachPayload({
    hint: "Open the claim screen",
    pack_url: "https://openadapt.ai/j/secret",
    screenshot: "data:image/png;base64,aaa",
  });
  expect(JSON.stringify(payload)).not.toContain("openadapt.ai");
  expect(JSON.stringify(payload)).not.toContain("screenshot");
  expect(JSON.stringify(payload)).not.toContain("data:image");
});

it("stages the overlay only while a bound ring can stay click-through", () => {
  const coach = normalizeCoachPayload({
    target: {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x: 0.2, y: 0.2, width: 0.1, height: 0.1 },
      binding: { kind: "observation_hmac_sha256", observation_hmac_sha256: HMAC },
    },
  });
  expect(overlayUsesStage(true, false, coach)).toBe(true);
  expect(overlayUsesStage(true, true, coach)).toBe(false);
  expect(overlayLayoutFor(true, true, true, coach)).toBe("paused");
  expect(overlayLayoutFor(true, false, false, EMPTY_COACH)).toBe("compact");
});

it("keeps auth copy off the closed overlay vocabulary", () => {
  expect(AUTH_PAUSE_COPY).toBe(
    "Type in the application. Continue here when done.",
  );
});
