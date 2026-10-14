# Operator screens: Tasks, Needs you, Results

Desktop opens on three tabs. Settings and every expert tool sit behind the
gear menu. This page says what each screen does, which engine commands it
calls, the safety rules it keeps, and what the engine can't do for it yet.

## Screens

| Screen | What a person does there | Engine commands |
| --- | --- | --- |
| Welcome | Chooses the sample or shows a first task. Cloud sign-in is a link. | none until a choice |
| Sample | Watches Flow's built-in tutorial run twice on a synthetic clinic app: once normally, once with a backend that drops the save after the screen says "saved". No account and no operating-system permission. | `run_sample` |
| Tasks | Sees one card per recorded task with one status (Not checked, Checking, Ready, Needs you, Needs a fix, Check the record) and one next step: Check it, Run it, Show it, or Open Needs you. Versions of a task are grouped by recording. | `get_workflows`, `get_results_summary` |
| Show it | Records a task in its app. The app type is checked just in time, so browser tasks need no native permission. | `start_recording`, `stop_recording`, `compile_recording` |
| Check it | Confirms what the task changes, the right-record check, and the record check; runs one practice item; runs the safety drills; approves and locks the version. | `get_qualification`, `propose_qualification_from_demo`, `initialize_qualification`, `set_qualification_risk`, `arm_qualification_identity`, `bind_qualification_effect`, `add_qualification_case`, `run_qualification_case`, `certify_qualification`, `seal_qualification_workflow` |
| Run it | Pastes a list, opens a CSV, or types one item, then watches per-item progress. | `get_task_inputs`, `run_item` |
| Needs you | Answers each stopped item: the question, what was prevented, the stopped screen with the point ringed, and only the choices the engine allows. | `list_attention_items`, `get_attention_screenshot`, `resolve_attention_item` |
| Results | Sees items done and checked, items that needed a person, wrong-record saves prevented, and a time-saved estimate whose per-item baseline is shown and editable. | `get_results_summary`, `get_workflows` |
| Advanced (gear) | Exact checks, every task version with replay and governed run, recording with all options, the Cloud runner, phone pairing, and recording setup. | the existing commands |

Check it uses the same engine commands as the exact checks page, so the short
path and the full contract can't disagree. The full page stays one click away
on every task.

## Safety rules the screens keep

- Every plain result comes from Flow's transaction outcome
  (`engine/outcomes.py`, mirrored in `src/lib/outcomes.ts`; both are tested
  against `src/lib/outcome-cases.json`). Only `HALTED_BEFORE_EFFECT`,
  `REJECTED_POLICY`, `CANCELED`, `FAILED_PLATFORM`, and a refusal before Flow
  was invoked prove that nothing was written. A coarse `HALTED`, an unknown
  outcome, or a transaction outcome that disagrees with the execution outcome
  reads as "Check the record".
- A retry is offered only for an item whose run proved nothing was written.
  For a possible save, the person first confirms they looked at the record,
  then says it saved, it didn't save (run it again), or they finished it by
  hand. The engine refuses each of these three choices without that
  confirmation, not only the screen.
- While any item of a task may have saved, Desktop refuses to run more items
  of that task. Run it stops the list at that item.
- An item whose run was interrupted (Desktop or the engine stopped mid-run)
  becomes an open item with an unknown outcome. It is never run again until a
  person checks the record.
- A done item isn't repeated by accident: `run_item` refuses values that
  already finished for that task. Only the engine's `allow_repeat` flag
  overrides that; the screens don't offer it.
- Item values reach Flow only through an owner-only params file that exists
  for the length of the call. They never enter a process argument list, an
  event, a log line, a notification, or a Cloud payload.
- Results count an item as done only when its transaction outcome is
  `VERIFIED`. Items a person finished count under "Needed a person".
- A first task leaves the read-only first-run path only before anything ran.
  A first run that started, or one whose outcome is uncertain, keeps its own
  screen until a person has checked the target.
- A task is ready to run only when its current version is certified and
  sealed, because Flow's governed run refuses a plaintext bundle.

## What the engine can't do for these screens yet

Each gap below is stated precisely so the next change can close it.

1. **Lists run one Flow process per item.** `run_item` calls
   `openadapt-flow run --params-file` once per item, in order, from the
   Desktop engine. Flow's program bundles (`for-each`, `induce`) aren't wired
   into Desktop, so there's no single governed batch run, no parallel items,
   and each item pays Flow's start-up time. A list that stops early leaves
   the remaining rows unrun; they are not queued anywhere.
2. **The practice run writes for real.** Flow has no attended
   confirm-before-each-save mode for an uncertified bundle. The practice item
   runs the qualification case end to end on the test record the person
   names, with every check on. Check it says so and asks the person to
   confirm the values are test values.
3. **Safety drills need Flow's fault driver for the app type.** Drills run
   `run_qualification_case` with a fault target. Where the installed Flow has
   no fault driver for the target type, the drill reports the engine's
   refusal and the exact checks page is the fallback.
4. **The record check needs a connected system of record.** When the
   recording shows no system-of-record change, Check it asks plain questions
   that become an effect binding. Reading the record back still needs a
   verifier in the deployment settings (REST, FHIR, SQL, file, or document
   hash), which IT configures once.
5. **The time baseline is the recording length.** Desktop doesn't measure
   how long the task takes by hand. Results use the person's recording as the
   default minutes per item and let the reader type their own number. The
   estimate doesn't subtract time spent in Needs you.
6. **Spreadsheets must be saved as CSV.** Run it reads CSV and TSV. It
   doesn't read `.xlsx` files, because Desktop doesn't ship a spreadsheet
   parser.
7. **Stops that Cloud sent here offer one choice.** A hosted run that stopped
   shows in Needs you with "I checked the record" (or "Mark as handled" when
   nothing was written). Continue, skip, and teach for hosted runs stay in
   Cloud and on the paired phone.
8. **Teach doesn't close the item.** "Show OpenAdapt the right way" opens the
   existing teach screen and leaves the item waiting until a person closes it
   in Needs you.

## Screenshots

`docs/assets/operator/` holds the README's screenshots. They come from
`scripts/capture-cockpit-previews.mjs` against the synthetic fixture table in
`scripts/cockpit-fixtures.mjs`; no engine, account, recording, or customer
data is involved. `docs/assets/operator/provenance.json` lists each image.
