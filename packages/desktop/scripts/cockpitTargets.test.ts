// The capture contract: the harness must be able to re-shoot every Desktop
// screenshot another repository publishes, not just the ones someone
// remembered.
//
// Eight images sat on the marketing site through a palette change because the
// adapter that made them was never committed. Before that, a four-state set was
// re-shot one state at a time because only one state was listed as a target,
// and the other three kept the old palette while the set looked finished. And
// docs.openadapt.ai shipped a program-workbench capture that no script in any
// repository could regenerate, because it was shot by hand.
//
// So the contract covers two consumers, and every target must name one:
//
//   PUBLISHED_WEB   the names openadapt-web pins in
//                   public/desktop-preview/MANIFEST.json and
//                   tests/desktopPreview.test.js.
//   PUBLISHED_DOCS  the names openadapt-ops pins in
//                   docs/assets/visual-palette.json and
//                   docs/assets/screenshots/PROVENANCE.txt.
//
// Publishing a Desktop capture in either repository without adding it here
// fails this test, and the fix is to add the capture target rather than to edit
// these lists.

import { describe, expect, it } from "vitest";

import {
  CONSUMERS,
  DOCS_PREVIEW_VIEWPORT,
  TARGETS,
  VIEWPORT,
  targetsFor,
  viewportFor,
} from "./cockpit-targets.mjs";

const PUBLISHED_WEB = [
  "01_login.png",
  "05_onboarding.png",
  "10_dashboard_workflows.png",
  "20_settings.png",
  "30_record.png",
  "40_watchrun_halted.png",
  "45_watchrun_verified.png",
  "50_teach.png",
  "60_program_workbench.png",
];

const PUBLISHED_DOCS = ["program-workbench-desktop.png"];

const names = (consumer: string) =>
  targetsFor(consumer)
    .map((target) => target.file)
    .sort();

describe("cockpit capture contract", () => {
  it("covers every cockpit image openadapt-web publishes, and shoots nothing extra", () => {
    expect(names(CONSUMERS.WEB)).toEqual([...PUBLISHED_WEB].sort());
  });

  it("covers every Desktop image openadapt-ops publishes, and shoots nothing extra", () => {
    expect(names(CONSUMERS.OPS)).toEqual([...PUBLISHED_DOCS].sort());
  });

  it("gives every target a consumer, so none escapes both published lists", () => {
    const known = new Set(Object.values(CONSUMERS));
    for (const target of TARGETS) {
      expect(known.has(target.consumer), `${target.file} needs a known consumer`).toBe(true);
    }
    expect(TARGETS).toHaveLength(PUBLISHED_WEB.length + PUBLISHED_DOCS.length);
  });

  it("shoots every cockpit image at the geometry the published manifest records", () => {
    expect(VIEWPORT).toEqual({ width: 1600, height: 1100 });
    for (const target of targetsFor(CONSUMERS.WEB)) {
      expect(viewportFor(target), `${target.file} must use the cockpit geometry`).toEqual(VIEWPORT);
    }
  });

  it("shoots the docs preview at the geometry visual-palette.json records", () => {
    expect(DOCS_PREVIEW_VIEWPORT).toEqual({ width: 1440, height: 1000 });
    for (const target of targetsFor(CONSUMERS.OPS)) {
      expect(viewportFor(target), `${target.file} must use the docs geometry`)
        .toEqual(DOCS_PREVIEW_VIEWPORT);
    }
  });

  it("gives every target the provenance fields the manifest needs", () => {
    for (const target of TARGETS) {
      expect(target.surface, `${target.file} needs a surface`).toBeTruthy();
      expect(target.shows, `${target.file} needs a shows`).toBeTruthy();
    }
  });

  it("gives every cockpit target a fixture session", () => {
    // A cockpit capture shows the application answering the engine IPC surface
    // from the committed fixture table. Without a session it would be shooting
    // some other thing under a cockpit name.
    for (const target of targetsFor(CONSUMERS.WEB)) {
      expect(target.session, `${target.file} needs a session`).toBeTruthy();
    }
  });

  it("reaches a sessionless target by an explicit route, not by chance", () => {
    // The alternative to a fixture session is a surface that renders from
    // static data inside the app. Such a target must say which route it is,
    // because the harness loads it with no fixture host at all.
    for (const target of TARGETS) {
      if (target.session) continue;
      expect(target.query, `${target.file} has no session and so needs a query`).toBeTruthy();
    }
  });
});
