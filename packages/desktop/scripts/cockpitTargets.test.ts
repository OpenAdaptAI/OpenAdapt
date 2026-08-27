// The capture contract: the harness must be able to re-shoot every cockpit
// screenshot openadapt.ai publishes, not just the ones someone remembered.
//
// Eight images sat on the marketing site through a palette change because the
// adapter that made them was never committed. Before that, a four-state set was
// re-shot one state at a time because only one state was listed as a target,
// and the other three kept the old palette while the set looked finished.
//
// PUBLISHED below is the list openadapt-web pins in
// public/desktop-preview/MANIFEST.json and tests/desktopPreview.test.js. Adding
// a cockpit image there without adding it here fails this test, and the fix is
// to add the capture target rather than to edit this list.

import { describe, expect, it } from "vitest";

import { TARGETS, VIEWPORT } from "./cockpit-targets.mjs";

const PUBLISHED = [
  "01_login.png",
  "05_onboarding.png",
  "10_dashboard_workflows.png",
  "20_settings.png",
  "30_record.png",
  "40_watchrun_halted.png",
  "45_watchrun_verified.png",
  "50_teach.png",
];

describe("cockpit capture contract", () => {
  it("covers every published cockpit image, and shoots nothing extra", () => {
    const captured = TARGETS.map((target) => target.file).sort();
    expect(captured).toEqual([...PUBLISHED].sort());
  });

  it("shoots at the geometry the published manifest records", () => {
    expect(VIEWPORT).toEqual({ width: 1600, height: 1100 });
  });

  it("gives every target the provenance fields the manifest needs", () => {
    for (const target of TARGETS) {
      expect(target.surface, `${target.file} needs a surface`).toBeTruthy();
      expect(target.shows, `${target.file} needs a shows`).toBeTruthy();
      expect(target.session, `${target.file} needs a session`).toBeTruthy();
    }
  });
});
