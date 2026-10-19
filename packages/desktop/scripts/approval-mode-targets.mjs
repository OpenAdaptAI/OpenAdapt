// Design-review captures for "Only ask a person about problems".
//
// These shots are not published anywhere, so they are not part of the
// capture contract in scripts/cockpit-targets.mjs. Shoot them with:
//
//   npm run dev                                                  # one shell
//   node scripts/capture-cockpit-previews.mjs --set approval-mode --out <dir>
//
// Every value is invented: the clinic staff (Sam Ortiz, Lee Park), member IDs,
// counts, digests and screens. No engine, account, Cloud, recording or run is
// involved.
//
// What the engine can produce today and what it can't:
//
// * "Off" with the first unmet requirement, and "Not used on this computer"
//   for a switch that is on in Cloud, are states the engine returns now.
// * "On" (with "Only problems come to you"), the Needs-you and Results views
//   with items saved without a person, and the Run it list that stops at a
//   problem need Flow's governed run to take the approval gate, a Cloud status
//   client, spot checks and an engine-held list. None of those exists yet, so
//   these shots show the screens as they will read once they do.

import { TASK_INPUTS, WORKFLOWS, ipcTable } from './cockpit-fixtures.mjs';

// A generic, synthetic referral screen. No vendor's layout, name, or trade
// dress; every value is invented.
function referralScreen({ member, referredTo, banner = '' }) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800">
<rect width="1280" height="800" fill="#f4f6f8"/>
<rect width="1280" height="56" fill="#2f4858"/>
<text x="28" y="35" font-family="Helvetica, Arial, sans-serif" font-size="20" fill="#ffffff" font-weight="700">Sample clinic records</text>
<text x="290" y="35" font-family="Helvetica, Arial, sans-serif" font-size="14" fill="#d7e1e8">Synthetic data for screenshots</text>
<rect x="28" y="84" width="880" height="52" fill="#e6edf3" stroke="#9fb3c4"/>
<text x="46" y="117" font-family="Helvetica, Arial, sans-serif" font-size="18" fill="#1d2a33">${member}</text>
${banner ? `<rect x="28" y="152" width="880" height="44" fill="#dff3e6" stroke="#5aa874"/><text x="46" y="180" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#1e5b33">${banner}</text>` : ''}
<text x="28" y="246" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#3b4a55">Referred to</text>
<rect x="28" y="258" width="560" height="44" fill="#ffffff" stroke="#9fb3c4"/>
<text x="42" y="286" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#1d2a33">${referredTo}</text>
<rect x="28" y="330" width="190" height="42" rx="4" fill="#2f6fa3"/>
<text x="62" y="357" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#ffffff">Save referral</text>
</svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}

const screenAt = (options, focus) => ({
  ok: true,
  data_url: referralScreen(options),
  width: 1280,
  height: 800,
  focus,
  step: "click 'Save referral'",
});

const WRONG_RECORD_SCREEN = screenAt(
  { member: 'Member M-4803   Riley Example   DOB 1975-06-14', referredTo: 'Cardiology' },
  { x: 260, y: 110 },
);
const WORKAROUND_SCREEN = screenAt(
  {
    member: 'Member M-4867   Jordan Example   DOB 1990-02-27',
    referredTo: 'Neurology',
    banner: 'Referral saved',
  },
  { x: 123, y: 351 },
);

export const APPROVAL_VIEWPORT = { width: 1280, height: 900 };
const TALL = { width: 1280, height: 1240 };

const REFERRAL = 'wf-referral-intake-v2';

const REFERRAL_TASK = {
  id: REFERRAL,
  name: 'Referral intake',
  steps: 7,
  updated_at: '2026-10-09T15:02:00Z',
  last_run_state: 'verified',
  open_halts: 0,
  synced: true,
  workflow_id: 'cloud-referral-intake',
  last_result: 'done_checked',
  open_items: 0,
  blocked: false,
  readiness: 'ready',
  certified: true,
  encrypted: true,
  version: 2,
  capture_id: 'cap-referral-intake',
  task: 'Enter a referral from the clinic intake list into the EMR',
  demo_seconds: 96,
};

const CHECKING_TASK = { ...WORKFLOWS[1], open_items: 0, open_halts: 0 };

const NOT_BUILT = ['engine_cannot_check_before_save', 'cloud_status', 'spot_checks', 'list_queue'];

