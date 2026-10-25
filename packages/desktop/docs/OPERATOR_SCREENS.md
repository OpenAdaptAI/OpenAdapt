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
| Check it | Answers three questions: is this the right record check, which values change per item, and approve. Everything else the recording shows is filled in and shown once, folded. A person's agent can answer the first two (see [SETUP_INPUTS.md](SETUP_INPUTS.md)). Without a record check learned from the recording, or after Change, it asks the parts: what the task changes, the right-record check, and the record check. | `get_check_setup`, `answer_check_setup`, `get_qualification`, `propose_qualification_from_demo`, `initialize_qualification`, `set_qualification_risk`, `arm_qualification_identity`, `bind_qualification_effect`, `seal_qualification_workflow`, `add_qualification_case`, `run_qualification_case`, `certify_qualification` |
| Run it | Pastes a list, opens a CSV, or types one item, then watches per-item progress. | `get_task_inputs`, `run_item` |
| Needs you | Answers each stopped item: the question, what was prevented, the stopped screen with the point ringed, and only the choices the engine allows. | `list_attention_items`, `get_attention_screenshot`, `resolve_attention_item` |
| Results | Sees items done and checked, items that needed a person, wrong-record saves prevented, and a time-saved estimate whose per-item baseline is shown and editable. | `get_results_summary`, `get_workflows` |
| Advanced (gear) | Exact checks, every task version with replay and governed run, recording with all options, the Cloud runner, phone pairing, and recording setup. | the existing commands |

Check it uses the same engine commands as the exact checks page, so the short
path and the full contract can't disagree. The full page stays one click away
on every task.

## Safety rules the screens keep

