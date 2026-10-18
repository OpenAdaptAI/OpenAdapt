# Throughput: parallel worklists

A worklist run enters one item after another in one signed-in session. That is
as fast as one careful person. `--workers N` runs the same items on N
sessions at once, each signed in as its own user, with every check still on.
An optional read-back window lets each worker start its next item while the
previous save is still being read back.

Speed figures are published in one place, the
scoreboard (`benchmark/scoreboard/`, added in [#1303](https://github.com/OpenAdaptAI/OpenAdapt/pull/1303)): arm 4 (`program_fast`) is the `fast`
speed profile below, and arm 6 (`parallel`) is `--workers N`.

## What changes and what doesn't

Each item is the same governed run a serial `--worklist` loop makes for that
row: the program with a one-row worklist, in its own run directory
(`<run-dir>/items/NNNNN/`), with its own authorization bound to that row.
Every check stays on for every item: the right-record check before each
action, the input read-back, the screen postconditions, and the
system-of-record read-back after the save. Only the scheduling changes.

| Rule | What it means |
| --- | --- |
| One claim per item | Items live in a durable SQLite queue in the run directory (`worklist_queue.sqlite3`). A worker claims the next item in one transaction, so no two workers ever hold the same item. A claimed item is never handed out again, even after a crash: after a restart it is marked `uncertain` for a person to check, because something may have been saved. |
| One session per worker | Each worker opens its own browser context and signs in with its own account from the deployment file. The run refuses to start when two workers would share a session (identical session cookies) or sign in as the same user. Many EMRs lock a record per session or per user. |
| A declared limit | The deployment file states how many sessions the app tolerates at once (`worklist.max_workers`). `--workers` above it is refused. Without a deployment file the limit is 1. |
| One stop for all | A halt, a failure, a worker error, or `RECONCILIATION_REQUIRED` on any worker stops new claims on every worker. Items already in flight finish their own checks and record their results. Unstarted items stay `pending`. |
| Per-worker results | The run writes `worklist_pool.json` and `WORKLIST.md`: per worker, the items it ran and how each ended; per item, the worker, the state and the time. |

## Run it

1. Compile the task, and wrap it in a loop over a worklist with `for-each`:

   ```bash
   openadapt-flow for-each referral-bundle --records referrals.csv --out referral-queue
   ```

   `--workers` needs this shape: one loop over a worklist and nothing before
   or after it. Steps outside the loop would run once per item instead of once
   per run, so other program shapes are refused.

2. Record the sign-in once as its own bundle, with the user name as a
   parameter and the password as a secret step.

3. Declare the accounts and the app's limit in the deployment file:

   ```yaml
   backend:
     kind: web
     url: https://emr.example.test/
   worklist:
     max_workers: 4            # sessions this app tolerates at once
     items_independent: true   # items touch different records, any order
     sign_in_bundle: ./sign-in-bundle
     accounts:
       - name: clerk-1
         params_env: {username: EMR_USER_1}
         secrets_env: {password: EMR_PASS_1}
       - name: clerk-2
         params_env: {username: EMR_USER_2}
         secrets_env: {password: EMR_PASS_2}
       # one account per worker
   ```

   The file holds only the names of environment variables, never a
   credential. Instead of a sign-in bundle, an account can name
   `browser_cdp_endpoint`, a Chromium already signed in as that user; the
   worker opens a tab there and never closes the browser.

4. Run with N workers:

   ```bash
   openadapt-flow run referral-queue --config deployment.yaml --workers 4
   ```

   `replay` takes the same flags. With `run`, each item gets its own
   single-use authorization from the same admission gate, bound to that one
   row. A managed Cloud dispatch and a qualification case run serially.

The exit code is 0 only when every item finished and nothing stopped the run.

## Pipelined read-back

`--readback-window K` (or `worklist.readback_window`) lets a worker start
item n+1 while the read-back of item n's save is still running, with at most
K read-backs waiting. The default, 0, verifies each save before the next item
starts.

- Only the last action of an item is deferred, and only when its read-back
  doesn't use the screen (a REST, FHIR or database read). If the item runs
  any further step, the engine waits for the read-back first and halts on a
  failure, as it would inline.
- The item's `report.json` is rewritten with the verdict when the read-back
  finishes, and its outcome is classified again from the completed evidence.
- If a read-back fails, the run stops on every worker. The report names the
  items, at most K per worker, that the same worker wrote after the failed
  one. Each of those still gets its own read-back.
- It needs `items_independent: true`. Durable and idempotency-ledger runs
  verify inline, because their terminal state is written once; the Standard
  and Regulated profiles are durable, so for them the window has no effect.

## One entry, faster

Every check still runs on every step. These engine changes only remove
repeated work around them, and are always on:

- **Scroll readiness asks the page first.** While the DOM shows the next
  target exists but is still off-screen, the scroll keeps going without a
  full-frame OCR search. "Not ready" is the stricter answer, and the target
  step still runs the full resolver.
- **Text postconditions are confirmed from the page** when the recorded text
  matches its letters and digits (OCR-recorded texts lose spaces and misread
  punctuation), and rendered text counts when hidden option lists interleave
  `textContent`. A DOM miss still falls back to OCR, and the exact DOM match
  is stricter than the OCR check's 80% contiguous match.
- **A structural lookup checks the element's box and frame chain once,**
  after binding it, instead of before and after: the same test, later.

The settings that trade margin for time are grouped as named speed profiles
(`openadapt_flow.runtime.speed`, or `runtime.speed` in the deployment file):

| Setting | `standard` (default) | `fast` | Tradeoff |
| --- | --- | --- | --- |
| Typing into ordinary text fields | One key event per character | One insert per field (native fill): the field's `input` event fires, and `change` on blur | Apps that react to individual key presses in a plain text field won't see them. Comboboxes, autocomplete and datalist fields, and typing through the guarded keyboard path, always get every key. The input read-back and the system-of-record read-back still check the value. |
| Page-quiet window before a step counts as settled | 100 ms | 50 ms | The app gets less time to start reacting to an action before the next check. A reaction that starts later is caught by postconditions, identity checks or the read-back, as a halt rather than a wait. `runtime.page_quiet_ms` overrides it. |
| OCR engine threads | onnxruntime default (all cores) | 2 | With several workers, all-core OCR calls compete for the CPU; with one worker, 2 threads can be slower. |

The profiles are process-wide. Apply one before the run starts.

What was left alone: the guarded click (it re-checks the bound element,
frame chain and context around Playwright's actionability trial), the
identity checks, the input read-back, and the system-of-record read-back.
Those are the checks.

Every step in `report.json` now records where its time went, as `timings`:
milliseconds of exclusive time for `settle_before`, `screenshot`, `resolve`,
`identity`, `ocr`, `actuate`, `settle_after`, `postcondition`, `readback`,
`artifact_write` and `other`. They add up to the step's `elapsed_ms`.

## Worker processes

`--workers` runs its workers as threads in one process. Browser automation
makes many small calls per step, so on a busy machine workers can wait on each
other inside one Python interpreter. The library's `ProcessWorker`
(`runtime/worklist_pool.py`) runs each worker in its own process with the same
queue and stop flag; the benchmark uses it for 4 and 8 workers.

## Remote desktop

On RDP, `--workers` uses the FreeRDP session pool
(`backends/xfreerdp.py`, Linux): one headless session per account, connected
together, each driven by one worker. Each account names `rdp_username` and
`rdp_password_env`. This path has unit tests only; no parallel run over a real
RDP host has been measured.

## Limits

- Items must be independent. The run can't tell whether two rows touch the
  same record; you declare it with `items_independent`. If two rows write the
  same patient, run them serially.
- The app's capacity sets the ceiling. More workers than the app or its
  database can serve make every item slower, and a busy app can trip
  screen checks that wait for the page to settle. Those items halt; they don't
  save the wrong thing.
- Each worker needs its own account, and the accounts need the same
  permissions and screen layout the recording was made with.
- Each item must start where the recording started. On the web the worker
  opens `worklist.entry_url` (default `backend.url`) before each item. Apps
  that keep state between items, such as an open patient chart, need a way
  back to the start page; the OpenEMR benchmark closes the chart first (see
  `benchmark/openemr_throughput/harness.py`).
- Throughput was measured on one machine with a local test copy of OpenEMR.
  A real deployment adds network latency, a shared database and other users.

## Where the time goes in one entry

Every step in `report.json` records `timings`, so any run shows where its
time went. For a finer split by function, `benchmark/openemr_throughput/`
has an exclusive-time profiler. The scoreboard publishes the breakdown for
its arms.
