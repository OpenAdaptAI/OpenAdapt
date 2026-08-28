// The complete published set of Desktop screenshots.
//
// This list is the capture contract. Every Desktop screenshot another
// repository publishes must appear here, and scripts/cockpitTargets.test.ts
// fails when one stops being covered.
//
// The reason it is a contract and not a comment: the previous palette pass
// re-shot only the states that were listed as capture targets and left the rest
// on the retired palette, which looks finished and is not.
//
// Two repositories consume these captures, so each target names its own:
//
//   CONSUMERS.WEB  openadapt-web publishes these under
//                  public/desktop-preview/cockpit/ and pins their names, hashes
//                  and geometry in public/desktop-preview/MANIFEST.json and
//                  tests/desktopPreview.test.js.
//   CONSUMERS.OPS  openadapt-ops publishes these under
//                  docs/assets/screenshots/ on docs.openadapt.ai and pins their
//                  hashes and measured palette in docs/assets/visual-palette.json.
//                  program-workbench-desktop.png had no generator at all until
//                  it became a target here: it was shot by hand from a
//                  throwaway script, which is the failure this file prevents.

import { WORKFLOWS, IDS } from './cockpit-fixtures.mjs';

export const CONSUMERS = {
  WEB: 'openadapt-web',
  OPS: 'openadapt-ops',
};

// The published cockpit geometry, with no device-scale multiplier. It is what
// openadapt-web's MANIFEST.json records for every cockpit capture, and it is
// the default for a target that does not name its own viewport.
export const VIEWPORT = { width: 1600, height: 1100 };

// The geometry openadapt-ops recorded for the program-workbench preview.
export const DOCS_PREVIEW_VIEWPORT = { width: 1440, height: 1000 };

const GRANTED = { screen_recording: true, accessibility: true, input_monitoring: true };
const DENIED = { screen_recording: false, accessibility: false, input_monitoring: false };

/**
 * The complete published set.
 *
 * `file` is the published file name in the consumer repository.
 * `consumer` is which repository publishes it; see CONSUMERS above.
 * `viewport` overrides VIEWPORT for this one target.
 * `query` is appended to the base URL, for a surface reached by a route flag
 *   rather than by navigation.
 * `session` seeds the Tauri fixture table. A surface that renders from static
 *   data inside the app has no session, and the harness then loads the page
 *   with no fixture host at all.
 *
 * `session.localSession` seeds the local-first flag the sign-in screen writes,
 * and `session.workflows` decides whether the app shows first-run onboarding or
 * the library, because App.tsx gates onboarding on an empty library.
 */
export const TARGETS = [
  {
    file: '01_login.png',
    consumer: CONSUMERS.WEB,
    surface: 'Sign in',
    shows: 'Synthetic connection screen with a local-first entry and optional Cloud sign-in controls.',
    session: { localSession: false, authenticated: false, workflows: [], permissions: DENIED },
  },
  {
    file: '05_onboarding.png',
    consumer: CONSUMERS.WEB,
    surface: 'First-run onboarding',
    shows: 'Synthetic first-run workflow recording setup with permission and local video-engine status.',
    session: { localSession: true, authenticated: false, workflows: [], permissions: GRANTED },
  },
  {
    file: '10_dashboard_workflows.png',
    consumer: CONSUMERS.WEB,
    surface: 'Home / Workflows library',
    shows: 'Synthetic local workflow library with one halted workflow and one verified workflow.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
  },
  {
    file: '20_settings.png',
    consumer: CONSUMERS.WEB,
    surface: 'Settings / policy',
    shows: 'Synthetic settings view for the execution lane, privacy mode, grounding controls, and overlay.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    nav: 'Settings',
  },
  {
    file: '30_record.png',
    consumer: CONSUMERS.WEB,
    surface: 'Record & review',
    shows: 'Synthetic idle record-and-review view for a new demonstration.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    nav: 'Record',
  },
  {
    file: '40_watchrun_halted.png',
    consumer: CONSUMERS.WEB,
    surface: 'Run detail — halted with effect-verification evidence',
    shows: 'Synthetic six-step workflow halted because identity and postcondition evidence were insufficient.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '45_watchrun_verified.png',
    consumer: CONSUMERS.WEB,
    surface: 'Run detail — fully verified with metrics',
    shows: 'Synthetic six-step workflow with a verified outcome and its evidence contract.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.VERIFIED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '50_teach.png',
    consumer: CONSUMERS.WEB,
    surface: 'Teach the fix',
    shows: 'Synthetic local correction view for the safely halted workflow.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Teach fix' },
  },
  {
    // The program workbench inside the real cockpit, reached the way an
    // operator reaches it: Qualify on a library row, then the "Inspect
    // workflow" step of the qualification journey. It is not the dev-only
    // preview route below, so the published gallery shows the application.
    file: '60_program_workbench.png',
    consumer: CONSUMERS.WEB,
    surface: 'Qualification — program workbench',
    shows: 'Synthetic compiled program map for the halted workflow, with its identity gates, effect checks, and stop rules.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Qualify' },
    journey: 'Inspect workflow',
  },
  {
    // docs.openadapt.ai/concepts/program-visualizer publishes this one. It
    // renders ProgramWorkbenchPreview, which carries its own static graph, so
    // there is no fixture session and the route flag is the whole navigation.
    // The route is development-only, so the harness must point at `npm run dev`.
    file: 'program-workbench-desktop.png',
    consumer: CONSUMERS.OPS,
    surface: 'Program workbench preview (development route)',
    shows: 'The production qualification workbench rendered from a public synthetic graph, with no bound live trace.',
    viewport: DOCS_PREVIEW_VIEWPORT,
    query: '?surface=program-workbench-preview',
    session: null,
  },
];

/** The viewport a target is shot at. */
export function viewportFor(target) {
  return target.viewport || VIEWPORT;
}

/** Every target one repository publishes. */
export function targetsFor(consumer) {
  return TARGETS.filter((target) => target.consumer === consumer);
}
