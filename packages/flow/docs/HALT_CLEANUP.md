# A clean screen after a halt

A halted item can leave the app's screen in a state the next item must not
start from: a confirm dialog open, a form half filled, another page in front.
In a worklist pool the next item runs on the same signed-in session, so it
would start there. This page covers what Flow does about the screen after a
halted item. The records a halted item saved are covered by
[AFTER_A_STOP.md](AFTER_A_STOP.md#declared-undo-steps).

The code is in `openadapt_flow/halt_cleanup.py`; the tests are in
`tests/test_halt_cleanup.py`.

## When the cleanup runs

Only when the session will take another item: the pool runs with
`on_halt: skip_unwritten`, the item halted before any write
(`HALTED_BEFORE_EFFECT`), and the run hasn't stopped. After any other outcome,
or once the run has stopped, Flow leaves the screen as it is. That screen is
the evidence the person who reviews the stop needs.

## What happens after a halted item

1. **Read the records.** The worker reads the records back through the
   deployment's effect verifier. If it can't, the cleanup doesn't run and the
   session is dirty.
2. **Cleanup steps**, from the bundle's qualified `halt_cleanup`: Escape, then
   each cancel, close, discard or dismiss control the recordings clicked.
   Each click runs only when its control is on screen. No step saves, types,
   calls an API or declares an effect.
3. **Back to the entry.** A web worker opens the entry page again (its entry
   URL or entry bundle), as it does before every item.
4. **Start-screen check.** The screen every item starts on must show: the
   first anchored step's anchor must resolve.
5. **Read the records again.** They must match the first read.

If the start check fails, the session is **dirty**. The worker takes no more
items, and the pool report and console name the reason (`dirty`). The other
workers carry on. When the last clean worker goes dirty, the run stops and
says why, and the remaining items stay pending.

If the records changed during the cleanup, or can't be read after it, the
item's write is **uncertain**. A "Close" that saves the form on the way out
(as an app with autosave on leave does) ends here. The item is recorded as
`reconciliation_required` with `uncertainty: effect_uncertain`, never as
halted before any write, so nobody retries it as unwritten. The run stops for
a person to reconcile. A record another user changed at the same moment also
ends here; the check can't tell the two apart, so it stops.

A bundle without a qualified cleanup still gets steps 1, 3, 4 and 5: the
worker re-enters and checks the start screen. Cleanup steps run only from a
cleanup qualified on a test copy, and only while it matches its
qualification: an untested "Cancel" click could do something nobody
intended.

## The start check on a fresh session

With `on_halt: skip_unwritten`, each worker runs the start check once on its
fresh session, before any item. If the start screen doesn't show from the
entry page (for example, the bundle's first anchored step is reached only
after a key press), the pool is refused with the reason, and no item runs.
Set `worklist.entry_url` or `entry_bundle`, or use `on_halt: stop`.

## Qualify the cleanup

```bash
openadapt-flow cleanup qualify bundle --config test-copy.yaml --from-recording cancel-recording-bundle --report cleanup.json
```

It runs only on a target the deployment file declares a test copy, the same
gate as `induce --probe-on`. Flow derives the cleanup from the bundle and the
other recordings, then tries a halt after every step but the last. Each halt
point runs twice, each time in a fresh session on a reset copy:

1. The steps up to the halt point, the cleanup steps, then the start-screen
   check.
2. The steps up to the halt point alone.

The test copy's records must be the same after both runs. A cleanup that
added or changed a record fails that halt point, even when the start screen
came back. Only when every halt point passes is the cleanup stored in the
bundle, with its qualification (re-sealed; certify it again before
production use). A halt point that fails is reported with the reason, for
example "the start screen isn't showing" when no recorded control leads back
from that screen.

Qualification tests the cleanup steps alone. In a web pool the worker also
re-enters the entry page, so production has one more way back than the
qualification does. The run-time read-back covers what re-entry itself
saves.

## What this doesn't do

- **Only screens after a step that succeeded.** Qualification halts after a
  step that succeeded. A real halt often follows a step that failed, which
  can leave a validation banner or an error dialog. Qualification doesn't
  try those screens; at run time, the start check and the read-back still
  apply.
- **No data undo.** A record the halted item saved stays. That's
  [undo after a stop](AFTER_A_STOP.md#declared-undo-steps).
- **The start check is an anchor.** It confirms the start screen's first
  control shows, not that every field on it is empty. A web worker's
  re-entry reloads the page, which clears a client-side form; an app that
  keeps a server-side draft needs a compensation, not a cleanup.
- **Pools only.** A serial `run --worklist` already ends at the first halt,
  so no later item runs on the session.
- **No cleanup steps on a desktop or RDP worker without a qualified
  cleanup.** Those workers can't re-enter an entry page; they only check the
  start screen.
- **"Back" and "Exit" aren't cleanup controls.** Leaving a page can save the
  form in some apps, so Flow derives only cancel, close, discard, dismiss and
  "don't save" controls.