- Every plain result comes from Flow's transaction outcome
  (`openadapt_desktop/outcomes.py`, mirrored in `src/lib/outcomes.ts`; both are tested
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
- The index database stores item values, labels, and right-record check text
  encrypted, with the key in the OS keychain. A closed item's values are
  deleted. Without the key, `run_item` refuses before anything runs.
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
4. **The record check needs a read the recording holds, or a person.** When
   the recording's network capture holds the read the app made after the
   save, Check it proposes the record check from it, and a yes writes that
   read as the record-check settings every run uses. Desktop's recorder
   doesn't keep network traffic yet, and Desktop binds only REST reads, so
   otherwise Check it asks plain questions that become an effect binding,
   and reading the record back needs a verifier in the deployment settings
   (REST, FHIR, SQL, file, or document hash), which IT configures once.
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

## A person approves each save

Every Desktop run of real items stops before each save and waits for a
person (Flow's `each_item` approval mode, openadapt-flow PR 486). A run
without a profile isn't gated unless the caller asks, so Desktop always
passes `--approval-mode each_item`, and refuses to start on a Flow that can't
gate. Check it's practice item is gated by Flow itself (`qualify run-case`
runs under Standard); a drill never asks.

| Screen | What it shows | Engine commands |
| --- | --- | --- |
| Any screen, while a run waits | A dialog: the item's own values next to Flow's screenshot of the filled form, the step it's about to take, and when the run stops waiting (10 minutes after it asks). Approve and save, or Don't save with one of three reasons (wrong values, wrong record, something wrong on the screen). Approve isn't offered when the screenshot is missing or doesn't match the digest in Flow's request. | `get_item_approval`, `answer_item_approval`; event `item_approval` |
| Run it | The list stops at an item nobody approved in time. A declined item goes to Needs you with the reason the person gave. | `run_item` |
| Check it, question 2 | Desktop seals the version first, so the cases bind the version that runs. The practice item then stops before its save for the same approval. A practice save nobody approved, or one the person declined, records no result. A deployment settings file from IT, which replaces the learned record check, goes under Change the test values or where it runs. | `seal_qualification_workflow`, `run_qualification_case` |
| Check it, question 2 (drills) | After the practice item passes, the drills run on their own with its values. Flow changes one thing right before the save; the drill passes only when Flow's own check refuses it before saving. | `run_qualification_case` |

How it works:

- `get_item_approval` and `answer_item_approval` are served while a run
  holds the command queue (`openadapt_desktop/ipc.py` `CONCURRENT_COMMANDS`). They read
  and answer only a run this engine started and hasn't finished, and only a
  request that is waiting now, for the exact digest the person saw.
- Desktop signs each answer with this computer's approver key, and Flow
  counts it as a person's only because of that signature. Desktop has no
  person-level sign-in (its Cloud credential belongs to the organization),
  so the answer names no Cloud user (`--unauthenticated`, `--route
  desktop_run_it`); it passes `--approver-key`, and the key's passphrase
  reaches Flow only through the child process's environment. Flow re-checks
  the answer, re-reads the form and runs the right-record check again before
  it saves.
- On a Mac with a Secure Enclave (Apple silicon or a T2 chip), the
  approval screen shows **Set up your approver key** in place of Approve
  until the key exists. One click and one Touch ID (or login password)
  prompt make the key, seal it to the Secure Enclave, keep it in the login
  Keychain and register its public key under `approvers` with Flow's own
  `register_principal`. Each answer prompts again, and Flow gets only the
  signature (`--approver-signer stdio`). Settings > Approver key replaces
  the key (the new one is registered before the old one is retired) or
  removes it. See `openadapt_desktop/approver_keychain.py`.
- Elsewhere, set the key up once per computer, at a terminal. With
  `OPENADAPT_JIT_KEY_PASSPHRASE` set, run `openadapt-flow jit keygen --out
  ~/.openadapt/approver/desktop-approver.pem`, then a person registers the
  public key it prints: `openadapt-flow jit register --role approver
  --principal NAME --public-key HEX`. Desktop then needs the same
  passphrase in its environment. `openadapt-desktop approver-key setup` does
  the keygen step instead and keeps a random passphrase in the OS keychain;
  the register step is the same. Until a key is set up and registered,
  Desktop sends no answer, and Run it shows these steps.
- A late answer (the run already stopped waiting) is never turned into a
  save: Desktop doesn't resume the run, and says to run the item again.
- Before the practice item runs, Desktop calls `openadapt-flow qualify drills
  BUNDLE --json`. It adds the five drills for the practice case's first save
  and trusts this computer's drill key. It changes the project, so it runs
  before any result is recorded. After a drill, Desktop keeps
  `qualification-fault/receipt.json` and `mutation.json` as the result's
  `fault_receipt` and `fault_mutation` evidence. A drill whose
  `status.json` says `staged: false` hasn't passed, and nothing is recorded.

## Only ask a person about problems

This switch lets a clinic stop approving routine items one by one. With it
on, an item saves without a person only when every check before and after
the save passes, including a read-back from the records system. Flow's
engine part is `openadapt_flow/approval_mode/` (Flow `docs/APPROVAL_MODES.md`).

| Screen | What it shows | Engine commands |
| --- | --- | --- |
| Tasks | On a ready task that has a switch on this computer: the switch's state (On, On but waiting for Cloud, Not used on this computer, Off), who turned it on and who confirmed, why it is off, and an Evidence disclosure with the bound version, runner and record check, the local switch log, and requirements E1 to E8 from Flow. Turn off asks once. There is no Turn on: that happens in OpenAdapt Cloud. | `get_approval_mode`, `turn_off_approval_mode` |
| Needs you | While a switch is on, the page says only problems come here. Each item from such a list says so, and says in words which check sent it to a person. A switch that turned itself off gets one banner. | `list_attention_items` (its `switches` field) |
| Results | Done and checked, and how many of those Flow saved with every check passed and no person. Each task's switch state. | `get_results_summary` (`saved_without_person`, `switches`) |
| Run it | While the switch applies, the page says only problems come to you, and the list stops at the first item that needs a person, even one that wrote nothing. | `get_approval_mode` |

Safety rules:

- Desktop never turns the switch on. No IPC command writes a switch record.
  `openadapt_desktop/approval_mode.py` `accept_switch` stores one that Cloud sent, for
  the Cloud status client (not built) and for tests.
- Anyone at this computer can turn it off, signed in or not, offline too.
  Desktop appends Flow's `switched_off` event to the runner's hash-chained
  switch log, which Flow's gate reads before every save, so the switch-off
  applies before the next save and Cloud can't undo it. If the append
  fails (for example, on a full disk), Desktop moves the log aside, and
  Flow's gate reads a missing log as off.
- A switch that turned off stays off. `accept_switch` refuses a switch id
  this computer turned off or moved aside, and the same switch sent again
  keeps its log, so its interrupted items and problem count survive. A new
  switch that replaces a log with an unfinished item first closes that log
  as `uncertain_result`.
- A switch Desktop can't read or confirm reads as off: a broken or missing
  log, a record for another version or runner, an item that started and
  never finished. A switch for one version never carries over to a new one.
- An item counts as saved without a person only when Flow's own approval
  block says `no_person`, both post-run checks (R1 and R2) passed, and the
  transaction outcome is a consistent `VERIFIED`. A block Flow's model
  rejects needs a person. A `VERIFIED` execution outcome with no
  transaction outcome never counts, and the item waits in Needs you.
- Desktop also checks `no_person` against what it knows (design 5.2, step
  5). The block must list every item check, every pre-save check and both
  post-run checks Flow's gate runs, each passed, and name the switch it was
  decided under. This computer must have that switch on for the task
  version, and the item must not be a retry, a repeat or a run with a
  caller-supplied configuration. Otherwise the item waits in Needs you, and
  its approval block says why (`unconfirmed`). Flow's own evidence model is
  looser: it accepts `no_person` with no item checks.
- A run whose transaction outcome disagrees with its execution outcome
  reads "Check the record" (`openadapt_desktop/flow_bridge.py`, `classify_outcome`).
  Before, Desktop dropped the disagreeing transaction outcome and kept a
  bare `VERIFIED`, which closed the item as "Done and checked".
- A pasted list never changes a value on the way to the task. A row with
  more values than columns ("Smith, Jane" in a one-column list) or a
  missing cell doesn't run. A value with a comma runs when it's in quotes.
- No input takes the value from the recording, optional ones included
  (design 7.2). `run_item` refuses an item that leaves out any declared
  input; an empty value is an explicit empty value. A pasted list needs a
  column for every input, and Type one sends an empty field as empty.
- A run of the task that OpenAdapt Cloud started on this computer, and that
  may have saved, blocks the task's lists until a person checks it in
  Needs you, the same as a Desktop item (design 7.5).
- The switch never changes an outcome word. A save the record check
  confirmed that still went to a person keeps "Done and checked", waits in
  Needs you, blocks more items of the task, and offers no re-run.
  `src/lib/outcome-cases.json` `switch_cases` pins this for both the engine
  and the screens.
- An item the switch held for approval before saving offers skip, decline,
  or finished by hand: running it again would stop at the same check. An
  item nobody approved in time under `each_item` can run again.

What isn't built yet, so the switch can't take effect on any computer
(`openadapt_desktop/approval_mode.py` `NOT_BUILT`; each is listed under "Missing on
this computer"):

1. **No field-change check.** Flow can't yet check that a save changed
   only the fields it was given, and the three drills for that check don't
   exist. Until they do, Desktop passes `each_item`, never `problems_only`.
2. **No Cloud status client.** Flow's P1 check needs a fresh signed status
   from Cloud before every save. Desktop doesn't fetch one, and no Cloud
   endpoint issues one yet.
3. **No spot checks.** The design makes them mandatory.
4. **No engine-held list.** A list stopped at a problem can't continue
   after the person answers; the rest of the items run as a new list.
5. **No runner setup.** The design shows the switch only on runners with a
   current runner setup, so the card shows the switch only for a task that
   already has one on this computer.
6. **No approve-and-save path** for an item the switch held for approval.

## Review stops

Needs you has **Review one by one**, which opens Review stops: every stop as
one card (`openadapt_desktop/stop_queue.py`, screen `src/screens/Stops.tsx`).

| Card | Question | Evidence | Choices |
| --- | --- | --- | --- |
| `same_record` | "Same person?" or "Same record?" | The item's values next to each possible match the app listed, each value marked Same or Different, when Flow recorded the rows. Otherwise the row text and the screen where the run stopped. | Same: don't add it (skip); this item shouldn't be done (decline); different: I entered it by hand; not sure, leave it |
| `confirm_save` | "Did it save?" | The item's values and the record check's differences | I checked: it saved; I checked: nothing saved, run it again; I fixed it by hand; leave it |
| `halted` | "What should happen to this item?" | The item's values and why it stopped | Run it again; skip; decline; I did it by hand; leave it |
| `approve_save` | "Approve this save?" | The item's values and the filled form | Approve and save; don't save (three reasons); leave it |
| `rearm_problems_only` | "Turn "only ask about problems" back on?" | Why it went off, when, and the items saved and checked in a row since (Flow offers it only after 10) | Turn it back on; leave it |

How it answers:

- **It never decides.** A card's id is a digest of everything it shows. The
  engine rebuilds the cards before it acts, so a card that changed, expired
  or is gone, or a choice it doesn't offer, changes nothing.
- **Signed.** An answer to a stopped item is a decision receipt that the
  approver key signs (Touch ID on a Mac). Then the item's own action runs,
  with its usual checks. A waiting save is answered through the signed
  item approval. Receipts go to `stop_decisions.jsonl` with how long each
  card was on screen (`shown_ms`).
- **Keys.** 1-9 answer, J and K (or the arrows) move between cards, X adds
  a card to a batch, Shift+1-9 answers the batch, Esc clears it, and ?
  lists the keys.
- **Batches.** Only stops with the same `batch_key` (same kind, task and
  reason) batch together. A batch takes one choice and one Touch ID, and
  each card is still checked on its own. One stale card stops the whole
  batch. Save approvals never batch.
- **No default.** No choice is styled as the default; the evidence decides.
- **Re-arm.** Turning the switch back on is one Touch ID over Flow's own
  re-arm bytes (`rearm_to_sign`) for exactly the request the card showed;
  if another item finished since, the card changed and nothing happens.
  Flow verifies the signature against the key registered under `approvers`,
  never a key Desktop supplies for itself. Desktop's switch view honours a
  re-arm signed by a registered approver. It is answered on the computer
  only, and needs a Flow with signed re-arm (#1517).

A stop where nothing was written now also offers "I did it by hand". For a
possible duplicate that is a different person, running it again would stop
at the same check.

## Review stops on a paired phone

A phone paired with the decision portal ([DECISION_PORTAL.md](DECISION_PORTAL.md))
shows the same stop cards as Review stops, at `/stops` on the portal's
address. The Decisions page links to it.

| Where | What the person sees | Routes (all need an approved device session) |
| --- | --- | --- |
| `/stops` | One card at a time: the question, the item's values next to what the app shows with the differences marked, the screen where the run stopped, and each choice with what it does. Similar stops have checkboxes; the picked stops get one answer, which takes a second tap to confirm. | `GET /api/portal/stops`, `GET /api/portal/stops/<card>/screenshot`, `POST /api/portal/stops/<card>/answer`, `POST /api/portal/stops/answer` (a batch: `card_ids` and one `choice`) |

How a phone's answer counts:

- **Signed with the portal's key.** The phone can't reach Touch ID on the
  computer. Desktop signs the decision receipt with the decision portal's
  approver key (route `portal`, which `openadapt-desktop approver-key setup`
  creates; a person registers it as `decision-portal`). The receipt in `stop_decisions.jsonl`
  records `"key": "portal"` and the device session (`device.id`, the id the
  computer lists and revokes devices by, and its label). The window can't
  ask for this key: the portal calls a dispatcher method that no command
  registers. Without the key, the phone shows a warning and every answer
  changes nothing (`approver_key_missing`).
- **Same checks as the window.** Desktop rebuilds every card before it acts.
  A card that changed, expired or is gone, or a choice the card doesn't
  offer, changes nothing and returns the dispatcher's code (`card_changed`,
  `choice_not_offered`). A phone answer waits up to two seconds for a
  command the window started when its answer runs an item again, then
  returns `busy` and changes nothing. Closing a stop (skip, done by hand,
  saved) doesn't wait, so it goes through while a run waits for a save.
- **View-only on the phone.** A waiting save (`approve_save`) is answered on
  the Decisions page, through the portal's save approval, which the Flow
  console signs with the same portal key. Turning "only ask about problems"
  back on is answered on the computer.
- **Requests.** Answers need the portal's origin, the session's CSRF token
  and a JSON body, like the portal's other decisions. Reads need the session
  and are refused when `Origin` or `Sec-Fetch-Site` says another site sent
  them. Every response is `no-store`, and the service worker caches none of
  it.

### Remote access

The portal binds `127.0.0.1` by default, so only this computer can open it.
To answer stops from a phone elsewhere, use the existing customer ingress:
an HTTPS origin on the clinic's own reverse proxy, VPN or ZTNA
(`OPENADAPT_PORTAL_INGRESS_MODE=customer_ingress` with an explicit
`OPENADAPT_PORTAL_PUBLIC_ORIGIN`), then pair the phone with the one-use QR
code and confirm its code on the computer. Every route except the pairing
claim needs that pairing. OpenAdapt runs no relay for the portal: the cards,
screenshots and answers stay between the phone and the clinic's network.

## Screenshots

`docs/assets/operator/` holds the README's screenshots. They come from
`scripts/capture-cockpit-previews.mjs` against the synthetic fixture table in
`scripts/cockpit-fixtures.mjs`; no engine, account, recording, or customer
data is involved. `docs/assets/operator/provenance.json` lists each image.
`--set approval-mode` shoots the unpublished design-review set for the switch
from `scripts/approval-mode-targets.mjs`, also from invented data.
