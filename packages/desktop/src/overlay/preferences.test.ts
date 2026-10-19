import { beforeEach, expect, it, vi } from "vitest";
import { ensureControlOverlayCaptureExcluded } from "../lib/engine";
import {
  LEGACY_OVERLAY_PRESENTATION_KEY,
  OVERLAY_PRESENTATION_KEY,
  overlayPresentationEnabled,
  saveOverlayPresentation,
} from "./preferences";

vi.mock("../lib/engine", () => ({
  ensureControlOverlayCaptureExcluded: vi.fn(() => Promise.resolve()),
}));

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

it("migrates the recording preference to derivative exports", () => {
  window.localStorage.setItem(LEGACY_OVERLAY_PRESENTATION_KEY, "true");
  expect(overlayPresentationEnabled()).toBe(true);
  expect(window.localStorage.getItem(OVERLAY_PRESENTATION_KEY)).toBe("true");
  expect(window.localStorage.getItem(LEGACY_OVERLAY_PRESENTATION_KEY)).toBeNull();
});

it("persists only after native capture exclusion is acknowledged", async () => {
  await saveOverlayPresentation(true);
  expect(ensureControlOverlayCaptureExcluded).toHaveBeenCalledOnce();
  expect(window.localStorage.getItem(OVERLAY_PRESENTATION_KEY)).toBe("true");
});
