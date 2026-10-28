# A clean screen after a halt

A halted item can leave the app's screen in a state the next item must not
start from: a confirm dialog open, a form half filled, another page in front.
In a worklist pool the next item runs on the same signed-in session, so it
would start there. This page covers what Flow does about the screen after a
halted item. The records a halted item saved are covered by
[ROLLBACK.md](ROLLBACK.md).

The code is in `openadapt_flow/halt_cleanup.py`; the tests are in
`tests/test_halt_cleanup.py`.

## What happens after a halted item

When an item in a pool doesn't succeed, the worker cleans up before it takes
another item:

1. **Cleanup steps**, from the bundle's qualified `halt_cleanup`: Escape, then
   each cancel, close, discard or dismiss control the recordings clicked.
   Each click runs only when its control is on screen. No step saves, types,
   calls an API or declares an effect.
2. **Back to the entry.** A web worker opens the entry page again (its entry
   URL or entry bundle), as it does before every item.
3. **Start-screen check.** The screen every item starts on must show: the
   first anchored step's anchor must resolve.

If the check fails, the session is **dirty**. The worker takes no more items,
the pool report names the reason (`dirty`), and the other workers carry on.
The next item never runs on a poisoned session.

A bundle without a qualified cleanup still gets steps 2 and 3: the worker
re-enters and checks the start screen. Cleanup steps run only from a cleanup
qualified on a test copy, and only while it matches its qualification: an
untested "Cancel" click could do something nobody intended.

## Qualify the cleanup

```bash
openadapt-flow cleanup qualify bundle --config test-copy.yaml --from-recording cancel-recording-bundle --report cleanup.json
```

It runs only on a target the deployment file declares a test copy, the same
gate as `induce --probe-on`. Flow derives the cleanup from the bundle and the
other recordings, then tries a halt after every step but the last. Each halt
point is a fresh session on a reset copy: the steps up to the halt point, the
cleanup steps, then the start-screen check. Only when every halt point
reaches the start screen is the cleanup stored in the bundle, with its
qualification (re-sealed; certify it again before production use). A halt
point that fails is reported with the reason, for example "the start screen
isn't showing" when no recorded control leads back from that screen.

Qualification tests the cleanup steps alone. In a web pool the worker also
re-enters the entry page, so production has one more way back than the
qualification does.

## What this doesn't do

- **No data undo.** A record the halted item saved stays. That's
  [rollback](ROLLBACK.md).
- **The start check is an anchor.** It confirms the start screen's first
  control shows, not that every field on it is empty. A web worker's
  re-entry reloads the page, which clears a client-side form; an app that
  keeps a server-side draft needs a compensation, not a cleanup.
- **Pools only.** A serial `run --worklist` already ends at the first halt,
  so no later item runs on the session.
- **No cleanup steps on a desktop or RDP worker without a qualified
  cleanup.** Those workers can't re-enter an entry page; they only check the
  start screen.
