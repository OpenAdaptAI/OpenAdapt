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

import { WORKFLOWS, IDS, NEW_TASK } from './cockpit-fixtures.mjs';

export const CONSUMERS = {
  WEB: 'openadapt-web',
  OPS: 'openadapt-ops',
  // This repository's own README, under docs/assets/operator/.
  README: 'openadapt-desktop',
};

// The README shots use the README's own geometry: a typical laptop window.
export const README_VIEWPORT = { width: 1280, height: 860 };
// Run it keeps its action bar in view; a taller window shows the whole list.
export const README_TALL_VIEWPORT = { width: 1280, height: 1060 };

// Every expert surface is behind the gear menu now. These are the clicks an
// operator makes to reach them.
const GEAR = { role: 'button', name: 'Settings and advanced tools' };
const ADVANCED = [GEAR, { role: 'menuitem', name: 'Advanced', exact: false }];
const ALL_VERSIONS = [...ADVANCED, { name: 'Open all versions' }];

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
    shows: 'Synthetic Cloud sign-in screen, reached from the welcome screen, with the option to continue without an account.',
    session: { localSession: false, authenticated: false, workflows: [], permissions: DENIED },
    steps: [{ name: 'Connect to OpenAdapt Cloud' }],
  },
  {
    file: '05_onboarding.png',
    consumer: CONSUMERS.WEB,
    surface: 'First-run welcome',
    shows: 'Synthetic welcome screen: what OpenAdapt does, the three steps, and a sample run that needs no permission.',
    session: { localSession: true, authenticated: false, workflows: [], permissions: GRANTED },
  },
  {
    file: '10_dashboard_workflows.png',
    consumer: CONSUMERS.WEB,
    surface: 'Home / Tasks',
    shows: 'Synthetic task list: one task ready to run with an item waiting for a person, and one task still being checked.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
  },
  {
    file: '20_settings.png',
    consumer: CONSUMERS.WEB,
    surface: 'Settings / policy',
    shows: 'Synthetic settings view for the execution lane, privacy mode, grounding controls, and overlay.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    steps: [GEAR, { role: 'menuitem', name: 'Settings', exact: false }],
  },
  {
    file: '30_record.png',
    consumer: CONSUMERS.WEB,
    surface: 'Record & review',
    shows: 'Synthetic idle record-and-review view for a new demonstration (Advanced, Record with all options).',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    steps: [...ADVANCED, { name: 'Open recording' }],
  },
  {
    file: '40_watchrun_halted.png',
    consumer: CONSUMERS.WEB,
    surface: 'Run detail — halted with effect-verification evidence',
    shows: 'Synthetic six-step workflow halted because identity and postcondition evidence were insufficient.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    steps: ALL_VERSIONS,
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '45_watchrun_verified.png',
    consumer: CONSUMERS.WEB,
    surface: 'Run detail — fully verified with metrics',
    shows: 'Synthetic six-step workflow with a verified outcome and its evidence contract.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    steps: ALL_VERSIONS,
    openRow: { workflow: IDS.VERIFIED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '50_teach.png',
    consumer: CONSUMERS.WEB,
    surface: 'Teach the fix',
    shows: 'Synthetic local correction view for the safely halted workflow.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    steps: ALL_VERSIONS,
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
    steps: ALL_VERSIONS,
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

  // This repository's README shows the operator screens. Every value is the
  // synthetic fixture; the session's `operator` flag adds the Needs-you items
  // and results that a team would have after running a list.
  {
    file: 'tasks.png',
    consumer: CONSUMERS.README,
    surface: 'Tasks',
    shows: 'Synthetic task list: each task with one status and one next step.',
    viewport: README_VIEWPORT,
    session: {
      localSession: true, authenticated: false, permissions: GRANTED, operator: true,
      workflows: [...WORKFLOWS, NEW_TASK],
    },
  },
  {
    file: 'needs-you.png',
    consumer: CONSUMERS.README,
    surface: 'Needs you',
    shows: 'Synthetic stop on the wrong record: the question, what was prevented, the screen, and the allowed choices.',
    viewport: { width: 1280, height: 1200 },
    session: { localSession: true, authenticated: false, permissions: GRANTED, operator: true, workflows: WORKFLOWS },
    steps: [{ name: 'Needs you', exact: false }],
  },
  {
    file: 'results.png',
    consumer: CONSUMERS.README,
    surface: 'Results',
    shows: 'Synthetic results: items done and checked, items that needed a person, a prevented wrong-record save, and a labeled time estimate.',
    viewport: README_VIEWPORT,
    session: { localSession: true, authenticated: false, permissions: GRANTED, operator: true, workflows: WORKFLOWS },
    steps: [{ name: 'Results' }],
  },
  {
    file: 'check-it.png',
    consumer: CONSUMERS.README,
    surface: 'Check it',
    shows: 'Synthetic short readiness test: what it changes, the right-record check, the record check, practice run, drills, approval.',
    viewport: README_VIEWPORT,
    session: {
      localSession: true, authenticated: false, permissions: GRANTED, operator: false,
      workflows: [{ ...WORKFLOWS[0], readiness: 'checking', certified: false, encrypted: false, open_items: 0, open_halts: 0 }],
    },
    steps: [{ name: 'Continue checking', within: 'Claims review handoff' }],
  },
  {
    file: 'run-it.png',
    consumer: CONSUMERS.README,
    surface: 'Run it',
    shows: 'Synthetic list of member IDs and notes pasted into Run it, checked against the inputs the task needs.',
    viewport: README_TALL_VIEWPORT,
    session: {
      localSession: true, authenticated: false, permissions: GRANTED, operator: false,
      workflows: [{ ...WORKFLOWS[0], open_items: 0, open_halts: 0 }],
    },
    steps: [
      { name: 'Run it', within: 'Claims review handoff' },
      {
        fill: 'Paste rows from a spreadsheet or a list',
        text: 'member_id, status_note\nM-5601, Reviewed\nM-5602, Reviewed\nM-5603, Sent to billing\nM-5604,',
      },
    ],
  },
  {
    file: 'sample.png',
    consumer: CONSUMERS.README,
    surface: 'Sample run',
    shows: 'Synthetic sample results: one run done and checked, and one where the app showed saved but the record check disagreed.',
    viewport: README_VIEWPORT,
    session: { localSession: false, authenticated: false, permissions: DENIED, workflows: [] },
    steps: [{ name: 'See it work on a sample app', exact: false }, { name: 'Run the sample', wait: 900 }],
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