const PEOPLE = {
  turned_on: { name: 'Sam Ortiz', role: 'admin', at: '2026-10-04T15:02:00Z' },
  confirmed: { name: 'Lee Park', at: '2026-10-04T15:20:00Z' },
};

const EVIDENCE = {
  switch_id: 'sw-7f3a21',
  record_sha256: 'sha256:5f0c2a9d3b1e7c6a4f8d2e0b9a7c5e3d1f2a4b6c8e0d2f4a6b8c0e2d4f6a8b0c',
  bundle_digest: '9c41d7e2a05b38f6c1d9e4a7b2f05c83d6e1a9b4c7f20d5e8a3b6c9f1e4d7a0b',
  runner_id: 'runner-intake-01',
  verifiers: 1,
  profile: 'regulated',
  log_intact: true,
  log_events: 371,
  items_finished: 182,
  items_without_person: 162,
};

const ALL = ['E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'E7', 'E8'];
const REQUIREMENTS_MET = {
  available: true,
  source: 'this_computer',
  items: ALL.map((code) => ({ code, met: true, reason: 'ok', checked_by: code === 'E1' || code === 'E5' ? 'software' : 'software_and_person' })),
};
// What this computer can confirm today: the version is checked and sealed,
// and nothing Cloud holds is known here.
const REQUIREMENTS_TODAY = {
  available: true,
  source: 'this_computer',
  items: [
    { code: 'E1', met: false, reason: 'no_active_admission', checked_by: 'software' },
    { code: 'E2', met: false, reason: 'not_qualified_with_regulated_pack', checked_by: 'software' },
    { code: 'E3', met: false, reason: 'sign_attest_order_not_recorded', checked_by: 'person' },
    { code: 'E4', met: false, reason: 'production_write_authorization_missing', checked_by: 'person' },
    { code: 'E5', met: false, reason: 'too_few_approved_items', checked_by: 'software' },
    { code: 'E6', met: false, reason: 'runner_setup_not_current', checked_by: 'software_and_person' },
    { code: 'E7', met: false, reason: 'channel_not_covered', checked_by: 'software' },
    { code: 'E8', met: false, reason: 'no_stop_answered_in_cloud', checked_by: 'software' },
  ],
};

const CLOUD_URL = 'https://app.openadapt.ai/dashboard/workflows/cloud-referral-intake';

// `visible` is what makes the card show the switch. The engine sets it only
// for a task with a switch on this computer; the "none" shots show a runner
// IT has set up, which the design requires and which isn't built yet.
const MODES = {
  none: {
    ok: true, workflow_id: REFERRAL, state: 'none', recorded_state: 'none', visible: true, blockers: NOT_BUILT,
    can_turn_off: false, cloud_url: CLOUD_URL, requirements: REQUIREMENTS_TODAY,
    turned_on: null, confirmed: null, evidence: null, off_reason: null,
  },
  not_used_here: {
    ok: true, workflow_id: REFERRAL, state: 'not_used_here', recorded_state: 'on', visible: true, blockers: NOT_BUILT,
    can_turn_off: true, cloud_url: CLOUD_URL, requirements: REQUIREMENTS_TODAY,
    ...PEOPLE, evidence: { ...EVIDENCE, log_events: 2, items_finished: 0, items_without_person: 0 },
    off_reason: null,
  },
  on: {
    ok: true, workflow_id: REFERRAL, state: 'on', recorded_state: 'on', visible: true, blockers: [],
    can_turn_off: true, cloud_url: CLOUD_URL, requirements: REQUIREMENTS_MET,
    ...PEOPLE, evidence: EVIDENCE, off_reason: null,
  },
  off_screen: {
    ok: true, workflow_id: REFERRAL, state: 'off', recorded_state: 'off', visible: true, blockers: [],
    can_turn_off: false, cloud_url: CLOUD_URL, requirements: REQUIREMENTS_MET,
    ...PEOPLE, evidence: { ...EVIDENCE, log_events: 389 },
    off_reason: 'screen_changed', off_at: '2026-10-09T14:41:00Z', off_by_this_computer: false,
    off_step: "click 'Save referral'",
  },
};

const SWITCH_SUMMARY = (mode) => ({
  workflow_id: REFERRAL,
  task_name: 'Referral intake',
  state: mode.state,
  off_reason: mode.off_reason ?? null,
  off_at: mode.off_at ?? null,
  off_by_this_computer: Boolean(mode.off_by_this_computer),
  turned_on: mode.turned_on ?? null,
  off_step: mode.off_step ?? '',
});

const check = (code, passed = true, detail = 'ok') => ({ code, passed, detail });

const APPROVAL = {
  wrong_record: {
    validated: true, mode: 'problems_only', decision: 'stop_before_saving', reason: 'right_record',
    needs_person: true, counts_toward_problems: true, switch_off_reason: null,
    failed_check: check('right_record', false, 'identity_mismatch'),
    post_checks: { verified: false, whole_run_clean: true }, saves_without_person: 0,
  },
  recent_record: {
    validated: true, mode: 'problems_only', decision: 'approval_required', reason: 'record_key_not_recent',
    needs_person: true, counts_toward_problems: false, switch_off_reason: null,
    failed_check: check('record_key_not_recent', false, 'seen_recently'),
    post_checks: { verified: false, whole_run_clean: true }, saves_without_person: 0,
  },
  workaround: {
    validated: true, mode: 'problems_only', decision: 'problem', reason: 'screen_changed',
    needs_person: true, counts_toward_problems: true, switch_off_reason: 'screen_changed',
    failed_check: check('whole_run_clean', false, 'repair_applied'),
    post_checks: { verified: true, whole_run_clean: false }, saves_without_person: 1,
  },
  no_person: {
    validated: true, mode: 'problems_only', decision: 'no_person', reason: null,
    needs_person: false, counts_toward_problems: false, switch_off_reason: null,
    failed_check: null, post_checks: { verified: true, whole_run_clean: true }, saves_without_person: 1,
  },
};

const ITEM_BASE = {
  source: 'item', workflow_id: REFERRAL, task_name: 'Referral intake', batch_id: 'batch-oct-09',
  status: 'open', run_id: 'run-2026-10-09-0015', duration_s: 41.2,
};

const ITEMS = {
  wrong_record: {
    ...ITEM_BASE, item_id: 'item-0015', position: 14, label: 'M-4830', result: 'stopped_before_saving',
    nothing_written: true, execution_outcome: 'HALTED', transaction_outcome: 'HALTED_BEFORE_EFFECT',
    halt_kind: 'wrong_record', halt_step: "click 'Save referral'",
    halt_reason: 'The record banner showed a different member before the save.',
    expected: 'M-4830', observed: 'M-4803', created_at: '2026-10-09T14:12:00Z',
    actions: ['retry', 'skip', 'decline', 'teach'], approval: APPROVAL.wrong_record,
  },
  recent_record: {
    ...ITEM_BASE, item_id: 'item-0022', position: 21, label: 'M-4851', result: 'stopped_before_saving',
    nothing_written: true, execution_outcome: 'HALTED', transaction_outcome: 'HALTED_BEFORE_EFFECT',
    halt_kind: 'other', halt_step: "click 'Save referral'",
    halt_reason: 'Approval mode: this item is waiting for a person to approve it before saving.',
    expected: '', observed: '', created_at: '2026-10-09T14:20:00Z',
    actions: ['skip', 'decline', 'done_by_person'], approval: APPROVAL.recent_record,
  },
  workaround: {
    ...ITEM_BASE, item_id: 'item-0031', position: 30, label: 'M-4867', result: 'done_checked',
    nothing_written: false, execution_outcome: 'VERIFIED', transaction_outcome: 'VERIFIED',
    halt_kind: null, halt_step: '', halt_reason: '', expected: '', observed: '',
    created_at: '2026-10-09T14:41:00Z', actions: ['confirmed_saved', 'done_by_person'],
    approval: APPROVAL.workaround,
  },
};

const COUNTS = {
  done_checked: 182, saved_without_person: 162, needed_person: 9, waiting: 2,
  wrong_record_prevented: 3, done_by_person: 1, skipped: 2, declined: 0, items: 191,
  baseline_seconds: 96, estimated_seconds_saved: 17472,
};

const recent = (n, label, minutes, extra = {}) => ({
  item_id: `r${n}`, batch_id: 'batch-oct-09', workflow_id: REFERRAL, position: n, label,
  status: 'done', result: 'done_checked', nothing_written: false, execution_outcome: 'VERIFIED',
  transaction_outcome: 'VERIFIED', finished_at: `2026-10-09T14:${String(minutes).padStart(2, '0')}:00Z`,
  actions: [], task_name: 'Referral intake', saved_without_person: true, approval: APPROVAL.no_person,
  ...extra,
});

const RESULTS = (mode) => ({
  ok: true,
  total: COUNTS,
  tasks: { [REFERRAL]: COUNTS },
  names: { [REFERRAL]: 'Referral intake' },
  recent: [
    recent(33, 'M-4869', 47),
    recent(32, 'M-4868', 45),
    recent(31, 'M-4867', 41, { status: 'open', saved_without_person: false, approval: APPROVAL.workaround }),
    recent(30, 'M-4866', 39),
    recent(22, 'M-4851', 20, {
      status: 'open', result: 'stopped_before_saving', nothing_written: true,
      execution_outcome: 'HALTED', transaction_outcome: 'HALTED_BEFORE_EFFECT',
      saved_without_person: false, approval: APPROVAL.recent_record,
    }),
  ],
  switches: [SWITCH_SUMMARY(mode)],
});

const runItem = (position, label, outcome) => {
  const done = outcome === 'done';
  return {
    ok: true,
    item: {
      item_id: `item-run-${position}`, batch_id: 'batch-run', workflow_id: REFERRAL, position, label,
      status: done ? 'done' : 'open',
      result: done ? 'done_checked' : 'stopped_before_saving',
      nothing_written: !done,
      execution_outcome: done ? 'VERIFIED' : 'HALTED',
      transaction_outcome: done ? 'VERIFIED' : 'HALTED_BEFORE_EFFECT',
      actions: done ? [] : ['retry', 'skip', 'decline', 'teach'],
      saved_without_person: done,
      approval: done ? APPROVAL.no_person : APPROVAL.wrong_record,
    },
  };
};

const REFERRAL_INPUTS = {
  ok: true,
  workflow_id: REFERRAL,
  inputs: [
    { name: 'member_id', type: 'string', secret: false, required: true, example: 'M-4821', choices: [] },
    { name: 'referred_to', type: 'string', secret: false, required: true, example: 'Cardiology', choices: [] },
  ],
  target: { backend: 'web', url: 'https://emr.example.test/referrals' },
};

/** The fixture table for one shot: the cockpit table plus this feature's answers. */
function session({ mode, items = [], workflows = [REFERRAL_TASK, CHECKING_TASK], extra = {} }) {
  const modeValue = MODES[mode];
  return {
    localSession: true,
    authenticated: true,
    permissions: { screen_recording: true, accessibility: true, input_monitoring: true },
    operator: false,
    workflows,
    table: {
      get_approval_mode: modeValue,
      get_task_inputs: { ...TASK_INPUTS, [REFERRAL]: REFERRAL_INPUTS },
      list_attention_items: {
        ok: true,
        items,
        switches: mode === 'none' ? [] : [SWITCH_SUMMARY(modeValue)],
      },
      get_attention_screenshot: items.some((entry) => entry.item_id === 'item-0031')
        ? WORKAROUND_SCREEN
        : WRONG_RECORD_SCREEN,
      get_results_summary: RESULTS(modeValue),
      get_needs_attention: { count: items.length, open_halts: 0, open_items: items.length, failed_runs: 0 },
      ...extra,
    },
  };
}

export const APPROVAL_TARGETS = [
  {
    file: '01-tasks-switch-off.png',
    surface: 'Tasks: switch off, not available yet (runner setup not built)',
    shows: 'The switch on a ready task, off, with the first requirement this computer can\'t confirm. The engine computes this state today, but the card shows the switch only on a runner with a runner setup, which isn\'t built yet.',
    viewport: APPROVAL_VIEWPORT,
    session: session({ mode: 'none' }),
  },
  {
    file: '02-tasks-switch-requirements.png',
    surface: 'Tasks: what the switch needs (runner setup not built)',
    shows: 'The same switch with its "What it needs" disclosure open: the sentence, what is missing on this computer, and requirements E1 to E8 as this computer sees them.',
    viewport: TALL,
    session: session({ mode: 'none' }),
    steps: [{ text: 'What it needs' }],
  },
  {
    file: '03-tasks-switch-not-used-here.png',
    surface: 'Tasks: on in Cloud, not used on this computer',
    shows: 'A switch Cloud turned on, which this computer can\'t apply yet. The card keeps today\'s words; Turn off still works.',
    viewport: APPROVAL_VIEWPORT,
    session: session({ mode: 'not_used_here' }),
  },
  {
    file: '04-tasks-switch-on.png',
    surface: 'Tasks: switch on (future state)',
    shows: 'The switch on: who turned it on and who confirmed, and the card says only problems come to the person. Needs the pieces listed in the provenance before the engine can return it.',
    viewport: APPROVAL_VIEWPORT,
    session: session({ mode: 'on' }),
  },
  {
    file: '05-tasks-switch-evidence.png',
    surface: 'Tasks: the switch\'s evidence (future state)',
    shows: 'The evidence behind an on switch: who turned it on and confirmed, the bound version, runner and record check, the local switch log, and E1 to E8.',
    viewport: TALL,
    session: session({ mode: 'on' }),
    steps: [{ text: 'Evidence' }],
  },
  {
    file: '06-tasks-turn-off.png',
    surface: 'Tasks: turning the switch off',
    shows: 'Turn off asks once, says it applies before the next save even offline, and says what turning it on again costs.',
    viewport: APPROVAL_VIEWPORT,
    session: session({ mode: 'on' }),
    steps: [{ name: 'Turn off' }],
  },
  {
    file: '07-tasks-switched-off.png',
    surface: 'Tasks: the switch turned itself off',
    shows: 'After a run had to work around a changed screen at "Save referral", the switch is off and says who can turn it on again.',
    viewport: APPROVAL_VIEWPORT,
    session: session({ mode: 'off_screen' }),
  },
  {
    file: '08-needs-you-only-problems.png',
    surface: 'Needs you with the switch on (future state)',
    shows: 'Only problems come here: a stop on the wrong record, and an item sent to a person because its record was entered recently. Each says why it came to a person.',
    viewport: TALL,
    session: session({ mode: 'on', items: [ITEMS.wrong_record, ITEMS.recent_record] }),
    steps: [{ name: 'Needs you', exact: false }],
  },
  {
    file: '08b-needs-you-waiting-for-approval.png',
    surface: 'Needs you: an item held for approval (future state)',
    shows: 'An item the switch sent to a person before saving, because its record was entered recently. It says why, and offers no re-run because approving and saving from Desktop is not built.',
    viewport: TALL,
    session: session({ mode: 'on', items: [ITEMS.wrong_record, ITEMS.recent_record] }),
    steps: [{ name: 'Needs you', exact: false }, { name: 'M-4851', exact: false }],
  },
  {
    file: '09-needs-you-switched-off.png',
    surface: 'Needs you after an automatic switch-off (future state)',
    shows: 'One banner explains the switch-off. The item keeps "Done and checked" because the record check confirmed it, still goes to a person, and offers no re-run.',
    viewport: TALL,
    session: session({ mode: 'off_screen', items: [ITEMS.workaround] }),
    steps: [{ name: 'Needs you', exact: false }],
  },
  {
    file: '10-results-done-and-checked.png',
    surface: 'Results with the switch on (future state)',
    shows: 'Done and checked counts every item the record check confirmed, and how many of them were saved without a person. No item is labeled approved.',
    viewport: TALL,
    session: session({ mode: 'on' }),
    steps: [{ name: 'Results' }],
  },
  {
    file: '11-run-it-only-problems.png',
    surface: 'Run it with the switch on (future state)',
    shows: 'Run it says only problems come to the person, and the list stops at the first item that needs one. The items after it run as a new list.',
    viewport: TALL,
    session: session({
      mode: 'on',
      workflows: [REFERRAL_TASK],
      extra: {
        run_item: {
          __sequence: [
            runItem(0, 'M-4821', 'done'),
            runItem(1, 'M-4822', 'done'),
            runItem(2, 'M-4830', 'stop'),
          ],
        },
      },
    }),
    steps: [
      { name: 'Run it', within: 'Referral intake' },
      {
        fill: 'Paste rows from a spreadsheet or a list',
        text: 'member_id, referred_to\nM-4821, Cardiology\nM-4822, Dermatology\nM-4830, Cardiology\nM-4833, Neurology',
      },
      { name: 'Run 4 items', wait: 1200 },
    ],
  },
];

export { ipcTable };
