// The complete published set of Desktop cockpit screenshots.
//
// This list is the capture contract. openadapt-web pins the same eight names in
// public/desktop-preview/MANIFEST.json and in tests/desktopPreview.test.js, and
// scripts/cockpitTargets.test.ts here fails if this list stops covering them.
//
// The reason it is a contract and not a comment: the previous palette pass
// re-shot only the states that were listed as capture targets and left the rest
// on the retired palette, which looks finished and is not.

import { WORKFLOWS, IDS } from './cockpit-fixtures.mjs';

// Published at 1600x1100 with no device-scale multiplier, which is the geometry
// openadapt-web's MANIFEST.json records for all eight.
export const VIEWPORT = { width: 1600, height: 1100 };

const GRANTED = { screen_recording: true, accessibility: true, input_monitoring: true };
const DENIED = { screen_recording: false, accessibility: false, input_monitoring: false };

/**
 * The complete published set. `file` is the name in
 * openadapt-web/public/desktop-preview/cockpit/.
 *
 * `session.localSession` seeds the local-first flag the sign-in screen writes,
 * and `session.workflows` decides whether the app shows first-run onboarding or
 * the library, because App.tsx gates onboarding on an empty library.
 */
export const TARGETS = [
  {
    file: '01_login.png',
    surface: 'Sign in',
    shows: 'Synthetic connection screen with a local-first entry and optional Cloud sign-in controls.',
    session: { localSession: false, authenticated: false, workflows: [], permissions: DENIED },
  },
  {
    file: '05_onboarding.png',
    surface: 'First-run onboarding',
    shows: 'Synthetic first-run workflow recording setup with permission and local video-engine status.',
    session: { localSession: true, authenticated: false, workflows: [], permissions: GRANTED },
  },
  {
    file: '10_dashboard_workflows.png',
    surface: 'Home / Workflows library',
    shows: 'Synthetic local workflow library with one halted workflow and one verified workflow.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
  },
  {
    file: '20_settings.png',
    surface: 'Settings / policy',
    shows: 'Synthetic settings view for the execution lane, privacy mode, grounding controls, and overlay.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    nav: 'Settings',
  },
  {
    file: '30_record.png',
    surface: 'Record & review',
    shows: 'Synthetic idle record-and-review view for a new demonstration.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    nav: 'Record',
  },
  {
    file: '40_watchrun_halted.png',
    surface: 'Run detail — halted with effect-verification evidence',
    shows: 'Synthetic six-step workflow halted because identity and postcondition evidence were insufficient.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '45_watchrun_verified.png',
    surface: 'Run detail — fully verified with metrics',
    shows: 'Synthetic six-step workflow with a verified outcome and its evidence contract.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.VERIFIED_WORKFLOW, action: 'Watch run' },
  },
  {
    file: '50_teach.png',
    surface: 'Teach the fix',
    shows: 'Synthetic local correction view for the safely halted workflow.',
    session: { localSession: true, authenticated: true, workflows: WORKFLOWS, permissions: GRANTED },
    openRow: { workflow: IDS.HALTED_WORKFLOW, action: 'Teach fix' },
  },
];
